#!/usr/bin/env python3
"""Cascade Walker — Phase 2 for tweet-tracker.

Reads Phase 1's known replies + quotes (replies.jsonl, quotes.jsonl), then
for each one fetches its own 1-hop sub-engagement via xapi.to
twitter.tweet_detail endpoint. Builds cascade tree and computes:
  - cascade_size      total nodes (root + reply/quote + sub-replies)
  - cascade_depth     max layer (root=0, reply=1, sub-reply=2)
  - cascade_breadth   nodes per layer
  - structural_virality  Wiener index (avg shortest path between all node pairs)
  - reach (layered)   weighted follower reach with overlap discount

Outputs:
  cascade_nodes.jsonl    — every discovered node + parent + depth + author
  cascade_edges.jsonl    — parent → child edges with edge_type (reply/quote)
  cascade_metrics.jsonl  — per-cycle aggregated metrics (size/depth/breadth/wiener/reach)
  walker_state.json      — set of walked nodes + seen sub-node IDs (resume safe)

Walker policy:
  - Each Phase 1 node is walked ONCE (1-hop expansion).
  - Each cycle picks up newly discovered Phase 1 nodes since last walk.
  - Old nodes are NOT re-walked (avoids API blow-up).

API: xapi.to via CLI (npx xapi-to call ...)

Required env:
  XAPI_API_KEY               xapi.to API key (or pre-configured)
  TWEET_ID                   root tweet ID

Optional env:
  DATA_DIR                   default /opt/tweet-tracker/data
  WALKER_INTERVAL_SEC        default 1800 (30 min)
"""

