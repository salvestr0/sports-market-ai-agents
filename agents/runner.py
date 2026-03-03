"""
Hourly batch runner for the 4-agent sports betting pipeline.

Execution order:
  1. Max   — finds upcoming events and researches them
  2. Nova  — fetches sharp book odds + Polymarket prices per event
  3. Lumi  — devil's advocate risk assessment
  4. Sage  — final BET/SKIP, writes approved_picks → scraper_data/

Reports saved to:
  agents/reports/max_{ts}.json
  agents/reports/nova_{ts}.json
  agents/reports/lumi_{ts}.json
  (Sage output goes directly to scraper_data/agents_{ts}.json)

Usage:
  python -m agents.runner               # run hourly forever
  python -m agents.runner --once        # single batch run
  python -m agents.runner --test-tools  # test tool connectivity only
"""

import os
import sys

# Force UTF-8 I/O on Windows (avoids CP1252 encoding errors with Unicode chars)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Add project root to path so we can import from agents/
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import max_agent, nova_agent, lumi_agent, sage_agent
from agents import notifier, telegram_listener, self_review, tools, resolver

# ─── Logging ─────────────────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
_file_handler = logging.FileHandler("agents/agent_pipeline.log", mode="a", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])
logger = logging.getLogger("agents.runner")

# ─── Config ───────────────────────────────────────────────────────────────────

SELF_REVIEW_INTERVAL_S = 4 * 3600   # every 4 hours
_last_self_review: float = 0.0      # epoch seconds; 0 = never run

SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "soccer_uefa_champs_league",
    "mma_mixed_martial_arts",
]

HOURS_AHEAD = 24  # Sharp books post lines ~12-24h out — research beyond 24h produces UNKNOWN edges
REPORTS_DIR = "agents/reports"
SCRAPER_DIR = "scraper_data"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _save_report(data: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info(f"  Saved → {path}")


def _attach_slugs(candidates: list, pm_events: list) -> None:
    """
    Deterministically attach polymarket_slug to each candidate by fuzzy-matching
    against pre-fetched pm_events. Sets slug in-place; skips if already set.

    Totals candidates (market_type == "totals") get the totals_slug; moneyline
    candidates get the standard moneyline slug.
    """
    for c in candidates:
        if c.get("polymarket_slug"):
            continue
        home = c.get("home_team", "")
        away = c.get("away_team", "")
        is_totals = c.get("market_type") == "totals"
        for e in pm_events:
            ta = e.get("team_a", "")
            tb = e.get("team_b", "")
            if (tools._names_match(home, ta) and tools._names_match(away, tb)) or \
               (tools._names_match(home, tb) and tools._names_match(away, ta)):
                slug_key = "totals_slug" if is_totals else "slug"
                slug = e.get(slug_key, "")
                if slug:
                    c["polymarket_slug"] = slug
                    logger.info(f"  [SLUGS] {home} vs {away} ({c.get('market_type','moneyline')}) → {slug!r}")
                break




def _get_bankroll_context() -> dict:
    """
    Query sports_trades.db to build portfolio health context for Lumi.
    Returns a safe dict even if the DB doesn't exist yet.
    """
    db_path = "sports_trades.db"
    if not os.path.exists(db_path):
        starting = float(os.getenv("BOT_BANKROLL", "100"))
        return {
            "total_bets": 0,
            "total_pnl": 0.0,
            "open_bets": 0,
            "open_exposure_usd": 0.0,
            "recent_streak": "no trades yet",
            "win_rate_7d": None,
            "starting_bankroll": starting,
            "current_bankroll_estimate": starting,
        }

    try:
        import sqlite3
        from datetime import timedelta
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        # Total bets + P&L
        c.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM trades WHERE status IN ('won','lost')")
        row = c.fetchone()
        total_bets = row[0] or 0
        total_pnl  = row[1] or 0.0

        # Open positions
        c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM trades WHERE status='open'")
        row = c.fetchone()
        open_bets    = row[0] or 0
        open_exposure = row[1] or 0.0

        # 7-day win rate
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        c.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) "
            "FROM trades WHERE status IN ('won','lost') AND created_at >= ?",
            (week_ago,),
        )
        row = c.fetchone()
        bets_7d = row[0] or 0
        wins_7d = row[1] or 0
        win_rate_7d = wins_7d / bets_7d if bets_7d > 0 else None

        # Recent streak (last 5 resolved trades)
        c.execute(
            "SELECT status FROM trades WHERE status IN ('won','lost') "
            "ORDER BY created_at DESC LIMIT 5"
        )
        recent = [r[0] for r in c.fetchall()]
        if recent:
            streak_str = " → ".join("W" if s == "won" else "L" for s in recent)
        else:
            streak_str = "no resolved trades"

        conn.close()
        starting = float(os.getenv("BOT_BANKROLL", "100"))
        current_bankroll_estimate = round(starting + total_pnl, 2)
        return {
            "total_bets":                total_bets,
            "total_pnl":                 round(total_pnl, 2),
            "open_bets":                 open_bets,
            "open_exposure_usd":         round(open_exposure, 2),
            "recent_streak":             streak_str,
            "win_rate_7d":               win_rate_7d,
            "starting_bankroll":         starting,
            "current_bankroll_estimate": current_bankroll_estimate,
        }
    except Exception as e:
        logger.warning(f"[PIPELINE] Could not read bankroll context: {e}")
        starting = float(os.getenv("BOT_BANKROLL", "100"))
        return {
            "total_bets": 0, "total_pnl": 0.0,
            "open_bets": 0, "open_exposure_usd": 0.0,
            "recent_streak": "DB read error", "win_rate_7d": None,
            "starting_bankroll": starting,
            "current_bankroll_estimate": starting,
        }


