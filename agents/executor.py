"""
Sage Direct Executor — place Polymarket bets immediately when Sage approves picks.

Reads config from env vars each call (no module-level state).
Writes to the `trades` table in sports_trades.db (same table runner.py queries).
sports_bot.py uses a separate `bets` table — no schema conflict.
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timezone

import requests

from .tools import _names_match

logger = logging.getLogger("agents.executor")


# ─── Config ───────────────────────────────────────────────────────────────────

def _cfg() -> dict:
    return {
        "dry_run":  os.getenv("BOT_DRY_RUN", "true").lower() not in ("0", "false", "no"),
        "bankroll": float(os.getenv("SPORTS_BANKROLL", "200")),
        "max_bet":  float(os.getenv("SPORTS_MAX_BET",  "25")),
        "min_bet":  float(os.getenv("SPORTS_MIN_BET",  "2")),
        "kelly":    float(os.getenv("SPORTS_KELLY",    "0.25")),
        "pk":       os.getenv("POLYMARKET_PRIVATE_KEY", ""),
        "funder":   os.getenv("POLYMARKET_FUNDER", ""),
        "sig_type": int(os.getenv("POLYMARKET_SIG_TYPE", "2")),
        "db_path":  "sports_trades.db",
    }


# ─── DB ───────────────────────────────────────────────────────────────────────

def _ensure_trades_table(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id     TEXT,
            sport        TEXT,
            league       TEXT,
            home_team    TEXT,
            away_team    TEXT,
            selection    TEXT,
            market_type  TEXT,
            amount       REAL,
            shares       REAL,
            price        REAL,
            polymarket_slug TEXT,
            token_id     TEXT,
            status       TEXT DEFAULT 'open',
            pnl          REAL,
            created_at   TEXT,
            resolved_at  TEXT,
            notes        TEXT,
            source       TEXT
        );
    """)
    conn.commit()


