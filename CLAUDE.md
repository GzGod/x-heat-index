# x-heat-index

X 平台单推**综合热度指数系统**。对单条 X 推文 7 天窗口内的传播做
高频采样，输出一个组合了 heat / velocity / cascade structure / reach
的综合 signal。给乙方做 promotion attribution 和投前投后 lift 报告用。

## Quick Reference

| 项 | 值 |
|----|-----|
| Stack | Python 3.12 stdlib only (urllib, json, socket) — zero deps |
| 本地路径 | `Projects/x-heat-index/` |
| 服务器 | `/opt/tweet-tracker/` (systemd templates: `tweet-tracker@<id>` + `cascade-walker@<id>`) — **注意**：server paths 保留历史实现命名，未重命名 |
| 数据 | `/opt/tweet-tracker/data/<tweet_id>/` (JSONL files, no DB) |
| 部署 | scp `scripts/*.py` → server, no service restart needed (script picks up on next cycle if running, otherwise next start) |
| API | Twitter241 (RapidAPI) — primary + fallback key auto-switch |

> **Product name vs implementation name**: product = `x-heat-index`
> (本地 repo 名); 实现 = `tweet-tracker` + `cascade-walker` (server
> 路径 + systemd unit 名)。重命名 server 会导致重启交易中服务，所以
> 保持实现层稳定。新代码 / 新 instance 的命名就用 `x-heat-index`。

## Architecture

```
                   ┌─────────────────────────────────────────────────┐
                   │  Phase 1 — tweet-tracker@<id>.service           │
                   │  every 5 min                                    │
                   │                                                 │
                   │  ┌──────────┐  ┌─────────┐  ┌──────────┐        │
                   │  │ /tweet   │→ │ metrics │→ │ derived  │        │
                   │  │ /comments│→ │ replies │  │ (heat,   │        │
                   │  │ /quotes  │→ │ quotes  │  │ velocity,│        │
                   │  └──────────┘  └─────────┘  │ reach)   │        │
                   │                              └──────────┘       │
                   └─────────────────────────────────────────────────┘
                                       │
                                       ▼  (Phase 2 reads jsonl from Phase 1)
                   ┌─────────────────────────────────────────────────┐
                   │  Phase 2 — cascade-walker@<id>.service          │
                   │  every 30 min                                   │
                   │                                                 │
                   │  for each NEW reply/quote from Phase 1:         │
                   │    fetch /comments?pid=<id>  (sub-replies)      │
                   │    fetch /quotes?pid=<id>    (sub-quotes)       │
                   │    save sub-nodes + edges                       │
                   │                                                 │
                   │  ┌──────────────────┐  ┌────────────────────┐   │
                   │  │ cascade_nodes    │  │ cascade_metrics    │   │
                   │  │ cascade_edges    │  │ (size, depth,      │   │
                   │  │                  │  │ wiener, reach,     │   │
                   │  └──────────────────┘  │ engagers)          │   │
                   │                         └────────────────────┘  │
                   └─────────────────────────────────────────────────┘
```

依赖方向：Phase 2 单向读 Phase 1，Phase 1 不感知 Phase 2 存在。可以独立重启。

## Constraints (必须遵守)

1. **No hardcoded API keys** — keys read from env vars only
   (`TWITTER241_RAPIDAPI_KEY` + `_FALLBACK`). Enforced by claim
   `no_hardcoded_rapidapi_key`.
2. **Crash-safe state** — every cycle writes `state.json` (Phase 1) /
   `walker_state.json` (Phase 2) atomically (write tmp + rename).
   Phase 2 also checkpoints every 10 nodes mid-cycle.
3. **Append-only data files** — `metrics.jsonl`, `replies.jsonl`,
   `quotes.jsonl`, `cascade_nodes.jsonl` are NEVER rewritten or
   trimmed. Crashes/duplicates handled at read time, not write.
4. **Hard socket timeout** — `socket.setdefaulttimeout(20)` at module
   top in cascade_walker.py, because `urlopen(timeout=)` is per-read
   only and won't protect against drip-feeding API responses.
5. **Single-tweet daemon** — each systemd unit instance tracks
   exactly one tweet (`tweet-tracker@<tweet_id>.service`). No
   multi-tweet pooling.
6. **API budget contract** — Phase 1 ≤ ~5 calls per cycle,
   Phase 2 ≤ ~2 calls per new sub-node. 7-day total per tweet
   should stay under ~25K calls. If a tweet's cascade is bigger,
   walker self-throttles via `MAX_PAGES_PER_CYCLE`.