def _maybe_run_self_review():
    """Run self-review if 4 hours have elapsed since the last one."""
    global _last_self_review
    now = time.time()
    if now - _last_self_review < SELF_REVIEW_INTERVAL_S:
        return
    logger.info("[PIPELINE] 4-hour mark — running self-review...")
    try:
        self_review.run()
        _last_self_review = now
        notifier.send("📋 <b>Self-review complete</b> — check agents/self-review.md")
    except Exception as e:
        logger.warning(f"[PIPELINE] Self-review failed: {e}")


def _check_env():
    """Warn about missing env vars before starting."""
    issues = []
    if not os.getenv("ANTHROPIC_API_KEY"):
        issues.append("ANTHROPIC_API_KEY not set - Sage (final agent) will fail")
    if not os.getenv("ODDS_API_KEY"):
        issues.append("ODDS_API_KEY not set - Nova sharp odds will be unavailable")
    if not os.getenv("TAVILY_API_KEY"):
        issues.append("TAVILY_API_KEY not set - Max will use DuckDuckGo fallback")
    if not os.getenv("GEMINI_API_KEY"):
        issues.append("GEMINI_API_KEY not set - Max/Nova/Lumi will fail")
    if not os.getenv("GROK_API_KEY"):
        issues.append("GROK_API_KEY not set - Max breaking-news pre-pass will fail")
    for issue in issues:
        logger.warning(f"  [ENV] {issue}")
    return True


# ─── Single batch ─────────────────────────────────────────────────────────────

