"""
Two-way Telegram interface for the 4-agent sports betting pipeline.

The listener runs as a background thread inside runner.py.
It polls Telegram for incoming messages and routes them:

  /run      — Trigger a pipeline batch immediately
  /status   — Show last batch results + picks approved
  /reports  — Summarise the latest agent reports in detail
  /help     — List available commands
  <text>    — Ask a free-text question; Claude answers as the agent team

The listener only responds to messages from the configured CHAT_ID
(same as the chat we send notifications to), so strangers can't
trigger your pipeline.

Usage (background thread, started by runner.py):
    from agents import telegram_listener
    telegram_listener.start_background(run_callback=run_batch)

Standalone test:
    python -m agents.telegram_listener
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("agents.tg_listener")

# Reuse same credentials as notifier.py
BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "8552387840:AAF_zMGo4UYoJR4uHIvUrLg_WAoc_iP4CTw")
CHAT_ID   = os.getenv("TG_CHAT_ID",   "403403270")

REPORTS_DIR  = Path("agents/reports")
SCRAPER_DIR  = Path("scraper_data")

# Shared state
batch_lock      = threading.Lock()   # PUBLIC — runner.py acquires this for scheduled batches too
_run_callback   = None               # set by runner.py — calls run_batch()
_last_batch_ts  = None               # ISO timestamp of last completed batch
_last_picks     = []                 # picks from last batch


# ─── Telegram API helpers ─────────────────────────────────────────────────────

def _api(method: str, **kwargs) -> dict:
    """Call a Telegram Bot API method. Returns the response JSON."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=35)
        return r.json()
    except Exception as e:
        logger.warning(f"[TG Listener] API error ({method}): {e}")
        return {}


