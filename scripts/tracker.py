#!/usr/bin/env python3
"""Tweet Tracker — high-frequency snapshot of one tweet's engagement.

Phase 1 only (Phase 2 cascade walker is a separate script).

API: Twitter241 (RapidAPI) — supports cursor pagination for replies/quotes.

Required env:
  TWITTER241_RAPIDAPI_KEY    Twitter241 API key
  TWEET_ID                   target tweet pid

Optional env:
  TWITTER241_RAPIDAPI_KEY_FALLBACK   optional fallback key
  DATA_DIR                   default /opt/tweet-tracker/data
  SNAPSHOT_INTERVAL_SEC      default 300 (5 min)
  MAX_PAGES_PER_CYCLE        default 5 (cap pagination per endpoint per cycle)
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Hard socket timeout
socket.setdefaulttimeout(20)

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
KEY_PRIMARY = os.environ["TWITTER241_RAPIDAPI_KEY"]
KEY_FALLBACK = os.environ.get("TWITTER241_RAPIDAPI_KEY_FALLBACK", "")
HOST = "twitter241.p.rapidapi.com"
TWEET_ID = os.environ["TWEET_ID"]

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

# ──────────────────────────────────────────────────────────────
# XHI™ Signal Weight Framework v2
# ──────────────────────────────────────────────────────────────
W_VIEWS = 0.01
W_LIKES = 1.0
W_RTS = 2.0
W_REPLIES = 3.0
W_QUOTES = 5.0
W_QUOTES_WITH_COMMENTARY = 7.0
W_REPLY_CATALYST = 4.5
W_BOOKMARKS = 2.0

QUALITY_TIERS = [
    (100_000, 3.0),
    (10_000,  1.5),
    (1_000,   1.0),
    (0,       0.8),
]
QUALITY_NEW_ACCOUNT_DAYS = 30
QUALITY_NEW_ACCOUNT_MULTIPLIER = 0.3

TEMPORAL_DECAY = [
    (1,   1.0),
    (6,   0.85),
    (24,  0.5),
    (72,  0.2),
    (None, 0.05),
]

BONUS_QUOTE_AND_REPLY = 2.0
BONUS_CATALYST_QUOTE = 3.0
BONUS_BURST_QUOTES = 5.0
BONUS_RT_TO_QUOTE_UPGRADE = 4.0
COMMENTARY_MIN_CHARS = 50


# ──────────────────────────────────────────────────────────────
# Twitter241 HTTP client
# ──────────────────────────────────────────────────────────────
def _switch_to_fallback() -> bool:
    global _active_key, _using_fallback
    if _using_fallback or not KEY_FALLBACK:
        return False
    _using_fallback = True
    _active_key = KEY_FALLBACK
    print(f"[{now_iso()}] [QUOTA] switching to fallback key", flush=True)
    return True


def call_api(path: str, retries: int = 3) -> dict:
    url = f"https://{HOST}{path}"
    last_err = None
    for attempt in range(retries):
        headers = {"x-rapidapi-key": _active_key, "x-rapidapi-host": HOST}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
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
    raise RuntimeError(f"call_api failed: {last_err}")


# ──────────────────────────────────────────────────────────────
# Parsers (Twitter241 nested format)
# ──────────────────────────────────────────────────────────────
def parse_tweet_node(node: dict) -> dict | None:
    if not node or node.get("__typename") not in ("Tweet", None):
        return None
    legacy = node.get("legacy") or {}
    if not legacy:
        return None
    user_result = node.get("core", {}).get("user_results", {}).get("result", {}) or {}
    user_legacy = user_result.get("legacy") or {}
    user_core = user_result.get("core") or {}
    screen_name = user_core.get("screen_name") or user_legacy.get("screen_name") or ""

    views_count = 0
    views = node.get("views") or {}
    if views.get("count"):
        try:
            views_count = int(views["count"])
        except (TypeError, ValueError):
            pass

    return {
        "tweet_id": str(node.get("rest_id") or legacy.get("id_str", "")),
        "author_username": screen_name.lower(),
        "author_followers": user_legacy.get("followers_count", 0),
        "author_created_at": user_legacy.get("created_at", ""),
        "text": legacy.get("full_text", ""),
        "created_at": legacy.get("created_at", ""),
        "view_count": views_count,
        "favorite_count": legacy.get("favorite_count", 0),
        "retweet_count": legacy.get("retweet_count", 0),
        "reply_count": legacy.get("reply_count", 0),
        "quote_count": legacy.get("quote_count", 0),
        "bookmark_count": legacy.get("bookmark_count", 0),
    }


def _parse_item_content(ic: dict, root_tid: str) -> dict | None:
    result = ic.get("tweet_results", {}).get("result", ) or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", {}) or {}
    rec = parse_tweet_node(result)
    if rec and rec.get("tweet_id") and rec["tweet_id"] != root_tid:
        return rec
    return None


def extract_tweets_from_instructions(instructions: list, root_tid: str) -> tuple[list[dict], str | None]:
    tweets = []
    next_cursor = None
    for inst in instructions or []:
        for entry in inst.get("entries", []):
            eid = entry.get("entryId", "")
            content = entry.get("content", {}) or {}
            if "cursor-bottom" in eid:
                next_cursor = content.get("value") or content.get("itemContent", {}).get("value")
                continue
            if "cursor" in eid:
                continue
            if eid.startswith("tweet-"):
                rec = _parse_item_content(content.get("itemContent", {}), root_tid)
                if rec:
                    tweets.append(rec)
            elif eid.startswith("conversationthread-"):
                for item in content.get("items", []):
                    item_inner = item.get("item", {}) or {}
                    rec = _parse_item_content(item_inner.get("itemContent", {}), root_tid)
                    if rec:
                        tweets.append(rec)
    return tweets, next_cursor


# ──────────────────────────────────────────────────────────────
# Fetch functions (Twitter241 with pagination)
# ──────────────────────────────────────────────────────────────
def fetch_root_metrics() -> dict | None:
    data = call_api(f"/tweet?pid={TWEET_ID}")
    inst = (data.get("data", {})
            .get("threaded_conversation_with_injections_v2", {})
            .get("instructions", []))
    for i in inst:
        for entry in i.get("entries", []):
            if entry.get("entryId") == f"tweet-{TWEET_ID}":
                content = entry.get("content", {}) or {}
                result = (content.get("itemContent", {})
                          .get("tweet_results", {}).get("result", {}))
                if result.get("__typename") == "TweetWithVisibilityResults":
                    result = result.get("tweet", {})
                return parse_tweet_node(result)
    return None


def fetch_replies_page(cursor: str = "") -> tuple[list[dict], str | None]:
    path = f"/comments?pid={TWEET_ID}&count=20"
    if cursor:
        path += f"&cursor={urllib.parse.quote(cursor)}"
    data = call_api(path)
    inst = data.get("result", {}).get("instructions", [])
    return extract_tweets_from_instructions(inst, TWEET_ID)


def fetch_quotes_page(cursor: str = "") -> tuple[list[dict], str | None]:
    path = f"/quotes?pid={TWEET_ID}&count=20"
    if cursor:
        path += f"&cursor={urllib.parse.quote(cursor)}"
    data = call_api(path)
    inst = data.get("result", {}).get("timeline", {}).get("instructions", [])
    return extract_tweets_from_instructions(inst, TWEET_ID)


# ──────────────────────────────────────────────────────────────
# State & Helpers
# ──────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "started_at": now_iso(),
        "last_metrics": None, "last_heat": 0.0, "last_ts": None,
        "replies_cursor": "", "quotes_cursor": "",
        "seen_reply_ids": [], "seen_quote_ids": [],
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
        "tweet_id": TWEET_ID, "tracker_started_at": now_iso(),
        "promotion_started_at": None, "promotion_kol_handles": [],
        "promotion_channels": [], "promotion_budget_usd": None, "notes": "",
    }


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def is_promoted(handle: str, cfg: dict) -> bool:
    if not handle:
        return False
    return handle.lower() in [h.lower().lstrip("@") for h in cfg.get("promotion_kol_handles", [])]


# ──────────────────────────────────────────────────────────────
# XHI v2 Scoring
# ──────────────────────────────────────────────────────────────
def quality_multiplier(followers: int, account_created_at: str = "") -> float:
    q = 1.0
    for threshold, mult in QUALITY_TIERS:
        if followers >= threshold:
            q = mult
            break
    if account_created_at:
        try:
            created = datetime.strptime(account_created_at, "%a %b %d %H:%M:%S %z %Y")
            if (datetime.now(timezone.utc) - created).days < QUALITY_NEW_ACCOUNT_DAYS:
                q *= QUALITY_NEW_ACCOUNT_MULTIPLIER
        except (ValueError, TypeError):
            pass
    return q


def temporal_decay(post_created_at: str, action_time: str) -> float:
    try:
        t_post = datetime.strptime(post_created_at, "%a %b %d %H:%M:%S %z %Y")
        t_action = datetime.fromisoformat(action_time)
        delta_hours = max(0, (t_action - t_post).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 1.0
    for max_hours, mult in TEMPORAL_DECAY:
        if max_hours is None or delta_hours <= max_hours:
            return mult
    return 0.05


def quote_base_weight(text: str) -> float:
    return W_QUOTES_WITH_COMMENTARY if len(text or "") > COMMENTARY_MIN_CHARS else W_QUOTES


def compute_interaction_bonus(replies: list, quotes: list) -> float:
    bonus = 0.0
    reply_authors = {r.get("author_username", "") for r in replies if r.get("author_username")}
    quote_authors = {q.get("author_username", "") for q in quotes if q.get("author_username")}
    bonus += len(reply_authors & quote_authors) * BONUS_QUOTE_AND_REPLY
    quote_times = []
    for q in quotes:
        try:
            qt = datetime.strptime(q.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y")
            quote_times.append(qt)
        except (ValueError, TypeError):
            continue
    quote_times.sort()
    for i in range(len(quote_times) - 2):
        if (quote_times[i + 2] - quote_times[i]).total_seconds() <= 600:
            bonus += BONUS_BURST_QUOTES
            break
    return bonus


def compute_heat_v2(metrics: dict, replies: list, quotes: list, post_created_at: str = "") -> dict:
    views_score = W_VIEWS * metrics["view_count"]
    likes_score = W_LIKES * metrics["favorite_count"]
    rts_score = W_RTS * metrics["retweet_count"]

    replies_score = 0.0
    for r in replies:
        q = quality_multiplier(int(r.get("author_followers", 0) or 0), r.get("author_created_at", ""))
        d = temporal_decay(post_created_at, r.get("fetched_at", ""))
        replies_score += W_REPLIES * q * d
    replies_score += max(0, metrics["reply_count"] - len(replies)) * W_REPLIES

    quotes_score = 0.0
    for q in quotes:
        w = quote_base_weight(q.get("text", ""))
        qm = quality_multiplier(int(q.get("author_followers", 0) or 0), q.get("author_created_at", ""))
        d = temporal_decay(post_created_at, q.get("fetched_at", ""))
        quotes_score += w * qm * d
    quotes_score += max(0, metrics["quote_count"] - len(quotes)) * W_QUOTES

    bonus = compute_interaction_bonus(replies, quotes)
    heat_raw = views_score + likes_score + rts_score + replies_score + quotes_score + bonus

    return {
        "heat_raw": heat_raw,
        "components": {
            "views": round(views_score, 2), "likes": round(likes_score, 2),
            "rts": round(rts_score, 2), "replies": round(replies_score, 2),
            "quotes": round(quotes_score, 2), "bonus": round(bonus, 2),
        },
    }


def compute_engagement_rate(m: dict) -> float:
    if m["view_count"] == 0:
        return 0.0
    return (m["favorite_count"] + m["retweet_count"] + m["reply_count"] + m["quote_count"]) / m["view_count"]


# ──────────────────────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────────────────────
def render_dashboard(metrics, derived, state, cfg, new_replies, new_quotes):
    lines = [
        "═" * 60,
        f" XHI Tweet Tracker — {TWEET_ID}",
        f" Updated: {derived['ts']}",
        f" API: Twitter241",
        "═" * 60, "",
        f" Author:        @{metrics.get('author_username', '?')} ({metrics.get('author_followers', 0):,} followers)",
        f" Cycle:         #{state['cycle_count']}",
        f" Tracker age:   {state['started_at']} → now", "",
        "─" * 60, " Raw counts", "─" * 60,
        f"  views:     {metrics['view_count']:>12,}",
        f"  likes:     {metrics['favorite_count']:>12,}",
        f"  retweets:  {metrics['retweet_count']:>12,}",
        f"  replies:   {metrics['reply_count']:>12,}  (+{new_replies} new)",
        f"  quotes:    {metrics['quote_count']:>12,}  (+{new_quotes} new)",
        f"  bookmarks: {metrics['bookmark_count']:>12,}", "",
        "─" * 60, " Derived (XHI v2)", "─" * 60,
        f"  Heat Score:        {derived['heat_score']:>12,.0f}",
        f"  Heat Delta:        {derived['heat_delta']:>12,.1f}",
        f"  Heat Velocity:     {derived['heat_velocity_per_min']:>12,.1f}  per minute",
        f"  Engagement Rate:   {derived['engagement_rate']:>12,.2%}",
    ]
    components = derived.get("heat_components", {})
    if components:
        lines += ["", "  Score breakdown:"]
        for k in ("views", "likes", "rts", "replies", "quotes", "bonus"):
            lines.append(f"    {k:8s} {components.get(k, 0):>10,.1f}")
    lines += ["", "═" * 60]
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
def cycle(state, cfg):
    ts = now_iso()
    state["cycle_count"] += 1

    metrics = fetch_root_metrics()
    if not metrics:
        print(f"[{ts}] WARN: failed to fetch root metrics, skipping", flush=True)
        return
    metrics["ts"] = ts
    append_jsonl(METRICS_FILE, metrics)

    # Fetch replies (paginated)
    seen_replies = set(state["seen_reply_ids"])
    new_replies = 0
    cursor = state.get("replies_cursor", "") or ""
    for page in range(MAX_PAGES):
        try:
            tweets, next_cursor = fetch_replies_page(cursor)
        except Exception as e:
            print(f"[{ts}] WARN: replies page={page}: {e}", flush=True)
            break
        if not tweets:
            break
        any_new = False
        for t in tweets:
            tid = t["tweet_id"]
            if tid and tid not in seen_replies:
                seen_replies.add(tid)
                t["fetched_at"] = ts
                t["is_promoted_kol"] = is_promoted(t.get("author_username", ""), cfg)
                append_jsonl(REPLIES_FILE, t)
                new_replies += 1
                any_new = True
        if not any_new or not next_cursor:
            break
        cursor = next_cursor
    state["replies_cursor"] = cursor
    state["seen_reply_ids"] = list(seen_replies)

    # Fetch quotes (paginated)
    seen_quotes = set(state["seen_quote_ids"])
    new_quotes = 0
    cursor = state.get("quotes_cursor", "") or ""
    for page in range(MAX_PAGES):
        try:
            tweets, next_cursor = fetch_quotes_page(cursor)
        except Exception as e:
            print(f"[{ts}] WARN: quotes page={page}: {e}", flush=True)
            break
        if not tweets:
            break
        any_new = False
        for t in tweets:
            tid = t["tweet_id"]
            if tid and tid not in seen_quotes:
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

    # XHI v2 scoring
    all_replies = load_jsonl(REPLIES_FILE)
    all_quotes = load_jsonl(QUOTES_FILE)
    post_created_at = metrics.get("created_at", "")

    heat_result = compute_heat_v2(metrics, all_replies, all_quotes, post_created_at)
    heat = heat_result["heat_raw"]
    heat_delta = 0.0
    velocity = 0.0
    if state.get("last_heat") and state.get("last_ts"):
        last_ts_dt = datetime.fromisoformat(state["last_ts"])
        cur_ts_dt = datetime.fromisoformat(ts)
        elapsed_min = (cur_ts_dt - last_ts_dt).total_seconds() / 60.0
        heat_delta = heat - state["last_heat"]
        if elapsed_min > 0:
            velocity = heat_delta / elapsed_min

    derived = {
        "ts": ts, "heat_score": heat, "heat_delta": round(heat_delta, 2),
        "heat_velocity_per_min": velocity,
        "engagement_rate": compute_engagement_rate(metrics),
        "heat_components": heat_result["components"],
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

    DASHBOARD_FILE.write_text(render_dashboard(metrics, derived, state, cfg, new_replies, new_quotes))

    print(
        f"[{ts}] cycle #{state['cycle_count']}: heat={heat:.0f} delta={heat_delta:.0f} "
        f"velocity={velocity:.1f}/min views={metrics['view_count']:,} "
        f"likes={metrics['favorite_count']:,} RTs={metrics['retweet_count']:,} "
        f"new_replies={new_replies} new_quotes={new_quotes}",
        flush=True,
    )


def main():
    print("=== XHI Tweet Tracker started ===", flush=True)
    print(f"  TWEET_ID:  {TWEET_ID}", flush=True)
    print(f"  DATA_DIR:  {TWEET_DIR}", flush=True)
    print(f"  INTERVAL:  {INTERVAL}s", flush=True)
    print(f"  API:       Twitter241", flush=True)

    state = load_state()
    cfg = load_config()
    save_config(cfg)

    while True:
        try:
            cycle(state, cfg)
            cfg = load_config()
        except Exception as e:
            print(f"[{now_iso()}] ERROR cycle: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