import json
import os
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
TWEET_ID = os.environ["TWEET_ID"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/tweet-tracker/data"))
INTERVAL = int(os.environ.get("WALKER_INTERVAL_SEC", "1800"))

TWEET_DIR = DATA_DIR / TWEET_ID

# Phase 1 input files
REPLIES_FILE = TWEET_DIR / "replies.jsonl"
QUOTES_FILE = TWEET_DIR / "quotes.jsonl"
ROOT_METRICS_FILE = TWEET_DIR / "metrics.jsonl"

# Phase 2 output files
CASCADE_NODES_FILE = TWEET_DIR / "cascade_nodes.jsonl"
CASCADE_EDGES_FILE = TWEET_DIR / "cascade_edges.jsonl"
CASCADE_METRICS_FILE = TWEET_DIR / "cascade_metrics.jsonl"
WALKER_STATE_FILE = TWEET_DIR / "walker_state.json"

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
def parse_xapi_reply(t: dict) -> dict | None:
    """Convert xapi.to reply/quote item to our internal format."""
    user = t.get("user") or t.get("author") or {}
    tid = str(t.get("tweet_id") or t.get("id") or "")
    if not tid:
        return None
    return {
        "tweet_id": tid,
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
    }


def fetch_sub_replies(parent_tid: str) -> list[dict]:
    """Fetch replies to a parent tweet via tweet_detail."""
    try:
        data = xapi_call("twitter.tweet_detail", {"tweet_id": parent_tid})
    except Exception as e:
        print(f"[{now_iso()}] WARN: fetch_sub_replies({parent_tid}) failed: {e}", flush=True)
        return []
    replies_raw = data.get("data", {}).get("replies") or []
    out = []
    for r in replies_raw:
        rec = parse_xapi_reply(r)
        if rec and rec["tweet_id"] != parent_tid:
            out.append(rec)
    return out


def fetch_sub_quotes(parent_tid: str) -> list[dict]:
    """Fetch quotes of a parent tweet via search."""
    try:
        query = f"quoted_tweet_id:{parent_tid}"
        data = xapi_call("twitter.search_timeline", {"raw_query": query, "count": 20})
    except Exception as e:
        print(f"[{now_iso()}] WARN: fetch_sub_quotes({parent_tid}) failed: {e}", flush=True)
        return []
    tweets_raw = data.get("data", {}).get("tweets") or []
    out = []
    for t in tweets_raw:
        if t.get("is_quote"):
            rec = parse_xapi_reply(t)
            if rec and rec["tweet_id"] != parent_tid:
                out.append(rec)
    return out


# ──────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict:
    if WALKER_STATE_FILE.exists():
        return json.loads(WALKER_STATE_FILE.read_text())
    return {
        "walked_node_ids": [],   # Phase 1 nodes already 1-hop expanded
        "seen_sub_node_ids": [], # all known sub-node tweet_ids (dedup)
        "cycle_count": 0,
        "started_at": now_iso(),
    }


def save_state(state: dict) -> None:
    tmp = WALKER_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(WALKER_STATE_FILE)


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


# ──────────────────────────────────────────────────────────────
# Cascade metrics
# ──────────────────────────────────────────────────────────────
def compute_wiener_index(nodes_by_parent: dict, root_id: str) -> float:
    """Compute Wiener index of the cascade tree.

    Wiener = sum of shortest path lengths between all pairs of nodes / C(n,2).
    For simplicity (cascade trees are shallow, depth ≤ 2), we compute via
    BFS from each node — O(n^2). Fine for n < few thousand.
    """
    adj = defaultdict(set)
    all_node_ids = {root_id}
    for parent_id, children in nodes_by_parent.items():
        all_node_ids.add(parent_id)
        for c in children:
            cid = c["tweet_id"]
            all_node_ids.add(cid)
            adj[parent_id].add(cid)
            adj[cid].add(parent_id)

    n = len(all_node_ids)
    if n < 2:
        return 0.0

    total_dist = 0
    pair_count = 0
    nodes_list = list(all_node_ids)
    for i, src in enumerate(nodes_list):
        dists = {src: 0}
        q = deque([src])
        while q:
            cur = q.popleft()
            for nb in adj.get(cur, ()):
                if nb not in dists:
                    dists[nb] = dists[cur] + 1
                    q.append(nb)
        for j in range(i + 1, len(nodes_list)):
            d = dists.get(nodes_list[j])
            if d is not None:
                total_dist += d
                pair_count += 1

    return total_dist / pair_count if pair_count else 0.0


def compute_cascade_metrics(
    root_id: str,
    direct_replies: list[dict],
    direct_quotes: list[dict],
    sub_nodes_by_parent: dict,
) -> dict:
    """Walk the full tree, return aggregated metrics.

    Layer convention:
      depth 0: root
      depth 1: direct reply / quote of root
      depth 2: sub-reply / sub-quote (1-hop expansion)
    """
    # Build nodes_by_parent map for Wiener
    nodes_by_parent = defaultdict(list)
    for r in direct_replies:
        nodes_by_parent[root_id].append(r)
    for q in direct_quotes:
        nodes_by_parent[root_id].append(q)
    for parent_id, sub_nodes in sub_nodes_by_parent.items():
        nodes_by_parent[parent_id].extend(sub_nodes)

    layer_0 = 1
    layer_1 = len(direct_replies) + len(direct_quotes)
    layer_2 = sum(len(s) for s in sub_nodes_by_parent.values())
    cascade_size = layer_0 + layer_1 + layer_2

    breadth = [layer_0, layer_1, layer_2]
    max_depth = 2 if layer_2 else (1 if layer_1 else 0)

    wiener = compute_wiener_index(nodes_by_parent, root_id)

    # ── Layered Reach (v2) ──
    seen_authors = set()
    reach_gross = 0
    reach_detail = {"l1_quote": 0, "l1_reply": 0, "l2_quote": 0, "l2_reply": 0}

    # L1 Quotes — full weight
    for q in direct_quotes:
        handle = q.get("author_username", "")
        followers = int(q.get("author_followers", 0) or 0)
        if handle and handle not in seen_authors:
            seen_authors.add(handle)
            contribution = followers
            reach_gross += contribution
            reach_detail["l1_quote"] += contribution

    # L1 Replies — 30% weight
    for r in direct_replies:
        handle = r.get("author_username", "")
        followers = int(r.get("author_followers", 0) or 0)
        if handle and handle not in seen_authors:
            seen_authors.add(handle)
            contribution = int(followers * 0.3)
            reach_gross += contribution
            reach_detail["l1_reply"] += contribution

    # L2 nodes — quote 10%, reply 0%
    for parent_id, subs in sub_nodes_by_parent.items():
        for s in subs:
            handle = s.get("author_username", "")
            followers = int(s.get("author_followers", 0) or 0)
            edge_type = s.get("edge_type", "reply")
            if handle and handle not in seen_authors:
                seen_authors.add(handle)
                if edge_type == "quote":
                    contribution = int(followers * 0.1)
                    reach_gross += contribution
                    reach_detail["l2_quote"] += contribution

    # Overlap discount
    overlap_factor = max(0.3, 1.0 - 0.03 * len(seen_authors))
    reach_adjusted = int(reach_gross * overlap_factor)
    reach_est_impressions = int(reach_adjusted * 0.10)

    return {
        "cascade_size": cascade_size,
        "cascade_max_depth": max_depth,
        "cascade_breadth_per_layer": breadth,
        "structural_virality_wiener": round(wiener, 3),
        "unique_engager_count": len(seen_authors),
        "reach_gross": reach_gross,
        "reach_adjusted": reach_adjusted,
        "reach_est_impressions": reach_est_impressions,
        "reach_overlap_discount": round(overlap_factor, 2),
        "reach_detail": reach_detail,
        "reach_followers_sum": reach_adjusted,  # backward compat
    }


# ──────────────────────────────────────────────────────────────
# Main cycle
# ──────────────────────────────────────────────────────────────
def cycle(state: dict) -> None:
    state["cycle_count"] += 1
    ts = now_iso()
    print(f"[{ts}] cascade walker cycle #{state['cycle_count']}", flush=True)

    # Load Phase 1 known nodes
    direct_replies = load_jsonl(REPLIES_FILE)
    direct_quotes = load_jsonl(QUOTES_FILE)

    # Determine which Phase 1 nodes haven't been walked yet
    walked = set(state["walked_node_ids"])
    seen_sub = set(state["seen_sub_node_ids"])

    new_to_walk = []
    for rec in direct_replies + direct_quotes:
        tid = rec.get("tweet_id")
        if tid and tid not in walked:
            new_to_walk.append(rec)

    print(f"  Phase 1 nodes total: {len(direct_replies)} replies + {len(direct_quotes)} quotes", flush=True)
    print(f"  new nodes to expand: {len(new_to_walk)}", flush=True)

    # Walk each new node — fetch its sub-replies + sub-quotes
    new_sub_nodes_count = 0
    for idx, parent_rec in enumerate(new_to_walk):
        parent_tid = parent_rec["tweet_id"]
        parent_handle = parent_rec.get("author_username", "")

        # Sub-replies
        try:
            sub_replies = fetch_sub_replies(parent_tid)
        except Exception as e:
            print(f"  [{idx}/{len(new_to_walk)}] WARN: sub_replies({parent_tid}) {e}", flush=True)
            sub_replies = []
        for sn in sub_replies:
            tid = sn.get("tweet_id")
            if not tid or tid in seen_sub:
                continue
            seen_sub.add(tid)
            node_record = {
                **sn,
                "parent_id": parent_tid,
                "parent_author": parent_handle,
                "depth": 2,
                "edge_type": "reply",
                "fetched_at": ts,
            }
            append_jsonl(CASCADE_NODES_FILE, node_record)
            append_jsonl(CASCADE_EDGES_FILE, {
                "parent_id": parent_tid,
                "child_id": tid,
                "edge_type": "reply",
                "discovered_at": ts,
            })
            new_sub_nodes_count += 1

        # Sub-quotes
        try:
            sub_quotes = fetch_sub_quotes(parent_tid)
        except Exception as e:
            print(f"  [{idx}/{len(new_to_walk)}] WARN: sub_quotes({parent_tid}) {e}", flush=True)
            sub_quotes = []
        for sn in sub_quotes:
            tid = sn.get("tweet_id")
            if not tid or tid in seen_sub:
                continue
            seen_sub.add(tid)
            node_record = {
                **sn,
                "parent_id": parent_tid,
                "parent_author": parent_handle,
                "depth": 2,
                "edge_type": "quote",
                "fetched_at": ts,
            }
            append_jsonl(CASCADE_NODES_FILE, node_record)
            append_jsonl(CASCADE_EDGES_FILE, {
                "parent_id": parent_tid,
                "child_id": tid,
                "edge_type": "quote",
                "discovered_at": ts,
            })
            new_sub_nodes_count += 1

        walked.add(parent_tid)

        # Checkpoint every 10 nodes
        if (idx + 1) % 10 == 0:
            state["walked_node_ids"] = list(walked)
            state["seen_sub_node_ids"] = list(seen_sub)
            save_state(state)
            print(f"  [{idx + 1}/{len(new_to_walk)}] checkpoint saved", flush=True)

        # Gentle pacing
        time.sleep(1.0)

    state["walked_node_ids"] = list(walked)
    state["seen_sub_node_ids"] = list(seen_sub)

    # Build full tree from disk for cascade metrics
    all_sub_nodes = load_jsonl(CASCADE_NODES_FILE)
    sub_by_parent = defaultdict(list)
    for sn in all_sub_nodes:
        sub_by_parent[sn["parent_id"]].append(sn)

    metrics = compute_cascade_metrics(
        root_id=TWEET_ID,
        direct_replies=direct_replies,
        direct_quotes=direct_quotes,
        sub_nodes_by_parent=sub_by_parent,
    )
    metrics["ts"] = ts
    metrics["cycle"] = state["cycle_count"]
    metrics["new_sub_nodes_this_cycle"] = new_sub_nodes_count
    metrics["walked_nodes_this_cycle"] = len(new_to_walk)
    append_jsonl(CASCADE_METRICS_FILE, metrics)

    save_state(state)

    print(
        f"  + {new_sub_nodes_count} new sub-nodes | "
        f"size={metrics['cascade_size']} depth={metrics['cascade_max_depth']} "
        f"breadth={metrics['cascade_breadth_per_layer']} "
        f"wiener={metrics['structural_virality_wiener']:.2f} "
        f"reach_adj={metrics['reach_adjusted']:,} "
        f"(gross={metrics['reach_gross']:,} ×{metrics['reach_overlap_discount']}) "
        f"engagers={metrics['unique_engager_count']}",
        flush=True,
    )


def main():
    print("=== Cascade Walker started ===", flush=True)
    print(f"  TWEET_ID:  {TWEET_ID}", flush=True)
    print(f"  DATA_DIR:  {TWEET_DIR}", flush=True)
    print(f"  INTERVAL:  {INTERVAL}s", flush=True)
    print(f"  API:       xapi.to", flush=True)

    if not TWEET_DIR.exists():
        print(f"ERROR: Phase 1 data dir does not exist: {TWEET_DIR}", flush=True)
        print("Start tweet-tracker first.", flush=True)
        return

    state = load_state()
    while True:
        try:
            cycle(state)
        except Exception as e:
            print(f"[{now_iso()}] ERROR cycle: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
