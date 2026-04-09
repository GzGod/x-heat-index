#!/usr/bin/env python3
"""Cascade Walker — Phase 2 for tweet-tracker.

Reads Phase 1's known replies + quotes (replies.jsonl, quotes.jsonl), then
for each one fetches its own 1-hop sub-engagement via Twitter241
/comments + /quotes endpoints. Builds cascade tree and computes:
  - cascade_size      total nodes (root + reply/quote + sub-replies)
  - cascade_depth     max layer (root=0, reply=1, sub-reply=2)
  - cascade_breadth   nodes per layer
  - structural_virality  Wiener index (avg shortest path between all node pairs)

Outputs:
  cascade_nodes.jsonl    — every discovered node + parent + depth + author
  cascade_edges.jsonl    — parent → child edges with edge_type (reply/quote)
  cascade_metrics.jsonl  — per-cycle aggregated metrics (size/depth/breadth/wiener)
  walker_state.json      — set of walked nodes + seen sub-node IDs (resume safe)

Walker policy:
  - Each Phase 1 node is walked ONCE (1-hop expansion).
  - Each cycle picks up newly discovered Phase 1 nodes since last walk.
  - Old nodes are NOT re-walked (avoids API blow-up).

Required env:
  TWITTER241_RAPIDAPI_KEY    primary
  TWITTER241_RAPIDAPI_KEY_FALLBACK   optional
  TWEET_ID                   root tweet ID

Optional env:
  DATA_DIR                   default /opt/tweet-tracker/data
  WALKER_INTERVAL_SEC        default 1800 (30 min)
"""

import json
import math
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

# Global hard socket timeout — urlopen's timeout= is per-read only and won't
# protect against drip-feeding servers. This caps any single socket op.
socket.setdefaulttimeout(20)

# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────
KEY_PRIMARY = os.environ["TWITTER241_RAPIDAPI_KEY"]
KEY_FALLBACK = os.environ.get("TWITTER241_RAPIDAPI_KEY_FALLBACK", "")
HOST = "twitter241.p.rapidapi.com"
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
# Twitter241 client (with fallback)
# ──────────────────────────────────────────────────────────────
_active_key = KEY_PRIMARY
_using_fallback = False


def _switch_to_fallback() -> bool:
    global _active_key, _using_fallback
    if _using_fallback or not KEY_FALLBACK:
        return False
    _using_fallback = True
    _active_key = KEY_FALLBACK
    print(f"[{now_iso()}] [QUOTA] primary key exhausted, switching to fallback", flush=True)
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
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError(f"call_api failed: {last_err}")


# ──────────────────────────────────────────────────────────────
# Parsers (same shape as tracker.py)
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
    }


def _parse_item_content(ic: dict, root_tid: str) -> dict | None:
    result = ic.get("tweet_results", {}).get("result", {}) or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", {}) or {}
    rec = parse_tweet_node(result)
    if rec and rec.get("tweet_id") and rec["tweet_id"] != root_tid:
        return rec
    return None


def extract_tweets_from_instructions(instructions: list, root_tid: str) -> list[dict]:
    tweets = []
    for inst in instructions or []:
        for entry in inst.get("entries", []):
            eid = entry.get("entryId", "")
            content = entry.get("content", {}) or {}
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
    return tweets


def fetch_sub_replies(parent_tid: str) -> list[dict]:
    """Fetch first page of /comments for parent tweet (its sub-replies)."""
    try:
        data = call_api(f"/comments?pid={parent_tid}&count=20")
    except Exception as e:
        print(f"[{now_iso()}] WARN: fetch_sub_replies({parent_tid}) failed: {e}", flush=True)
        return []
    inst = data.get("result", {}).get("instructions", [])
    return extract_tweets_from_instructions(inst, parent_tid)


def fetch_sub_quotes(parent_tid: str) -> list[dict]:
    """Fetch first page of /quotes for parent tweet (its sub-quotes)."""
    try:
        data = call_api(f"/quotes?pid={parent_tid}&count=20")
    except Exception as e:
        print(f"[{now_iso()}] WARN: fetch_sub_quotes({parent_tid}) failed: {e}", flush=True)
        return []
    inst = data.get("result", {}).get("timeline", {}).get("instructions", [])
    return extract_tweets_from_instructions(inst, parent_tid)


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
    For a tree, the shortest path between u and v is len(path(u, root)) + len(path(v, root)) - 2*len(path(LCA, root)).

    For simplicity (and because cascade trees are shallow, depth ≤ 2 here), we
    compute it via BFS from each node — O(n^2). Fine for n < few thousand.
    """
    # Build adjacency: parent → children + child → parent
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
        # BFS from src
        dists = {src: 0}
        q = deque([src])
        while q:
            cur = q.popleft()
            for nb in adj.get(cur, ()):
                if nb not in dists:
                    dists[nb] = dists[cur] + 1
                    q.append(nb)
        # Sum distances to nodes with index > i (each pair counted once)
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
    # Layer 1: direct
    for r in direct_replies:
        nodes_by_parent[root_id].append(r)
    for q in direct_quotes:
        nodes_by_parent[root_id].append(q)
    # Layer 2: sub-nodes
    for parent_id, sub_nodes in sub_nodes_by_parent.items():
        nodes_by_parent[parent_id].extend(sub_nodes)

    # Total nodes (including root)
    layer_0 = 1
    layer_1 = len(direct_replies) + len(direct_quotes)
    layer_2 = sum(len(s) for s in sub_nodes_by_parent.values())
    cascade_size = layer_0 + layer_1 + layer_2

    breadth = [layer_0, layer_1, layer_2]
    max_depth = 2 if layer_2 else (1 if layer_1 else 0)

    # Wiener index
    wiener = compute_wiener_index(nodes_by_parent, root_id)

    # Reach: deduped follower count of all unique authors at depth 1+2
    seen_authors = set()
    reach = 0
    for items in (direct_replies, direct_quotes, *sub_nodes_by_parent.values()):
        for it in items:
            handle = it.get("author_username", "")
            if handle and handle not in seen_authors:
                seen_authors.add(handle)
                reach += int(it.get("author_followers", 0) or 0)

    return {
        "cascade_size": cascade_size,
        "cascade_max_depth": max_depth,
        "cascade_breadth_per_layer": breadth,
        "structural_virality_wiener": round(wiener, 3),
        "unique_engager_count": len(seen_authors),
        "reach_followers_sum": reach,
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

        # Per-node try/except: a single bad parent shouldn't kill the cycle.
        try:
            # Sub-replies
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

        # Persist state every 10 nodes — survives mid-cycle crashes
        if (idx + 1) % 10 == 0:
            state["walked_node_ids"] = list(walked)
            state["seen_sub_node_ids"] = list(seen_sub)
            save_state(state)
            print(f"  [{idx + 1}/{len(new_to_walk)}] checkpoint saved", flush=True)

        # gentle pacing — don't burst the API
        time.sleep(0.5)

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
        f"reach={metrics['reach_followers_sum']:,} "
        f"engagers={metrics['unique_engager_count']}",
        flush=True,
    )


def main():
    print("=== Cascade Walker started ===", flush=True)
    print(f"  TWEET_ID:  {TWEET_ID}", flush=True)
    print(f"  DATA_DIR:  {TWEET_DIR}", flush=True)
    print(f"  INTERVAL:  {INTERVAL}s", flush=True)

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