def _record_trade(cfg, pick, token_id, price, size_usd, shares, order_id, status, reason="") -> int | None:
    """Insert a row into the trades table. Returns the new row id."""
    try:
        conn = sqlite3.connect(cfg["db_path"])
        _ensure_trades_table(conn)
        cur = conn.execute(
            """INSERT INTO trades
               (event_id, sport, league, home_team, away_team, selection,
                market_type, amount, shares, price, polymarket_slug, token_id,
                status, created_at, notes, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pick.get("event_id", ""),
                pick.get("sport", ""),
                pick.get("league", ""),
                pick.get("home_team", ""),
                pick.get("away_team", ""),
                pick.get("selection", ""),
                pick.get("market_type", "moneyline"),
                size_usd,
                shares,
                price,
                pick.get("polymarket_slug", ""),
                token_id or "",
                status,
                datetime.now(timezone.utc).isoformat(),
                reason or pick.get("notes", ""),
                "sage_agent",
            ),
        )
        db_id = cur.lastrowid
        conn.commit()
        conn.close()
        return db_id
    except Exception as e:
        logger.error(f"[Executor] DB write failed: {e}")
        return None


# ─── Duplicate guard ───────────────────────────────────────────────────────────

def _already_bet(cfg, pick: dict) -> bool:
    """
    Return True if we already have an open bet for this pick.

    Checks polymarket_slug + selection first (stable API-derived keys that are
    consistent across pipeline runs), then falls back to event_id + selection
    (LLM-generated, so less reliable but still useful as a secondary guard).
    """
    slug      = pick.get("polymarket_slug", "").strip()
    selection = pick.get("selection", "").strip()
    event_id  = pick.get("event_id", "").strip()
    try:
        conn = sqlite3.connect(cfg["db_path"])
        _ensure_trades_table(conn)

        # Primary: slug + selection (stable across runs — slug comes from Polymarket API)
        if slug and selection:
            row = conn.execute(
                "SELECT id FROM trades WHERE polymarket_slug=? AND selection=? "
                "AND status='open' AND source='sage_agent'",
                (slug, selection),
            ).fetchone()
            if row:
                conn.close()
                return True

        # Fallback: event_id + selection (LLM-generated, catches same-run dupes)
        if event_id and selection:
            row = conn.execute(
                "SELECT id FROM trades WHERE event_id=? AND selection=? "
                "AND status='open' AND source='sage_agent'",
                (event_id, selection),
            ).fetchone()
            if row:
                conn.close()
                return True

        conn.close()
        return False
    except Exception as e:
        logger.warning(f"[Executor] Duplicate check error: {e} — allowing bet")
        return False


# ─── Token resolution ──────────────────────────────────────────────────────────

def _resolve_token(slug: str, selection: str) -> tuple:
    """
    Query Gamma API by slug and match selection to an outcome.
    Returns (token_id, price, idx, outcomes) or (None, None, None, None).
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
            logger.warning(f"[Executor] No market returned for slug={slug!r}")
            return None, None, None, None

        m = data[0]
        token_ids = json.loads(m.get("clobTokenIds", "[]") or "[]")
        outcomes  = json.loads(m.get("outcomes", "[]")     or "[]")
        prices    = [float(p) for p in json.loads(m.get("outcomePrices", "[]") or "[]")]

        for idx, outcome in enumerate(outcomes):
            if _names_match(outcome, selection):
                if idx < len(token_ids) and idx < len(prices):
                    return str(token_ids[idx]), prices[idx], idx, outcomes

        logger.warning(
            f"[Executor] Selection {selection!r} not found in outcomes {outcomes} "
            f"for slug={slug!r}"
        )
        return None, None, None, None
    except Exception as e:
        logger.error(f"[Executor] Token resolution failed for slug={slug!r}: {e}")
        return None, None, None, None


# ─── Kelly sizing ──────────────────────────────────────────────────────────────

_CONFIDENCE_KELLY_SCALE = {
    "high":   1.00,   # full Kelly fraction (25%)
    "medium": 0.65,   # ~16% effective Kelly — caution for unverified data
    "low":    0.00,   # safety net — Sage filters these out but don't bet if reached
}

def _kelly_size(model_prob: float, price: float, cfg: dict, confidence: str = "medium") -> float:
    """
    Confidence-scaled fractional Kelly, clamped to [min_bet, max_bet].
    Returns 0 if no edge or confidence is 'low'.

    Scale:
      high   → 1.0x Kelly fraction (e.g. 25% fractional → effectively 25%)
      medium → 0.65x Kelly fraction (e.g. 25% fractional → effectively 16%)
      low    → 0 (no bet — Sage should already block these)
    """
    if price <= 0 or price >= 1:
        return cfg["min_bet"]
    kelly_full = (model_prob - price) / (1 - price)
    if kelly_full <= 0:
        return 0.0
    scale = _CONFIDENCE_KELLY_SCALE.get(confidence, 0.65)
    if scale == 0.0:
        return 0.0
    return max(cfg["min_bet"], min(kelly_full * cfg["kelly"] * scale * cfg["bankroll"], cfg["max_bet"]))


# ─── CLOB client ──────────────────────────────────────────────────────────────

def _build_clob_client(cfg):
    from py_clob_client.client import ClobClient
    pk = cfg["pk"]
    if not pk or pk.startswith("<"):
        raise ValueError("POLYMARKET_PRIVATE_KEY not set")
    kwargs = {
        "host": "https://clob.polymarket.com",
        "key": pk,
        "chain_id": 137,
        "signature_type": cfg["sig_type"],
    }
    if cfg["funder"]:
        kwargs["funder"] = cfg["funder"]
    client = ClobClient(**kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


# ─── Order placement ───────────────────────────────────────────────────────────

def _place_limit(client, token_id: str, price: float, size_usd: float) -> dict | None:
    """Post a GTC limit buy. Returns resp dict on success, None on failure."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY
    shares = round(size_usd / price, 2)
    signed = client.create_order(
        OrderArgs(token_id=token_id, price=round(price, 3), size=shares, side=BUY)
    )
    resp = client.post_order(signed, OrderType.GTC)
    return resp if resp and resp.get("success") else None


# ─── Public API ───────────────────────────────────────────────────────────────

def execute_picks(picks: list) -> list:
    """
    Execute approved Sage picks immediately.

    For each pick:
      1. Validate slug + selection present
      2. Duplicate-bet guard
      3. Resolve token_id + price from Gamma API
      4. Kelly size (skip if no edge)
      5. Paper mode: record to DB, return status='paper'
      6. Live mode: build CLOB client, place GTC limit, record to DB

    Returns list of result dicts, one per pick.
    """
    cfg = _cfg()
    results = []
    clob_client = None  # built once per batch in live mode

    for pick in picks:
        result = {
            "pick":     pick,
            "status":   "failed",
            "reason":   "",
            "token_id": "",
            "price":    0.0,
            "size_usd": 0.0,
            "shares":   0.0,
            "order_id": "",
            "db_id":    None,
        }

        slug      = pick.get("polymarket_slug", "").strip()
        selection = pick.get("selection", "").strip()
        event_id  = pick.get("event_id", "")

        # 1. Validate
        if not slug or not selection:
            result["reason"] = f"Missing slug={slug!r} or selection={selection!r}"
            logger.warning(f"[Executor] Skipping pick — {result['reason']}")
            result["status"] = "skipped"
            results.append(result)
            continue

        # 2. Duplicate guard
        if _already_bet(cfg, pick):
            result["reason"] = f"Already have open bet for {selection!r} in {event_id!r}"
            logger.info(f"[Executor] {result['reason']} — skipping")
            result["status"] = "skipped"
            results.append(result)
            continue

        # 3. Resolve token
        token_id, price, idx, outcomes = _resolve_token(slug, selection)
        if not token_id:
            result["reason"] = f"Could not resolve token for slug={slug!r}, selection={selection!r}"
            logger.warning(f"[Executor] {result['reason']}")
            result["status"] = "failed"
            try:
                from . import notifier as _notifier
                _notifier.pipeline_error("Executor", result["reason"])
            except Exception:
                pass
            results.append(result)
            continue

        result["token_id"] = token_id
        result["price"]    = price

        # 4. Kelly size — scaled by confidence tier
        model_prob = float(pick.get("model_probability", 0.5))
        confidence = pick.get("confidence", "medium")
        size_usd   = _kelly_size(model_prob, price, cfg, confidence)
        if size_usd <= 0:
            result["reason"] = (
                f"No edge — model_prob={model_prob:.3f} <= price={price:.3f}"
            )
            logger.info(f"[Executor] {result['reason']} — skipping {selection!r}")
            result["status"] = "skipped"
            results.append(result)
            continue

        shares = round(size_usd / price, 2)
        result["size_usd"] = size_usd
        result["shares"]   = shares

        # 5. Paper mode
        if cfg["dry_run"]:
            db_id = _record_trade(
                cfg, pick, token_id, price, size_usd, shares,
                order_id="", status="open",
                reason=f"PAPER | model_prob={model_prob:.3f} | conf={confidence} | {len(outcomes or [])} outcomes",
            )
            result["status"] = "paper"
            result["db_id"]  = db_id
            logger.info(
                f"[Executor] PAPER | {selection!r} @ {price:.4f} "
                f"${size_usd:.2f} → {shares} shares | db_id={db_id}"
            )
            results.append(result)
            continue

        # 6. Live mode
        try:
            if clob_client is None:
                clob_client = _build_clob_client(cfg)
        except Exception as e:
            result["reason"] = f"CLOB init failed: {e}"
            logger.error(f"[Executor] {result['reason']}")
            results.append(result)
            continue

        try:
            resp = _place_limit(clob_client, token_id, price, size_usd)
        except Exception as e:
            result["reason"] = f"Order error: {e}"
            logger.error(f"[Executor] {result['reason']}")
            db_id = _record_trade(
                cfg, pick, token_id, price, size_usd, shares,
                order_id="", status="failed", reason=result["reason"],
            )
            result["db_id"] = db_id
            results.append(result)
            continue

        if resp:
            order_id = resp.get("orderID", "")
            db_id = _record_trade(
                cfg, pick, token_id, price, size_usd, shares,
                order_id=order_id, status="open",
                reason=f"LIVE | model_prob={model_prob:.3f}",
            )
            result["status"]   = "placed"
            result["order_id"] = order_id
            result["db_id"]    = db_id
            logger.info(
                f"[Executor] PLACED | {selection!r} @ {price:.4f} "
                f"${size_usd:.2f} → {shares} shares | order={order_id[:20]}... | db_id={db_id}"
            )
        else:
            result["reason"] = "Order rejected by CLOB"
            logger.warning(f"[Executor] {result['reason']} for {selection!r}")
            db_id = _record_trade(
                cfg, pick, token_id, price, size_usd, shares,
                order_id="", status="failed", reason=result["reason"],
            )
            result["db_id"] = db_id

        results.append(result)

    placed = sum(1 for r in results if r["status"] == "placed")
    paper  = sum(1 for r in results if r["status"] == "paper")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] == "failed")
    logger.info(
        f"[Executor] Done — placed={placed} paper={paper} "
        f"skipped={skipped} failed={failed} / {len(picks)} picks"
    )
    return results