## Data Files

每个被追踪的 tweet 在 `/opt/tweet-tracker/data/<tweet_id>/` 下有：

| 文件 | 写入者 | 内容 |
|------|--------|------|
| `metrics.jsonl` | Phase 1 (5 min) | 原始 counts (views, likes, RTs, replies, quotes, bookmarks) + ts |
| `replies.jsonl` | Phase 1 (5 min) | 增量去重的直接 reply：tweet_id, author, text, metric, fetched_at, is_promoted_kol |
| `quotes.jsonl` | Phase 1 (5 min) | 增量去重的直接 quote |
| `derived.jsonl` | Phase 1 (5 min) | heat_score, heat_velocity_per_min, engagement_rate, new_replies_this_cycle, ... |
| `cascade_nodes.jsonl` | Phase 2 (30 min) | sub-nodes (depth=2)：parent_id, parent_author, edge_type, depth, fetched_at |
| `cascade_edges.jsonl` | Phase 2 (30 min) | parent → child 边 (parent_id, child_id, edge_type) |
| `cascade_metrics.jsonl` | Phase 2 (30 min) | cascade_size, max_depth, breadth_per_layer, wiener, reach, engagers |
| `state.json` | Phase 1 | last cursor, seen IDs, last metric (for delta + recovery) |
| `walker_state.json` | Phase 2 | walked Phase 1 node IDs, seen sub-node IDs |
| `config.json` | manual edit | promotion config (start_ts, KOL handles, channels) |
| `dashboard.txt` | Phase 1 | human-readable status, overwritten each cycle |

## Heat Score Definition

```
H(t) = 0.2·views + 1·likes + 5·RTs + 2·replies + 3·quotes
```

权重的物理含义：RTs > quotes > replies > likes > views。RTs 权重最高因为是
主动放大；quotes 是 RT + commentary；replies 是对话深度但不扩大 reach；
likes 是被动信号；views 信噪比最低。

**Heat Velocity** = `ΔH / Δt`，per minute. 这是 promotion decision 的核心。

**Stage classification** (derived from velocity trajectory):
- Discovery / Amplification / Saturation / Decay / Dead

详见 `README.md`.

## Cascade Metrics

- **cascade_size** = root + Σ direct + Σ sub
- **cascade_max_depth** = 2 (root=0, direct=1, sub=2)
- **cascade_breadth_per_layer** = `[1, n_direct, n_sub]`
- **structural_virality_wiener** = avg shortest path between all node pairs
  (Goel et al. 2016). Pure star ≈ 1.0. Real cascade with branching ≥ 2.0.
- **reach_followers_sum** = Σ unique engagers' follower_count

## API budget

每追踪一条 tweet 7 天：

| Phase | Calls |
|-------|-------|
| Phase 1 (5 min × 7 天) | ~8,000 |
| Phase 2 (30 min × 7 天，1-hop expansion only) | ~12,000 |
| Author baseline (one-time) | ~20 |
| **Per-tweet 7-day total** | **~20,000** |

占 Twitter241 RapidAPI 1.5M/month 配额的 ~1.3%。

## Operations

### 启动新追踪
```bash
TID=<tweet_id>
ssh mango "mkdir -p /opt/tweet-tracker/data/$TID && \
           systemctl start tweet-tracker@$TID && \
           sleep 60 && \
           systemctl start cascade-walker@$TID"
```

### 停止
```bash
ssh mango "systemctl stop tweet-tracker@$TID cascade-walker@$TID"
```

### 看状态
```bash
ssh mango "cat /opt/tweet-tracker/data/$TID/dashboard.txt"
```

### 部署改动
```bash
scp scripts/tracker.py mango:/opt/tweet-tracker/tracker.py
scp scripts/cascade_walker.py mango:/opt/tweet-tracker/cascade_walker.py
ssh mango "systemctl restart tweet-tracker@$TID cascade-walker@$TID"
```

## Circuit Rules

Hook 自动读取本 section，作为前置约束 + 后置验证的规则源。

### Lint
- no_hardcoded_secrets
- no_bare_except
- crash_safe_writes_only

### Constraints
- All API keys MUST come from env vars (no string literals containing `msh`)
- All JSONL files MUST be append-only
- State files MUST be written via tmp+rename for atomicity
- `socket.setdefaulttimeout` MUST be set at module top in any module that
  calls Twitter241 (else drip-feed responses can hang the daemon forever)

### Circuits
- no_hardcoded_rapidapi_key
