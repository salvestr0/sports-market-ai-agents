"""
Shared trade resolver — imported by both runner.py and telegram_listener.py.

Kept separate to avoid the circular import that would arise if
telegram_listener.py imported runner.py (runner already imports telegram_listener).
"""

import os
import json
import sqlite3
import requests
import logging
from datetime import datetime, timezone

from . import tools

logger = logging.getLogger("agents.resolver")

_PAPER_FEE = 0.015   # 1.5% taker fee simulated in paper mode
_DB_PATH   = "sports_trades.db"


# ─── Polymarket resolution check ─────────────────────────────────────────────

def check_trade_resolution(slug: str, selection: str):
    """
    Query Polymarket Gamma API for a market by slug.
    Returns 'WIN', 'LOSS', or None if not yet resolved.
    """
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None

        m = data[0]
        if not m.get("resolved", False):
            return None

        prices_raw = m.get("outcomePrices", "")
        if isinstance(prices_raw, str):
            prices_raw = json.loads(prices_raw)
        prices = [float(p) for p in prices_raw] if prices_raw else []

        outcomes_raw = m.get("outcomes", "")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except Exception:
                outcomes = ["Yes", "No"]
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = ["Yes", "No"]

        for outcome, price in zip(outcomes, prices):
            if price >= 0.95:
                return "WIN" if tools._names_match(outcome, selection, 0.5) else "LOSS"

        return None  # resolved flag set but no clear winner yet
    except Exception as e:
        logger.debug(f"[RESOLVER] check failed for {slug!r}: {e}")
        return None


# ─── DB write helper ──────────────────────────────────────────────────────────

def _write_settlement(trade_id: int, entry_price: float, amount: float,
                      notes: str, outcome: str) -> float | None:
    """
    Write WIN/LOSS to the trades table. Returns pnl on success, None on failure.
    """
    is_paper = (notes or "").startswith("PAPER")
    fee      = amount * _PAPER_FEE if is_paper else 0.0
    pnl      = round(amount * (1.0 / entry_price - 1.0) - fee, 4) if outcome == "WIN" \
               else round(-amount - fee, 4)
    new_status = "won" if outcome == "WIN" else "lost"
    now_ts     = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "UPDATE trades SET status=?, pnl=?, resolved_at=? WHERE id=?",
            (new_status, pnl, now_ts, trade_id),
        )
        conn.commit()
        conn.close()
        return pnl
    except Exception as e:
        logger.warning(f"[RESOLVER] DB update failed for trade #{trade_id}: {e}")
        return None


# ─── Batch auto-resolver (Polymarket-driven) ─────────────────────────────────

def resolve_open_trades() -> list:
    """
    Poll Polymarket for every open trade and settle any that have resolved.
    Returns list of settled trade dicts.
    Called by runner.py at batch start and by telegram_listener /resolve command.
    """
    from . import notifier

    if not os.path.exists(_DB_PATH):
        return []

    try:
        conn = sqlite3.connect(_DB_PATH)
        rows = conn.execute(
            "SELECT id, selection, league, polymarket_slug, price, amount, notes "
            "FROM trades WHERE status='open' AND source='sage_agent'"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"[RESOLVER] Could not read open trades: {e}")
        return []

    if not rows:
        logger.info("[RESOLVER] No open trades to check")
        return []

    logger.info(f"[RESOLVER] Checking {len(rows)} open trade(s) against Polymarket...")
    settled = []

    for trade_id, selection, league, slug, entry_price, amount, notes in rows:
        if not slug:
            continue

        outcome = check_trade_resolution(slug, selection)
        if outcome is None:
            continue

        pnl = _write_settlement(trade_id, entry_price, amount, notes, outcome)
        if pnl is None:
            continue

        emoji = "+++" if outcome == "WIN" else "---"
        logger.info(
            f"  [{emoji}] {outcome} | {league} {selection} @ {entry_price:.3f} | "
            f"PnL: ${pnl:+.2f}"
        )
        settled.append({
            "id":          trade_id,
            "selection":   selection,
            "league":      league or "?",
            "outcome":     outcome,
            "entry_price": entry_price,
            "amount":      amount,
            "pnl":         pnl,
            "is_paper":    (notes or "").startswith("PAPER"),
        })

    if not settled:
        logger.info("[RESOLVER] No trades resolved this check")
        return []

    wins      = sum(1 for t in settled if t["outcome"] == "WIN")
    losses    = len(settled) - wins
    total_pnl = sum(t["pnl"] for t in settled)

    logger.info(
        f"[RESOLVER] Settled {len(settled)} — W:{wins} L:{losses} | PnL: ${total_pnl:+.2f}"
    )
    notifier.send(
        f"📊 <b>Bets resolved</b> — {wins}W / {losses}L | PnL: ${total_pnl:+.2f}\n"
        + "\n".join(
            f"  {'✅' if t['outcome'] == 'WIN' else '❌'} "
            f"{t['league']} {t['selection']} — ${t['pnl']:+.2f}"
            for t in settled
        )
    )

    for t in settled:
        try:
            from sports_bot import _reflect_on_outcome
            _reflect_on_outcome(t, t["outcome"], t["entry_price"])
        except Exception as e:
            logger.debug(f"[RESOLVER] Reflection skipped for {t['selection']}: {e}")

    return settled
