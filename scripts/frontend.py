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
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/tweet-tracker/data"))
PORT = int(os.environ.get("FRONTEND_PORT", "3301"))
BIND = os.environ.get("FRONTEND_BIND", "127.0.0.1")


# ──────────────────────────────────────────────────────────────
# HTML (inline, zero-build, single-file deployment)
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>x-heat-index</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --accent: #f78166;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --blue: #58a6ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", Segoe UI, sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.5;
  }
  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: var(--surface);
  }
  header h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  header h1 .accent { color: var(--accent); }
  header .meta {
    color: var(--muted);
    font-size: 12px;
    font-family: "SF Mono", Menlo, Consolas, monospace;
  }
  #tweet-selector {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-family: "SF Mono", Menlo, monospace;
    font-size: 12px;
    min-width: 280px;
    cursor: pointer;
  }
  main {
    padding: 24px;
    max-width: 1400px;
    margin: 0 auto;
  }
  .kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .kpi {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 18px;
  }
  .kpi .label {
    color: var(--muted);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .kpi .value {
    font-size: 22px;
    font-weight: 600;
    font-feature-settings: "tnum";
    letter-spacing: -0.02em;
  }
  .kpi .subvalue {
    color: var(--muted);
    font-size: 11px;
    margin-top: 4px;
    font-family: "SF Mono", monospace;
  }
  .kpi .stage { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .stage-discovery { background: rgba(88,166,255,0.18); color: var(--blue); }
  .stage-amplification { background: rgba(63,185,80,0.18); color: var(--green); }
  .stage-saturation { background: rgba(210,153,34,0.18); color: var(--yellow); }
  .stage-decay { background: rgba(248,81,73,0.18); color: var(--red); }
  .stage-dead { background: rgba(139,148,158,0.18); color: var(--muted); }
  .stage-unknown { background: rgba(139,148,158,0.10); color: var(--muted); }
  .charts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    min-height: 320px;
  }
  .chart-card.full { grid-column: 1 / -1; }
  .chart-title {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--text);
  }
  .chart-desc {
    font-size: 11px;
    color: var(--muted);
    margin-bottom: 12px;
  }
  .chart-wrap { position: relative; height: 260px; }
  .empty {
    color: var(--muted);
    text-align: center;
    padding: 40px 0;
    font-style: italic;
  }
  footer {
    padding: 16px 24px;
    color: var(--muted);
    font-size: 11px;
    text-align: center;
    border-top: 1px solid var(--border);
    margin-top: 32px;
    font-family: "SF Mono", monospace;
  }
  @media (max-width: 900px) {
    .charts { grid-template-columns: 1fr; }
    .chart-card.full { grid-column: 1; }
  }
</style>
</head>
<body>
  <header>
    <h1>x-heat-<span class="accent">index</span></h1>
    <select id="tweet-selector"></select>
    <div class="meta" id="updated-at">—</div>
  </header>

  <main>
    <div class="kpi-row" id="kpi-row"></div>
    <div class="charts">
      <div class="chart-card full">
        <div class="chart-title">Heat Score &amp; Velocity</div>
        <div class="chart-desc">Composite heat = 0.2·views + 1·likes + 5·RTs + 2·replies + 3·quotes. Velocity = Δheat/min.</div>
        <div class="chart-wrap"><canvas id="chart-heat"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Raw Engagement Counts</div>
        <div class="chart-desc">views (right axis) vs likes / RTs / replies / quotes (left axis)</div>
        <div class="chart-wrap"><canvas id="chart-counts"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-title">Cascade Size &amp; Structural Virality</div>
        <div class="chart-desc">cascade_size = total tree nodes; Wiener = avg shortest path between node pairs (Goel et al. 2016)</div>
        <div class="chart-wrap"><canvas id="chart-cascade"></canvas></div>
      </div>
      <div class="chart-card full">
        <div class="chart-title">Reach &amp; Unique Engagers</div>
        <div class="chart-desc">Reach = sum of unique reply/quote authors' followers. Impressions ≈ reach × 0.05–0.15.</div>
        <div class="chart-wrap"><canvas id="chart-reach"></canvas></div>
      </div>
    </div>
  </main>

  <footer>
    auto-refreshing every 30s · <span id="footer-info">—</span>
  </footer>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (n) => new Intl.NumberFormat("en-US").format(Math.round(n));
const fmtCompact = (n) => {
  if (n == null) return "—";
  if (Math.abs(n) < 1000) return String(Math.round(n));
  if (Math.abs(n) < 1e6) return (n / 1000).toFixed(1) + "K";
  if (Math.abs(n) < 1e9) return (n / 1e6).toFixed(2) + "M";
  return (n / 1e9).toFixed(2) + "B";
};

