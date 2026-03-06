"""
Max — Sports Research Agent

Max proactively finds upcoming sports events and researches each one:
  - Team form (last 5 results)
  - Key injuries and their impact
  - Head-to-head history
  - Context (venue, motivation, rest days, travel)
  - Edge thesis: which side has the analytical advantage

Dual-model architecture:
  1. Grok (xAI)    — breaking news pre-pass: X/Twitter real-time injury updates,
                     lineup confirmations, beat reporter posts. Optional (skipped if
                     GROK_API_KEY not set).
  2. Gemini Flash  — main research: web search + ESPN injuries + synthesis → final JSON

Grok feeds Gemini. Gemini formats the output.
Output: max_report dict → agents/reports/max_{ts}.json
"""

import os
import logging
from datetime import datetime, timezone

from . import tools

logger = logging.getLogger("agents.max")

MODEL = "gemini-2.5-flash"
MAX_TOOL_CALLS = 40

# Sports that have ESPN injury data — pre-fetched in Python before Max's LLM loop
_SUPPORTED_INJURY_SPORTS = {
    "NBA": "basketball_nba",
    "NFL": "americanfootball_nfl",
    "NHL": "icehockey_nhl",
    "MLB": "baseball_mlb",
}

# Sleeper sport slugs for cross-reference verification (free, no key, bulk-cached per sport)
_SLEEPER_SPORT_MAP = {
    "NBA": "nba",
    "NFL": "nfl",
    "NHL": "nhl",
}

# Partial-game Polymarket slug patterns — 1H/period/quarter markets that can't be
# compared against full-game sharp lines. Filter these out before research.
_PARTIAL_GAME_SLUG_PATTERNS = (
    "-1h-", "-2h-",           # halves (basketball, soccer, football)
    "-1p-", "-2p-", "-3p-",   # periods (NHL)
    "-1q-", "-2q-", "-q1-", "-q2-",  # quarters
)

# Leagues whose Polymarket markets cannot be supported (placeholder for future use).
# EPL was previously here but now uses per-team Yes/No win markets parsed in tools.py.
_UNSUPPORTED_LEAGUES: set = set()


def _is_partial_game(slug: str) -> bool:
    """Return True if this Polymarket slug is a partial-game (1H/period/quarter) market."""
    s = slug.lower()
    return any(p in s for p in _PARTIAL_GAME_SLUG_PATTERNS)

