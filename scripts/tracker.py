#!/usr/bin/env python3
"""Tweet Tracker — high-frequency snapshot of one tweet's engagement.

Phase 1 only (Phase 2 cascade walker is a separate script).

Outputs (per tweet, in DATA_DIR/<tweet_id>/):
  metrics.jsonl   — counts every cycle (views, likes, RTs, replies, quotes, bookmarks)
  replies.jsonl   — incremental new replies (dedup)
  quotes.jsonl    — incremental new quotes
  derived.jsonl   — heat / velocity / reach / engagement_rate per cycle
  state.json      — seen IDs (for crash recovery)
  config.json     — promotion start ts, KOL list, channels (manually edited later)
  dashboard.txt   — human-readable status, overwritten each cycle

API: xapi.to via CLI (npx xapi-to call ...)

Required env:
  XAPI_API_KEY               xapi.to API key (or pre-configured via npx xapi-to config)
  TWEET_ID                   target tweet ID

Optional env:
  DATA_DIR                   default /opt/tweet-tracker/data
  SNAPSHOT_INTERVAL_SEC      default 300 (5 min)
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
TWEET_ID = os.environ["TWEET_ID"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/tweet-tracker/data"))
INTERVAL = int(os.environ.get("SNAPSHOT_INTERVAL_SEC", "300"))

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

# Layer 1: Base Signal Weights
W_VIEWS = 0.01
W_LIKES = 1.0
W_RTS = 2.0
W_REPLIES = 3.0
W_QUOTES = 5.0
W_QUOTES_WITH_COMMENTARY = 7.0  # Quote with >50 chars commentary
W_REPLY_CATALYST = 4.5          # Reply that triggers sub-thread (≥3 sub-replies)
W_BOOKMARKS = 2.0               # Latent interest (when API available)

# Layer 2: Quality Multiplier thresholds
QUALITY_TIERS = [
    (100_000, 3.0),   # >100K followers: Tier-1 KOL
    (10_000,  1.5),   # 10K-100K: Mid-tier
    (1_000,   1.0),   # 1K-10K: baseline
    (0,       0.8),   # <1K: low influence
]
QUALITY_NEW_ACCOUNT_DAYS = 30       # accounts younger than this
QUALITY_NEW_ACCOUNT_MULTIPLIER = 0.3

# Layer 3: Temporal Decay brackets (hours → multiplier)
TEMPORAL_DECAY = [
    (1,   1.0),    # 0-1h: peak window
    (6,   0.85),   # 1-6h: active propagation
    (24,  0.5),    # 6-24h: mid-lifecycle
    (72,  0.2),    # 24-72h: long tail
    (None, 0.05),  # 72h+: residual
]

# Layer 4: Cross-Signal Interaction Bonus
BONUS_QUOTE_AND_REPLY = 2.0        # same user quotes + replies
BONUS_CATALYST_QUOTE = 3.0         # quote triggers ≥3 sub-replies
BONUS_BURST_QUOTES = 5.0           # ≥3 independent quotes within 10min
BONUS_RT_TO_QUOTE_UPGRADE = 4.0    # same user RT then quote within 30min

COMMENTARY_MIN_CHARS = 50          # threshold for "Quote with Commentary"


# ──────────────────────────────────────────────────────────────
# xapi.to CLI client
# ──────────────────────────────────────────────────────────────
def xapi_call(action: str, params: dict, retries: int = 3) -> dict:
    """Call xapi.to via CLI, return parsed JSON response."""
    cmd = ["npx", "xapi-to", "call", action, "--input", json.dumps(params)]
    last_err = None
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                last_err = RuntimeError(f"xapi-to exit {result.returncode}: {result.stderr.strip()}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise last_err
            data = json.loads(result.stdout)
            if not data.get("success", True):
                err = data.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(f"xapi error: {msg}")
            return data
        except subprocess.TimeoutExpired:
            last_err = RuntimeError("xapi-to call timed out (60s)")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
        except json.JSONDecodeError as e:
            last_err = RuntimeError(f"xapi-to returned invalid JSON: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
    raise last_err or RuntimeError("xapi_call failed")


# ──────────────────────────────────────────────────────────────
# Parsers (xapi.to flat format)
# ──────────────────────────────────────────────────────────────
def parse_xapi_tweet(t: dict) -> dict:
    """Convert xapi.to tweet record to our internal flat format.

    Handles both search_timeline format (user field) and
    tweet_detail format (author field, id instead of tweet_id).
    """
    user = t.get("user") or t.get("author") or {}
    return {
        "tweet_id": str(t.get("tweet_id") or t.get("id") or ""),
        "author_username": (user.get("screen_name") or t.get("screen_name") or "").lower(),
        "author_followers": user.get("followers_count", 0),
        "author_created_at": user.get("created_at", ""),
        "text": t.get("text") or t.get("full_text") or "",
        "created_at": t.get("created_at", ""),
        "view_count": int(t.get("view_count") or t.get("views_count") or 0),
        "favorite_count": int(t.get("favorite_count") or 0),
        "retweet_count": int(t.get("retweet_count") or 0),
        "reply_count": int(t.get("reply_count") or 0),
        "quote_count": int(t.get("quote_count") or 0),
        "bookmark_count": int(t.get("bookmark_count") or 0),
    }


def parse_xapi_reply(t: dict) -> dict:
    """Convert an xapi.to reply/quote item to our internal format.

    tweet_detail replies use: id, author, full_text, is_quote_status, in_reply_to_status_id
    search_timeline uses: tweet_id, user, text, is_quote, is_reply
    """
    user = t.get("user") or t.get("author") or {}
    return {
        "tweet_id": str(t.get("tweet_id") or t.get("id") or ""),
        "author_username": (user.get("screen_name") or "").lower(),
        "author_followers": user.get("followers_count", 0),
        "author_created_at": user.get("created_at", ""),
        "text": t.get("text") or t.get("full_text") or "",
        "created_at": t.get("created_at", ""),
        "view_count": int(t.get("view_count") or t.get("views_count") or 0),
        "favorite_count": int(t.get("favorite_count") or 0),
        "retweet_count": int(t.get("retweet_count") or 0),
        "reply_count": int(t.get("reply_count") or 0),
        "quote_count": int(t.get("quote_count") or 0),
        "is_quote": bool(t.get("is_quote") or t.get("is_quote_status")),
    }


def _fetch_tweet_detail() -> dict | None:
    """Fetch tweet_detail once (used by metrics, replies, and quotes)."""
    try:
        return xapi_call("twitter.tweet_detail", {"tweet_id": TWEET_ID})
    except Exception as e:
        print(f"[{now_iso()}] WARN: tweet_detail failed: {e}", flush=True)
        return None


def fetch_root_metrics() -> dict | None:
    """Fetch the root tweet's current metrics via xapi.to."""
    data = _fetch_tweet_detail()
    if not data:
        return None
    tweet_data = data.get("data", {}).get("tweet") or data.get("data", {})
    if not tweet_data:
        return None
    return parse_xapi_tweet(tweet_data)