def _send(text: str):
    """Send an HTML-formatted reply to the configured chat."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.debug("[TG Listener] No credentials")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if not r.ok:
            logger.warning(f"[TG Listener] Send failed: {r.status_code}")
    except Exception as e:
        logger.warning(f"[TG Listener] Send error: {e}")


def _get_updates(offset=None):
    """Long-poll for new updates. Returns list of update dicts, or None on error."""
    params = {"timeout": 25, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        logger.debug(f"[TG Listener] getUpdates error: {e}")
        return None


# ─── Report helpers ────────────────────────────────────────────────────────────

def _latest_report(prefix: str):
    """Load the most recent report file matching prefix (max_*, nova_*, etc.)."""
    pattern = list(REPORTS_DIR.glob(f"{prefix}_*.json"))
    if not pattern:
        return None
    latest = max(pattern, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest_sage():
    """Load the most recent Sage output from scraper_data/."""
    pattern = list(SCRAPER_DIR.glob("agents_*.json"))
    if not pattern:
        return None
    latest = max(pattern, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None


def _build_context_summary() -> str:
    """Build a concise text summary of the latest agent reports for Claude."""
    parts = []

    max_r = _latest_report("max")
    if max_r:
        candidates = max_r.get("candidates", [])
        lines = [f"Max researched {len(candidates)} event(s):"]
        for c in candidates:
            lines.append(
                f"  - {c.get('league','?')}: {c.get('home_team','?')} vs {c.get('away_team','?')}"
                f" | {c.get('max_verdict','?')} [{c.get('confidence','?')}]"
                f"\n    Thesis: {c.get('research',{}).get('edge_thesis','')[:200]}"
            )
        parts.append("\n".join(lines))

    nova_r = _latest_report("nova")
    if nova_r:
        analyses = nova_r.get("analyses", [])
        lines = [f"Nova analysed {len(analyses)} event(s):"]
        for a in analyses:
            edge = a.get("edge") or {}
            lines.append(
                f"  - {a.get('event_id','?')[:40]} | {a.get('nova_verdict','?')}"
                + (f" | edge {edge.get('edge_pct',0):.1f}% backing {edge.get('selection','?')}" if a.get("nova_verdict") == "VALUE" else "")
            )
        parts.append("\n".join(lines))

    lumi_r = _latest_report("lumi")
    if lumi_r:
        assessments = lumi_r.get("assessments", [])
        lines = [f"Lumi assessed {len(assessments)} event(s):"]
        for a in assessments:
            lines.append(
                f"  - {a.get('event_id','?')[:40]} | {a.get('lumi_verdict','?')}"
                + (f" — {a.get('skip_reason','')}" if a.get("lumi_verdict") == "ABORT" else "")
            )
        parts.append("\n".join(lines))

    sage_r = _latest_sage()
    if sage_r:
        picks = sage_r.get("picks", [])
        if picks:
            lines = [f"Sage approved {len(picks)} bet(s):"]
            for p in picks:
                lines.append(
                    f"  - {p.get('league','?')}: {p.get('selection','?')}"
                    f" | prob={p.get('model_probability','?')} conf={p.get('confidence','?')}"
                    f"\n    {p.get('notes','')[:200]}"
                )
        else:
            lines = ["Sage approved 0 bets this run."]
        parts.append("\n".join(lines))

    if not parts:
        return "No reports available yet — the pipeline hasn't run."

    ts = _last_batch_ts or "unknown"
    return f"[Last batch: {ts}]\n\n" + "\n\n".join(parts)


# ─── Command handlers ─────────────────────────────────────────────────────────

def _handle_help():
    _send(
        "🤖 <b>Agent Pipeline Commands</b>\n\n"
        "/run — Trigger a full pipeline batch now\n"
        "/status — Quick summary of last batch\n"
        "/reports — Detailed latest agent reports\n"
        "/help — This message\n\n"
        "<i>Or just type any question and the agent team will answer.</i>"
    )


def _handle_status():
    sage_r = _latest_sage()
    if not sage_r:
        _send("📭 No batch has run yet. Use /run to start one.")
        return

    picks = sage_r.get("picks", [])
    ts = sage_r.get("generated_at", "?")[:19].replace("T", " ")

    if picks:
        lines = [f"📊 <b>Last batch</b> ({ts} UTC) — {len(picks)} bet(s) approved:"]
        for p in picks:
            prob = float(p.get("model_probability", 0))
            lines.append(
                f"\n  🎯 <b>{p.get('league','?')}</b> — <b>{p.get('selection','?')}</b>\n"
                f"     Prob: {prob:.0%} | Conf: {p.get('confidence','?')}\n"
                f"     <i>{p.get('notes','')[:120]}</i>"
            )
    else:
        lines = [f"📊 <b>Last batch</b> ({ts} UTC) — No bets approved."]

    _send("\n".join(lines))


def _handle_reports():
    summary = _build_context_summary()
    # Telegram has a 4096-char limit per message
    if len(summary) > 3800:
        summary = summary[:3800] + "\n... (truncated)"
    _send(f"<pre>{summary}</pre>")


def _handle_run_command():
    """Run a batch. Called in its own thread to avoid blocking the poll loop."""
    global _last_batch_ts, _last_picks
    if _run_callback is None:
        _send("⚠️ No run callback registered. Start runner.py first.")
        return

    # Non-blocking acquire — if locked, a batch is already running
    acquired = batch_lock.acquire(blocking=False)
    if not acquired:
        _send("⏳ A batch is already running. Please wait for it to finish.")
        return

    try:
        _send("🚀 <b>Triggering pipeline batch now...</b>\nThis takes a few minutes — I'll notify you when done.")
        result = _run_callback()
        _last_batch_ts = datetime.now(timezone.utc).isoformat()
        _last_picks = result.get("picks", []) if result else []
    except Exception as e:
        logger.error(f"[TG Listener] Batch triggered via Telegram failed: {e}", exc_info=True)
        _send(f"🚨 Batch failed: <code>{str(e)[:200]}</code>")
    finally:
        batch_lock.release()


_COMPLEX_KEYWORDS = {
    "why", "should", "strategy", "recommend", "analyse", "analyze",
    "compare", "think", "opinion", "improve", "change", "explain",
    "decide", "reason", "consider", "evaluate", "assess", "suggest",
    "advice", "better", "worse", "best", "optimal", "review",
}

def _pick_model(text: str) -> str:
    """Haiku for simple lookups, Sonnet for reasoning/analysis."""
    words = set(text.lower().split())
    if len(text) > 120 or words & _COMPLEX_KEYWORDS:
        return "claude-sonnet-4-6"
    return "claude-haiku-4-5-20251001"


def _handle_question(text: str):
    """Answer a free-text question using Claude with full pipeline context."""
    try:
        import anthropic
    except ImportError:
        _send("⚠️ anthropic package not installed. Can't answer questions.")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        _send("⚠️ ANTHROPIC_API_KEY not set. Can't answer questions.")
        return

    _send("🤔 <i>Thinking...</i>")

    context = _build_context_summary()

    system = """You are the collective voice of four AI sports betting agents: Max (researcher), Nova (odds analyst), Lumi (risk assessor), and Sage (portfolio manager). You are speaking directly to your owner via Telegram.

Answer their questions naturally, drawing on the pipeline context provided. Be concise (2-5 sentences max unless detail is genuinely needed). Be specific — use real numbers from the reports. Don't be generic.

If they ask about a specific agent, answer from that agent's perspective. If they ask about strategy, decisions, or improvements, give an honest and analytical response.

Keep responses under 400 words. Format using plain text + HTML bold (<b>word</b>) for key terms. No markdown."""

    user_prompt = f"""Pipeline context (latest batch results):
{context}

Owner's question: {text}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_pick_model(text),
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        answer = response.content[0].text if response.content else "Sorry, I couldn't generate a response."
        _send(f"💬 {answer}")
    except Exception as e:
        logger.warning(f"[TG Listener] Question handler failed: {e}")
        _send(f"⚠️ Couldn't answer: <code>{str(e)[:150]}</code>")