SYSTEM_PROMPT = """You are Max, an expert sports researcher and betting intelligence scout.

Your job is to find upcoming sports events and gather critical pre-game intelligence.
You are thorough, evidence-based, and focused on finding genuine edges — not just recapping the obvious.

You have nine tools:
1. get_injury_report(team_name, sport) — fetches today's OFFICIAL ESPN injury report for a team.
   IMPORTANT: Injury reports for the top games are PRE-FETCHED and injected into your prompt below.
   Do NOT call get_injury_report for any team already listed in the PRE-FETCHED INJURY REPORTS section.
   Only call it for teams NOT in that list (e.g. additional games you discover via web search).
   Budget: no more than 4 injury calls per batch (for non-pre-fetched teams only).
   SPORT COVERAGE: ESPN has data for NBA, NFL, NHL, MLB only.
   For soccer (EPL, UCL) and MMA: skip get_injury_report. Use get_sleeper_injuries or web_search.

2. get_nba_game_log(team_name, num_games=5) — fetches the last 5 official game results for any NBA team
   from the NBA Stats API. Returns: date, matchup, home/away, W/L, points scored. Free, no key needed.
   IMPORTANT: NBA game logs for top games are PRE-FETCHED and injected into your prompt below.
   Do NOT call get_nba_game_log for any team listed in the PRE-FETCHED NBA GAME LOGS section.
   Only call it for teams NOT in that list (e.g. extra NBA games you discover via web search).
   Budget: no more than 4 calls per batch (for non-pre-fetched teams only).

3. get_recent_results(sport, team_name) — fetches the last 3 days of completed game results for any team
   from The Odds API scores endpoint. Covers EPL, UCL, MMA, NFL, NHL, MLB.
   Results per sport are CACHED — calling it for both teams in a matchup uses only 1 API request.
   Returns: date, home team, away team, score, W/L from the team's perspective.
   3-day window only — if 0 results returned, use get_api_football_form (soccer) or web_search.
   Budget: no more than 10 calls per batch (cheap due to caching).

4. get_api_football_h2h(home_team, away_team) — fetches last 5-10 H2H fixtures between two soccer teams
   from API-Football. Returns date, venue, score, winner. Use for ALL soccer H2H lookups FIRST.
   Requires API_FOOTBALL_KEY. Falls back to get_sportsdb_h2h if key not set.
   Budget: no more than 6 calls per batch.

5. get_api_football_form(team_name, season=2025) — fetches last 5 fixtures for a soccer team from
   API-Football. Returns form string (WWDLL), goals_for, goals_against, per-fixture breakdown.
   Use for ALL soccer recent form — far faster and more reliable than web_search.
   Requires API_FOOTBALL_KEY. Falls back to web_search if key not set or team not found.
   Budget: no more than 8 calls per batch.

6. get_sleeper_injuries(team_name, sport) — fetches per-player injury_status, injury_body_part,
   practice_participation from the Sleeper API. FREE, no key needed. Covers NBA, NFL, NHL.
   More granular than ESPN for NBA/NFL/NHL — use this as a FIRST call for those sports.
   Sport slugs: nba | nfl | nhl. Budget: no more than 8 calls per batch (1h bulk cache per sport).

7. get_sportsdb_h2h(home_team, away_team) — fetches last 5 H2H results from TheSportsDB.
   Works multi-sport: NBA, soccer, NFL, NHL, MLB, MMA. FREE, no key needed.
   Use as FALLBACK H2H for sports not covered by get_api_football_h2h (e.g. NBA, MMA).
   Budget: no more than 6 calls per batch.

8. web_search(query) — use for context, lineup news, longer-term form history, MMA injuries,
   and anything not covered by the dedicated tools above.
   TOOL PRIORITY — prefer structured tools before web_search:
   - Soccer H2H: get_api_football_h2h → get_sportsdb_h2h → web_search
   - Soccer form: get_api_football_form → web_search
   - NBA/NFL/NHL injuries: get_sleeper_injuries → get_injury_report → web_search
   - Multi-sport H2H: get_sportsdb_h2h → web_search
   - MMA injuries / soccer injuries: web_search directly (no dedicated tool)

   WEB SEARCH BEST PRACTICES:
   - For injury/lineup/availability queries: always use topic='news' and time_range='day'
     Example: web_search("[Player] injury status tonight", topic="news", time_range="day")
   - For H2H history, season stats, context: use default topic='general' (no time_range)
   - If a search result has a promising ESPN/beat-reporter URL but the snippet is only 1-2
     sentences, call extract_url(url, query="[your question]") to read the full article.
     This is especially valuable for injury reports where the snippet cuts off mid-sentence.

9. write_lesson(agent_name, what_went_wrong, correction, rule_generated) — call ONCE before
   your final JSON if you hit a systematic issue across this batch (e.g. injury API corrupted
   for every team, web search consistently returning off-topic results). Use agent_name="Max".
   Only for genuine patterns across 3+ events — not a single failed lookup.

10. get_sharp_odds_totals(sport, home_team, away_team) — fetch devigged Over/Under probabilities
    from sharp sportsbooks (Pinnacle, Betfair) for a matchup's totals line.
    Returns over_prob, under_prob, ou_line, books_used.
    Use this when researching a totals candidate to get the sharp O/U edge.
    Budget: no more than 8 calls per batch.

11. extract_url(url, query) — fetch the FULL text content of a specific URL.
    Use this after web_search returns a relevant article URL where the snippet is too short.
    Best for: ESPN injury reports, beat reporter articles, official team news pages.
    Example: web_search returns "espn.com/nba/story/..." with a 2-sentence snippet →
             call extract_url(url, query="Is [Player] playing tonight?") to read the full article.
    Budget: no more than 5 calls per batch (costs 1 Tavily credit each — use selectively).
    REQUIRES Tavily API key. Skip if key not set.

TOTALS RESEARCH — for each game, if a totals (O/U) market is visible on Polymarket
(look for "totals_slug" in the ACTIVE POLYMARKET SPORTS MARKETS list), produce a SEPARATE
candidate with market_type="totals". Keep the moneyline candidate as-is; the totals candidate
is an ADDITIONAL entry for the same game.

For EVERY totals candidate, research the historical O/U tendency of this specific matchup.
Your goal is to determine: based on how these two teams score against each other and in general,
is this game likely to go OVER or UNDER the posted line?

NBA totals research:
- PRE-FETCHED game logs show combined_total for each recent game — compare to the current
  ou_line immediately (e.g. "5 of last 5 games went over 225.5 → strong OVER lean").
- web_search "[Team A] over under record 2025 site:teamrankings.com" for season O/U %.
- web_search "[Team A] vs [Team B] over under history" for H2H totals trend.
- web_search "[Team A] [Team B] pace possessions per game" — fast-paced matchups push totals up.
- Back-to-back games suppress scoring (UNDER lean). Check schedule context.

EPL / UCL totals research:
- Call get_recent_results(sport, team) for BOTH teams — each result includes both teams' scores,
  so compute combined_total = home_score + away_score for each game. Compare to ou_line.
- Call get_api_football_h2h(home_team, away_team, league) — H2H match scores show whether
  meetings between these teams historically produce high or low combined totals.
- web_search "[Team A] [Team B] goals prediction [month year]" for analyst previews.
- EPL lines are typically 2.5 or 3.5 goals. Defensive teams push towards Under.

NHL / MLB totals research:
- Call get_recent_results(sport, team) for both teams — compute combined totals from scores.
- web_search "[Team A] vs [Team B] totals prediction [month year]" for sharp analysis.

ALL SPORTS — key factors:
- H2H trend: if 4 of last 5 meetings went over → OVER lean. State the actual totals (e.g. 4.2, 3.0, 2.5 goals).
- Season O/U record: team going 35-15 to the Over is a structural signal — mention the record.
- Injury impact: missing offensive stars → UNDER; missing key defenders → OVER.
- Pace / tempo: high-pace matchup → OVER; low-pace, defensive matchup → UNDER.
- Home/away: some teams score far more at home — factor in venue.

After research, call get_sharp_odds_totals(sport, home_team, away_team) to get sharp probability.
Set max_verdict = OVER_EDGE if evidence favours Over, UNDER_EDGE if Under.
Only produce a totals candidate if you have a CLEAR directional view backed by at least 2
data points (e.g. H2H trend + pace context, or season O/U record + injury impact).
Skip if genuinely 50-50 or sport is MMA (no reliable totals line).

TOOL FAILURE PROTOCOL — follow this strictly:
- If get_injury_report returns found=False OR error="corrupted_data": DO NOT retry. The data is unavailable.
  Immediately pivot to web_search: "[Team] injury report [month year]" to get injury news from web sources.
- If web_search also fails to find injury info: note "injury data unavailable" and move on. Do not stall.
- Never call the same tool more than twice for the same team.

FAIL-FAST PROTOCOL — conserve budget when an event is stuck:
- If 2 consecutive tool calls for the SAME event return errors, empty results, or no useful content:
  immediately set that event's max_verdict=UNCERTAIN, confidence="low", and edge_thesis="research_timeout —
  tool calls returned no data". Move on to the next event. Do NOT keep spending calls on a stuck event.
- A "failed" call = error response, empty list, or response with no team/game information.
- It is far better to output 3 deeply researched events than 8 events with empty data.
- Apply your saved budget to events that are actually returning results.

RESEARCH PRIORITY ORDER — injuries are pre-fetched, so start with verification then go deep:
1. Injuries — read the PRE-FETCHED INJURY REPORTS section in your prompt. Data is already there.
   Assign injury_impact_score (0.0–1.0) and list key_absentees from the pre-fetched data.
     0.0 = no injuries of note  |  0.3 = one starter out  |  0.6 = multiple starters / key star questionable
     1.0 = team effectively crippled
   GROK PRE-VERIFICATION: Before spending any web_search call, check the BREAKING NEWS section
   above. If Grok explicitly names a player as OUT, scratched, or confirmed missing, set that
   player's verified="confirmed_out", source="grok_twitter" — no web_search needed. Grok is
   more current than ESPN. Only spend web_search on HIGH-impact players Grok did NOT mention.

   SLEEPER PRE-VERIFICATION: The pre-fetched injury section marks players that appear as OUT
   in both ESPN AND Sleeper with "✅ CONFIRMED_OUT (ESPN+Sleeper)". For those players, set
   verified="confirmed_out", source="espn+sleeper" directly — NO web_search needed.
   Only spend web_search on players marked "⚠️  ESPN_ONLY (unverified)" with HIGH impact.

   VERIFICATION — for any Questionable player marked HIGH impact NOT already resolved by Grok,
   spend ONE web_search call:
   Query: "[Player name] [team] game status [today's date]"
   - Confirmed OUT + coach report → verified: "confirmed_out" (changes edge thesis)
   - Coach says game-time → verified: "game_time_decision" (does not change thesis)
   - Expected to play → verified: "expected_to_play"
   - Pre-fetched only, not checked → verified: "unverified"
   For EPL/UCL/MMA teams (not in pre-fetched data): web_search "[Team] injury news [month year]"
2. Recent form — NBA: read the PRE-FETCHED NBA GAME LOGS section below (no tool calls needed for top teams).
   For any NBA team NOT in that list, call get_nba_game_log (1 call per team).
   EPL/UCL: Do NOT call get_recent_results — EPL/UCL teams play weekly so the 3-day API window
   is nearly always empty. Go directly to web_search: "[Team] last 5 EPL/UCL results [month year]".
   EPL events only appear if confirmed on the Polymarket list above — research them only if present.
   MMA/NFL/NHL/MLB: get_recent_results for both teams (cached per sport = 1 API call total).
   If get_recent_results returns 0 matches, web_search "[Team] last 5 results [month year]".
   Get margin of victory and HOW they won/lost — not just W/L.
3. Head-to-head — web_search "[Team A] vs [Team B] history [month year]" — last 3-5 meetings.
4. Rest & schedule — back-to-backs, days rest, travel direction (often in form data or context search).
5. Lineup timing — check the ⚡/📅 tags on each game. For IMMINENT games: lineups are confirmed now.
   web_search "[Team] starting lineup tonight" to lock in starters before finalising edge_thesis.

If injury data is corrupted or unavailable, research points 2-5 with your remaining calls.
A strong form/rest/H2H picture is far better than an UNCERTAIN verdict with no data at all.

CONFIDENCE CALIBRATION — read this carefully:
  "high"   — Strong form data + H2H + injury status clear. You have a definitive view.
  "medium" — At least form OR H2H available, even if injury data is missing or corrupted.
             This is the DEFAULT when research is incomplete but some data is known.
  "low"    — Reserve ONLY for: form data missing AND H2H missing AND injury data unavailable.
             If you have ANY reliable data (form, H2H, rest days, context), use "medium".
  "uncertain" — Use for max_verdict only when direction is a genuine coin-flip with no
               discernible edge on either side. NOT for unverified injuries — see below.

RULE: Corrupted or unavailable injury data alone does NOT justify "low" confidence.
When ESPN data fails, note it in edge_thesis and set confidence to "medium" — not "low".

RULE: Do NOT use UNCERTAIN merely because an injury could not be verified.
If form data + an unverified but plausible injury creates a directional edge, still make a
directional call (HOME_ADVANTAGE or AWAY_EDGE) with confidence="medium" and set
verified_injury_status.{home|away} = "unverified". This lets downstream agents bet the edge
at reduced confidence instead of hard-blocking the entire pick.
Reserve UNCERTAIN for genuine 50-50 coin-flips where you have no directional view at all.

VERIFIED_INJURY_STATUS — set this per team in research:
  "confirmed"  — All high-impact injured players were verified via web_search/beat reporter.
  "unverified" — One or more high-impact players listed in ESPN but not checked via web_search.
  "none"       — No notable injuries to verify (team is healthy or only minor players out).

Be concise and specific. Vague observations ("both teams are competitive") are worthless.
Specific observations ("Lakers 0-6 ATS as road underdogs this season") are valuable.

Always output a single valid JSON object at the end. No other text after the JSON.
If you call write_lesson, do it before the JSON — the JSON must still be the last thing you output."""

