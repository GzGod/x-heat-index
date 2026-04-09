#!/usr/bin/env python3
"""x-heat-index frontend — single-file HTTP server + dashboard.

Serves:
  GET /                        — dashboard HTML
  GET /api/tweets              — list all tracked tweets (for tweet selector)
  GET /api/data/<tweet_id>     — derived + cascade_metrics time series + raw metrics

Stdlib only. Chart.js and vanilla JS on the client side. Reads JSONL
files on each request (no caching — data is small and freshness matters).

Required env:
  DATA_DIR        default /opt/tweet-tracker/data
  FRONTEND_PORT   default 3301
  FRONTEND_BIND   default 127.0.0.1 (use 0.0.0.0 ONLY behind a reverse proxy)
"""

import json
import os
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/tweet-tracker/data"))
PORT = int(os.environ.get("FRONTEND_PORT", "3301"))
BIND = os.environ.get("FRONTEND_BIND", "127.0.0.1")


# ──────────────────────────────────────────────────────────────
# HTML (inline, zero-build, single-file deployment)
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>x-heat-index · 传播实时监控</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface-hover: #1c2128;
    --border: #30363d;
    --text: #c9d1d9;
    --text-bright: #f0f6fc;
    --muted: #8b949e;
    --muted-dim: #6e7681;
    --accent: #f78166;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", Segoe UI, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.6;
  }
  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 32px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--surface);
    flex-wrap: wrap;
    gap: 12px;
  }
  header h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  header h1 .accent { color: var(--accent); }
  header h1 .sub { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 10px; }
  header .meta {
    color: var(--muted);
    font-size: 11px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
  }
  #tweet-selector {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 6px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: 12px;
    min-width: 320px;
    cursor: pointer;
  }
  main {
    padding: 24px 32px 48px;
    max-width: 1400px;
    margin: 0 auto;
  }

  /* ── Hero status card ── */
  .hero {
    background: var(--surface);
    border: 1px solid var(--border);
    border-left-width: 4px;
    border-radius: 10px;
    padding: 24px 28px;
    margin-bottom: 24px;
  }
  .hero.stage-amplification { border-left-color: var(--green); background: linear-gradient(90deg, rgba(63,185,80,0.08), var(--surface) 40%); }
  .hero.stage-discovery { border-left-color: var(--blue); background: linear-gradient(90deg, rgba(88,166,255,0.08), var(--surface) 40%); }
  .hero.stage-saturation { border-left-color: var(--yellow); background: linear-gradient(90deg, rgba(210,153,34,0.08), var(--surface) 40%); }
  .hero.stage-decay { border-left-color: var(--red); background: linear-gradient(90deg, rgba(248,81,73,0.08), var(--surface) 40%); }
  .hero.stage-dead { border-left-color: var(--muted-dim); background: linear-gradient(90deg, rgba(139,148,158,0.06), var(--surface) 40%); }
  .hero.stage-unknown { border-left-color: var(--muted-dim); }

  .hero-headline {
    font-size: 24px;
    font-weight: 600;
    color: var(--text-bright);
    margin: 0 0 8px 0;
    letter-spacing: -0.01em;
  }
  .hero-emoji { font-size: 28px; margin-right: 8px; vertical-align: middle; }
  .hero-narrative {
    font-size: 14px;
    color: var(--text);
    margin: 12px 0 0 0;
    max-width: 780px;
  }
  .hero-narrative p { margin: 6px 0; }
  .hero-recommendation {
    margin-top: 16px;
    padding: 12px 16px;
    background: rgba(88,166,255,0.06);
    border-left: 3px solid var(--blue);
    border-radius: 4px;
    font-size: 13px;
  }
  .hero-recommendation strong { color: var(--blue); }

  /* ── KPI row ── */
  .section-label {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin: 32px 0 12px;
    font-weight: 600;
  }
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 12px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    position: relative;
    cursor: help;
    transition: background 0.12s;
  }
  .kpi:hover { background: var(--surface-hover); }
  .kpi .label {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .kpi .label .q {
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 1px solid var(--muted-dim);
    border-radius: 50%;
    text-align: center;
    line-height: 12px;
    font-size: 10px;
    color: var(--muted-dim);
  }
  .kpi .value {
    font-size: 26px;
    font-weight: 600;
    font-feature-settings: "tnum";
    letter-spacing: -0.02em;
    color: var(--text-bright);
  }
  .kpi .value .unit { font-size: 13px; color: var(--muted); margin-left: 4px; font-weight: 400; }
  .kpi .value .arrow { font-size: 16px; margin-left: 6px; }
  .kpi .arrow.up { color: var(--green); }
  .kpi .arrow.down { color: var(--red); }
  .kpi .arrow.flat { color: var(--muted); }
  .kpi .subvalue {
    color: var(--muted);
    font-size: 11px;
    margin-top: 4px;
  }
  .kpi .tooltip {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    z-index: 10;
    background: #0d1117;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 12px;
    color: var(--text);
    box-shadow: 0 6px 20px rgba(0,0,0,0.4);
    opacity: 0;
    transform: translateY(-4px);
    pointer-events: none;
    transition: opacity 0.12s, transform 0.12s;
    margin-top: 6px;
  }
  .kpi:hover .tooltip { opacity: 1; transform: translateY(0); }

  /* ── Charts ── */
  .charts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 18px 20px;
  }
  .chart-card.full { grid-column: 1 / -1; }
  .chart-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text-bright);
  }
  .chart-desc {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 14px;
    line-height: 1.5;
  }
  .chart-wrap { position: relative; height: 280px; }

  .empty {
    color: var(--muted);
    text-align: center;
    padding: 60px 0;
    font-style: italic;
  }
  footer {
    padding: 16px 32px;
    color: var(--muted-dim);
    font-size: 11px;
    text-align: center;
    border-top: 1px solid var(--border);
    margin-top: 32px;
    font-family: "SF Mono", monospace;
  }
  @media (max-width: 900px) {
    .charts { grid-template-columns: 1fr; }
    .chart-card.full { grid-column: 1; }
    header { padding: 12px 16px; }
    main { padding: 16px; }
    .hero { padding: 18px 20px; }
    .hero-headline { font-size: 20px; }
  }
