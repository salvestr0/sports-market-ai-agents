"""
Self-Review Agent — runs every 4 hours.

Reads the last 4 hours of agent reports + sports_trades.db outcomes,
uses Claude Haiku to identify patterns and write a structured review,
then updates agents/self-review.md and agents/memory.md.

This gives the pipeline a genuine feedback loop: what's being blocked,
what's winning, what should change — written in the agents' own voice.
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger("agents.self_review")

REPORTS_DIR   = Path("agents/reports")
SELF_REVIEW_PATH = Path("agents/self-review.md")
MEMORY_PATH   = Path("agents/memory.md")
MODEL         = "claude-haiku-4-5-20251001"   # cheap — just summarising existing data


# ─── Data collectors ──────────────────────────────────────────────────────────

def _read_recent_reports(hours: int = 4) -> list[dict]:
    """Return all agent JSON reports from the last `hours` hours."""
    if not REPORTS_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    reports = []

    for f in sorted(REPORTS_DIR.glob("*.json"), reverse=True)[:40]:
        try:
            # Filename format: max_20260220_143022.json
            ts_str = "_".join(f.stem.split("_")[1:])  # "20260220_143022"
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            if ts < cutoff:
                break
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_filename"] = f.name
            reports.append(data)
        except Exception:
            continue

    return reports


def _read_recent_trades(hours: int = 48) -> list[dict]:
    """Read recent resolved trades from sports_trades.db."""
    db_path = Path("sports_trades.db")
    if not db_path.exists():
        return []
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        c.execute(
            "SELECT * FROM trades WHERE created_at >= ? ORDER BY created_at DESC LIMIT 50",
            (cutoff,),
        )
        trades = [dict(r) for r in c.fetchall()]
        conn.close()
        return trades
    except Exception as e:
        logger.warning(f"[SelfReview] Could not read trades: {e}")
        return []


def _read_previous_review() -> str:
    """Return the last 1500 chars of self-review.md for continuity."""
    if not SELF_REVIEW_PATH.exists():
        return "No previous review."
    text = SELF_REVIEW_PATH.read_text(encoding="utf-8")
    return text[-1500:] if len(text) > 1500 else text


# ─── Summarise data for the LLM ───────────────────────────────────────────────

def _build_review_context(reports: list[dict], trades: list[dict]) -> str:
    """Compress reports + trades into a concise text block for the LLM."""
    lines = []

    # Batch outcomes
    batches = {}
    for r in reports:
        fname  = r.get("_filename", "")
        agent  = r.get("agent", fname.split("_")[0] if fname else "?")
        ts     = "_".join(fname.split("_")[1:]).replace(".json", "") if fname else "?"
        key    = ts

        if key not in batches:
            batches[key] = {}
        batches[key][agent] = r

    for ts, agents in sorted(batches.items(), reverse=True)[:6]:
        lines.append(f"\n--- Batch {ts} ---")

        max_r  = agents.get("max", {})
        nova_r = agents.get("nova", {})
        lumi_r = agents.get("lumi", {})

        candidates = max_r.get("candidates", [])
        analyses   = nova_r.get("analyses", [])
        assessments = lumi_r.get("assessments", [])

        lines.append(f"Max researched: {len(candidates)} events")

        for a in analyses:
            lines.append(
                f"  Nova | {a.get('event_id','?')} | {a.get('nova_verdict','?')} | "
                f"edge={a.get('edge',{}).get('edge_pct',0):.1f}% | "
                f"PM vol=${a.get('polymarket',{}).get('volume',0):,.0f}"
            )

        for a in assessments:
            lines.append(
                f"  Lumi | {a.get('event_id','?')} | {a.get('lumi_verdict','?')}"
                + (f" — {a.get('skip_reason','')}" if a.get("skip_reason") else "")
            )

    # Recent trades
    if trades:
        lines.append("\n--- Recent Bets ---")
        won  = [t for t in trades if t.get("status") == "won"]
        lost = [t for t in trades if t.get("status") == "lost"]
        open_t = [t for t in trades if t.get("status") == "open"]
        lines.append(f"Won: {len(won)} | Lost: {len(lost)} | Open: {len(open_t)}")
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades)
        lines.append(f"Total P&L (sample): {total_pnl:+.2f}")
        for t in trades[:10]:
            lines.append(
                f"  {t.get('status','?')} | {t.get('selection','?')} | "
                f"pnl={t.get('pnl',0):+.2f} | sport={t.get('sport','?')}"
            )
    else:
        lines.append("\n--- No bets placed yet ---")

    return "\n".join(lines)


# ─── Write outputs ────────────────────────────────────────────────────────────

def _append_to_review(review_text: str, ts: str):
    """Append a new review section to agents/self-review.md."""
    SELF_REVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)

    header = f"\n\n---\n\n## Self-Review — {ts}\n\n"
    entry  = header + review_text.strip() + "\n"

    if SELF_REVIEW_PATH.exists():
        existing = SELF_REVIEW_PATH.read_text(encoding="utf-8")
        SELF_REVIEW_PATH.write_text(existing + entry, encoding="utf-8")
    else:
        SELF_REVIEW_PATH.write_text(
            "# Agent Self-Review Log\n\n"
            "*Written automatically every 4 hours by the pipeline.*\n"
            + entry,
            encoding="utf-8",
        )
    logger.info(f"[SelfReview] Appended review to {SELF_REVIEW_PATH}")


def _update_memory_patterns(patterns_text: str, ts: str):
    """Replace the Patterns section in memory.md with the latest findings."""
    if not MEMORY_PATH.exists():
        return

    text    = MEMORY_PATH.read_text(encoding="utf-8")
    marker  = "## Patterns & Learnings\n"
    end_mrk = "\n---"
    start   = text.find(marker)
    if start < 0:
        return

    section_end = text.find(end_mrk, start + len(marker))
    if section_end < 0:
        section_end = len(text)

    new_section = (
        marker
        + f"\n*(Last updated: {ts})*\n\n"
        + patterns_text.strip()
        + "\n"
    )
    text = text[:start] + new_section + text[section_end:]
    MEMORY_PATH.write_text(text, encoding="utf-8")
    logger.info("[SelfReview] Updated Patterns section in agents/memory.md")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(hours_back: int = 4):
    """
    Run a self-review of the last `hours_back` hours of pipeline activity.
    Appends findings to agents/self-review.md and updates agents/memory.md.
    """
    try:
        import anthropic
    except ImportError:
        logger.error("[SelfReview] anthropic package not installed")
        return

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("[SelfReview] ANTHROPIC_API_KEY not set")
        return

    now_ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.info(f"[SelfReview] Starting 4-hour self-review at {now_ts}")

    reports = _read_recent_reports(hours=hours_back)
    trades  = _read_recent_trades(hours=48)
    context = _build_review_context(reports, trades)
    prev    = _read_previous_review()

    if not reports and not trades:
        logger.info("[SelfReview] No data to review yet — skipping")
        return

    prompt = f"""You are the collective voice of a 4-agent sports betting pipeline conducting a self-review.