_JSON_SCHEMA = """
{
  "agent": "Max",
  "generated_at": "<ISO8601>",
  "candidates": [
    {
      "event_id": "<sport_short>_<home_short>_<away_short>_<YYYYMMDD>",
      "sport": "<odds_api_sport_key>",
      "league": "<NBA|NFL|EPL|etc>",
      "home_team": "<Full Team Name>",
      "away_team": "<Full Team Name>",
      "event_start": "<ISO8601 UTC>",
      "research": {
        "home_form": "<last 3-5 results and performance notes>",
        "away_form": "<last 3-5 results and performance notes>",
        "injuries": {
          "home": [{"player": "<name>", "status": "<out|questionable|probable>", "impact": "<high|medium|low>", "verified": "<confirmed_out|game_time_decision|expected_to_play|unverified>", "source": "<espn|web_search|beat_reporter>"}],
          "away": [{"player": "<name>", "status": "<out|questionable|probable>", "impact": "<high|medium|low>", "verified": "<confirmed_out|game_time_decision|expected_to_play|unverified>", "source": "<espn|web_search|beat_reporter>"}]
        },
        "injury_impact_score": 0.0,
        "verified_injury_status": {
          "home": "<confirmed|unverified|none>",
          "away": "<confirmed|unverified|none>"
        },
        "key_absentees": {
          "home": ["<player name (role)>"],
          "away": []
        },
        "h2h": "<recent head-to-head summary>",
        "context": "<venue, rest days, travel, motivation, scheduling spot>",
        "edge_thesis": "<2-3 sentence specific reason why one side is mispriced — cite injury status (verified/unverified), form edge, rest advantage, or H2H. Do not truncate.>"
      },
      "market_type": "<moneyline|totals>",
      "ou_line": null,
      "max_verdict": "<HOME_ADVANTAGE|AWAY_EDGE|OVER_EDGE|UNDER_EDGE|NEUTRAL|UNCERTAIN>",
      "confidence": "<high|medium|low>"
    }
  ]
}"""