</style>
</head>
<body>
  <header>
    <div>
      <h1>x-heat-<span class="accent">index</span><span class="sub">推文传播实时监控</span></h1>
    </div>
    <select id="tweet-selector"></select>
    <div class="meta" id="updated-at">—</div>
  </header>

  <main>
    <div id="hero" class="hero stage-unknown">
      <div class="hero-headline"><span class="hero-emoji">⏳</span><span id="hero-title">加载中…</span></div>
      <div class="hero-narrative" id="hero-narrative"></div>
      <div class="hero-recommendation" id="hero-recommendation" style="display:none"></div>
    </div>

    <div class="section-label">关键指标</div>
    <div class="kpi-row" id="kpi-row"></div>

    <div class="section-label">传播曲线</div>
    <div class="charts">
      <div class="chart-card full">
        <div class="chart-title">热度曲线 & 传播速度</div>
        <div class="chart-desc">
          橙色实线 = XHI 综合热度（多层加权：基础权重 × 影响力系数 × 时间衰减 + 组合加成）。<br>
          蓝色虚线 = 当前每分钟新增热度（越高说明正在传播越快；接近 0 = 停滞）。
        </div>
        <div class="chart-wrap"><canvas id="chart-heat"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">原始互动数据</div>
        <div class="chart-desc">
          观看数 (灰线，右轴) 和 点赞/转发/评论/引用 (左轴) 的时间变化。
        </div>
        <div class="chart-wrap"><canvas id="chart-counts"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">参与人数 & 扩散深度</div>
        <div class="chart-desc">
          橙色实线 = 参与讨论的账户总数（包括回复的回复）。<br>
          蓝色虚线 = "扩散深度指数"——越高说明是"树状分叉传播"（有人回回复），越接近 1 说明是"单向广播"。
        </div>
        <div class="chart-wrap"><canvas id="chart-cascade"></canvas></div>
      </div>
      <div class="chart-card full">
        <div class="chart-title">预估触达 & 参与账户</div>
        <div class="chart-desc">
          绿色实线 = 分层加权触达（一级引用100%、一级回复30%、二级引用10%、二级回复不计，经粉丝重叠修正）。<br>
          黄色虚线 = 实际参与讨论的独立账户数。
        </div>
        <div class="chart-wrap"><canvas id="chart-reach"></canvas></div>
      </div>
    </div>
  </main>

  <footer>
    每 30 秒自动刷新 · <span id="footer-info">—</span>
  </footer>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (n) => new Intl.NumberFormat("zh-CN").format(Math.round(n));
