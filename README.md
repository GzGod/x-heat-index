# x-heat-index

**X 平台单推热度指数系统。** 对**单条** X 推文 7 天窗口内的传播做高频
采样，算出一个综合"热度指数"—— 不是单个 metric 数字，是
`heat score + velocity + cascade structure + reach` 四个维度合成的
actionable signal，给乙方做 promotion attribution 和投前投后 lift 报告用。

> **只做 X**（所以叫 x-heat-index，不叫 heat-index）。
> **单条 tweet 粒度**，不是账号级或话题级流量监测。
> 一个 systemd unit instance 追一条推。想追多条就开多个 instance
> （template `@<tweet_id>` 让你 zero-config）。

> **命名说明：** server 上实现层的路径是 `/opt/tweet-tracker/`，systemd
> template 也叫 `tweet-tracker@.service` + `cascade-walker@.service`。
> 这些是**历史遗留的实现命名**，没改；product 层面的名字 = x-heat-index。
> 就像 Orion repo 和 /opt/orion 的关系。

---

## 它能告诉你什么

- **Heat 曲线** — 累积影响力，按 `0.2·views + 1·likes + 5·RTs + 2·replies + 3·quotes` 加权
- **Velocity 曲线** — 每分钟 heat 增量，promotion ROI 决策核心信号
- **Stage** — Discovery / Amplification / Saturation / Decay / Dead
- **Cascade tree** — root → reply/quote → sub-reply/sub-quote 两层结构
- **Structural virality** (Wiener index) — 数字告诉你"是浅广播还是深扩散"
- **Reach** — 所有 unique engagers 的 follower 总和（去重）
- **Promotion lift**（要先配 promotion KOL 列表）— 投放期 vs baseline 对比

---

## 30 秒快速上手

**追新推文：**

```bash
TID=2042067459570856303    # 推文 ID（URL 末段）
ssh mango "
  mkdir -p /opt/tweet-tracker/data/$TID
  systemctl start tweet-tracker@$TID
  sleep 60
  systemctl start cascade-walker@$TID
"
```

**看实时状态：**

```bash
ssh mango "cat /opt/tweet-tracker/data/$TID/dashboard.txt"
```

**停止追踪：**

```bash
ssh mango "systemctl stop tweet-tracker@$TID cascade-walker@$TID"
```

**导出 7 天数据：**

```bash
scp -r mango:/opt/tweet-tracker/data/$TID ./report-$TID/
```

---

## 架构

两个独立的 systemd template service，**单向数据流**：

```
Phase 1: tweet-tracker@<id>.service       Phase 2: cascade-walker@<id>.service
─────────────────────────────────────     ─────────────────────────────────────
每 5 分钟一次                              每 30 分钟一次
                                          
1. /tweet?pid=ROOT          → metrics    1. 读 Phase 1 的 replies.jsonl
2. /comments?pid=ROOT       → replies    2. 读 Phase 1 的 quotes.jsonl  
3. /quotes?pid=ROOT         → quotes     3. 对每个 NEW reply/quote:
4. compute heat/velocity/reach              /comments?pid=THAT_ID  → sub-replies
5. write derived.jsonl                      /quotes?pid=THAT_ID    → sub-quotes
6. write dashboard.txt                   4. write cascade_nodes / edges / metrics

→ ~5 calls/cycle                         → ~2 calls/new node
→ ~8K calls / 7 days                     → ~12K calls / 7 days
```

**为什么分两个 daemon：** Phase 1 需要高频（病毒早期峰值在前 1 小时），
Phase 2 是 O(N) 在已知节点数上扫描，太频繁会浪费 API。30 min 一次足够
追上 cascade 增长但不爆 quota。

---

## 数据文件

`/opt/tweet-tracker/data/<tweet_id>/` 下面：