// ---- Stage classification (mirrors README definition) ----
function classifyStage(derived) {
  if (!derived || derived.length < 2) return { stage: "unknown", label: "Unknown" };
  const recent = derived.slice(-6);
  const vs = recent.map(d => d.heat_velocity_per_min || 0);
  const vAvg = vs.reduce((a, b) => a + b, 0) / vs.length;

  // Slope over last N points
  const n = vs.length;
  const xMean = (n - 1) / 2;
  const yMean = vAvg;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - xMean) * (vs[i] - yMean);
    den += (i - xMean) ** 2;
  }
  const slope = den > 0 ? num / den : 0;

  if (vAvg < 0.5) return { stage: "dead", label: "Dead" };
  if (vAvg < 1.5 && slope < 0) return { stage: "decay", label: "Decay" };
  if (slope < -1 && vAvg < 10) return { stage: "saturation", label: "Saturation" };
  if (vAvg > 50) return { stage: "amplification", label: "Amplification (extreme)" };
  if (slope > 0 && vAvg < 10) return { stage: "discovery", label: "Discovery" };
  if (slope < 0 && vAvg > 3) return { stage: "saturation", label: "Saturation" };
  return { stage: "amplification", label: "Amplification" };
}

// ---- Charts ----
Chart.defaults.color = "#c9d1d9";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
Chart.defaults.font.size = 11;

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
          time: { unit: opts.timeUnit || "hour", displayFormats: { hour: "MMM d HH:mm" } },
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
        legend: { position: "top", labels: { boxWidth: 10, padding: 12, font: { size: 11 } } },
        tooltip: { backgroundColor: "#161b22", borderColor: "#30363d", borderWidth: 1 },
      },
    },
  });
}