def _handle_nova_question(question: str):
    """Route a question directly to Nova's live odds engine."""
    _send("🔍 <b>Nova:</b> <i>Fetching live market data...</i>")
    try:
        from agents import nova_agent
        reply = nova_agent.chat(question)
        _send(f"<b>Nova:</b> {reply}")
    except Exception as e:
        logger.error(f"[TG Listener] Nova chat failed: {e}", exc_info=True)
        _send(f"❌ Nova error: <code>{str(e)[:150]}</code>")


def _handle_nova_question(question: str):
    """Route a direct question to Nova — fetches live odds and computes edges."""
    _send("🔍 <b>Nova:</b> <i>Fetching live market data...</i>")
    try:
        from agents import nova_agent
        reply = nova_agent.chat(question)
        _send(f"<b>Nova:</b> {reply}")
    except Exception as e:
        logger.error(f"[TG Listener] Nova chat failed: {e}", exc_info=True)
        _send(f"⚠️ Nova error: <code>{str(e)[:150]}</code>")


# ─── Routing ──────────────────────────────────────────────────────────────────

def _process_update(update: dict):
    """Route an incoming Telegram update to the right handler."""
    msg = update.get("message") or update.get("edited_message", {})
    if not msg:
        return

    # Only respond to the configured chat
    incoming_chat = str(msg.get("chat", {}).get("id", ""))
    if incoming_chat != str(CHAT_ID):
        logger.debug(f"[TG Listener] Ignoring message from unknown chat {incoming_chat}")
        return

    text = msg.get("text", "").strip()
    if not text:
        return

    text_lower = text.lower()
    cmd = text.split()[0].lower().lstrip("/")

    if cmd in ("run", "start", "go"):
        threading.Thread(target=_handle_run_command, daemon=True).start()
    elif cmd == "status":
        _handle_status()
    elif cmd in ("reports", "report"):
        _handle_reports()
    elif cmd in ("help", "start"):
        _handle_help()
    elif text_lower.startswith(("nova:", "nova,")):
        # Route directly to Nova's live odds engine
        question = text[5:].strip()
        threading.Thread(target=_handle_nova_question, args=(question,), daemon=True).start()
    elif text.lower().startswith(("nova:", "nova,")):
        question = text.split(":", 1)[-1].split(",", 1)[-1].strip() if (":" in text or "," in text) else text[4:].strip()
        threading.Thread(target=_handle_nova_question, args=(question,), daemon=True).start()
    else:
        # Free-text question — run in background thread so poll loop stays responsive
        threading.Thread(target=_handle_question, args=(text,), daemon=True).start()


# ─── Poll loop ────────────────────────────────────────────────────────────────

def poll_loop():
    """Main Telegram polling loop. Blocks forever. Run in a daemon thread."""
    if not BOT_TOKEN:
        logger.warning("[TG Listener] No BOT_TOKEN — listener disabled")
        return

    logger.info("[TG Listener] Polling started (long-poll, 25s timeout)")
    offset = None
    consecutive_errors = 0

    # On startup, send a quick hello so the owner knows the listener is live
    _send(
        "👂 <b>Agent pipeline is live and listening.</b>\n"
        "Type /help to see available commands or just ask me anything."
    )

    while True:
        updates = _get_updates(offset)
        if updates is None:
            # _get_updates returns [] on error (already logged); apply backoff
            consecutive_errors += 1
            backoff = min(60, 5 * consecutive_errors)  # 5s, 10s, 15s … capped at 60s
            if consecutive_errors <= 3 or consecutive_errors % 10 == 0:
                logger.debug(f"[TG Listener] Polling error #{consecutive_errors}, retrying in {backoff}s")
            time.sleep(backoff)
            continue

        consecutive_errors = 0  # reset on success
        for update in updates:
            offset = update.get("update_id", 0) + 1
            try:
                _process_update(update)
            except Exception as e:
                logger.error(f"[TG Listener] Error processing update: {e}", exc_info=True)


# ─── Public API ───────────────────────────────────────────────────────────────

def start_background(run_callback=None):
    """
    Start the Telegram listener as a background daemon thread.

    Args:
        run_callback: callable — called when user sends /run.
                      Should accept no args and return the sage_report dict.

    Returns:
        The Thread object (daemon, will die when main process exits).
    """
    global _run_callback
    if run_callback is not None:
        _run_callback = run_callback

    t = threading.Thread(target=poll_loop, name="TG-Listener", daemon=True)
    t.start()
    logger.info("[TG Listener] Background thread started")
    return t


# ─── Standalone test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Force UTF-8 on Windows
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    logger.info("Running TG listener standalone (Ctrl+C to stop)")
    poll_loop()