| 文件 | 写者 | 字段 | 用途 |
|------|------|------|------|
| `metrics.jsonl` | Phase 1 | ts, view_count, favorite_count, retweet_count, reply_count, quote_count, bookmark_count | 时间序列原始 |
| `replies.jsonl` | Phase 1 | tweet_id, author_username, author_followers, text, created_at, view_count, fetched_at, is_promoted_kol | 直接 reply 全量 |
| `quotes.jsonl` | Phase 1 | 同上 | 直接 quote 全量 |
| `derived.jsonl` | Phase 1 | ts, heat_score, heat_velocity_per_min, engagement_rate, new_replies_this_cycle, ... | 导出指标时序 |
| `cascade_nodes.jsonl` | Phase 2 | + parent_id, parent_author, depth, edge_type | sub-engagement 节点 |
| `cascade_edges.jsonl` | Phase 2 | parent_id, child_id, edge_type, discovered_at | 树边 |
| `cascade_metrics.jsonl` | Phase 2 | ts, cascade_size, cascade_max_depth, cascade_breadth_per_layer, structural_virality_wiener, unique_engager_count, reach_followers_sum | cascade 指标时序 |
| `state.json` | Phase 1 | last cursor, seen IDs, last metrics | crash recovery |
| `walker_state.json` | Phase 2 | walked Phase 1 nodes, seen sub-nodes | crash recovery |
| `config.json` | manual | promotion config（见下节） | promotion attribution |
| `dashboard.txt` | Phase 1 | 人类可读快照 | `cat` 看现状 |

所有 `*.jsonl` 都是 **append-only**，可以随时 `wc -l` 看节点数量，`tail -1` 看最新。

---

## Promotion Attribution（乙方场景）

刚启动 tracker 的时候 `config.json` 是空骨架：

```json
{
  "tweet_id": "2042067459570856303",
  "tracker_started_at": "2026-04-09T17:22:50+00:00",
  "promotion_started_at": null,
  "promotion_kol_handles": [],
  "promotion_channels": [],
  "promotion_budget_usd": null,
  "notes": ""
}
```

**Promotion 真正开始时**，编辑 `config.json` 填三个字段：

```json
{
  "promotion_started_at": "2026-04-10T03:00:00+00:00",
  "promotion_kol_handles": ["@kol1", "@kol2", "@kol3"],
  "promotion_channels": ["paid-rt", "discord", "telegram"]
}
```

**Phase 1 每 5 分钟会重新读 config**，从下一个 cycle 开始：
- 任何 reply/quote 的 author 命中 KOL 列表 → 标记 `is_promoted_kol: true`
- 之后 analyzer 算 lift 时，这条线之前的就是 baseline，之后的就是 promoted period
- 投放结束就改回 `null` 或加 `promotion_ended_at`

> ⚠️ **promotion 开始之前 tracker 必须已经在跑**，才能采到 baseline。
> 已经开播再启 tracker = baseline 永远丢失。

---

## 指标定义（详细）

### Heat Score `H(t)`

```
H(t) = 0.2·views + 1·likes + 5·RTs + 2·replies + 3·quotes
```

校准：8K-follower 的 KOL 普通推 24h `H ≈ 500-2000`，准爆推 `5000-20000`，
爆款 `50000+`。

### Heat Velocity `V(t)`

```
V(t) = ΔH / Δt   per minute
```

Promotion decision matrix（如果你**就是**作者方，不是乙方）：

| V | 阶段 | 决策 |
|---|------|------|
| > 50 | 爆炸期 | 不投，让它跑 |
| 10-50 | 健康放大 | 轻投 |
| 1-10 | 稳定 | 重投（promotion 杠杆最大窗口） |
| < 1 | 停滞 | 停 / pivot |

### Cascade Size

```
cascade_size = root + Σ(direct replies + direct quotes) + Σ(sub-replies + sub-quotes)
```

只有两层（root=0, direct=1, sub=2），1-hop expansion only。

### Structural Virality (Wiener Index)

```
W = (Σ shortest_path(u, v) for all node pairs (u,v)) / C(n,2)
```

**经验值：**
- W ≈ 1.0 → 纯星型广播（一个 root，所有 reply 都是叶子，没有 sub-reply）
- W ≈ 1.5-2.0 → 有少量 sub-reply
- W ≈ 2.5-4.0 → 真有分叉传播，结构性病毒
- W > 5.0 → 罕见，深嵌套讨论

