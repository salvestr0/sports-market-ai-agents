"""
Nova — Sharp Odds Analysis Agent

Nova takes Max's research candidates and finds mathematical edges by:
  1. Matching each candidate to its Polymarket market (via fetch_polymarket_events)
  2. Fetching sharp book (Pinnacle, Betfair) devigged probabilities directly in Python
  3. Computing edge = sharp_prob - polymarket_price in Python (not via LLM tool calls)
  4. Passing the pre-computed data table to the LLM for verdict + synthesis

This architecture eliminates the fragile "LLM calls get_sharp_odds with right params"
pattern that caused UNKNOWN verdicts. Nova's math is now deterministic; the LLM
just interprets and formats.

Model: qwen2.5:3b via Ollama
Output: nova_report dict → agents/reports/nova_{ts}.json
"""

import json
import logging
from datetime import datetime, timezone

from . import tools

logger = logging.getLogger("agents.nova")

MODEL = "gemini-2.5-flash"


# Odds API sport keys that map to each Polymarket league label
_LEAGUE_TO_SPORT = {
    "NBA":  "basketball_nba",
    "NFL":  "americanfootball_nfl",
    "EPL":  "soccer_epl",
    "NHL":  "icehockey_nhl",
    "MLB":  "baseball_mlb",
    "UCL":  "soccer_uefa_champs_league",
    "MMA":  "mma_mixed_martial_arts",
}

# Reverse lookup: Max's sport key → Polymarket league label
_SPORT_TO_LEAGUE = {v: k for k, v in _LEAGUE_TO_SPORT.items()}


def _find_pm_event(home: str, away: str, pm_events: list, slug: str = "") -> dict | None:
    """
    Find the best-matching Polymarket event for a given matchup.
    1. Exact slug match (most reliable — no fuzzy matching required).
    2. Fuzzy name match fallback (handles short vs full names).
    """
    # 1. Exact slug match
    if slug:
        for e in pm_events:
            if e.get("slug") == slug:
                return e
    # 2. Fuzzy name match fallback
    for e in pm_events:
        ta = e.get("team_a", "")
        tb = e.get("team_b", "")
        if (tools._names_match(home, ta) and tools._names_match(away, tb)) or \
           (tools._names_match(home, tb) and tools._names_match(away, ta)):
            return e
    return None