Review this pipeline activity data from the last {hours_back} hours:

{context}

Previous review (tail):
{prev}

Write a structured self-review with these sections:

**What happened this period**
- Which events were researched, which passed/failed each filter

**Dominant failure modes**
- Most common reason events were not approved (no market, no sharp odds, edge too low, Lumi ABORT)
- If this is consistently the same reason, flag it prominently

**Patterns observed**
- Any recurring patterns in what gets approved vs rejected
- Any sports/teams that always have data gaps
- If bets were placed: is P&L trending right?

**What should change**
- ONE concrete, specific suggestion for the next cycle
- Only suggest a change if you have seen it fail 3+ times — not from a single data point

**What to watch next**
- A specific thing to track in the next 4 hours

Keep the whole review under 350 words. Be specific — use the actual event IDs, team names, and numbers from the data. Do not be generic."""

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        review_text = response.content[0].text if response.content else ""
    except Exception as e:
        logger.error(f"[SelfReview] Claude call failed: {e}")
        return

    if not review_text:
        logger.warning("[SelfReview] Empty response from Claude")
        return

    _append_to_review(review_text, now_ts)

    # Extract the "Patterns observed" section to write back to memory.md
    patterns_start = review_text.lower().find("**patterns observed**")
    next_section   = review_text.lower().find("**what should change**")
    if patterns_start >= 0 and next_section > patterns_start:
        patterns_block = review_text[patterns_start:next_section].strip()
        _update_memory_patterns(patterns_block, now_ts)

    logger.info("[SelfReview] Done")