const fmtCompact = (n) => {
  if (n == null) return "—";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  if (Math.abs(n) < 1e4) return (n / 1000).toFixed(1) + "K";
  if (Math.abs(n) < 1e8) return (n / 1e4).toFixed(1) + "万";
  return (n / 1e8).toFixed(2) + "亿";
};

// ── Stage classification (mirrors README) ──
function classifyStage(derived) {
  if (!derived || derived.length < 2) return { stage: "unknown", label: "数据不足" };
  const recent = derived.slice(-6);
  const vs = recent.map(d => d.heat_velocity_per_min || 0);
  const vAvg = vs.reduce((a, b) => a + b, 0) / vs.length;

  const n = vs.length;
  const xMean = (n - 1) / 2;
  const yMean = vAvg;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - xMean) * (vs[i] - yMean);
    den += (i - xMean) ** 2;
  }
  const slope = den > 0 ? num / den : 0;

  if (vAvg < 0.5) return { stage: "dead", label: "传播停止" };
  if (vAvg < 1.5 && slope < 0) return { stage: "decay", label: "长尾衰退" };
  if (slope < -1 && vAvg < 10) return { stage: "saturation", label: "进入平台期" };
  if (vAvg > 50) return { stage: "amplification", label: "爆发传播" };
  if (slope > 0 && vAvg < 10) return { stage: "discovery", label: "早期发现" };
  if (slope < 0 && vAvg > 3) return { stage: "saturation", label: "进入平台期" };
  return { stage: "amplification", label: "活跃传播" };
}

function stageEmoji(stage) {
  return {
    discovery: "🌱",
    amplification: "🔥",
    saturation: "📈",
    decay: "📉",
    dead: "💤",
    unknown: "⏳",
  }[stage] || "⏳";
}