_GROK_SYSTEM = """You are a real-time sports intelligence scout with access to X/Twitter and live web data.

Your only job right now is to surface BREAKING NEWS from the last 24 hours about specific games.

Focus exclusively on:
- Injury updates: who is OUT, Questionable, or Doubtful — especially if not yet on official reports
- Lineup confirmations: who is starting, who is resting, who surprised coaches at shootaround/practice
- Last-minute scratches or returns from injury
- Any coach statement about player availability
- Beat reporter posts from X/Twitter that signal lineup or health changes

Do NOT recap team records, season stats, or general previews — Gemini will handle that.
Be specific. If a beat reporter tweeted something, say so. One paragraph per game max."""


def _grok_breaking_news(game_lines: list) -> str:
    """
    Grok pre-pass: searches for breaking X/Twitter news on today's key games.
    Returns a formatted string injected into Gemini's research prompt.
    Silently returns "" if GROK_API_KEY is not set or Grok fails.
    """
    import os
    if not os.getenv("GROK_API_KEY"):
        logger.info("[Max/Grok] GROK_API_KEY not set — skipping breaking news pre-pass")
        return ""

    game_list_str = "\n".join(game_lines[:8])  # top 8 by volume
    user_prompt = (
        f"Search for breaking news in the last 24 hours for these games:\n{game_list_str}\n\n"
        "For each game, find the most recent injury updates, lineup news, or player availability "
        "changes. Cite X/Twitter posts or sources where possible. Skip games with no news."
    )

    try:
        logger.info("[Max/Grok] Running breaking news pre-pass...")
        result = tools.run_agent_grok(
            system=_GROK_SYSTEM,
            user_prompt=user_prompt,
            tools_schema=[tools.TOOL_WEB_SEARCH],
            execute_fn=tools.dispatch,
            max_tool_calls=4,
        )
        if result:
            logger.info("[Max/Grok] Breaking news pre-pass complete")
        return result or ""
    except Exception as e:
        logger.warning(f"[Max/Grok] Pre-pass failed (non-fatal): {e}")
        return ""