def _compute_analysis(candidate: dict, pm_events: list) -> dict:
    """
    Compute a full odds analysis for one candidate in Python.
    Returns a nova_analysis dict ready for inclusion in the report.
    """
    event_id  = candidate.get("event_id", "?")
    home_team = candidate.get("home_team", "")
    away_team = candidate.get("away_team", "")
    sport     = candidate.get("sport", "basketball_nba")
    league    = candidate.get("league", _SPORT_TO_LEAGUE.get(sport, "?"))

    # ── Step 1: Polymarket market ────────────────────────────────────────────
    pm_event = _find_pm_event(
        home_team, away_team, pm_events,
        slug=candidate.get("polymarket_slug", ""),
    )

    if pm_event:
        prices   = pm_event.get("moneyline_prices", {})
        ta, tb   = pm_event.get("team_a",""), pm_event.get("team_b","")
        # Map to home/away
        if tools._names_match(home_team, ta):
            home_pm_price = prices.get(ta, 0)
            away_pm_price = prices.get(tb, 0)
        else:
            home_pm_price = prices.get(tb, 0)
            away_pm_price = prices.get(ta, 0)
        pm_data = {
            "found":      True,
            "slug":       pm_event.get("slug", ""),
            "title":      pm_event.get("title", ""),
            "home_price": round(home_pm_price, 4),
            "away_price": round(away_pm_price, 4),
            "volume":     pm_event.get("total_volume", 0),
        }
    else:
        pm_data = {
            "found":   False,
            "message": f"No Polymarket market found for {home_team} vs {away_team}",
        }

    # ── Step 2: Sharp book odds ──────────────────────────────────────────────
    odds = tools.get_sharp_odds(sport, home_team, away_team)

    if odds.get("found") and odds.get("consensus"):
        cons = odds["consensus"]
        home_sharp = cons.get("home_prob", 0)
        away_sharp = cons.get("away_prob", 0)
        books_used = cons.get("books_used", 0)
        books_list = [
            {"book": b["book"], "home_prob": list(b["probs"].values())[0] if b["probs"] else 0}
            for b in odds.get("books", [])[:5]
        ]
        sharp_data = {
            "found":      True,
            "home_prob":  round(home_sharp, 4),
            "away_prob":  round(away_sharp, 4),
            "books_used": books_used,
            "books":      books_list,
        }
    else:
        sharp_data = {
            "found":   False,
            "message": odds.get("message", odds.get("error", "No sharp odds returned")),
        }
        home_sharp = 0
        away_sharp = 0

    # ── Step 3: Edge calculation ─────────────────────────────────────────────
    edge_data   = {}
    nova_verdict = "UNKNOWN"
    notes        = ""

    if not pm_data["found"]:
        nova_verdict = "NO_MARKET"
        notes = f"No matching Polymarket market found for {home_team} vs {away_team}."
        logger.warning(f"[Nova] NO_MARKET — {event_id}: {home_team} vs {away_team} not on Polymarket")
    elif not sharp_data["found"]:
        nova_verdict = "UNKNOWN"
        reason = sharp_data.get("message", sharp_data.get("error", "no reason given"))
        notes = f"No sharp odds: {reason}"
        logger.warning(f"[Nova] UNKNOWN odds — {event_id}: {reason}")
    else:
        home_edge = home_sharp - pm_data["home_price"]
        away_edge = away_sharp - pm_data["away_price"]

        if home_edge >= away_edge and home_edge > 0:
            best_side       = "home"
            best_team       = home_team
            best_poly_price = pm_data["home_price"]
            best_sharp_prob = home_sharp
            best_edge_pct   = home_edge * 100
        else:
            best_side       = "away"
            best_team       = away_team
            best_poly_price = pm_data["away_price"]
            best_sharp_prob = away_sharp
            best_edge_pct   = away_edge * 100

        # ── Direction conflict check ─────────────────────────────────────────
        # Flag when Max's verdict points to the opposite side from Nova's best edge
        max_verdict = candidate.get("max_verdict", "NEUTRAL")
        direction_conflict = False
        conflict_note = ""
        if best_edge_pct >= 2.0:  # only flag meaningful edges
            nova_favors_home = (best_side == "home")
            home_edge_pct = home_edge * 100
            away_edge_pct = away_edge * 100
            if max_verdict == "HOME_ADVANTAGE" and not nova_favors_home:
                direction_conflict = True
                conflict_note = (
                    f"Max says HOME_ADVANTAGE but Nova finds better edge on AWAY "
                    f"({away_team} +{away_edge_pct:.1f}%)"
                )
            elif max_verdict == "AWAY_EDGE" and nova_favors_home:
                direction_conflict = True
                conflict_note = (
                    f"Max says AWAY_EDGE but Nova finds better edge on HOME "
                    f"({home_team} +{home_edge_pct:.1f}%)"
                )
            if direction_conflict:
                logger.warning(f"[Nova] DIRECTION CONFLICT — {event_id}: {conflict_note}")

        edge_data = {
            "selection":        best_team,
            "side":             best_side,
            "polymarket_price": round(best_poly_price, 4),
            "sharp_prob":       round(best_sharp_prob, 4),
            "edge_pct":         round(best_edge_pct, 2),
            "home_edge_pct":    round(home_edge * 100, 2),
            "away_edge_pct":    round(away_edge * 100, 2),
            "direction_conflict": direction_conflict,
            "conflict_note":    conflict_note,
        }

        if best_edge_pct >= 8:
            nova_verdict = "VALUE"
            notes = f"Strong edge: {best_team} at {best_poly_price:.0%} vs sharp {best_sharp_prob:.0%} (+{best_edge_pct:.1f}%)"
        elif best_edge_pct >= 5:
            nova_verdict = "VALUE"
            notes = f"Edge: {best_team} at {best_poly_price:.0%} vs sharp {best_sharp_prob:.0%} (+{best_edge_pct:.1f}%)"
        elif best_edge_pct > 0:
            nova_verdict = "FAIR"
            notes = f"Edge {best_edge_pct:.1f}% — within noise margin, not enough to bet."
        else:
            nova_verdict = "OVERPRICED"
            notes = f"Polymarket overprices the favourite. No edge found."

    return {
        "event_id":            event_id,
        "home_team":           home_team,
        "away_team":           away_team,
        "league":              league,
        "polymarket":          pm_data,
        "sharp_books":         sharp_data,
        "consensus_sharp_prob": {
            "home": round(home_sharp, 4),
            "away": round(away_sharp, 4),
        } if sharp_data["found"] else {},
        "edge":                edge_data,
        "nova_verdict":        nova_verdict,
        "notes":               notes,
    }


