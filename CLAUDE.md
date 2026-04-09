# x-heat-index

X 平台单推**综合热度指数系统**。对单条 X 推文 7 天窗口内的传播做
高频采样，输出一个组合了 heat / velocity / cascade structure / reach
的综合 signal。给乙方做 promotion attribution 和投前投后 lift 报告用。

## Quick Reference

| 项 | 值 |
|----|-----|
| Stack | Python 3.12 + subprocess (npx xapi-to CLI) — no pip deps |
| 本地路径 | `Projects/x-heat-index/` |
| 服务器 | `/opt/tweet-tracker/` (systemd templates: `tweet-tracker@<id>` + `cascade-walker@<id>`) — **注意**：server paths 保留历史实现命名，未重命名 |
| 数据 | `/opt/tweet-tracker/data/<tweet_id>/` (JSONL files, no DB) |
| 部署 | scp `scripts/*.py` → server, no service restart needed (script picks up on next cycle if running, otherwise next start) |
| API | **xapi.to** via CLI (`npx xapi-to call`). Key configured via `XAPI_API_KEY` env var or `npx xapi-to config set apiKey=<key>` |

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
                   │  xapi.to CLI calls:                             │
                   │  ┌──────────────────┐  ┌─────────┐  ┌────────┐ │
                   │  │ tweet_detail     │→ │ metrics │→ │derived │ │
                   │  │ (replies inside) │→ │ replies │  │(heat,  │ │
                   │  │ search_timeline  │→ │ quotes  │  │velocity│ │
                   │  │ (quoted_tweet_id)│  └─────────┘  │reach)  │ │
                   │  └──────────────────┘               └────────┘ │
                   └─────────────────────────────────────────────────┘
                                       │
                                       ▼  (Phase 2 reads jsonl from Phase 1)
                   ┌─────────────────────────────────────────────────┐
                   │  Phase 2 — cascade-walker@<id>.service          │
                   │  every 30 min                                   │
                   │                                                 │
                   │  for each NEW reply/quote from Phase 1:         │
                   │    tweet_detail(parent_id)  → sub-replies       │
                   │    search_timeline(quoted)  → sub-quotes        │
                   │    save sub-nodes + edges                       │
                   │                                                 │
                   │  ┌──────────────────┐  ┌────────────────────┐  │
                   │  │ cascade_nodes    │  │ cascade_metrics    │  │
                   │  │ cascade_edges    │  │ (size, depth,      │  │
                   │  │                  │  │ wiener, reach,     │  │
                   │  └──────────────────┘  │ engagers)          │  │
                   │                         └────────────────────┘ │
                   └─────────────────────────────────────────────────┘
```

依赖方向：Phase 2 单向读 Phase 1，Phase 1 不感知 Phase 2 存在。可以独立重启。

## Constraints (必须遵守)

1. **No hardcoded API keys** — keys set via `XAPI_API_KEY` env var
   or `npx xapi-to config set apiKey=<key>`. Never in source code.
2. **Crash-safe state** — every cycle writes `state.json` (Phase 1) /
   `walker_state.json` (Phase 2) atomically (write tmp + rename).
   Phase 2 also checkpoints every 10 nodes mid-cycle.
3. **Append-only data files** — `metrics.jsonl`, `replies.jsonl`,
   `quotes.jsonl`, `cascade_nodes.jsonl` are NEVER rewritten or
   trimmed. Crashes/duplicates handled at read time, not write.
4. **Single-tweet daemon** — each systemd unit instance tracks
   exactly one tweet (`tweet-tracker@<tweet_id>.service`). No
   multi-tweet pooling.
5. **API calls via xapi.to CLI** — all Twitter data fetched through
   `npx xapi-to call twitter.*`. Subprocess timeout = 60s with retry.

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

## Heat Score Definition (XHI v2)

XHI v2 uses a 4-layer signal scoring framework:

### Layer 1: Base Weights
```
Quote with Commentary (>50 chars):  7.0
Quote:                              5.0
Reply (conversation catalyst):     4.5
Reply:                              3.0
RT:                                 2.0
Like:                               1.0
Bookmark:                           2.0  (when API available)
View:                               0.01
```

### Layer 2: Quality Multipliers
- >100K followers: ×3.0
- 10K-100K: ×1.5
- 1K-10K: ×1.0
- <1K: ×0.8
- Account age <30 days: ×0.3 (stacks)

### Layer 3: Temporal Decay
- 0-1h: ×1.0, 1-6h: ×0.85, 6-24h: ×0.5, 24-72h: ×0.2, 72h+: ×0.05

### Layer 4: Cross-Signal Interaction Bonus
- Same user Quote + Reply: +2.0
- Quote triggers ≥3 sub-replies: +3.0
- ≥3 quotes within 10min: +5.0
- RT → Quote upgrade within 30min: +4.0

**Composite**: `XHI_raw = Σ(Base × Quality × Decay) + Bonus`

## Cascade Metrics

- **cascade_size** = root + Σ direct + Σ sub
- **cascade_max_depth** = 2 (root=0, direct=1, sub=2)
- **cascade_breadth_per_layer** = `[1, n_direct, n_sub]`
- **structural_virality_wiener** = avg shortest path between all node pairs
  (Goel et al. 2016). Pure star ≈ 1.0. Real cascade with branching ≥ 2.0.

### Reach (v2 — Layered)
- **reach_gross** = Σ weighted follower counts by layer:
  - L1 Quote: 100% (appears on quoter's timeline)
  - L1 Reply: 30% (low timeline exposure)
  - L2 Quote: 10% (two clicks from original)
  - L2 Reply: 0% (invisible to replier's followers)
- **reach_adjusted** = reach_gross × overlap_discount
  - overlap_discount = max(0.3, 1.0 - 0.03 × unique_engager_count)
- **reach_est_impressions** = reach_adjusted × 0.10
- **reach_followers_sum** = reach_adjusted (backward compat alias)

## API budget

xapi.to 按调用计费（余额制）。每追踪一条 tweet 7 天：

| Phase | Calls |
|-------|-------|
| Phase 1 (5 min × 7 天, ~2 calls/cycle) | ~4,000 |
| Phase 2 (30 min × 7 天, ~2 calls/node) | ~6,000 |
| **Per-tweet 7-day total** | **~10,000** |

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
- All API keys MUST come from env vars or xapi-to config (never in source code)
- All JSONL files MUST be append-only
- State files MUST be written via tmp+rename for atomicity

### Circuits
- no_hardcoded_api_key
