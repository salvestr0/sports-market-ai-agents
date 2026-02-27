"""
Sports Betting Bot Dashboard — sports_server.py
================================================
Flask web dashboard for the sports betting bot.
Run alongside sports_bot.py (or use it to start/stop the bot).

Usage:
  python sports_server.py
  Open http://localhost:8050
"""

import json
import os
import sys
import subprocess
import threading
import sqlite3
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

DB_PATH    = "sports_trades.db"
STATUS_FILE = "sports_status.json"
BOT_LOG    = "sports_bot.log"

# Bot subprocess handle
_bot_proc = None
_bot_lock = threading.RLock()

# Agent pipeline subprocess handle
AGENT_LOG = "agents/runner.log"
_agent_proc = None
_agent_lock = threading.RLock()

def agent_running() -> bool:
    global _agent_proc
    with _agent_lock:
        return _agent_proc is not None and _agent_proc.poll() is None


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def read_status() -> dict:
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def query_db(sql: str, args=()) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

def tail_log(n: int = 80) -> str:
    try:
        with open(BOT_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return "No log file yet."

def tail_agent_log(n: int = 100) -> str:
    try:
        with open(AGENT_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return "No agent log yet."

def bot_running() -> bool:
    global _bot_proc
    with _bot_lock:
        return _bot_proc is not None and _bot_proc.poll() is None


# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    status = read_status()
    status["bot_running"] = bot_running()
    status["agent_running"] = agent_running()
    return jsonify(status)

@app.route("/api/bets")
def api_bets():
    bets = query_db("SELECT * FROM bets ORDER BY id DESC LIMIT 50")
    return jsonify(bets)

@app.route("/api/open")
def api_open():
    bets = query_db("SELECT * FROM bets WHERE resolved=0 AND status IN ('live','paper') ORDER BY id DESC")
    return jsonify(bets)

@app.route("/api/performance")
def api_performance():
    rows = query_db("""
        SELECT sport, league,
               COUNT(*) as bets,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               COALESCE(SUM(pnl),0) as pnl,
               COALESCE(AVG(edge_pct),0) as avg_edge
        FROM bets WHERE status IN ('live','paper','settled')
        GROUP BY sport, league ORDER BY bets DESC
    """)
    overall = query_db("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
               SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as pending,
               COALESCE(SUM(pnl),0) as total_pnl,
               COALESCE(SUM(size_usd),0) as volume,
               COALESCE(AVG(edge_pct),0) as avg_edge
        FROM bets WHERE status IN ('live','paper','settled')
    """)
    return jsonify({"by_sport": rows, "overall": overall[0] if overall else {}})

@app.route("/api/logs")
def api_logs():
    return jsonify({"log": tail_log(100)})

@app.route("/api/agent/logs")
def api_agent_logs():
    return jsonify({"log": tail_agent_log(100)})

@app.route("/api/agent/start", methods=["POST"])
def agent_start():
    global _agent_proc
    with _agent_lock:
        if agent_running():
            return jsonify({"ok": False, "msg": "Agent pipeline already running"})
        try:
            os.makedirs("agents", exist_ok=True)
            _agent_proc = subprocess.Popen(
                [sys.executable, "-m", "agents.runner"],
                stdout=open(AGENT_LOG, "a"),
                stderr=subprocess.STDOUT,
            )
            return jsonify({"ok": True, "pid": _agent_proc.pid})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/agent/stop", methods=["POST"])
def agent_stop():
    global _agent_proc
    with _agent_lock:
        if not agent_running():
            return jsonify({"ok": False, "msg": "Agent pipeline not running"})
        try:
            _agent_proc.terminate()
            _agent_proc.wait(timeout=5)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/scraper-picks")
def api_scraper_picks():
    """Read picks from scraper_data/ and return them for the dashboard."""
    import glob
    picks = []
    files = sorted(glob.glob("scraper_data/*.json"), key=os.path.getmtime, reverse=True)
    for fp in files[:5]:
        try:
            with open(fp) as f:
                data = json.load(f)
            for p in data.get("picks", []):
                p["_file"] = os.path.basename(fp)
                picks.append(p)
        except Exception:
            pass
    return jsonify(picks)

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    global _bot_proc
    with _bot_lock:
        if bot_running():
            return jsonify({"ok": False, "msg": "Bot already running"})
        try:
            _bot_proc = subprocess.Popen(
                [sys.executable, "sports_bot.py"],
                stdout=open(BOT_LOG, "a"),
                stderr=subprocess.STDOUT,
            )
            return jsonify({"ok": True, "pid": _bot_proc.pid})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/bot/start-live", methods=["POST"])
def bot_start_live():
    global _bot_proc
    with _bot_lock:
        if bot_running():
            return jsonify({"ok": False, "msg": "Bot already running"})
        try:
            _bot_proc = subprocess.Popen(
                [sys.executable, "sports_bot.py", "--live"],
                stdout=open(BOT_LOG, "a"),
                stderr=subprocess.STDOUT,
            )
            return jsonify({"ok": True, "pid": _bot_proc.pid, "mode": "LIVE"})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    global _bot_proc
    with _bot_lock:
        if not bot_running():
            return jsonify({"ok": False, "msg": "Bot not running"})
        try:
            _bot_proc.terminate()
            _bot_proc.wait(timeout=5)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "msg": str(e)})


# ─────────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sports Betting Bot</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface2: #1c2128;
    --border: #30363d;
    --accent: #00c853;
    --accent2: #1565c0;
    --red: #f44336;
    --yellow: #ffc107;
    --text: #e6edf3;
    --muted: #7d8590;
    --win: #00c853;
    --loss: #f44336;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .logo { font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: 1px; }
  .logo span { color: var(--text); font-weight: 300; }
  .header-right { display: flex; align-items: center; gap: 12px; }
  #mode-badge {
    padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 700;
    background: #1a3a1a; color: var(--accent); border: 1px solid var(--accent);
  }
  #mode-badge.live { background: #3a1a1a; color: var(--red); border-color: var(--red); }

  .btn {
    padding: 7px 16px; border-radius: 6px; font-size: 13px; font-weight: 600;
    border: none; cursor: pointer; transition: opacity 0.2s;
  }
  .btn:hover { opacity: 0.85; }
  .btn-green { background: var(--accent); color: #000; }
  .btn-red   { background: var(--red); color: #fff; }
  .btn-blue  { background: var(--accent2); color: #fff; }
  .btn-gray  { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }

  main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }

  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
    text-align: center;
  }
  .stat-card .val {
    font-size: 26px; font-weight: 700; line-height: 1.2;
  }
  .stat-card .lbl { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase; }
  .positive { color: var(--win); }
  .negative { color: var(--loss); }
  .neutral  { color: var(--text); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .panel-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    font-size: 13px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .panel-header .dot {
    width: 8px; height: 8px; border-radius: 50%; margin-right: 8px;
    display: inline-block;
  }
  .dot-green { background: var(--accent); }
  .dot-blue  { background: #4fc3f7; }
  .dot-yellow{ background: var(--yellow); }
  .dot-red   { background: var(--red); }

  table { width: 100%; border-collapse: collapse; }
  th { padding: 8px 12px; text-align: left; font-size: 11px; color: var(--muted); text-transform: uppercase; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid #1c2128; font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--surface2); }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 700;
  }
  .badge-win  { background: #0d2d0d; color: var(--win); }
  .badge-loss { background: #2d0d0d; color: var(--loss); }
  .badge-open { background: #0d1a2d; color: #4fc3f7; }
  .badge-paper{ background: #1a1a00; color: var(--yellow); }
  .badge-high { background: #0d2d0d; color: var(--accent); }
  .badge-medium { background: #1a1a00; color: var(--yellow); }

  #log-box {
    font-family: 'Consolas', 'Cascadia Code', monospace;
    font-size: 11px;
    background: #0a0c10;
    color: #c9d1d9;
    padding: 12px;
    height: 240px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }

  .scraper-pick {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .scraper-pick:last-child { border-bottom: none; }
  .pick-header { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .pick-detail { font-size: 12px; color: var(--muted); }
  .pick-prob   { font-size: 18px; font-weight: 700; }

  .sport-bar { display: flex; align-items: center; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--border); }
  .sport-bar:last-child { border-bottom: none; }
  .sport-name { width: 150px; }
  .bar-track  { flex: 1; height: 6px; background: var(--surface2); border-radius: 3px; }
  .bar-fill   { height: 100%; border-radius: 3px; background: var(--accent); transition: width 0.5s; }
  .bar-fill.loss { background: var(--loss); }
  .sport-pnl  { width: 80px; text-align: right; font-weight: 700; }

  .no-data { padding: 32px; text-align: center; color: var(--muted); font-size: 13px; }

  .tabs { display: flex; gap: 2px; padding: 0 16px; border-bottom: 1px solid var(--border); }
  .tab { padding: 10px 14px; cursor: pointer; font-size: 12px; color: var(--muted); border-bottom: 2px solid transparent; transition: all 0.2s; }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .refresh-indicator { font-size: 11px; color: var(--muted); }
  #last-refresh { color: var(--muted); }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:16px">
    <div class="logo">SPORTS<span>BOT</span></div>
    <div id="bot-status-dot" style="width:10px;height:10px;border-radius:50%;background:#555"></div>
    <span id="bot-status-text" style="font-size:12px;color:var(--muted)">Checking...</span>
  </div>
  <div class="header-right">
    <span id="mode-badge">PAPER</span>
    <span class="refresh-indicator">Auto-refresh: <span id="last-refresh">—</span></span>
    <button class="btn btn-green" id="btn-start" onclick="startBot()">Start (Paper)</button>
    <button class="btn btn-blue"  id="btn-start-live" onclick="startLive()">Start (Live)</button>
    <button class="btn btn-red"   id="btn-stop" onclick="stopBot()" style="display:none">Stop Bot</button>
    <div style="border-left:1px solid #333;margin-left:4px;padding-left:12px;display:flex;align-items:center;gap:8px">
      <div id="agent-status-dot" style="width:10px;height:10px;border-radius:50%;background:#555"></div>
      <span id="agent-status-text" style="font-size:12px;color:var(--muted)">Agent: Stopped</span>
      <button class="btn btn-green" id="btn-agent-start" onclick="startAgent()">Start Agents</button>
      <button class="btn btn-red"   id="btn-agent-stop" onclick="stopAgent()" style="display:none">Stop Agents</button>
    </div>
  </div>
</header>

<main>

  <!-- Stats row -->
  <div class="stats-row" id="stats-row">
    <div class="stat-card"><div class="val neutral" id="s-total">—</div><div class="lbl">Total Bets</div></div>
    <div class="stat-card"><div class="val positive" id="s-wins">—</div><div class="lbl">Wins</div></div>
    <div class="stat-card"><div class="val negative" id="s-losses">—</div><div class="lbl">Losses</div></div>
    <div class="stat-card"><div class="val" id="s-wr">—</div><div class="lbl">Win Rate</div></div>
    <div class="stat-card"><div class="val" id="s-pnl">—</div><div class="lbl">Total P&L</div></div>
    <div class="stat-card"><div class="val" id="s-roi">—</div><div class="lbl">ROI</div></div>
    <div class="stat-card"><div class="val neutral" id="s-open">—</div><div class="lbl">Open Bets</div></div>
    <div class="stat-card"><div class="val neutral" id="s-picks">—</div><div class="lbl">Scraper Picks</div></div>
  </div>

  <!-- Top row: Open bets + Scraper picks -->
  <div class="grid-2">

    <!-- Open Positions -->
    <div class="panel">
      <div class="panel-header">
        <div><span class="dot dot-blue"></span>Open Positions</div>
        <span id="open-count" style="color:var(--text)">0</span>
      </div>
      <div id="open-bets-body">
        <div class="no-data">No open positions</div>
      </div>
    </div>

    <!-- Scraper Picks -->
    <div class="panel">
      <div class="panel-header">
        <div><span class="dot dot-yellow"></span>Scraper Picks</div>
        <span style="font-size:11px;color:var(--muted)">from scraper_data/</span>
      </div>
      <div id="scraper-picks-body">
        <div class="no-data">No picks yet — drop JSON files in scraper_data/</div>
      </div>
    </div>

  </div>

  <!-- Performance by sport -->
  <div class="panel" style="margin-bottom:20px">
    <div class="panel-header"><span class="dot dot-green"></span>Performance by Sport</div>
    <div id="sport-perf-body">
      <div class="no-data">No settled bets yet</div>
    </div>
  </div>

  <!-- Bet history + logs -->
  <div class="grid-2">

    <!-- Recent Bets -->
    <div class="panel">
      <div class="panel-header"><span class="dot dot-green"></span>Recent Bets</div>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Sport</th>
              <th>Selection</th>
              <th>Price</th>
              <th>Edge</th>
              <th>Size</th>
              <th>Result</th>
              <th>P&L</th>
            </tr>
          </thead>
          <tbody id="bets-tbody">
            <tr><td colspan="8" class="no-data">No bets yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Logs -->
    <div class="panel">
      <div class="panel-header">
        <span><span class="dot dot-red"></span>Bot Log</span>
        <button class="btn btn-gray" style="font-size:11px;padding:3px 10px" onclick="refreshLog()">Refresh</button>
      </div>
      <div id="log-box">Loading...</div>
    </div>

  </div>

  <!-- Agent Pipeline Logs -->
  <div class="panel" style="margin-top:20px">
    <div class="panel-header">
      <span><span class="dot dot-blue"></span>Agent Pipeline Log</span>
      <button class="btn btn-gray" style="font-size:11px;padding:3px 10px" onclick="refreshAgentLog()">Refresh</button>
    </div>
    <div id="agent-log-box" style="font-family:'Consolas','Cascadia Code',monospace;font-size:11px;background:#0a0c10;color:#c9d1d9;padding:12px;height:240px;overflow-y:auto;white-space:pre-wrap;word-break:break-all">Loading...</div>
  </div>

</main>

<script>
let refreshTimer = null;

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    return await r.json();
  } catch(e) {
    return null;
  }
}

function fmt(n, decimals=2) {
  if (n === null || n === undefined) return '—';
  return parseFloat(n).toFixed(decimals);
}

function pnlClass(v) {
  if (v > 0) return 'positive';
  if (v < 0) return 'negative';
  return 'neutral';
}

function statusBadge(status, outcome) {
  if (outcome === 'WIN')  return '<span class="badge badge-win">WIN</span>';
  if (outcome === 'LOSS') return '<span class="badge badge-loss">LOSS</span>';
  if (status === 'paper') return '<span class="badge badge-paper">PAPER</span>';
  if (status === 'live')  return '<span class="badge badge-open">OPEN</span>';
  return `<span class="badge" style="background:#1c2128;color:#7d8590">${status}</span>`;
}

function sportLabel(sport) {
  const m = {
    'americanfootball_nfl': 'NFL',
    'basketball_nba': 'NBA',
    'soccer_epl': 'EPL',
    'soccer_uefa_champs_league': 'UCL',
    'soccer_usa_mls': 'MLS',
    'mma_mixed_martial_arts': 'MMA',
  };
  return m[sport] || sport || '—';
}

async function refresh() {
  const status = await fetchJSON('/api/status');
  const perf   = await fetchJSON('/api/performance');
  const bets   = await fetchJSON('/api/bets');
  const open   = await fetchJSON('/api/open');
  const picks  = await fetchJSON('/api/scraper-picks');

  if (status) updateHeader(status);
  if (perf) updateStats(perf, open, picks);
  if (bets) updateBetsTable(bets);
  if (open) updateOpenBets(open);
  if (picks) updateScraperPicks(picks);
  if (perf) updateSportPerf(perf.by_sport || []);

  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
}

function updateHeader(status) {
  const running = status.bot_running;
  const dot = document.getElementById('bot-status-dot');
  const txt = document.getElementById('bot-status-text');
  const modeBadge = document.getElementById('mode-badge');
  const btnStart = document.getElementById('btn-start');
  const btnStartLive = document.getElementById('btn-start-live');
  const btnStop = document.getElementById('btn-stop');

  dot.style.background = running ? '#00c853' : '#555';
  txt.textContent = running ? 'Running' : 'Stopped';

  if (running) {
    btnStart.style.display = 'none';
    btnStartLive.style.display = 'none';
    btnStop.style.display = '';
  } else {
    btnStart.style.display = '';
    btnStartLive.style.display = '';
    btnStop.style.display = 'none';
  }

  const agentRunning = status.agent_running;
  const agentDot = document.getElementById('agent-status-dot');
  const agentTxt = document.getElementById('agent-status-text');
  const btnAgentStart = document.getElementById('btn-agent-start');
  const btnAgentStop  = document.getElementById('btn-agent-stop');

  agentDot.style.background = agentRunning ? '#00c853' : '#555';
  agentTxt.textContent = agentRunning ? 'Agent: Running' : 'Agent: Stopped';
  btnAgentStart.style.display = agentRunning ? 'none' : '';
  btnAgentStop.style.display  = agentRunning ? '' : 'none';
}

function updateStats(perf, open, picks) {
  const o = perf.overall || {};
  const wins = o.wins || 0;
  const losses = o.losses || 0;
  const total = o.total || 0;
  const wr = total > 0 ? ((wins / (wins + losses || 1)) * 100).toFixed(1) : '—';
  const pnl = o.total_pnl || 0;
  const vol = o.volume || 0;
  const roi = vol > 0 ? (pnl / vol * 100).toFixed(1) : '—';

  document.getElementById('s-total').textContent = total;
  document.getElementById('s-wins').textContent = wins;
  document.getElementById('s-losses').textContent = losses;
  document.getElementById('s-wr').textContent = wr !== '—' ? wr + '%' : '—';
  const pnlEl = document.getElementById('s-pnl');
  pnlEl.textContent = `$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`;
  pnlEl.className = 'val ' + pnlClass(pnl);
  const roiEl = document.getElementById('s-roi');
  roiEl.textContent = roi !== '—' ? `${roi >= 0 ? '+' : ''}${roi}%` : '—';
  roiEl.className = 'val ' + pnlClass(parseFloat(roi) || 0);
  document.getElementById('s-open').textContent = (open || []).length;
  document.getElementById('s-picks').textContent = (picks || []).length;
}

function updateBetsTable(bets) {
  const tbody = document.getElementById('bets-tbody');
  if (!bets || bets.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="no-data">No bets yet</td></tr>';
    return;
  }
  tbody.innerHTML = bets.slice(0, 20).map(b => {
    const ts = b.timestamp ? b.timestamp.slice(11, 16) + ' UTC' : '—';
    const sport = sportLabel(b.sport);
    const pnl = b.pnl || 0;
    const pnlStr = b.resolved ? `<span class="${pnlClass(pnl)}">$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</span>` : '—';
    return `<tr>
      <td style="color:var(--muted)">${ts}</td>
      <td>${sport}</td>
      <td>${b.selection || '—'}</td>
      <td>${fmt(b.polymarket_price, 3)}</td>
      <td>${fmt(b.edge_pct, 1)}%</td>
      <td>$${fmt(b.size_usd, 2)}</td>
      <td>${statusBadge(b.status, b.outcome)}</td>
      <td>${pnlStr}</td>
    </tr>`;
  }).join('');
}

function updateOpenBets(open) {
  document.getElementById('open-count').textContent = (open || []).length;
  const el = document.getElementById('open-bets-body');
  if (!open || open.length === 0) {
    el.innerHTML = '<div class="no-data">No open positions</div>';
    return;
  }
  el.innerHTML = `<div style="overflow-x:auto"><table>
    <thead><tr><th>League</th><th>Selection</th><th>Entry</th><th>Size</th><th>Edge</th><th>Event</th></tr></thead>
    <tbody>` + open.map(b => {
      const start = b.event_start ? b.event_start.slice(0, 16).replace('T', ' ') : '—';
      return `<tr>
        <td>${b.league || sportLabel(b.sport)}</td>
        <td><strong>${b.selection}</strong></td>
        <td>${fmt(b.polymarket_price, 3)}</td>
        <td>$${fmt(b.size_usd, 2)}</td>
        <td class="positive">${fmt(b.edge_pct, 1)}%</td>
        <td style="color:var(--muted);font-size:12px">${start}</td>
      </tr>`;
    }).join('') + '</tbody></table></div>';
}

function updateScraperPicks(picks) {
  const el = document.getElementById('scraper-picks-body');
  if (!picks || picks.length === 0) {
    el.innerHTML = '<div class="no-data">No picks yet — drop JSON files in scraper_data/</div>';
    return;
  }
  el.innerHTML = picks.slice(0, 8).map(p => {
    const prob = p.model_probability ? (p.model_probability * 100).toFixed(1) : '?';
    const conf = p.confidence || 'medium';
    const start = p.event_start ? p.event_start.slice(0, 16).replace('T', ' ') : '—';
    return `<div class="scraper-pick">
      <div class="pick-header">
        <span class="badge badge-${conf}">${conf.toUpperCase()}</span>
        <span>${p.league || '—'}</span>
        <span style="color:var(--muted)">·</span>
        <span>${p.home_team} vs ${p.away_team}</span>
      </div>
      <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
        <div class="pick-prob positive">${prob}%</div>
        <div>
          <div>Backing: <strong>${p.selection}</strong></div>
          <div class="pick-detail">${start} UTC · ${p.market_type || 'moneyline'}</div>
        </div>
      </div>
      ${p.notes ? `<div class="pick-detail" style="margin-top:4px;font-style:italic">${p.notes}</div>` : ''}
    </div>`;
  }).join('');
}

function updateSportPerf(sports) {
  const el = document.getElementById('sport-perf-body');
  if (!sports || sports.length === 0) {
    el.innerHTML = '<div class="no-data">No settled bets yet</div>';
    return;
  }
  const maxPnl = Math.max(...sports.map(s => Math.abs(s.pnl || 0)), 1);
  el.innerHTML = sports.map(s => {
    const pnl = s.pnl || 0;
    const wins = s.wins || 0;
    const losses = s.losses || 0;
    const resolved = wins + losses;
    const wr = resolved > 0 ? ((wins / resolved) * 100).toFixed(0) + '%' : '—';
    const barPct = Math.min(100, (Math.abs(pnl) / maxPnl) * 100);
    const barClass = pnl >= 0 ? '' : 'loss';
    return `<div class="sport-bar">
      <div class="sport-name">${sportLabel(s.sport)} <span style="color:var(--muted);font-size:11px">${wr}</span></div>
      <div class="bar-track"><div class="bar-fill ${barClass}" style="width:${barPct}%"></div></div>
      <div style="color:var(--muted);font-size:12px;width:80px;text-align:center">${s.bets || 0} bets</div>
      <div class="sport-pnl ${pnlClass(pnl)}">$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</div>
    </div>`;
  }).join('');
}

async function refreshLog() {
  const data = await fetchJSON('/api/logs');
  if (data) {
    const box = document.getElementById('log-box');
    box.textContent = data.log;
    box.scrollTop = box.scrollHeight;
  }
}

async function refreshAgentLog() {
  const data = await fetchJSON('/api/agent/logs');
  if (data) {
    const box = document.getElementById('agent-log-box');
    box.textContent = data.log;
    box.scrollTop = box.scrollHeight;
  }
}

async function startAgent() {
  await fetch('/api/agent/start', {method:'POST'});
  await refresh();
  await refreshAgentLog();
}

async function stopAgent() {
  await fetch('/api/agent/stop', {method:'POST'});
  await refresh();
}

async function startBot() {
  await fetch('/api/bot/start', {method:'POST'});
  await refresh();
}

async function startLive() {
  if (!confirm('Start LIVE trading with REAL MONEY?')) return;
  await fetch('/api/bot/start-live', {method:'POST'});
  document.getElementById('mode-badge').textContent = 'LIVE';
  document.getElementById('mode-badge').classList.add('live');
  await refresh();
}

async function stopBot() {
  await fetch('/api/bot/stop', {method:'POST'});
  await refresh();
}

// Initial load
refresh();
refreshLog();
refreshAgentLog();

// Auto-refresh every 30 seconds
setInterval(() => { refresh(); refreshLog(); refreshAgentLog(); }, 30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    print("Sports Betting Dashboard")
    print("  Open: http://localhost:8050")
    print("  Bot logs: sports_bot.log")
    print("  DB: sports_trades.db")
    print("  Scraper: drop JSON files in scraper_data/")
    app.run(host="0.0.0.0", port=8050, debug=False)