def run_batch() -> dict:
    """Run one full pipeline batch. Returns Sage's output."""
    import time as _time
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    batch_start = _time.time()

    _maybe_run_self_review()
    resolver.resolve_open_trades()   # settle any bets that resolved since last batch
    tools.reset_injury_session()  # clear cross-call corruption tracker for this batch

    logger.info("=" * 65)
    logger.info(f"  AGENT PIPELINE BATCH - {ts}")
    logger.info("=" * 65)

    notifier.batch_start(SPORTS, HOURS_AHEAD)

    # ── Pre-fetch Polymarket events once (shared between Max and Nova) ────────
    logger.info("[PIPELINE] Pre-fetching Polymarket events...")
    pm_events = tools.fetch_polymarket_events(hours_ahead=HOURS_AHEAD)
    logger.info(f"[PIPELINE] {len(pm_events)} Polymarket events available")
    if not pm_events:
        logger.warning("[PIPELINE] No Polymarket events — pipeline may produce NO_MARKET for all")

    # ── Step 1: Max ───────────────────────────────────────────────────────────
    logger.info("[PIPELINE] Step 1/4: Max - researching upcoming events")
    try:
        max_report = max_agent.run(sports=SPORTS, hours_ahead=HOURS_AHEAD, pm_events=pm_events)
    except Exception as e:
        logger.error(f"[PIPELINE] Max failed: {e}", exc_info=True)
        notifier.pipeline_error("Max", str(e))
        return {"picks": [], "error": f"Max failed: {e}"}

    _save_report(max_report, f"{REPORTS_DIR}/max_{ts}.json")
    notifier.max_done(max_report)

    candidates = max_report.get("candidates", [])

    # Attach Polymarket slugs deterministically so Nova can do exact slug lookup
    _attach_slugs(candidates, pm_events)

    if not candidates:
        logger.info("[PIPELINE] Max found no candidates - ending batch early")
        notifier.batch_no_candidates()
        return {"picks": [], "note": "Max found no candidates"}

    # ── Step 2: Nova ──────────────────────────────────────────────────────────
    # Nova always runs — it does pure Python REST calls (no LLM), so skipping
    # it saves nothing and breaks all downstream odds data for Lumi and Sage.
    all_low_confidence = all(c.get("confidence") == "low" for c in candidates)
    if all_low_confidence:
        logger.warning(
            "[PIPELINE] All Max candidates are low-confidence (possible data failure). "
            "Running Nova anyway — odds data needed for Lumi assessment."
        )

    logger.info(f"[PIPELINE] Step 2/4: Nova - odds analysis for {len(candidates)} event(s)")
    try:
        nova_report = nova_agent.run(max_report, pm_events=pm_events)
    except Exception as e:
        logger.error(f"[PIPELINE] Nova failed: {e}", exc_info=True)
        notifier.pipeline_error("Nova", str(e))
        nova_report = {"agent": "Nova", "generated_at": datetime.now(timezone.utc).isoformat(), "analyses": []}
        logger.warning("[PIPELINE] Continuing without Nova data")

    _save_report(nova_report, f"{REPORTS_DIR}/nova_{ts}.json")
    notifier.nova_done(nova_report)

    # Build funnel stats for Sage (how many PM events existed before Max filtered)
    nova_analyses = nova_report.get("analyses", [])
    funnel_stats = {
        "pm_events_available": len(pm_events),
        "max_researched":      len(candidates),
        "nova_value":          sum(1 for a in nova_analyses if a.get("nova_verdict") == "VALUE"),
        "nova_no_market":      sum(1 for a in nova_analyses if a.get("nova_verdict") == "NO_MARKET"),
        "nova_unknown":        sum(1 for a in nova_analyses if a.get("nova_verdict") == "UNKNOWN"),
        "nova_fair_or_over":   sum(1 for a in nova_analyses if a.get("nova_verdict") in ("FAIR", "OVERPRICED")),
    }

    # Alert if Nova ran but all analyses came back blind (API or matching broken)
    if candidates:
        analyses = nova_report.get("analyses", [])
        blind_count = sum(1 for a in analyses if a.get("nova_verdict") in ("UNKNOWN", "NO_MARKET"))
        if analyses and blind_count == len(analyses):
            notifier.pipeline_error(
                "Nova — All events blind",
                f"{blind_count}/{len(analyses)} returned UNKNOWN or NO_MARKET. "
                f"Sharp odds feed or Polymarket matching may be broken."
            )

    # ── Step 3: Lumi ──────────────────────────────────────────────────────────
    logger.info(f"[PIPELINE] Step 3/4: Lumi - risk assessment")
    bankroll_ctx = _get_bankroll_context()
    try:
        lumi_report = lumi_agent.run(max_report, nova_report, bankroll_context=bankroll_ctx)
    except Exception as e:
        logger.error(f"[PIPELINE] Lumi failed: {e}", exc_info=True)
        notifier.pipeline_error("Lumi", str(e))
        lumi_report = {"agent": "Lumi", "generated_at": datetime.now(timezone.utc).isoformat(), "assessments": []}
        logger.warning("[PIPELINE] Continuing without Lumi data")

    _save_report(lumi_report, f"{REPORTS_DIR}/lumi_{ts}.json")
    notifier.lumi_done(lumi_report)

    # ── Step 4: Sage ──────────────────────────────────────────────────────────
    logger.info("[PIPELINE] Step 4/4: Sage - final decisions + writing picks")
    try:
        sage_report = sage_agent.run(
            max_report=max_report,
            nova_report=nova_report,
            lumi_report=lumi_report,
            scraper_folder=SCRAPER_DIR,
            bankroll_context=bankroll_ctx,
            funnel_stats=funnel_stats,
        )
    except Exception as e:
        logger.error(f"[PIPELINE] Sage failed: {e}", exc_info=True)
        notifier.pipeline_error("Sage", str(e))
        return {"picks": [], "error": f"Sage failed: {e}"}

    notifier.sage_done(sage_report)

    # ── Agent Discussion ───────────────────────────────────────────────────────
    logger.info("[PIPELINE] Generating agent discussion...")
    try:
        discussion = sage_agent.generate_discussion(
            max_report, nova_report, lumi_report, sage_report
        )
        if discussion:
            notifier.agent_discussion(discussion)
    except Exception as e:
        logger.warning(f"[PIPELINE] Discussion generation failed: {e}")

    picks = sage_report.get("picks", [])
    elapsed = _time.time() - batch_start
    notifier.batch_done(len(picks), len(candidates), elapsed)

    logger.info("=" * 65)
    logger.info(
        f"  BATCH DONE | {len(candidates)} researched → {len(picks)} approved pick(s)"
    )
    logger.info("=" * 65)
    return sage_report


# ─── Tool connectivity test ───────────────────────────────────────────────────