// ── Narrative generator (plain-language description) ──
function generateNarrative(derived, cascade, cfg, stage) {
  if (!derived || derived.length < 2) {
    return {
      title: "正在收集数据",
      narrative: "<p>刚启动，还没有足够的 cycle 可以做趋势判断。大约 3 个 cycle（15 分钟）后会开始显示状态。</p>",
      recommendation: null,
    };
  }

  const latest = derived[derived.length - 1];
  const latestCascade = cascade[cascade.length - 1] || {};
  const firstCascade = cascade[0] || {};
  const prev = derived[Math.max(0, derived.length - 7)]; // ~30min ago

  const views = latest.view_count || 0;
  const likes = latest.favorite_count || 0;
  const rts = latest.retweet_count || 0;
  const replies = latest.reply_count || 0;
  const quotes = latest.quote_count || 0;
  const velocity = latest.heat_velocity_per_min || 0;
  const heat = latest.heat_score || 0;

  const dViews = views - (prev.view_count || 0);
  const dLikes = likes - (prev.favorite_count || 0);
  const dRts = rts - (prev.retweet_count || 0);

  const cascadeSize = latestCascade.cascade_size || 0;
  const wiener = latestCascade.structural_virality_wiener || 0;
  const engagers = latestCascade.unique_engager_count || 0;
  const reach = latestCascade.reach_followers_sum || 0;

  // Title
  const title = stage.label;

  // Narrative paragraphs
  let narr = [];

  // 1. 当前传播状态
  if (stage.stage === "amplification") {
    narr.push(`<p>这条推<strong>正在活跃传播</strong>。每分钟在累积 <strong>${velocity.toFixed(1)}</strong> 的综合热度。</p>`);
  } else if (stage.stage === "discovery") {
    narr.push(`<p>这条推处于<strong>早期发现阶段</strong>，传播速度在上升但还没到峰值。</p>`);
  } else if (stage.stage === "saturation") {
    narr.push(`<p>这条推已经<strong>进入传播平台期</strong>——核心受众基本触达，曝光还在涨但增速在放缓。</p>`);
  } else if (stage.stage === "decay") {
    narr.push(`<p>这条推<strong>进入长尾衰退</strong>，每分钟新增互动很少。</p>`);
  } else if (stage.stage === "dead") {
    narr.push(`<p>传播基本停止了，最近几个 cycle 几乎没有新互动。</p>`);
  }

  // 2. 量级说明（最近 30 分钟的变化）
  if (dViews > 100 || dLikes > 5 || dRts > 2) {
    narr.push(`<p>过去 30 分钟新增：<strong>${fmt(dViews)}</strong> 次曝光，<strong>${dLikes}</strong> 个赞，<strong>${dRts}</strong> 次转发。</p>`);
  }

  // 3. 扩散结构
  if (cascadeSize > 0) {
    let shape;
    if (wiener < 1.3) shape = "<strong>浅层广播</strong>（大家都在直接回复作者，很少有人互相讨论）";
    else if (wiener < 2.5) shape = "<strong>开始有分叉</strong>（有人在回复别人的回复，形成对话）";
    else if (wiener < 4.0) shape = "<strong>树状扩散</strong>（真正的多层讨论在发生，传播结构有深度）";
    else shape = "<strong>深度病毒传播</strong>（极深的讨论嵌套，少见）";

    narr.push(`<p>已经有 <strong>${engagers}</strong> 个独立账户参与，总参与节点 <strong>${cascadeSize}</strong>（含回复的回复）。传播结构是${shape}。</p>`);
  }

  // 4. 触达
  if (reach > 0) {
    const reachAdj = latestCascade.reach_adjusted || reach;
    const discount = latestCascade.reach_overlap_discount || '—';
    narr.push(`<p>分层加权触达约 <strong>${fmtCompact(reachAdj)}</strong> 人（毛触达 ${fmtCompact(reach)}，重叠折扣 ${discount}）。</p>`);
  }

  // Recommendation (promotion decision)
  let recommendation = null;
  if (cfg && cfg.promotion_started_at) {
    recommendation = `<strong>🎯 投放监控中</strong>：从 ${cfg.promotion_started_at.split("T")[0]} 开始投放。后续会在 7 天结束时对比投放前/后的 lift 数据。`;
  } else if (stage.stage === "amplification" && velocity >= 1 && velocity <= 10) {
    recommendation = `<strong>💡 投放建议</strong>：当前处在"健康放大期"（每分钟 ${velocity.toFixed(1)} 热度累积）——这是 promotion ROI 最高的窗口，每投入一份预算可以撬动 5-10 倍自然传播。`;
  } else if (stage.stage === "amplification" && velocity > 50) {
    recommendation = `<strong>⚠️ 不要投放</strong>：当前是爆发期，自然传播已经在飙——投钱浪费，让它自己跑完峰值。`;
  } else if (stage.stage === "saturation") {
    recommendation = `<strong>⚠️ 投放效果有限</strong>：已进入平台期，自然传播减速——再投钱回报递减，建议等下一条推文或 pivot。`;
  } else if (stage.stage === "decay" || stage.stage === "dead") {
    recommendation = `<strong>⛔ 不建议投放</strong>：传播已停，投钱是救尸——把预算留给下一条还在增长的推。`;
  } else if (stage.stage === "discovery") {
    recommendation = `<strong>🚀 最佳投放时机</strong>：早期发现阶段，此时投放可以把它推进"爆发期"——每 1 单位预算预期带来 10× 以上的 heat 杠杆。`;
  }

  return {
    title,
    narrative: narr.join(""),
    recommendation,
  };
}

// ── Chart setup ──
Chart.defaults.color = "#c9d1d9";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, PingFang SC, Segoe UI, sans-serif";
Chart.defaults.font.size = 11;

// Date adapter loaded from CDN (chartjs-adapter-date-fns)