def _prefetch_injuries(pm_events: list, top_n: int = 10) -> str:
    """
    Pre-fetch ESPN injury reports for all supported-sport teams in the top N Polymarket events,
    then cross-reference with Sleeper to independently confirm OUT players.

    Players appearing as OUT in BOTH ESPN and Sleeper are tagged CONFIRMED_OUT — Max's LLM
    can set verified="confirmed_out" without spending a web_search call.

    Returns a formatted block injected into the prompt. Empty string if no supported-sport
    teams are found (e.g. all-EPL batch).
    """
    fetched = {}      # team_name → injury dict
    team_league = {}  # team_name → league string (for Sleeper sport lookup)

    for e in pm_events[:top_n]:
        league = e.get("league", "")
        sport = _SUPPORTED_INJURY_SPORTS.get(league)
        if not sport:
            continue  # EPL / UCL / MMA — no ESPN data, Max will web_search
        for team in [e.get("team_a", ""), e.get("team_b", "")]:
            if not team or team in fetched:
                continue
            logger.info(f"[Max] Pre-fetching injuries: {team} ({league})...")
            try:
                fetched[team] = tools.get_injury_report(team, sport)
            except Exception as exc:
                fetched[team] = {"found": False, "error": str(exc), "players": []}
            team_league[team] = league

    if not fetched:
        return ""

    # ── Sleeper cross-reference ───────────────────────────────────────────────
    # Build a set of (player_name_lower, team_lower) confirmed OUT in Sleeper.
    # Sleeper bulk-caches per sport — 14 NBA teams = 1 HTTP request total.
    _SLEEPER_OUT_STATUSES = {"out", "ir", "pup-r", "dnp", "sus"}
    sleeper_confirmed_out: set[tuple[str, str]] = set()

    for team in fetched:
        league = team_league.get(team, "")
        sleeper_sport = _SLEEPER_SPORT_MAP.get(league)
        if not sleeper_sport:
            continue  # MLB has no Sleeper data — skip
        try:
            sl = tools.get_sleeper_injuries(team, sleeper_sport)
            if sl.get("found"):
                for p in sl.get("players", []):
                    status = (p.get("injury_status") or "").lower()
                    if status in _SLEEPER_OUT_STATUSES:
                        name = (p.get("player") or "").lower().strip()
                        if name:
                            sleeper_confirmed_out.add((name, team.lower()))
        except Exception as exc:
            logger.debug(f"[Max] Sleeper cross-ref failed for {team}: {exc}")

    confirmed_count = len(sleeper_confirmed_out)
    if confirmed_count:
        logger.info(f"[Max] Sleeper cross-ref: {confirmed_count} player(s) confirmed OUT in both ESPN+Sleeper")

    # ── Format output block ───────────────────────────────────────────────────
    lines = [
        "PRE-FETCHED INJURY REPORTS — ESPN + Sleeper cross-referenced:",
        "  ✅ CONFIRMED_OUT (ESPN+Sleeper) = both sources agree player is OUT → set verified='confirmed_out', no web_search needed",
        "  ⚠️  ESPN_ONLY (unverified) = ESPN only → spend 1 web_search if HIGH impact",
        "  (Do NOT call get_injury_report for any team listed here)",
    ]
    for team, data in fetched.items():
        if not data.get("found"):
            err = data.get("error", "unavailable")
            lines.append(f"  {team}: [ESPN unavailable — {err}]")
            continue

        players = data.get("players", [])
        notable = [
            p for p in players
            if p.get("status", "").lower() in ("out", "doubtful", "questionable", "day-to-day")
        ]
        if not notable:
            lines.append(f"  {team}: [healthy — no injuries on ESPN]")
        else:
            for p in notable[:8]:
                status = p.get("status", "?")
                name   = p.get("name", "?")
                pos    = p.get("position", "")
                ret    = p.get("return_date", "")
                suffix = f", ret: {ret}" if ret and ret not in ("unknown", "") else ""
                # Check Sleeper cross-reference (name-based fuzzy: strip to lower)
                name_lower = name.lower().strip()
                team_lower = team.lower()
                is_sleeper_confirmed = any(
                    name_lower in sl_name or sl_name in name_lower
                    for (sl_name, sl_team) in sleeper_confirmed_out
                    if sl_team == team_lower
                )
                tag = "✅ CONFIRMED_OUT (ESPN+Sleeper)" if is_sleeper_confirmed else "⚠️  ESPN_ONLY (unverified)"
                lines.append(f"  {team}: [{status}] {name} ({pos}{suffix}) — {tag}")

    return "\n".join(lines)