def test_tools():
    """Quick sanity check of all tool connections."""
    from agents import tools

    print("\n--- Tool Connectivity Test ---")

    # web_search
    print("\n1. web_search (Tavily/DDG)...")
    results = tools.web_search("NBA games tonight 2026", max_results=2)
    if results:
        print(f"   OK — got {len(results)} result(s): '{results[0].get('title', '')[:60]}'")
    else:
        print("   WARN — no results returned")

    # get_sharp_odds
    print("\n2. get_sharp_odds (The Odds API)...")
    if os.getenv("ODDS_API_KEY"):
        odds = tools.get_sharp_odds("basketball_nba", "Boston Celtics", "Los Angeles Lakers")
        if odds.get("found"):
            print(f"   OK — found event: {odds.get('home_team')} vs {odds.get('away_team')}")
            print(f"        consensus home: {odds['consensus']['home_prob']:.3f} away: {odds['consensus']['away_prob']:.3f}")
        elif "error" in odds:
            print(f"   ERROR — {odds['error']}")
        else:
            print(f"   WARN — event not found: {odds.get('message', '')}")
    else:
        print("   SKIP — ODDS_API_KEY not set")

    # get_polymarket_market
    print("\n3. get_polymarket_market (Gamma API)...")
    pm = tools.get_polymarket_market("Boston Celtics", "Los Angeles Lakers", "basketball_nba")
    if pm.get("found"):
        print(f"   OK — found: '{pm.get('question', '')[:60]}'")
        print(f"        prices: {pm.get('prices')} vol: ${pm.get('volume', 0):,.0f}")
    else:
        print(f"   WARN — not found: {pm.get('message', '')} (may just be no current market)")

    # Gemini API (Max, Nova, Lumi)
    print("\n4. Gemini API (Max / Nova / Lumi)...")
    if os.getenv("GEMINI_API_KEY"):
        try:
            resp = tools.run_agent_gemini(
                system="You are a test agent.",
                user_prompt='Reply with exactly: {"status": "ok"}',
            )
            parsed = tools.extract_json(resp)
            if parsed.get("status") == "ok":
                print(f"   OK — inference test passed")
            else:
                print(f"   WARN — inference returned: {resp[:80]}")
        except Exception as e:
            print(f"   ERROR — {e}")
    else:
        print("   SKIP — GEMINI_API_KEY not set (required for Max/Nova/Lumi)")

    # Anthropic API (Sage only)
    print("\n5. Anthropic API (Sage only)...")
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": "Reply with the word READY only."}],
            )
            reply = resp.content[0].text if resp.content else ""
            print(f"   OK — response: '{reply}'")
        except Exception as e:
            print(f"   ERROR — {e}")
    else:
        print("   SKIP — ANTHROPIC_API_KEY not set (required for Sage)")

    print("\n--- Test complete ---\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="4-Agent Sports Betting Pipeline")
    parser.add_argument("--once", action="store_true", help="Run a single batch then exit")
    parser.add_argument("--test-tools", action="store_true", help="Test tool connectivity and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        metavar="MINUTES",
        help="Run batches automatically every N minutes (e.g. --interval 240 for every 4 hours). "
             "If 0 (default), wait for /run via Telegram.",
    )
    args = parser.parse_args()

    # Ensure reports dir exists
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(SCRAPER_DIR, exist_ok=True)

    if args.test_tools:
        test_tools()
        return

    _check_env()

    # Start two-way Telegram listener (background daemon thread)
    telegram_listener.start_background(run_callback=run_batch)

    if args.once:
        with telegram_listener.batch_lock:
            run_batch()
        return

    if args.interval > 0:
        # Scheduled auto-run mode
        interval_s = args.interval * 60
        logger.info("=" * 65)
        logger.info("  4-AGENT SPORTS BETTING PIPELINE (SCHEDULED)")
        logger.info(f"  Auto-run every {args.interval} minutes | Press Ctrl+C to stop")
        logger.info("=" * 65)
        # Run immediately on startup, then on the schedule
        with telegram_listener.batch_lock:
            run_batch()
        while True:
            try:
                time.sleep(interval_s)
            except KeyboardInterrupt:
                logger.info("\nPipeline stopped.")
                break
            with telegram_listener.batch_lock:
                run_batch()
        return

    # Manual-trigger mode — no auto-run on startup, no hourly loop.
    # Send /run via Telegram to trigger a batch whenever you want.
    logger.info("=" * 65)
    logger.info("  4-AGENT SPORTS BETTING PIPELINE")
    logger.info("  Ready — send /run via Telegram to start a batch")
    logger.info("  Press Ctrl+C to stop")
    logger.info("=" * 65)

    while True:
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("\nPipeline stopped.")
            break


if __name__ == "__main__":
    main()
