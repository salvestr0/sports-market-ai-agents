"""
Batch history database for the 4-agent pipeline.

Stores one row per event per batch in agents/batch_history.db so the team can
track performance over time, identify calibration issues, and answer questions
like "how often does NHL convert?" or "is max_confidence_low the main blocker?"

Public API:
  log_batch(batch_id, batch_ts, candidates, nova_analyses, lumi_assessments, picks)
  update_outcomes(resolved_trades)  — back-propagate WIN/LOSS from sports_bot resolution
  get_summary(n_events=50) -> str   — concise block for injection into Sage's prompt
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("agents.batch_db")

DB_PATH = Path("agents/batch_history.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS batch_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT    NOT NULL,
    batch_ts        TEXT    NOT NULL,
    event_id        TEXT,
    league          TEXT,
    home_team       TEXT,
    away_team       TEXT,
    nova_verdict    TEXT,
    edge_pct        REAL,
    sharp_books     INTEGER,
    max_verdict     TEXT,
    max_confidence  TEXT,
    lumi_verdict    TEXT,
    blockers        TEXT,       -- JSON array of short blocker codes
    sage_decision   TEXT,       -- BET or SKIP
    selection       TEXT,       -- team name if BET
    model_prob      REAL,
    outcome         TEXT    DEFAULT 'pending',   -- WIN / LOSS / PUSH / pending
    pnl             REAL,
    created_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_batch_ts      ON batch_events(batch_ts);
CREATE INDEX IF NOT EXISTS idx_league        ON batch_events(league);
CREATE INDEX IF NOT EXISTS idx_sage_decision ON batch_events(sage_decision);
"""


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(_CREATE_SQL)
    conn.commit()
    return conn