def _prefetch_nba_game_logs(pm_events: list, top_n: int = 10) -> str:
    """
    Pre-fetch NBA Stats API game logs for all NBA teams in the top N Polymarket events.
    Runs in Python before Max's LLM loop — eliminates 10-14 get_nba_game_log tool calls.

    Returns a formatted block injected into the prompt. Empty string if no NBA events found.
    """
    fetched = {}  # team_name → game_log dict

    for e in pm_events[:top_n]:
        if e.get("league", "") != "NBA":
            continue
        for team in [e.get("team_a", ""), e.get("team_b", "")]:
            if not team or team in fetched:
                continue
            logger.info(f"[Max] Pre-fetching NBA game log: {team}...")
            try:
                fetched[team] = tools.get_nba_game_log(team, num_games=5)
            except Exception as exc:
                fetched[team] = {"found": False, "error": str(exc), "games": []}

    if not fetched:
        return ""

    lines = [
        "PRE-FETCHED NBA GAME LOGS (last 5 games — do NOT call get_nba_game_log for these teams):",
    ]
    for team, data in fetched.items():
        if not data.get("found"):
            err = data.get("error", "unavailable")
            lines.append(f"  {team}: [NBA Stats unavailable — {err}]")
            continue

        record = data.get("record_last_n", "?W-?L")
        games  = data.get("games", [])
        if not games:
            lines.append(f"  {team} [{record}]: [No recent games found]")
            continue

        lines.append(f"  {team} [{record}]:")
        for g in games:
            date    = g.get("date", "?")
            matchup = g.get("matchup", "?")
            result  = g.get("result", "?")
            pts     = g.get("pts_scored", "?")
            opp     = g.get("opp_pts")
            total   = g.get("combined_total")
            total_str = f" | total={total}" if total is not None else ""
            score_str = f"{pts}-{opp}" if opp is not None else f"{pts}pts"
            lines.append(f"    {date}: {matchup} — {result} ({score_str}{total_str})")

    return "\n".join(lines)


