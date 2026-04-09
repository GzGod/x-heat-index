#!/usr/bin/env python3
"""Tweet Tracker — high-frequency snapshot of one tweet's engagement.

Phase 1 only (Phase 2 cascade walker is a separate script).

Outputs (per tweet, in DATA_DIR/<tweet_id>/):
  metrics.jsonl   — counts every cycle (views, likes, RTs, replies, quotes, bookmarks)
  replies.jsonl   — incremental new replies (cursor-based, dedup)
  quotes.jsonl    — incremental new quotes
  derived.jsonl   — heat / velocity / reach / engagement_rate per cycle
  state.json      — last cursors + seen IDs (for crash recovery)
  config.json     — promotion start ts, KOL list, channels (manually edited later)
  dashboard.txt   — human-readable status, overwritten each cycle

Required env:
  TWITTER241_RAPIDAPI_KEY    Twitter241 API key
  TWEET_ID                   target tweet pid

Optional env:
  DATA_DIR                   default /opt/tweet-tracker/data
  SNAPSHOT_INTERVAL_SEC      default 300 (5 min)
  MAX_PAGES_PER_CYCLE        default 5 (cap pagination per endpoint per cycle)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
KEY_PRIMARY = os.environ["TWITTER241_RAPIDAPI_KEY"]
KEY_FALLBACK = os.environ.get("TWITTER241_RAPIDAPI_KEY_FALLBACK", "")
HOST = "twitter241.p.rapidapi.com"
TWEET_ID = os.environ["TWEET_ID"]

# Mutable state: which key to use right now (switches to fallback on quota exhaustion)
_active_key = KEY_PRIMARY
_using_fallback = False
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/tweet-tracker/data"))
INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL_SEC", "300"))
MAX_PAGES = int(os.environ.get("MAX_PAGES_PER_CYCLE", "5"))

TWEET_DIR = DATA_DIR / TWEET_ID
TWEET_DIR.mkdir(parents=True, exist_ok=True)

METRICS_FILE = TWEET_DIR / "metrics.jsonl"
REPLIES_FILE = TWEET_DIR / "replies.jsonl"
QUOTES_FILE = TWEET_DIR / "quotes.jsonl"
DERIVED_FILE = TWEET_DIR / "derived.jsonl"
STATE_FILE = TWEET_DIR / "state.json"
CONFIG_FILE = TWEET_DIR / "config.json"
DASHBOARD_FILE = TWEET_DIR / "dashboard.txt"

# Heat Score weights
W_VIEWS = 0.2
W_LIKES = 1.0
W_RTS = 5.0
W_REPLIES = 2.0
W_QUOTES = 3.0


# ──────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────
def _switch_to_fallback() -> bool:
    """Switch to fallback key. Returns True if switched, False if no fallback."""
    global _active_key, _using_fallback
    if _using_fallback or not KEY_FALLBACK:
        return False
    _using_fallback = True
    _active_key = KEY_FALLBACK
    print(f"[{now_iso()}] [QUOTA] primary key exhausted, switching to fallback", flush=True)
    return True


def call_api(path: str, retries: int = 3) -> dict:
    """Call Twitter241 endpoint, return parsed JSON.

    Auto-switches to fallback key on 429 (rate limit) or body-level quota errors.
    """
    url = f"https://{HOST}{path}"
    last_err = None
    for attempt in range(retries):
        headers = {"x-rapidapi-key": _active_key, "x-rapidapi-host": HOST}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                # Twitter241 / RapidAPI sometimes returns 200 + body error on quota out
                if isinstance(data, dict) and data.get("message", "").lower().startswith("you have exceeded"):
                    if _switch_to_fallback():
                        continue
                    raise RuntimeError(f"quota exhausted: {data.get('message')}")
                return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                if _switch_to_fallback():
                    continue
                time.sleep(2 ** attempt)
                continue
            if e.code in (502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise RuntimeError(f"unreachable: {last_err}")


# ──────────────────────────────────────────────────────────────
# Parsers
# ──────────────────────────────────────────────────────────────
def parse_tweet_node(node: dict) -> dict | None:
    """Extract flat tweet record from a Twitter241 nested 'result' node."""
    if not node or node.get("__typename") not in ("Tweet", None):
        return None
    legacy = node.get("legacy") or {}
    if not legacy:
        return None

    user_result = (
        node.get("core", {}).get("user_results", {}).get("result", {}) or {}
    )
    user_legacy = user_result.get("legacy") or {}
    user_core = user_result.get("core") or {}
    screen_name = user_core.get("screen_name") or user_legacy.get("screen_name") or ""

    views_count = 0
    views = node.get("views") or {}
    if views.get("count"):
        try:
            views_count = int(views["count"])
        except (TypeError, ValueError):
            views_count = 0

    return {
        "tweet_id": str(node.get("rest_id") or legacy.get("id_str", "")),
        "author_username": screen_name.lower(),
        "author_followers": user_legacy.get("followers_count", 0),
        "text": legacy.get("full_text", ""),
        "created_at": legacy.get("created_at", ""),
        "view_count": views_count,
        "favorite_count": legacy.get("favorite_count", 0),
        "retweet_count": legacy.get("retweet_count", 0),
        "reply_count": legacy.get("reply_count", 0),
        "quote_count": legacy.get("quote_count", 0),
        "bookmark_count": legacy.get("bookmark_count", 0),
    }


def _parse_item_content(item_content: dict, root_tid: str) -> dict | None:
    """Parse a single TimelineTweet itemContent into a flat record."""
    result = item_content.get("tweet_results", {}).get("result", {}) or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", {}) or {}
    rec = parse_tweet_node(result)
    if rec and rec.get("tweet_id") and rec["tweet_id"] != root_tid:
        return rec
    return None


def extract_tweets_from_instructions(instructions: list, root_tid: str) -> tuple[list[dict], str | None]:
    """Walk timeline instructions, return (tweet_records, next_cursor).

    Handles three entry shapes:
      - tweet-<id>             → standalone tweet (e.g. /quotes results)
      - conversationthread-<id>→ TimelineTimelineModule with items[] (e.g. /comments results)
      - cursor-bottom-...      → pagination
    """
    tweets = []
    next_cursor = None
    for inst in instructions or []:
        for entry in inst.get("entries", []):
            eid = entry.get("entryId", "")
            content = entry.get("content", {}) or {}

            if "cursor-bottom" in eid:
                next_cursor = (
                    content.get("value")
                    or content.get("itemContent", {}).get("value")
                )
                continue
            if "cursor" in eid:
                continue

            if eid.startswith("tweet-"):
                # Standalone tweet entry
                ic = content.get("itemContent", {})
                rec = _parse_item_content(ic, root_tid)
                if rec:
                    tweets.append(rec)
                continue

            if eid.startswith("conversationthread-"):
                # Module with multiple items (replier + sub-replies)
                for item in content.get("items", []):
                    item_inner = item.get("item", {}) or {}
                    ic = item_inner.get("itemContent", {})
                    if not ic:
                        continue
                    rec = _parse_item_content(ic, root_tid)
                    if rec:
                        tweets.append(rec)
                continue

    return tweets, next_cursor


def fetch_root_metrics() -> dict | None:
    """Fetch the root tweet's current metrics."""
    data = call_api(f"/tweet?pid={TWEET_ID}")
    inst = (
        data.get("data", {})
        .get("threaded_conversation_with_injections_v2", {})
        .get("instructions", [])
    )
    for i in inst:
        for entry in i.get("entries", []):
            eid = entry.get("entryId", "")
            if eid == f"tweet-{TWEET_ID}":
                content = entry.get("content", {}) or {}
                result = (
                    content.get("itemContent", {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                if result.get("__typename") == "TweetWithVisibilityResults":
                    result = result.get("tweet", {})
                return parse_tweet_node(result)
    return None


def fetch_replies_page(cursor: str = "") -> tuple[list[dict], str | None]:
    """Fetch one page of replies. /comments returns result.instructions."""
    path = f"/comments?pid={TWEET_ID}&count=20"
    if cursor:
        path += f"&cursor={urllib.parse.quote(cursor)}"
    data = call_api(path)
    inst = data.get("result", {}).get("instructions", [])
    tweets, next_cursor = extract_tweets_from_instructions(inst, TWEET_ID)
    return tweets, next_cursor


def fetch_quotes_page(cursor: str = "") -> tuple[list[dict], str | None]:
    """Fetch one page of quotes. /quotes nests one level deeper: result.timeline.instructions."""
    path = f"/quotes?pid={TWEET_ID}&count=20"
    if cursor:
        path += f"&cursor={urllib.parse.quote(cursor)}"
    data = call_api(path)
    inst = data.get("result", {}).get("timeline", {}).get("instructions", [])
    tweets, next_cursor = extract_tweets_from_instructions(inst, TWEET_ID)
    return tweets, next_cursor


# ──────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "started_at": now_iso(),
        "last_metrics": None,
        "last_heat": 0.0,
        "last_ts": None,
        "replies_cursor": "",
        "quotes_cursor": "",
        "seen_reply_ids": [],
        "seen_quote_ids": [],
        "cycle_count": 0,
    }


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {
        "tweet_id": TWEET_ID,
        "tracker_started_at": now_iso(),
        "promotion_started_at": None,  # set this when promotion goes live
        "promotion_kol_handles": [],   # lowercase usernames
        "promotion_channels": [],      # ["paid-ads", "telegram", ...]
        "promotion_budget_usd": None,
        "notes": "",
    }


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def compute_heat(m: dict) -> float:
    return (
        W_VIEWS * m["view_count"]
        + W_LIKES * m["favorite_count"]
        + W_RTS * m["retweet_count"]
        + W_REPLIES * m["reply_count"]
        + W_QUOTES * m["quote_count"]
    )


def compute_engagement_rate(m: dict) -> float:
    if m["view_count"] == 0:
        return 0.0
    eng = m["favorite_count"] + m["retweet_count"] + m["reply_count"] + m["quote_count"]
    return eng / m["view_count"]


def is_promoted(handle: str, cfg: dict) -> bool:
    if not handle:
        return False
    return handle.lower() in [h.lower().lstrip("@") for h in cfg.get("promotion_kol_handles", [])]


# ──────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────
def render_dashboard(metrics: dict, derived: dict, state: dict, cfg: dict, new_replies: int, new_quotes: int) -> str:
    lines = []
    lines.append("═" * 60)
    lines.append(f" Tweet Tracker — {TWEET_ID}")
    lines.append(f" Updated: {derived['ts']}")
    lines.append("═" * 60)
    lines.append("")
    lines.append(f" Author:        @{metrics.get('author_username', '?')} ({metrics.get('author_followers', 0):,} followers)")
    lines.append(f" Cycle:         #{state['cycle_count']}")
    lines.append(f" Tracker age:   {state['started_at']} → now")
    lines.append("")
    lines.append("─" * 60)
    lines.append(" Raw counts")
    lines.append("─" * 60)
    lines.append(f"  views:     {metrics['view_count']:>12,}")
    lines.append(f"  likes:     {metrics['favorite_count']:>12,}")
    lines.append(f"  retweets:  {metrics['retweet_count']:>12,}")
    lines.append(f"  replies:   {metrics['reply_count']:>12,}  (+{new_replies} new this cycle)")
    lines.append(f"  quotes:    {metrics['quote_count']:>12,}  (+{new_quotes} new this cycle)")
    lines.append(f"  bookmarks: {metrics['bookmark_count']:>12,}")
    lines.append("")
    lines.append("─" * 60)
    lines.append(" Derived")
    lines.append("─" * 60)
    lines.append(f"  Heat Score:        {derived['heat_score']:>12,.0f}")
    lines.append(f"  Heat Velocity:     {derived['heat_velocity_per_min']:>12,.1f}  per minute")
    lines.append(f"  Engagement Rate:   {derived['engagement_rate']:>12,.2%}")
    lines.append("")
    lines.append("─" * 60)
    lines.append(" Promotion config")
    lines.append("─" * 60)
    if cfg.get("promotion_started_at"):
        lines.append(f"  Status:        ACTIVE since {cfg['promotion_started_at']}")
        lines.append(f"  KOL list:      {len(cfg.get('promotion_kol_handles', []))} accounts")
        lines.append(f"  Channels:      {', '.join(cfg.get('promotion_channels', []) or ['(none)'])}")
    else:
        lines.append(f"  Status:        NOT YET STARTED (collecting baseline)")
        lines.append(f"  Edit {CONFIG_FILE} to mark promotion start.")
    lines.append("")
    lines.append("─" * 60)
    lines.append(" Files")
    lines.append("─" * 60)
    lines.append(f"  metrics:   {METRICS_FILE} ({METRICS_FILE.stat().st_size if METRICS_FILE.exists() else 0:,} bytes)")
    lines.append(f"  replies:   {REPLIES_FILE} ({len(state['seen_reply_ids'])} total)")
    lines.append(f"  quotes:    {QUOTES_FILE} ({len(state['seen_quote_ids'])} total)")
    lines.append(f"  derived:   {DERIVED_FILE}")
    lines.append("═" * 60)
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
def cycle(state: dict, cfg: dict) -> None:
    """Run one snapshot cycle."""
    ts = now_iso()
    state["cycle_count"] += 1

    # Fetch root metrics
    metrics = fetch_root_metrics()
    if not metrics:
        print(f"[{ts}] WARN: failed to fetch root metrics, skipping cycle", flush=True)
        return
    metrics["ts"] = ts
    append_jsonl(METRICS_FILE, metrics)

    # Fetch new replies (paginated, capped at MAX_PAGES per cycle)
    seen_replies = set(state["seen_reply_ids"])
    new_replies = 0
    cursor = state.get("replies_cursor", "") or ""
    for page in range(MAX_PAGES):
        try:
            tweets, next_cursor = fetch_replies_page(cursor)
        except Exception as e:
            print(f"[{ts}] WARN: replies fetch failed page={page}: {e}", flush=True)
            break
        if not tweets:
            break
        any_new = False
        for t in tweets:
            tid = t["tweet_id"]
            if not tid or tid in seen_replies:
                continue
            seen_replies.add(tid)
            t["fetched_at"] = ts
            t["is_promoted_kol"] = is_promoted(t.get("author_username", ""), cfg)
            append_jsonl(REPLIES_FILE, t)
            new_replies += 1
            any_new = True
        if not any_new or not next_cursor:
            break
        cursor = next_cursor
    # Save the latest cursor for next cycle
    state["replies_cursor"] = cursor
    state["seen_reply_ids"] = list(seen_replies)

    # Fetch new quotes
    seen_quotes = set(state["seen_quote_ids"])
    new_quotes = 0
    cursor = state.get("quotes_cursor", "") or ""
    for page in range(MAX_PAGES):
        try:
            tweets, next_cursor = fetch_quotes_page(cursor)
        except Exception as e:
            print(f"[{ts}] WARN: quotes fetch failed page={page}: {e}", flush=True)
            break
        if not tweets:
            break
        any_new = False
        for t in tweets:
            tid = t["tweet_id"]
            if not tid or tid in seen_quotes:
                continue
            seen_quotes.add(tid)
            t["fetched_at"] = ts
            t["is_promoted_kol"] = is_promoted(t.get("author_username", ""), cfg)
            append_jsonl(QUOTES_FILE, t)
            new_quotes += 1
            any_new = True
        if not any_new or not next_cursor:
            break
        cursor = next_cursor
    state["quotes_cursor"] = cursor
    state["seen_quote_ids"] = list(seen_quotes)

    # Compute derived metrics
    heat = compute_heat(metrics)
    velocity = 0.0
    if state.get("last_heat") and state.get("last_ts"):
        last_ts_dt = datetime.fromisoformat(state["last_ts"])
        cur_ts_dt = datetime.fromisoformat(ts)
        elapsed_min = (cur_ts_dt - last_ts_dt).total_seconds() / 60.0
        if elapsed_min > 0:
            velocity = (heat - state["last_heat"]) / elapsed_min

    derived = {
        "ts": ts,
        "heat_score": heat,
        "heat_velocity_per_min": velocity,
        "engagement_rate": compute_engagement_rate(metrics),
        "view_count": metrics["view_count"],
        "favorite_count": metrics["favorite_count"],
        "retweet_count": metrics["retweet_count"],
        "reply_count": metrics["reply_count"],
        "quote_count": metrics["quote_count"],
        "bookmark_count": metrics["bookmark_count"],
        "new_replies_this_cycle": new_replies,
        "new_quotes_this_cycle": new_quotes,
        "cumulative_replies_seen": len(seen_replies),
        "cumulative_quotes_seen": len(seen_quotes),
    }
    append_jsonl(DERIVED_FILE, derived)

    state["last_heat"] = heat
    state["last_ts"] = ts
    state["last_metrics"] = metrics
    save_state(state)

    # Render dashboard
    DASHBOARD_FILE.write_text(render_dashboard(metrics, derived, state, cfg, new_replies, new_quotes))

    print(
        f"[{ts}] cycle #{state['cycle_count']}: heat={heat:.0f} velocity={velocity:.1f}/min "
        f"views={metrics['view_count']:,} likes={metrics['favorite_count']:,} "
        f"RTs={metrics['retweet_count']:,} new_replies={new_replies} new_quotes={new_quotes}",
        flush=True,
    )


def main():
    print(f"=== Tweet Tracker started ===", flush=True)
    print(f"  TWEET_ID:  {TWEET_ID}", flush=True)
    print(f"  DATA_DIR:  {TWEET_DIR}", flush=True)
    print(f"  INTERVAL:  {INTERVAL}s", flush=True)

    state = load_state()
    cfg = load_config()
    save_config(cfg)  # ensure file exists

    while True:
        try:
            cycle(state, cfg)
            cfg = load_config()  # reload in case promotion config was edited
        except Exception as e:
            print(f"[{now_iso()}] ERROR cycle: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