// ---- Chart.js time adapter (lightweight, stdlib-enough) ----
// Register a minimal adapter so we don't need to bundle date-fns.
const _adapter = {
  formats: () => ({ datetime: "MMM d HH:mm", hour: "HH:mm", day: "MMM d", month: "MMM yyyy", year: "yyyy" }),
  parse: (v) => typeof v === "number" ? v : new Date(v).valueOf(),
  format: (v, fmt) => {
    const d = new Date(v);
    const pad = (n) => String(n).padStart(2, "0");
    const mo = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][d.getMonth()];
    if (fmt === "HH:mm") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (fmt === "MMM d") return `${mo} ${d.getDate()}`;
    if (fmt === "MMM d HH:mm") return `${mo} ${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (fmt === "MMM yyyy") return `${mo} ${d.getFullYear()}`;
    if (fmt === "yyyy") return String(d.getFullYear());
    return d.toISOString();
  },
  add: (v, amt, unit) => {
    const d = new Date(v);
    const map = { millisecond: 1, second: 1000, minute: 60000, hour: 3600000, day: 86400000 };
    return d.valueOf() + amt * (map[unit] || 0);
  },
  diff: (a, b, unit) => {
    const map = { millisecond: 1, second: 1000, minute: 60000, hour: 3600000, day: 86400000 };
    return (new Date(a).valueOf() - new Date(b).valueOf()) / (map[unit] || 1);
  },
  startOf: (v) => new Date(v).valueOf(),
  endOf: (v) => new Date(v).valueOf(),
};
Chart._adapters._date.override(_adapter);

// ---- Data loading ----
async function loadTweets() {
  const r = await fetch("/api/tweets");
  return await r.json();
}

async function loadData(tid) {
  const r = await fetch(`/api/data/${tid}`);
  return await r.json();
}

// ---- Render ----
async function renderTweetList() {
  const tweets = await loadTweets();
  const sel = $("tweet-selector");
  sel.innerHTML = "";
  if (!tweets.length) {
    const opt = document.createElement("option");
    opt.textContent = "(no tracked tweets)";
    sel.appendChild(opt);
    return null;
  }
  for (const t of tweets) {
    const opt = document.createElement("option");
    opt.value = t.tweet_id;
    const author = t.author ? `@${t.author}` : "(unknown)";
    opt.textContent = `${t.tweet_id.slice(-8)} · ${author} · ${fmt(t.latest_views)} views · cycle ${t.cycles}`;
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
    $("kpi-row").innerHTML = '<div class="empty">No data yet. Wait for the first tracker cycle…</div>';
    return;
  }

  // KPI row
  const latest = derived[derived.length - 1];
  const latestCascade = cascade[cascade.length - 1] || {};
  const stage = classifyStage(derived);
  const promotionStatus = cfg.promotion_started_at
    ? `active since ${cfg.promotion_started_at.split("T")[0]}`
    : "not started (baseline)";

  $("kpi-row").innerHTML = `
    <div class="kpi">
      <div class="label">Stage</div>
      <div class="value"><span class="stage stage-${stage.stage}">${stage.label}</span></div>
      <div class="subvalue">${promotionStatus}</div>
    </div>
    <div class="kpi">
      <div class="label">Heat Score</div>
      <div class="value">${fmt(latest.heat_score || 0)}</div>
      <div class="subvalue">cycle #${derived.length}</div>
    </div>
    <div class="kpi">
      <div class="label">Velocity</div>
      <div class="value">${(latest.heat_velocity_per_min || 0).toFixed(1)} <span style="font-size:12px;color:var(--muted)">/min</span></div>
      <div class="subvalue">${velocityTier(latest.heat_velocity_per_min)}</div>
    </div>
    <div class="kpi">
      <div class="label">Cascade Size</div>
      <div class="value">${fmt(latestCascade.cascade_size || 0)}</div>
      <div class="subvalue">breadth ${JSON.stringify(latestCascade.cascade_breadth_per_layer || [])}</div>
    </div>
    <div class="kpi">
      <div class="label">Wiener (structural virality)</div>
      <div class="value">${(latestCascade.structural_virality_wiener || 0).toFixed(2)}</div>
      <div class="subvalue">${wienerLabel(latestCascade.structural_virality_wiener)}</div>
    </div>
    <div class="kpi">
      <div class="label">Reach (followers)</div>
      <div class="value">${fmtCompact(latestCascade.reach_followers_sum || 0)}</div>
      <div class="subvalue">${latestCascade.unique_engager_count || 0} unique engagers</div>
    </div>
  `;

  $("updated-at").textContent = `updated ${latest.ts}`;
  $("footer-info").textContent = `${derived.length} derived points · ${cascade.length} cascade snapshots · ${cfg.tracker_started_at || "—"}`;

  // ---- Chart data prep ----
  const derivedPoints = (fn) => derived.map(d => ({ x: d.ts, y: fn(d) }));
  const cascadePoints = (fn) => cascade.map(d => ({ x: d.ts, y: fn(d) }));

  // Heat + Velocity (dual axis)
  makeLineChart("chart-heat", [
    {
      label: "Heat Score",
      data: derivedPoints(d => d.heat_score || 0),
      borderColor: "#f78166",
      backgroundColor: "rgba(247,129,102,0.12)",
      fill: true,
      tension: 0.25,
      yAxisID: "y",
    },
    {
      label: "Velocity (/min)",
      data: derivedPoints(d => d.heat_velocity_per_min || 0),
      borderColor: "#58a6ff",
      backgroundColor: "rgba(88,166,255,0.08)",
      borderDash: [4, 4],
      tension: 0.25,
      yAxisID: "y1",
    },
  ], { y1: true });

  // Raw counts (views on right, others on left)
  makeLineChart("chart-counts", [
    { label: "views", data: derivedPoints(d => d.view_count), borderColor: "#8b949e", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y1", tension: 0.2 },
    { label: "likes", data: derivedPoints(d => d.favorite_count), borderColor: "#3fb950", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "RTs", data: derivedPoints(d => d.retweet_count), borderColor: "#58a6ff", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "replies", data: derivedPoints(d => d.reply_count), borderColor: "#d29922", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
    { label: "quotes", data: derivedPoints(d => d.quote_count), borderColor: "#f85149", backgroundColor: "transparent", borderWidth: 2, yAxisID: "y", tension: 0.2 },
  ], { y1: true });

  // Cascade size + Wiener (dual axis)
  makeLineChart("chart-cascade", [
    { label: "cascade_size", data: cascadePoints(d => d.cascade_size), borderColor: "#f78166", backgroundColor: "rgba(247,129,102,0.1)", fill: true, tension: 0.25, yAxisID: "y" },
    { label: "Wiener index", data: cascadePoints(d => d.structural_virality_wiener), borderColor: "#58a6ff", backgroundColor: "transparent", borderWidth: 2, borderDash: [4, 4], tension: 0.25, yAxisID: "y1" },
  ], { y1: true });

  // Reach + unique engagers
  makeLineChart("chart-reach", [
    { label: "Reach (Σ followers)", data: cascadePoints(d => d.reach_followers_sum), borderColor: "#3fb950", backgroundColor: "rgba(63,185,80,0.12)", fill: true, tension: 0.25, yAxisID: "y" },
    { label: "Unique engagers", data: cascadePoints(d => d.unique_engager_count), borderColor: "#d29922", backgroundColor: "transparent", borderWidth: 2, borderDash: [4, 4], tension: 0.25, yAxisID: "y1" },
  ], { y1: true });
}

function velocityTier(v) {
  if (v == null) return "—";
  if (v > 50) return "🔥 explosive — don't promote, let it ride";
  if (v > 10) return "⚡ amplification — light promotion";
  if (v > 1) return "🎯 stable — heavy promotion window";
  return "💤 stalled — pivot or stop";
}

function wienerLabel(w) {
  if (w == null || w === 0) return "—";
  if (w < 1.2) return "pure star broadcast";
  if (w < 2.0) return "shallow branching";
  if (w < 3.5) return "real structural cascade";
  return "deep viral cascade";
}

// ---- Main ----
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
    httpd = HTTPServer((BIND, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[frontend] shutting down", flush=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