let charts = {};
function makeLineChart(canvasId, datasets, opts = {}) {
  const ctx = $(canvasId).getContext("2d");
  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          type: "time",
          time: { unit: opts.timeUnit || "hour" },
          grid: { color: "rgba(48,54,61,0.4)" },
        },
        y: {
          grid: { color: "rgba(48,54,61,0.4)" },
          ticks: { callback: (v) => fmtCompact(v) },
        },
        ...(opts.y1 ? { y1: {
          position: "right",
          grid: { drawOnChartArea: false },
          ticks: { callback: (v) => fmtCompact(v) },
        } } : {}),
      },
      plugins: {
        legend: { position: "top", labels: { boxWidth: 10, padding: 14, font: { size: 12 } } },
        tooltip: { backgroundColor: "#0d1117", borderColor: "#30363d", borderWidth: 1 },
      },
    },
  });
}

// ── Data loading ──
async function loadTweets() { return (await fetch("/api/tweets")).json(); }
async function loadData(tid) { return (await fetch(`/api/data/${tid}`)).json(); }

// ── Arrow computation ──
function arrow(current, previous, threshold = 0.02) {
  if (previous == null || previous === 0) return { sym: "", cls: "" };
  const delta = (current - previous) / Math.abs(previous);
  if (delta > threshold) return { sym: "↑", cls: "up" };
  if (delta < -threshold) return { sym: "↓", cls: "down" };
  return { sym: "→", cls: "flat" };
}

function kpi(label, value, unit, arrow_, subvalue, tooltip) {
  return `
    <div class="kpi">
      <div class="label">${label} <span class="q">?</span></div>
      <div class="value">${value}${unit ? `<span class="unit">${unit}</span>` : ""}${arrow_ ? `<span class="arrow ${arrow_.cls}">${arrow_.sym}</span>` : ""}</div>
      ${subvalue ? `<div class="subvalue">${subvalue}</div>` : ""}
      <div class="tooltip">${tooltip}</div>
    </div>`;
}

// ── Render ──
async function renderTweetList() {
  const tweets = await loadTweets();
  const sel = $("tweet-selector");
  sel.innerHTML = "";
  if (!tweets.length) {
    const opt = document.createElement("option");
    opt.textContent = "(尚无追踪的推文)";
    sel.appendChild(opt);
    return null;
  }
  for (const t of tweets) {
    const opt = document.createElement("option");
    opt.value = t.tweet_id;
    const author = t.author ? `@${t.author}` : "(unknown)";
    opt.textContent = `${t.tweet_id.slice(-8)} · ${author} · ${fmt(t.latest_views)} 次曝光 · 第 ${t.cycles} 次采样`;
    sel.appendChild(opt);
  }
  return tweets[0].tweet_id;
}