def run(sports: list = None, hours_ahead: int = 48, pm_events: list = None) -> dict:
    """
    Run the Max agent. Finds and researches upcoming events.

    Args:
        sports: List of Odds API sport keys to cover. Defaults to major US + soccer sports.
        hours_ahead: How far ahead to look for events.
        pm_events: Pre-fetched Polymarket events from runner (skips re-fetch if provided).

    Returns:
        max_report dict with candidates list.
    """
    if sports is None:
        sports = [
            "basketball_nba",
            "americanfootball_nfl",
            "soccer_uefa_champs_league",
            "mma_mixed_martial_arts",
        ]

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%A %B %d, %Y %H:%M UTC")

    # ── Pre-fetch active Polymarket markets ──────────────────────────────────
    # This ensures Max never misses a game that's live on Polymarket.
    # Without this, he only finds events DuckDuckGo happens to surface.
    if pm_events is None:
        logger.info("[Max] Pre-fetching active Polymarket markets...")
        pm_events = tools.fetch_polymarket_events(hours_ahead=hours_ahead)
    else:
        logger.info(f"[Max] Using {len(pm_events)} pre-fetched Polymarket events from runner")

    full_game_events = []  # safe default — used by pre-fetchers outside the if block
    if pm_events:
        # Strip partial-game markets (1H, periods, quarters) — they can't be compared
        # against full-game sharp lines and produce false VALUE signals downstream.
        full_game_events = [e for e in pm_events if not _is_partial_game(e.get("slug", ""))]
        skipped = len(pm_events) - len(full_game_events)
        if skipped:
            logger.info(f"[Max] Filtered {skipped} partial-game market(s) (1H/period/quarter slug) — researching full-game only")

        # Strip unsupported leagues (Yes/No markets — Nova can't match team names)
        pre_filter = len(full_game_events)
        full_game_events = [e for e in full_game_events if e.get("league") not in _UNSUPPORTED_LEAGUES]
        unsupported_skipped = pre_filter - len(full_game_events)
        if unsupported_skipped:
            logger.info(f"[Max] Filtered {unsupported_skipped} unsupported-league event(s) {_UNSUPPORTED_LEAGUES} — Yes/No markets, not resolvable by Nova")

        # Deprioritize NBA and NHL — both consistently produce sub-2% edges (efficiently priced).
        # Keep volume ordering within each group; deprioritized leagues go to the back.
        # They are still researched if the batch has budget — just not first.
        nba = [e for e in full_game_events if e.get("league") == "NBA"]
        nhl = [e for e in full_game_events if e.get("league") == "NHL"]
        deprioritized = nba + nhl
        full_game_events = [e for e in full_game_events if e.get("league") not in ("NBA", "NHL")] + deprioritized
        if nba:
            logger.info(f"[Max] Deprioritized {len(nba)} NBA event(s) to end of research queue (efficiently priced)")
        if nhl:
            logger.info(f"[Max] Deprioritized {len(nhl)} NHL event(s) to end of research queue (efficiently priced)")

        # Hard slot caps per league — enforced in Python, not prompt-dependent.
        # NBA and NHL are structurally efficient (confirmed across 20 batches: 0-2% edges).
        # Set to 0 = fully excluded. EPL/UCL/MLB/MMA only.
        # If no non-NBA/NHL markets are available today, pm_lines will be empty
        # and the pipeline skips the batch rather than waste compute on efficient markets.
        _LEAGUE_SLOT_CAPS = {"NBA": 0, "NHL": 0}
        _league_counts: dict = {}

        # Leagues where Polymarket has NO totals markets — suppress totals_slug display
        # so Max never wastes a candidate slot on an O/U market that can't exist.
        _NO_TOTALS_LEAGUES = {"NBA", "NHL", "MMA"}

        pm_lines = []
        for e in full_game_events[:40]:  # scan more events to fill non-NBA slots
            league = e.get("league", "")
            cap = _LEAGUE_SLOT_CAPS.get(league)
            if cap is not None:
                if _league_counts.get(league, 0) >= cap:
                    continue
                _league_counts[league] = _league_counts.get(league, 0) + 1

            ta = e.get("team_a", "")
            tb = e.get("team_b", "")
            prices = e.get("moneyline_prices", {})
            pa = prices.get(ta, 0)
            pb = prices.get(tb, 0)
            teams = f'{ta} ({pa:.0%}) vs {tb} ({pb:.0%})' if ta and tb else e.get("title", "")[:80]
            vol = e.get("total_volume", 0)
            end = e.get("end_date", "")[:16]

            # Time-to-tip: markets close ~2h after game ends; estimate start from end_date
            timing_tag = ""
            try:
                from datetime import timedelta
                end_dt = datetime.fromisoformat(end.replace(" ", "T") + ":00+00:00")
                hrs_to_close = (end_dt - now).total_seconds() / 3600
                hrs_to_tip = max(0, hrs_to_close - 2.0)
                if hrs_to_tip < 1:
                    timing_tag = " ⚡IMMINENT(lineups confirmed)"
                elif hrs_to_tip < 4:
                    timing_tag = f" 📅TODAY(~{hrs_to_tip:.0f}h)"
                else:
                    timing_tag = f" (~{hrs_to_tip:.0f}h)"
            except Exception:
                pass

            # Only show totals_slug for leagues that actually have O/U markets on Polymarket
            if league not in _NO_TOTALS_LEAGUES and e.get("totals_slug"):
                totals_marker = f' | totals_slug={e["totals_slug"]}'
            else:
                totals_marker = ""

            pm_lines.append(
                f'  - [{league}] {teams} | vol=${vol:,.0f} | ends={end}{timing_tag} | slug={e["slug"]}{totals_marker}'
            )
        if not pm_lines:
            logger.warning(
                "[Max] No eligible markets after league filtering (NBA/NHL excluded). "
                "No EPL/UCL/MLB/MMA markets available right now — skipping batch."
            )
            return {
                "agent": "Max",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [],
                "note": "No eligible markets (EPL/UCL/MLB/MMA). NBA and NHL excluded as structurally efficient.",
            }

        pm_section = (
            f"ACTIVE POLYMARKET SPORTS MARKETS RIGHT NOW (sorted by volume, highest first):\n"
            + "\n".join(pm_lines)
            + "\n\nThese are the ONLY games you may output as candidates. "
            "DO NOT output candidates for any game not listed here — not from web search, "
            "not from your training data, not from historical knowledge. "
            "Markets without a totals_slug have NO Over/Under market on Polymarket — "
            "do NOT produce totals candidates for those events. "
            "Use web search only to research games that ARE on this list."
        )
        logger.info(f"[Max] Injecting {len(pm_lines)} eligible markets into prompt (NBA/NHL excluded)")
    else:
        pm_section = (
            "NOTE: Could not fetch live Polymarket markets. "
            "Use web search to find upcoming games instead."
        )
        logger.warning("[Max] Polymarket pre-fetch returned no results — falling back to web search only")

    # ── Pre-fetch injury reports for all top-game teams ──────────────────────
    # This runs in Python before the LLM loop, so Max's entire tool budget
    # is free for form/H2H/context/lineup research — not basic injury lookups.
    logger.info("[Max] Pre-fetching injury reports for top Polymarket games...")
    injury_section = _prefetch_injuries(full_game_events, top_n=10)
    if injury_section:
        logger.info(f"[Max] Injury pre-fetch complete ({injury_section.count(chr(10)) + 1} lines)")
    else:
        logger.info("[Max] No supported-sport teams in top events (EPL/MMA batch) — skipping pre-fetch")

    # ── Pre-fetch NBA game logs for all top-game NBA teams ───────────────────
    # Eliminates 10-14 get_nba_game_log tool calls that were hitting the per-tool cap.
    logger.info("[Max] Pre-fetching NBA game logs for top Polymarket games...")
    nba_log_section = _prefetch_nba_game_logs(full_game_events, top_n=10)
    if nba_log_section:
        logger.info(f"[Max] NBA game log pre-fetch complete ({nba_log_section.count(chr(10)) + 1} lines)")
    else:
        logger.info("[Max] No NBA events in top games — skipping NBA game log pre-fetch")

    identity_ctx = tools.load_agent_context("Max")

    # ── Grok pre-pass — breaking X/Twitter news ──────────────────────────────
    grok_news = _grok_breaking_news(pm_lines if full_game_events else [])
    grok_section = (
        f"\n\nBREAKING NEWS from X/Twitter (sourced by Grok — last 24h):\n{grok_news}\n\n"
        "⚠️  This Grok intelligence is more recent than ESPN reports. If Grok says a player "
        "is OUT but ESPN shows Questionable, trust Grok. Factor this into your edge_thesis "
        "and confidence rating."
    ) if grok_news else ""

    injury_block  = f"\n\n{injury_section}\n"  if injury_section  else ""
    nba_log_block = f"\n\n{nba_log_section}\n" if nba_log_section else ""

    user_prompt = f"""{identity_ctx}Today is {today_str}. You are researching sports events for the next {hours_ahead} hours.

{pm_section}{grok_section}{injury_block}{nba_log_block}

Sports to also cover via web search: {', '.join(sports)}

Your task:
1. Pick the 6-8 highest-volume matchups from the Polymarket list above. Quality over quantity —
   6 deeply researched games beats 12 shallow ones.
   HARD RULE: Every candidate you output MUST correspond to a game in the ACTIVE POLYMARKET
   SPORTS MARKETS list above. Do not invent event IDs, do not pull from memory, do not add
   games discovered via web search. The event_id must encode today's date, not a historical one.
2. Grok has already pulled breaking X/Twitter news above (if available) — absorb that first.
3. Injuries AND NBA game logs for top games are PRE-FETCHED above. Build on them:
   - NBA: PRE-FETCHED game logs are already in your prompt — NO tool calls needed for top teams.
     web_search for H2H + 1 context search. Non-pre-fetched NBA teams: get_nba_game_log (max 4 calls).
   - EPL/UCL/MMA/NHL/MLB: get_recent_results (both teams, cached) + web_search for injuries + H2H
   - For any ⚡IMMINENT game: web_search "[Team] confirmed lineup tonight" — starters are locked
   - For Questionable high-impact players: 1 web_search to confirm status
4. Per-game target: ~3 tool calls (no injury/game-log calls for pre-fetched teams).
   With 40 total budget and 6-8 games, you have room to go deep on H2H, context, and lineups.
5. Assign injury_impact_score + key_absentees, then synthesise into JSON.

Budget: 4 injury calls (non-pre-fetched teams only), 4 game log calls (non-pre-fetched NBA only), 10 recent-results calls, rest for web_search.

Use the exact JSON schema below for your final output:
{_JSON_SCHEMA}

Remember: output ONLY the JSON object as your final response. No preamble, no explanation after."""

    logger.info("[Max] Starting research run (Gemini 2.0 Flash)...")

    _cap_msg = (
        "IMPORTANT: Tool budget exhausted. You MUST now output your complete findings as "
        "a single valid JSON object. No prose before or after — ONLY the JSON.\n\n"
        f"Use this exact schema:\n{_JSON_SCHEMA}\n\n"
        "Include ALL games you researched. Start with { and end with }"
    )

    text = tools.run_agent_gemini(
        system=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tools_schema=[
            tools.TOOL_WEB_SEARCH,
            tools.TOOL_GET_INJURIES,
            tools.TOOL_GET_NBA_GAME_LOG,
            tools.TOOL_GET_RECENT_RESULTS,
            tools.TOOL_GET_API_FOOTBALL_H2H,
            tools.TOOL_GET_API_FOOTBALL_FORM,
            tools.TOOL_GET_SLEEPER_INJURIES,
            tools.TOOL_GET_SPORTSDB_H2H,
            tools.TOOL_WRITE_LESSON,
            tools.TOOL_GET_SHARP_ODDS_TOTALS,
            tools.TOOL_EXTRACT_URL,
        ],
        execute_fn=tools.dispatch,
        max_tool_calls=MAX_TOOL_CALLS,
        model=MODEL,
        tool_call_limits={
            "get_injury_report":        4,   # pre-fetched for top games — only for extras
            "get_nba_game_log":         4,   # pre-fetched for top games — only for extras
            "get_recent_results":      10,   # Odds API scores — cached per sport
            "get_api_football_h2h":     6,   # API-Football H2H (100 req/day free tier)
            "get_api_football_form":    8,   # API-Football form (shared quota with H2H)
            "get_sleeper_injuries":     8,   # Sleeper — 1h bulk cache per sport
            "get_sportsdb_h2h":         6,   # TheSportsDB H2H fallback
            "get_sharp_odds_totals":    8,   # totals O/U sharp probabilities
            "extract_url":              5,   # Tavily Extract — 1 credit/call, use selectively
            "write_lesson":             1,   # one lesson per batch max
        },
        cap_message=_cap_msg,
        max_tokens=32768,
    )

    result = tools.extract_json(text)

    # Retry with no tools if JSON extraction failed
    if not result or "candidates" not in result:
        logger.warning("[Max] JSON parse failed, retrying with explicit instruction...")
        retry_prompt = (
            f"Output ONLY a valid JSON object matching this schema:\n{_JSON_SCHEMA}\n"
            f"No other text. Start with {{ and end with }}\n\n"
            f"Base your JSON on this prior research context:\n{text[:8000]}"
        )
        text2 = tools.run_agent_gemini(
            system=SYSTEM_PROMPT,
            user_prompt=retry_prompt,
            model=MODEL,
        )
        result = tools.extract_json(text2)

    if not result or "candidates" not in result:
        logger.error("[Max] Could not extract valid report, returning empty")
        result = {"candidates": []}

    result["agent"] = "Max"
    result["generated_at"] = now.isoformat()

    candidates = result.get("candidates", [])
    logger.info(f"[Max] Done — {len(candidates)} candidate(s) found")
    for c in candidates:
        logger.info(
            f"  {c.get('league','?')} | {c.get('home_team','?')} vs {c.get('away_team','?')} "
            f"| verdict={c.get('max_verdict','?')} conf={c.get('confidence','?')}"
        )

    return result