def log_batch(
    batch_id: str,
    batch_ts: datetime,
    candidates: list,
    nova_analyses: list,
    lumi_assessments: list,
    picks: list,
) -> None:
    """
    Write one row per event for this batch into batch_history.db.
    Called by Sage after every run — non-fatal if DB is unavailable.
    """
    try:
        conn = _open()
    except Exception as e:
        logger.warning(f"[BatchDB] Could not open DB: {e}")
        return

    nova_by_id = {a.get("event_id"): a for a in nova_analyses}
    lumi_by_id = {a.get("event_id"): a for a in lumi_assessments}
    pick_by_id = {p.get("event_id"): p for p in picks}
    ts_str     = batch_ts.isoformat()

    rows = []
    for c in candidates:
        eid  = c.get("event_id", "")
        nova = nova_by_id.get(eid, {})
        lumi = lumi_by_id.get(eid, {})
        pick = pick_by_id.get(eid)

        edge_pct    = (nova.get("edge") or {}).get("edge_pct")
        sharp_books = (nova.get("sharp_books") or {}).get("books_used")
        nova_verdict = nova.get("nova_verdict", "UNKNOWN")
        lumi_verdict = lumi.get("lumi_verdict", "")

        # Short blocker codes (no full lumi text — that lives in the JSON reports)
        blockers = []
        if nova_verdict == "NO_MARKET":
            blockers.append("no_market")
        elif nova_verdict == "UNKNOWN":
            blockers.append("no_sharp_odds")
        elif nova_verdict in ("FAIR", "OVERPRICED"):
            ep = f"{edge_pct:.1f}%" if edge_pct is not None else "?"
            blockers.append(f"edge_low({ep})")
        if lumi_verdict == "ABORT":
            blockers.append("lumi_abort")
        if c.get("max_verdict") == "UNCERTAIN":
            blockers.append("max_uncertain")
        if c.get("confidence") == "low":
            blockers.append("max_low_conf")

        rows.append((
            batch_id, ts_str, eid,
            c.get("league", ""), c.get("home_team", ""), c.get("away_team", ""),
            nova_verdict, edge_pct, sharp_books,
            c.get("max_verdict", ""), c.get("confidence", ""),
            lumi_verdict,
            json.dumps(blockers),
            "BET" if pick else "SKIP",
            pick.get("selection", "") if pick else None,
            pick.get("model_probability") if pick else None,
        ))

    try:
        conn.executemany(
            """
            INSERT INTO batch_events
              (batch_id, batch_ts, event_id, league, home_team, away_team,
               nova_verdict, edge_pct, sharp_books,
               max_verdict, max_confidence, lumi_verdict,
               blockers, sage_decision, selection, model_prob)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
        logger.info(f"[BatchDB] Logged {len(rows)} event(s) for batch {batch_id}")
    except Exception as e:
        logger.warning(f"[BatchDB] Insert failed: {e}")
    finally:
        conn.close()


def update_outcomes(resolved_trades: list) -> None:
    """
    Back-propagate settled trade outcomes to batch_history.db so get_summary()
    can report win rates, P&L per league, etc.

    resolved_trades: list of dicts with keys:
      selection  (str)  — team name, matches batch_events.selection
      outcome    (str)  — 'WIN' or 'LOSS'
      pnl        (float)

    Matches on selection + outcome='pending' — updates the most recent pending row.
    Non-fatal: silently logs and returns if DB unavailable.
    """
    if not DB_PATH.exists() or not resolved_trades:
        return
    try:
        conn = _open()
        updated = 0
        for trade in resolved_trades:
            selection = trade.get("selection", "")
            outcome   = trade.get("outcome", "")
            pnl       = trade.get("pnl", 0.0)
            if not selection or outcome not in ("WIN", "LOSS"):
                continue
            cur = conn.execute(
                """
                UPDATE batch_events SET outcome=?, pnl=?
                WHERE id = (
                    SELECT id FROM batch_events
                    WHERE selection=? AND outcome='pending' AND sage_decision='BET'
                    ORDER BY batch_ts DESC LIMIT 1
                )
                """,
                (outcome, pnl, selection),
            )
            if cur.rowcount:
                updated += 1
        conn.commit()
        conn.close()
        if updated:
            logger.info(f"[BatchDB] Back-propagated {updated} outcome(s) to batch_history.db")
    except Exception as e:
        logger.warning(f"[BatchDB] update_outcomes failed: {e}")


def get_summary(n_events: int = 60) -> str:
    """
    Return a concise pipeline performance summary for injection into Sage's prompt.
    Covers the last N events. Returns "" if DB doesn't exist yet.
    """
    if not DB_PATH.exists():
        return ""

    try:
        conn = _open()
        cur  = conn.cursor()
    except Exception as e:
        logger.warning(f"[BatchDB] Could not query DB: {e}")
        return ""

    try:
        # ── Totals ────────────────────────────────────────────────────────────
        cur.execute(
            """
            SELECT COUNT(*),
                   SUM(sage_decision = 'BET'),
                   COUNT(DISTINCT batch_id)
            FROM (SELECT * FROM batch_events ORDER BY batch_ts DESC LIMIT ?)
            """,
            (n_events,),
        )
        row = cur.fetchone()
        total   = row[0] or 0
        bets    = int(row[1] or 0)
        batches = row[2] or 0
        if total == 0:
            return ""

        bet_rate = f"{bets / total:.0%}"

        # ── Blocker frequency ─────────────────────────────────────────────────
        cur.execute(
            "SELECT blockers FROM batch_events ORDER BY batch_ts DESC LIMIT ?",
            (n_events,),
        )
        blocker_counts: dict = {}
        for (b_json,) in cur.fetchall():
            try:
                for b in json.loads(b_json or "[]"):
                    key = b.split("(")[0].rstrip()   # normalise "edge_low(3.1%)" → "edge_low"
                    blocker_counts[key] = blocker_counts.get(key, 0) + 1
            except Exception:
                pass

        top_blockers = sorted(blocker_counts.items(), key=lambda x: -x[1])[:6]
        blocker_lines = "\n".join(f"  {k}: {v}×" for k, v in top_blockers)

        # ── Per-league breakdown ──────────────────────────────────────────────
        cur.execute(
            """
            SELECT league,
                   COUNT(*)                                                    AS n,
                   SUM(sage_decision = 'BET')                                 AS bets,
                   ROUND(AVG(CASE WHEN edge_pct IS NOT NULL
                                  THEN edge_pct END), 1)                      AS avg_edge
            FROM (SELECT * FROM batch_events ORDER BY batch_ts DESC LIMIT ?)
            WHERE league != ''
            GROUP BY league
            ORDER BY n DESC
            """,
            (n_events,),
        )
        league_lines = "\n".join(
            f"  {r[0]}: {r[1]} events, {int(r[2] or 0)} bets"
            + (f", avg edge {r[3]}%" if r[3] is not None else "")
            for r in cur.fetchall()
        )

        # ── Recent batches (last 6) ───────────────────────────────────────────
        cur.execute(
            """
            SELECT batch_id,
                   MAX(batch_ts)     AS ts,
                   COUNT(*)          AS n,
                   SUM(sage_decision = 'BET') AS b,
                   GROUP_CONCAT(DISTINCT
                       CASE WHEN sage_decision = 'SKIP'
                            THEN (
                                CASE WHEN blockers LIKE '%no_market%'     THEN 'no_market'
                                     WHEN blockers LIKE '%edge_low%'      THEN 'edge_low'
                                     WHEN blockers LIKE '%max_low_conf%'  THEN 'max_low'
                                     WHEN blockers LIKE '%lumi_abort%'    THEN 'lumi_abort'
                                     WHEN blockers LIKE '%max_uncertain%' THEN 'uncertain'
                                     ELSE 'other' END
                            ) END
                   ) AS top_blocks
            FROM batch_events
            GROUP BY batch_id
            ORDER BY ts DESC
            LIMIT 6
            """,
        )
        recent_lines = "\n".join(
            f"  {r[1][:16]} | {r[2]} events → {int(r[3] or 0)} bets"
            + (f" | {r[4]}" if r[4] else "")
            for r in cur.fetchall()
        )

        # ── Resolved outcomes (win rate + P&L attribution) ────────────────────
        cur.execute(
            """
            SELECT outcome,
                   COUNT(*)        AS n,
                   ROUND(SUM(pnl), 2) AS total_pnl
            FROM batch_events
            WHERE sage_decision = 'BET' AND outcome != 'pending'
            GROUP BY outcome
            """,
        )
        resolved_rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        wins_n,   wins_pnl  = resolved_rows.get("WIN",  (0, 0.0))
        losses_n, losses_pnl = resolved_rows.get("LOSS", (0, 0.0))
        resolved_total = wins_n + losses_n
        if resolved_total > 0:
            win_rate = f"{wins_n / resolved_total:.0%}"
            total_pnl = (wins_pnl or 0.0) + (losses_pnl or 0.0)
            outcomes_line = (
                f"Resolved: {resolved_total} bets | {wins_n}W / {losses_n}L | "
                f"WR: {win_rate} | P&L: ${total_pnl:+.2f}"
            )
        else:
            outcomes_line = "Resolved: 0 bets settled yet"

        return (
            f"=== BATCH HISTORY (last {total} events, {batches} batches) ===\n"
            f"Total researched: {total} | Bets placed: {bets} | Bet rate: {bet_rate}\n"
            f"{outcomes_line}\n\n"
            f"Top skip blockers:\n{blocker_lines}\n\n"
            f"By league:\n{league_lines}\n\n"
            f"Recent batches:\n{recent_lines}"
        )

    except Exception as e:
        logger.warning(f"[BatchDB] Summary query failed: {e}")
        return ""
    finally:
        conn.close()