async function renderDashboard(tid) {
  if (!tid) return;
  const data = await loadData(tid);
  const derived = data.derived || [];
  const cascade = data.cascade || [];
  const cfg = data.config || {};

  if (!derived.length) {
    $("kpi-row").innerHTML = '<div class="empty">暂无数据，等待第一次采样…</div>';
    return;
  }

  const latest = derived[derived.length - 1];
  const prev = derived[Math.max(0, derived.length - 7)]; // ~30 min ago
  const latestCascade = cascade[cascade.length - 1] || {};
  const prevCascade = cascade[Math.max(0, cascade.length - 2)] || {};
  const stage = classifyStage(derived);
  const narrative = generateNarrative(derived, cascade, cfg, stage);

  // ── Hero ──
  const hero = $("hero");
  hero.className = `hero stage-${stage.stage}`;
  $("hero-title").innerHTML = narrative.title;
  document.querySelector(".hero-emoji").textContent = stageEmoji(stage.stage);
  $("hero-narrative").innerHTML = narrative.narrative;
  const rec = $("hero-recommendation");
  if (narrative.recommendation) {
    rec.innerHTML = narrative.recommendation;
    rec.style.display = "block";
  } else {
    rec.style.display = "none";
  }

  // ── KPI row ──
  const viewsArrow = arrow(latest.view_count, prev.view_count);
  const heatArrow = arrow(latest.heat_score, prev.heat_score);
  const reachArrow = arrow(latestCascade.reach_adjusted || latestCascade.reach_followers_sum, prevCascade.reach_adjusted || prevCascade.reach_followers_sum);
  const engagerArrow = arrow(latestCascade.unique_engager_count, prevCascade.unique_engager_count);

  $("kpi-row").innerHTML = [
    kpi(
      "总曝光",
      fmt(latest.view_count || 0),
      "次", viewsArrow,
      "被展示在别人时间线上的次数",
      "每次推文出现在某个用户的时间线上都算 1 次曝光。不代表用户真的读了，只代表推文进入了他的 feed。"
    ),
    kpi(
      "综合热度",
      fmt(latest.heat_score || 0),
      "", heatArrow,
      "XHI v2 多层信号评分",
      "XHI v2 四层评分体系：Layer 1 基础权重（Quote 5.0 > Reply 3.0 > RT 2.0 > Like 1.0 > View 0.01），Layer 2 互动者影响力加权，Layer 3 时间衰减，Layer 4 信号组合加成。"
    ),
    kpi(
      "当前传播速度",
      (latest.heat_velocity_per_min || 0).toFixed(1),
      "/分", null,
      velocityTierLabel(latest.heat_velocity_per_min),
      "每分钟热度的增量。0 = 不再传播，10+ = 活跃传播，50+ = 爆发。这是判断「现在是不是还火」的核心指标，不是「总共多火」。"
    ),
    kpi(
      "参与讨论人数",
      fmt(latestCascade.unique_engager_count || 0),
      "人", engagerArrow,
      "独立账户，含回复和引用者",
      "所有回复、引用原推文及其下层讨论的独立 X 账户数（去重）。包括「回复的回复」，所以大于原推 reply_count。"
    ),
    kpi(
      "扩散深度",
      (latestCascade.structural_virality_wiener || 0).toFixed(1),
      "", null,
      wienerLabelZh(latestCascade.structural_virality_wiener),
      "学术上叫 Wiener index（Goel et al. 2016），通俗理解：越接近 1 = 所有人都在「直接回原帖」（浅层广播）；越高 = 「有人在回别人的回复」（真·多层讨论）。2-4 之间算健康的树状扩散。"
    ),
    kpi(
      "预估触达",
      fmtCompact(latestCascade.reach_adjusted || latestCascade.reach_followers_sum || 0),
      "人", reachArrow,
      `毛触达 ${fmtCompact(latestCascade.reach_gross || 0)} × 重叠折扣 ${latestCascade.reach_overlap_discount || '—'}`,
      "分层加权触达：一级引用全额计入，一级回复按30%计入，二级引用按10%计入，二级回复不计入。结果经粉丝重叠修正（互动者越多折扣越大）。"
    ),
  ].join("");

  $("updated-at").textContent = `最近更新：${latest.ts.replace("T", " ").replace(/\+.*$/, "")}`;
  $("footer-info").textContent = `${derived.length} 次采样 · ${cascade.length} 次扩散分析 · 启动于 ${(cfg.tracker_started_at || "—").replace("T", " ").replace(/\+.*$/, "")}`;

  // ── Chart data prep ──
  const derivedPoints = (fn) => derived.map(d => ({ x: d.ts, y: fn(d) }));
  const cascadePoints = (fn) => cascade.map(d => ({ x: d.ts, y: fn(d) }));

  makeLineChart("chart-heat", [
    {
      label: "综合热度",
      data: derivedPoints(d => d.heat_score || 0),
      borderColor: "#f78166",
      backgroundColor: "rgba(247,129,102,0.12)",
      fill: true,
      tension: 0.25,
      yAxisID: "y",
    },
    {
      label: "传播速度 (每分钟)",
      data: derivedPoints(d => d.heat_velocity_per_min || 0),
      borderColor: "#58a6ff",
      backgroundColor: "rgba(88,166,255,0.08)",
      borderDash: [5, 5],
      tension: 0.25,
      yAxisID: "y1",
    },
  ], { y1: true });

  makeLineChart("chart-counts", [
    { label: "曝光数", data: derivedPoints(d => d.view_count), borderColor: "#8b949e", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y1", tension: 0.2 },
    { label: "点赞", data: derivedPoints(d => d.favorite_count), borderColor: "#3fb950", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "转发", data: derivedPoints(d => d.retweet_count), borderColor: "#58a6ff", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "回复", data: derivedPoints(d => d.reply_count), borderColor: "#d29922", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "引用", data: derivedPoints(d => d.quote_count), borderColor: "#f85149", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
  ], { y1: true });

  makeLineChart("chart-cascade", [
    { label: "参与节点总数", data: cascadePoints(d => d.cascade_size), borderColor: "#f78166", backgroundColor: "rgba(247,129,102,0.1)", fill: true, tension: 0.25, yAxisID: "y" },
    { label: "扩散深度指数", data: cascadePoints(d => d.structural_virality_wiener), borderColor: "#58a6ff", backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 5], tension: 0.25, yAxisID: "y1" },
  ], { y1: true });

  makeLineChart("chart-reach", [
    { label: "预估触达 (分层加权)", data: cascadePoints(d => d.reach_adjusted || d.reach_followers_sum), borderColor: "#3fb950", backgroundColor: "rgba(63,185,80,0.12)", fill: true, tension: 0.25, yAxisID: "y" },
    { label: "参与独立账户数", data: cascadePoints(d => d.unique_engager_count), borderColor: "#d29922", backgroundColor: "transparent", borderWidth: 2, borderDash: [5, 5], tension: 0.25, yAxisID: "y1" },
  ], { y1: true });
}

function velocityTierLabel(v) {
  if (v == null) return "—";
  if (v > 50) return "🔥 爆发中";
  if (v > 10) return "⚡ 活跃放大";
  if (v > 1) return "🎯 稳定传播";
  if (v > 0.1) return "📉 减速";
  return "💤 基本停止";
}

function wienerLabelZh(w) {
  if (w == null || w === 0) return "—";
  if (w < 1.3) return "浅层广播";
  if (w < 2.5) return "开始分叉";
  if (w < 4.0) return "树状扩散";
  return "深度传播";
}

// ── Main ──
let currentTid = null;
async function init() {
  currentTid = await renderTweetList();
  $("tweet-selector").addEventListener("change", (e) => {
    currentTid = e.target.value;
    renderDashboard(currentTid);
  });
  if (currentTid) renderDashboard(currentTid);
  setInterval(() => { if (currentTid) renderDashboard(currentTid); }, 30000);
}
init();
</script>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────
# Data loading (stdlib, per-request)
# ──────────────────────────────────────────────────────────────
def load_jsonl(path: Path) -> list:
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


def list_tweets() -> list:
    tweets = []
    if not DATA_DIR.exists():
        return tweets
    for tid_dir in sorted(DATA_DIR.iterdir()):
        if not tid_dir.is_dir():
            continue
        metrics = load_jsonl(tid_dir / "metrics.jsonl")
        latest = metrics[-1] if metrics else {}
        tweets.append({
            "tweet_id": tid_dir.name,
            "author": latest.get("author_username"),
            "latest_views": latest.get("view_count", 0),
            "cycles": len(metrics),
        })
    return tweets


def load_tweet_data(tid: str) -> dict:
    d = DATA_DIR / tid
    if not d.exists() or not d.is_dir():
        return {"error": "not found"}
    derived = load_jsonl(d / "derived.jsonl")
    cascade = load_jsonl(d / "cascade_metrics.jsonl")
    config_file = d / "config.json"
    config = {}
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
        except json.JSONDecodeError:
            pass
    return {"derived": derived, "cascade": cascade, "config": config}


# ──────────────────────────────────────────────────────────────
# HTTP handler
# ──────────────────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    # Silence default access log — we log on-demand via print()
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/" or path == "/index.html":
                self._send_html(HTML)
            elif path == "/api/tweets":
                self._send_json(list_tweets())
            elif path.startswith("/api/data/"):
                tid = path[len("/api/data/"):].strip("/")
                # Basic sanity: tweet IDs are all-digits
                if not tid.isdigit():
                    self._send_json({"error": "invalid tweet_id"}, status=400)
                    return
                self._send_json(load_tweet_data(tid))
            elif path == "/health":
                self._send_json({"status": "ok"})
            else:
                self.send_error(404)
        except Exception as e:
            print(f"[frontend] ERROR {path}: {e}", file=sys.stderr, flush=True)
            self._send_json({"error": str(e)}, status=500)


def main():
    print(f"[frontend] x-heat-index serving on http://{BIND}:{PORT}", flush=True)
    print(f"[frontend] DATA_DIR={DATA_DIR}", flush=True)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[frontend] shutting down", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