参考：Goel et al. 2016, "The Structural Virality of Online Diffusion."

### Reach

```
R = Σ unique_engager.followers_count   (去重)
```

**注意：** retweeter 列表 Twitter API 不暴露（2023+ 限制），所以 R **只**
包含 reply + quote 的 author。这恰好是更有信号的部分（reply/quote 比纯 RT
更代表深度参与）。

实际 impression ≈ R × 0.05-0.15（不是每个 follower 都会看到）。

---

## 添加 / 移除 tweet（多条同时追）

**Template service**，instance 名 = tweet_id：

```bash
TID=ABC123

# 启动
ssh mango "systemctl start tweet-tracker@$TID cascade-walker@$TID"

# 状态
ssh mango "systemctl is-active tweet-tracker@$TID cascade-walker@$TID"

# 停止 + 保留数据
ssh mango "systemctl stop tweet-tracker@$TID cascade-walker@$TID"

# 停止 + 删数据
ssh mango "systemctl stop tweet-tracker@$TID cascade-walker@$TID && rm -rf /opt/tweet-tracker/data/$TID"

# 列出所有正在追的 tweet
ssh mango "systemctl list-units 'tweet-tracker@*' --state=active"
```

每追踪一条额外的推 ≈ 20K 额外 API call / 7 天。

---

## 故障排查

### Daemon 不跑

```bash
ssh mango "systemctl status tweet-tracker@$TID --no-pager"
ssh mango "tail -50 /opt/tweet-tracker/tracker.log"
ssh mango "tail -50 /opt/tweet-tracker/walker.log"
```

最常见原因：API key 过期 / quota 用完。但 fallback key 应该自动接管，
看 log 里有没有 `[QUOTA] primary key exhausted` 这一行。

### 数据停止增长

`tail -1 /opt/tweet-tracker/data/$TID/derived.jsonl` 看 ts。如果 ts 不
更新但 daemon active，看 tracker.log 有没有 ERROR。

### Walker 卡住不动

最常见：某个 reply 的 /comments 调用慢得像爬，触发 `socket.setdefaulttimeout`
（20 秒）后抛异常。Walker 现在有 try/except 单节点，会跳过坏节点继续。
如果还是卡，重启 walker：

```bash
ssh mango "systemctl restart cascade-walker@$TID"
```

State 会从 walker_state.json 恢复，已完成的节点不会重复。

### Cascade size 突然不增长

可能是：
1. 推文真的不传播了（看 Phase 1 derived.jsonl 的 V 是不是也接近 0）
2. Walker 没在跑（`systemctl is-active cascade-walker@$TID`）
3. 没有新的 Phase 1 节点（直接 reply/quote 不增长 → 没东西可 expand）

### 不知道追的是哪条推

```bash
ssh mango "ls /opt/tweet-tracker/data/"
```

每个目录名就是 tweet_id。

---

## API key 管理

Tracker 用 Twitter241 RapidAPI，**支持 primary → fallback 自动切换**。
Key 来自 systemd unit 的 `Environment=` 或 cron 行内 env：

```
Environment=TWITTER241_RAPIDAPI_KEY=primary_key
Environment=TWITTER241_RAPIDAPI_KEY_FALLBACK=fallback_key
```

切换轮换流程见 `Projects/orion/docs/server.md` §12（同一台服务器，所有
Twitter241 服务统一接 fallback pattern）。

---

## 已知 TODO

1. **analyzer.py + plot.py** — 7 天后从 jsonl 生成 6 张图（heat 曲线、velocity 曲线、cascade 时序、reach 累积、cascade tree 可视化、综合 dashboard）
2. **Bark 通知** — Stage 切换 / V 跨阈值 / 任何 anomaly 时主动推 iPhone
3. **情感分类** — 每条 reply 跑 Claude Haiku 分类 positive/negative/neutral，给 promotion report 加情感维度
4. **多推文聚合 dashboard** — 同时追多条时统一看