def run(max_report: dict, pm_events: list = None) -> dict:
    """
    Run Nova to analyse odds for Max's candidates.

    All data fetching (Polymarket prices + sharp odds) is done in Python.
    The LLM is used only for qualitative synthesis on VALUE candidates.

    Args:
        max_report: Output dict from max_agent.run()
        pm_events: Pre-fetched Polymarket events from runner (skips re-fetch if provided).

    Returns:
        nova_report dict with analyses list.
    """
    candidates = max_report.get("candidates", [])
    if not candidates:
        logger.info("[Nova] No candidates from Max — skipping odds analysis")
        return {
            "agent": "Nova",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "analyses": [],
        }

    now = datetime.now(timezone.utc)

    # ── Pre-fetch live Polymarket events once (reuse for all candidates) ─────
    if pm_events is None:
        logger.info("[Nova] Fetching live Polymarket events for matching...")
        pm_events = tools.fetch_polymarket_events(hours_ahead=72)
    else:
        logger.info(f"[Nova] Using {len(pm_events)} pre-fetched Polymarket events from runner")
    logger.info(f"[Nova] {len(pm_events)} Polymarket events available for matching")

    # ── Compute analysis for each candidate in Python ─────────────────────────
    analyses = []
    for candidate in candidates:
        home = candidate.get("home_team", "?")
        away = candidate.get("away_team", "?")
        logger.info(f"[Nova] Computing odds: {home} vs {away}...")
        analysis = _compute_analysis(candidate, pm_events)
        analyses.append(analysis)

        verdict = analysis["nova_verdict"]
        edge_pct = analysis.get("edge", {}).get("edge_pct", 0)
        pm_vol = analysis.get("polymarket", {}).get("volume", 0)
        logger.info(
            f"  {home} vs {away} | {verdict}"
            + (f" | +{edge_pct:.1f}% edge" if edge_pct else "")
            + (f" | PM vol=${pm_vol:,.0f}" if pm_vol else "")
        )

    value_count = sum(1 for a in analyses if a.get("nova_verdict") == "VALUE")
    logger.info(f"[Nova] Done — {len(analyses)} analysed, {value_count} VALUE")

    return {
        "agent":        "Nova",
        "generated_at": now.isoformat(),
        "analyses":     analyses,
    }


def chat(question: str) -> str:
    """
    Answer a direct user question about current odds and edges.

    Fetches live Polymarket data + sharp odds in Python, then uses Gemini
    to explain the analysis in natural language.

    Called by telegram_listener when user sends "Nova: <question>".
    """
    # ── Fetch live Polymarket markets ────────────────────────────────────────
    pm_events = tools.fetch_polymarket_events(hours_ahead=72)

    event_lines = []
    for e in pm_events[:20]:
        ta     = e.get("team_a", "")
        tb     = e.get("team_b", "")
        prices = e.get("moneyline_prices", {})
        pa     = prices.get(ta, 0)
        pb     = prices.get(tb, 0)
        vol    = e.get("total_volume", 0)
        league = e.get("league", "")
        event_lines.append(f"[{league}] {ta} ({pa:.0%}) vs {tb} ({pb:.0%}) | vol=${vol:,.0f}")

    # ── Compute real edge if question mentions a specific team ───────────────
    q_lower    = question.lower()
    edge_block = ""

    for e in pm_events[:15]:
        ta = e.get("team_a", "")
        tb = e.get("team_b", "")
        if not ta or not tb:
            continue
        if any(tools._names_match(t, word) for t in [ta, tb]
               for word in q_lower.split() if len(word) > 3):
            league = e.get("league", "NBA")
            sport  = _LEAGUE_TO_SPORT.get(league, "basketball_nba")
            analysis = _compute_analysis(
                {"event_id": f"chat_{ta}_{tb}", "home_team": ta,
                 "away_team": tb, "sport": sport, "league": league},
                pm_events,
            )
            edge  = analysis.get("edge", {})
            pm    = analysis.get("polymarket", {})
            sharp = analysis.get("sharp_books", {})
            csp   = analysis.get("consensus_sharp_prob", {})

            if edge:
                edge_block = (
                    f"\n\nReal-time edge for {ta} vs {tb}:\n"
                    f"  Polymarket: {ta} {pm.get('home_price', 0):.1%} | {tb} {pm.get('away_price', 0):.1%}\n"
                    f"  Sharp consensus: {ta} {csp.get('home', 0):.1%} | {tb} {csp.get('away', 0):.1%}\n"
                    f"  Best edge: {edge.get('selection')} +{edge.get('edge_pct', 0):.1f}% "
                    f"({analysis.get('nova_verdict')})\n"
                    f"  PM volume: ${pm.get('volume', 0):,.0f} | Sharp books: {sharp.get('books_used', 0)}"
                )
            else:
                edge_block = (
                    f"\n\n{ta} vs {tb}: {analysis.get('nova_verdict', 'UNKNOWN')}"
                    + (f" — {analysis.get('notes', '')}" if analysis.get("notes") else "")
                )
            break

    # ── Inject identity context + call Gemini ────────────────────────────────
    identity_ctx = tools.load_agent_context("Nova")

    system = (
        "You are Nova, a sharp odds analyst for a sports betting operation. "
        "You answer direct questions about live market prices and edges. "
        "Be concise, specific, and reference actual numbers from the data provided. "
        "If edge data is available, explain clearly whether it represents value."
    )

    user_prompt = (
        identity_ctx
        + f"User question: {question}\n\n"
        f"Live Polymarket sports markets right now (sorted by volume):\n"
        f"{chr(10).join(event_lines) if event_lines else 'No live markets found.'}"
        f"{edge_block}\n\n"
        "Answer in under 150 words. Be direct and reference specific numbers."
    )

    return tools.run_agent_gemini(
        system=system,
        user_prompt=user_prompt,
        model=MODEL,
    )