def fetch_replies_and_quotes(detail_data: dict | None) -> tuple[list[dict], list[dict]]:
    """Extract replies and quotes from tweet_detail response.

    Returns (replies, quotes) split by is_quote_status field.
    """
    if not detail_data:
        return [], []
    all_items = detail_data.get("data", {}).get("replies") or []
    replies = []
    quotes = []
    for item in all_items:
        rec = parse_xapi_reply(item)
        if not rec["tweet_id"]:
            continue
        if rec["is_quote"]:
            quotes.append(rec)
        else:
            replies.append(rec)
    return replies, quotes


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


def quality_multiplier(followers: int, account_created_at: str = "") -> float:
    """Layer 2: compute quality multiplier from follower count and account age."""
    q = 1.0
    for threshold, mult in QUALITY_TIERS:
        if followers >= threshold:
            q = mult
            break
    # New account penalty
    if account_created_at:
        try:
            created = datetime.strptime(account_created_at, "%a %b %d %H:%M:%S %z %Y")
            age_days = (datetime.now(timezone.utc) - created).days
            if age_days < QUALITY_NEW_ACCOUNT_DAYS:
                q *= QUALITY_NEW_ACCOUNT_MULTIPLIER
        except (ValueError, TypeError):
            pass
    return q


def temporal_decay(post_created_at: str, action_time: str) -> float:
    """Layer 3: compute temporal decay based on hours since post creation."""
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
    """Determine base weight for a quote: standard or with commentary."""
    if len(text or "") > COMMENTARY_MIN_CHARS:
        return W_QUOTES_WITH_COMMENTARY
    return W_QUOTES


def compute_interaction_bonus(replies: list, quotes: list, post_created_at: str) -> float:
    """Layer 4: cross-signal interaction bonuses."""
    bonus = 0.0

    reply_authors = {r.get("author_username", "") for r in replies if r.get("author_username")}
    quote_authors = {q.get("author_username", "") for q in quotes if q.get("author_username")}

    # Bonus: same user quote + reply
    overlap = reply_authors & quote_authors
    bonus += len(overlap) * BONUS_QUOTE_AND_REPLY

    # Bonus: burst quotes (≥3 independent quotes within 10min window)
    quote_times = []
    for q in quotes:
        try:
            qt = datetime.strptime(q.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y")
            quote_times.append(qt)
        except (ValueError, TypeError):
            continue
    quote_times.sort()
    for i in range(len(quote_times) - 2):
        window = (quote_times[i + 2] - quote_times[i]).total_seconds()
        if window <= 600:  # 10 minutes
            bonus += BONUS_BURST_QUOTES
            break  # count once per cycle

    return bonus


def compute_heat_v2(
    metrics: dict,
    replies: list,
    quotes: list,
    post_created_at: str = "",
) -> dict:
    """XHI v2 multi-layer heat scoring.

    Returns dict with heat_raw, component breakdown, and bonus.
    """
    # Views baseline (no quality multiplier, no decay for aggregate count)
    views_score = W_VIEWS * metrics["view_count"]

    # Likes baseline (aggregate — no per-user data available)
    likes_score = W_LIKES * metrics["favorite_count"]

    # RTs baseline (aggregate — no per-user data available)
    rts_score = W_RTS * metrics["retweet_count"]

    # Per-reply scoring with quality + decay
    replies_score = 0.0
    for r in replies:
        w = W_REPLIES
        q = quality_multiplier(
            int(r.get("author_followers", 0) or 0),
            r.get("author_created_at", "") or r.get("created_at", ""),
        )
        d = temporal_decay(post_created_at, r.get("fetched_at", ""))
        replies_score += w * q * d

    # Fallback: if aggregate reply_count > len(replies), add remainder at base weight
    tracked_reply_count = len(replies)
    untracked_replies = max(0, metrics["reply_count"] - tracked_reply_count)
    replies_score += untracked_replies * W_REPLIES

    # Per-quote scoring with quality + decay + commentary detection
    quotes_score = 0.0
    for q in quotes:
        w = quote_base_weight(q.get("text", ""))
        qm = quality_multiplier(
            int(q.get("author_followers", 0) or 0),
            q.get("author_created_at", "") or q.get("created_at", ""),
        )
        d = temporal_decay(post_created_at, q.get("fetched_at", ""))
        quotes_score += w * qm * d

    # Fallback for untracked quotes
    tracked_quote_count = len(quotes)
    untracked_quotes = max(0, metrics["quote_count"] - tracked_quote_count)
    quotes_score += untracked_quotes * W_QUOTES

    # Layer 4: interaction bonus
    bonus = compute_interaction_bonus(replies, quotes, post_created_at)

    heat_raw = views_score + likes_score + rts_score + replies_score + quotes_score + bonus

    return {
        "heat_raw": heat_raw,
        "components": {
            "views": round(views_score, 2),
            "likes": round(likes_score, 2),
            "rts": round(rts_score, 2),
            "replies": round(replies_score, 2),
            "quotes": round(quotes_score, 2),
            "bonus": round(bonus, 2),
        },
    }


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
    lines.append(f" XHI Tweet Tracker — {TWEET_ID}")
    lines.append(f" Updated: {derived['ts']}")
    lines.append(f" API: xapi.to")
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
    lines.append(" Derived (XHI v2)")
    lines.append("─" * 60)
    lines.append(f"  Heat Score:        {derived['heat_score']:>12,.0f}")
    lines.append(f"  Heat Velocity:     {derived['heat_velocity_per_min']:>12,.1f}  per minute")
    lines.append(f"  Engagement Rate:   {derived['engagement_rate']:>12,.2%}")
    components = derived.get("heat_components", {})
    if components:
        lines.append("")
        lines.append("  Score breakdown:")
        lines.append(f"    views:   {components.get('views', 0):>10,.1f}")
        lines.append(f"    likes:   {components.get('likes', 0):>10,.1f}")
        lines.append(f"    RTs:     {components.get('rts', 0):>10,.1f}")
        lines.append(f"    replies: {components.get('replies', 0):>10,.1f}")
        lines.append(f"    quotes:  {components.get('quotes', 0):>10,.1f}")
        lines.append(f"    bonus:   {components.get('bonus', 0):>10,.1f}")
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

    # Fetch tweet detail (one API call for metrics + replies + quotes)
    detail_data = _fetch_tweet_detail()
    if not detail_data:
        print(f"[{ts}] WARN: failed to fetch tweet detail, skipping cycle", flush=True)
        return
    tweet_data = detail_data.get("data", {}).get("tweet") or detail_data.get("data", {})
    if not tweet_data:
        print(f"[{ts}] WARN: no tweet data in response, skipping cycle", flush=True)
        return
    metrics = parse_xapi_tweet(tweet_data)
    metrics["ts"] = ts
    append_jsonl(METRICS_FILE, metrics)

    # Split replies and quotes from the same response
    reply_list, quote_list = fetch_replies_and_quotes(detail_data)

    # Process replies
    seen_replies = set(state["seen_reply_ids"])
    new_replies = 0
    for t in reply_list:
        tid = t["tweet_id"]
        if not tid or tid in seen_replies:
            continue
        seen_replies.add(tid)
        t["fetched_at"] = ts
        t["is_promoted_kol"] = is_promoted(t.get("author_username", ""), cfg)
        append_jsonl(REPLIES_FILE, t)
        new_replies += 1
    state["seen_reply_ids"] = list(seen_replies)

    # Process quotes (from same tweet_detail response)
    seen_quotes = set(state["seen_quote_ids"])
    new_quotes = 0
    for t in quote_list:
        tid = t["tweet_id"]
        if not tid or tid in seen_quotes:
            continue
        seen_quotes.add(tid)
        t["fetched_at"] = ts
        t["is_promoted_kol"] = is_promoted(t.get("author_username", ""), cfg)
        append_jsonl(QUOTES_FILE, t)
        new_quotes += 1
    state["seen_quote_ids"] = list(seen_quotes)

    # Compute derived metrics (XHI v2: multi-layer scoring)
    all_replies = load_jsonl(REPLIES_FILE)
    all_quotes = load_jsonl(QUOTES_FILE)
    post_created_at = metrics.get("created_at", "")

    heat_result = compute_heat_v2(metrics, all_replies, all_quotes, post_created_at)
    heat = heat_result["heat_raw"]
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

    # Render dashboard
    DASHBOARD_FILE.write_text(render_dashboard(metrics, derived, state, cfg, new_replies, new_quotes))

    print(
        f"[{ts}] cycle #{state['cycle_count']}: heat={heat:.0f} velocity={velocity:.1f}/min "
        f"views={metrics['view_count']:,} likes={metrics['favorite_count']:,} "
        f"RTs={metrics['retweet_count']:,} new_replies={new_replies} new_quotes={new_quotes}",
        flush=True,
    )


def main():
    print(f"=== XHI Tweet Tracker started ===", flush=True)
    print(f"  TWEET_ID:  {TWEET_ID}", flush=True)
    print(f"  DATA_DIR:  {TWEET_DIR}", flush=True)
    print(f"  INTERVAL:  {INTERVAL}s", flush=True)
    print(f"  API:       xapi.to", flush=True)

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
