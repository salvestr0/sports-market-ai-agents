"""
Shared tools and agentic loop helper for the 4-agent sports betting pipeline.

Tools available:
  web_search(query, max_results)           — Tavily API w/ DuckDuckGo fallback
  get_sharp_odds(sport, home, away)        — The Odds API devigged probabilities
  get_polymarket_market(home, away)        — Polymarket Gamma API market lookup
  get_nba_game_log(team_name)              — NBA Stats API last-N-games form (no key needed)
  get_recent_results(sport, team_name)     — Odds API scores: last 3 days, all sports (cached per sport)
  get_live_scores(sport)                   — Odds API in-progress scores, 2-min cache
  get_api_football_h2h(home, away)         — API-Football soccer H2H: last 5-10 results (API_FOOTBALL_KEY)
  get_api_football_form(team, season)      — API-Football soccer form: last 5 fixtures (API_FOOTBALL_KEY)
  get_sleeper_injuries(team, sport)        — Sleeper injury status: NBA/NFL/NHL (free, no key)
  get_sportsdb_h2h(home, away)             — TheSportsDB multi-sport H2H fallback (free, no key)
  write_lesson(agent, what, fix, rule)     — Append a row to agents/LEARNINGS.md
  update_brain(section, content)           — Replace a section in agents/BRAIN.md

Utilities:
  dispatch(name, input_data)      — Routes tool calls to Python functions
  run_agent(...)                  — Anthropic agentic loop (tool_use → end_turn)
  run_agent_gemini(...)           — Gemini agentic loop (Max / Nova / Lumi)
  run_agent_grok(...)             — Grok agentic loop (Max breaking-news pre-pass)
  extract_json(text)              — Robust JSON extractor from LLM text
"""

import os
import json
import time
import difflib
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
import openai
from openai import OpenAI
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
from cachetools import TTLCache

logger = logging.getLogger("agents.tools")

# ─── Agent identity + memory loader ──────────────────────────────────────────

_AGENTS_DIR = Path(__file__).parent


def load_agent_context(agent_name: str) -> str:
    """
    Load this agent's identity section from SOUL.md plus the full memory.md.

    Injected at the top of each agent's user_prompt so the agent always
    knows who it is and what the team has learned so far.

    Args:
        agent_name: "Max", "Nova", "Lumi", or "Sage"

    Returns:
        Formatted context string, or "" if files not found.
    """
    sections = []

    # ── SOUL.md — extract shared principles + this agent's section ───────────
    soul_path = _AGENTS_DIR / "SOUL.md"
    if soul_path.exists():
        soul_text = soul_path.read_text(encoding="utf-8", errors="replace")

        # Always include the shared foundation
        shared_start = soul_text.find("## Shared Foundation")
        first_agent  = soul_text.find("\n## ", shared_start + 1)
        shared_block = soul_text[shared_start:first_agent].strip() if first_agent > 0 else ""

        # Extract this agent's own section
        agent_header = f"## {agent_name} —"
        agent_start  = soul_text.find(agent_header)
        agent_end    = soul_text.find("\n## ", agent_start + 1)
        if agent_start > 0:
            agent_block = soul_text[agent_start: agent_end if agent_end > 0 else len(soul_text)].strip()
        else:
            agent_block = ""

        soul_section = "\n\n".join(filter(None, [shared_block, agent_block]))
        if soul_section:
            sections.append(f"=== WHO YOU ARE ===\n{soul_section}")

    # ── memory.md — settled knowledge ────────────────────────────────────────
    memory_path = _AGENTS_DIR / "memory.md"
    if memory_path.exists():
        memory_text = memory_path.read_text(encoding="utf-8", errors="replace").strip()
        if memory_text:
            sections.append(f"=== TEAM MEMORY ===\n{memory_text}")

    # ── BRAIN.md — live working memory ───────────────────────────────────────
    brain_path = _AGENTS_DIR / "BRAIN.md"
    if brain_path.exists():
        brain_text = brain_path.read_text(encoding="utf-8", errors="replace").strip()
        if brain_text:
            sections.append(f"=== LIVE BRAIN (what we are currently tracking) ===\n{brain_text}")

    # ── LEARNINGS.md — active rules from past mistakes ────────────────────────
    learnings_path = _AGENTS_DIR / "LEARNINGS.md"
    if learnings_path.exists():
        learnings_text = learnings_path.read_text(encoding="utf-8", errors="replace").strip()
        if learnings_text:
            sections.append(f"=== LEARNINGS (rules from mistakes — follow these) ===\n{learnings_text}")

    # ── HEARTBEAT.md — extract this agent's standing questions ───────────────
    heartbeat_path = _AGENTS_DIR / "HEARTBEAT.md"
    if heartbeat_path.exists():
        hb_text = heartbeat_path.read_text(encoding="utf-8", errors="replace")

        # Extract shared pulse + this agent's section
        shared_start = hb_text.find("## Shared Pulse")
        first_agent  = hb_text.find(f"\n## {agent_name}'s Heartbeat")
        next_agent   = hb_text.find("\n## ", first_agent + 1) if first_agent > 0 else -1

        shared_hb = hb_text[shared_start:first_agent].strip() if shared_start >= 0 and first_agent > 0 else ""
        agent_hb  = hb_text[first_agent: next_agent if next_agent > 0 else len(hb_text)].strip() if first_agent > 0 else ""

        hb_block = "\n\n".join(filter(None, [shared_hb, agent_hb]))
        if hb_block:
            sections.append(f"=== YOUR HEARTBEAT (ask yourself these every cycle) ===\n{hb_block}")

    # ── SKILLS.md — extract this agent's capability section ─────────────────
    skills_path = _AGENTS_DIR / "SKILLS.md"
    if skills_path.exists():
        sk_text = skills_path.read_text(encoding="utf-8", errors="replace")

        skill_header = f"## {agent_name} —"
        sk_start     = sk_text.find(skill_header)
        sk_end       = sk_text.find("\n## ", sk_start + 1) if sk_start >= 0 else -1
        agent_skills = sk_text[sk_start: sk_end if sk_end > 0 else len(sk_text)].strip() if sk_start >= 0 else ""

        if agent_skills:
            sections.append(f"=== YOUR SKILLS ===\n{agent_skills}")

    return "\n\n".join(sections) + "\n\n" if sections else ""

# ─── Tool Schemas (for Claude API) ───────────────────────────────────────────

TOOL_WEB_SEARCH = {
    "name": "web_search",
    "description": (
        "Search the web for sports news, injury reports, team form, match previews, "
        "and upcoming event schedules. Returns a list of results with title, url, and content.\n"
        "TIP: For injury/lineup/breaking news queries, set topic='news' and time_range='day' "
        "to get the most recent articles from the last 24 hours."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query string",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5)",
                "default": 5,
            },
            "topic": {
                "type": "string",
                "description": (
                    "Search topic filter. Use 'news' for injury reports, lineup news, "
                    "and breaking updates (returns recent news articles). "
                    "Use 'general' (default) for form, H2H, and historical context."
                ),
                "enum": ["general", "news", "finance"],
                "default": "general",
            },
            "time_range": {
                "type": "string",
                "description": (
                    "Filter results by recency. 'day' = last 24h (best for injury/lineup news), "
                    "'week' = last 7 days, 'month' = last 30 days. Leave empty for no filter."
                ),
                "enum": ["day", "week", "month", "year"],
            },
        },
        "required": ["query"],
    },
}

TOOL_GET_SHARP_ODDS = {
    "name": "get_sharp_odds",
    "description": (
        "Fetch devigged true probabilities from sharp sportsbooks (Pinnacle, Betfair) "
        "for a specific matchup via The Odds API. Returns consensus home/away probabilities."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sport": {
                "type": "string",
                "description": "Odds API sport key, e.g. basketball_nba, americanfootball_nfl, soccer_epl",
            },
            "home_team": {"type": "string", "description": "Home team name"},
            "away_team": {"type": "string", "description": "Away team name"},
        },
        "required": ["sport", "home_team", "away_team"],
    },
}

TOOL_GET_SHARP_ODDS_TOTALS = {
    "name": "get_sharp_odds_totals",
    "description": (
        "Fetch devigged Over/Under probabilities from sharp sportsbooks (Pinnacle, Betfair) "
        "for a matchup's totals (O/U) line via The Odds API. "
        "Returns over_prob, under_prob, ou_line, and books_used."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sport": {
                "type": "string",
                "description": "Odds API sport key, e.g. basketball_nba, americanfootball_nfl",
            },
            "home_team": {"type": "string", "description": "Home team name"},
            "away_team": {"type": "string", "description": "Away team name"},
        },
        "required": ["sport", "home_team", "away_team"],
    },
}

TOOL_GET_POLYMARKET = {
    "name": "get_polymarket_market",
    "description": (
        "Search Polymarket for an active market matching these teams. "
        "Returns market slug, outcome prices, and volume if found."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "home_team": {"type": "string"},
            "away_team": {"type": "string"},
            "sport": {
                "type": "string",
                "description": "Sport key for context (optional)",
                "default": "",
            },
        },
        "required": ["home_team", "away_team"],
    },
}

TOOL_GET_INJURIES = {
    "name": "get_injury_report",
    "description": (
        "Fetch today's official injury report for a team from ESPN. "
        "Returns a list of injured/questionable players with status and expected return date. "
        "Use this for every team in every matchup you research."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_name": {
                "type": "string",
                "description": "Team name, e.g. 'Houston Rockets', 'Rockets', 'Lakers'",
            },
            "sport": {
                "type": "string",
                "description": "Sport key: basketball_nba | americanfootball_nfl | icehockey_nhl | baseball_mlb",
            },
        },
        "required": ["team_name", "sport"],
    },
}

TOOL_GET_NBA_GAME_LOG = {
    "name": "get_nba_game_log",
    "description": (
        "Fetch the last N NBA game results for a team from the official NBA Stats API. "
        "Returns structured form data: date, opponent, home/away, W/L, points scored. "
        "Use this for ALL NBA recent form — one call per team, faster and more reliable than web_search. "
        "NBA only — for EPL/UCL/MMA/NFL form, use web_search instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_name": {
                "type": "string",
                "description": "NBA team name, e.g. 'Houston Rockets', 'Rockets', 'Lakers'",
            },
            "num_games": {
                "type": "integer",
                "description": "Number of recent games to return (default 5)",
                "default": 5,
            },
        },
        "required": ["team_name"],
    },
}

TOOL_GET_RECENT_RESULTS = {
    "name": "get_recent_results",
    "description": (
        "Fetch recent completed game results for a team from The Odds API scores endpoint. "
        "Covers all sports: EPL, UCL, MMA, NFL, NHL, MLB (and NBA — but prefer get_nba_game_log for NBA). "
        "Returns date, home team, away team, final score. Looks back up to 3 days. "
        "Results for the same sport are cached — calling it for both teams in a matchup "
        "only costs one API request. Fall back to web_search if 0 results returned."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sport": {
                "type": "string",
                "description": (
                    "Odds API sport key: soccer_epl | soccer_uefa_champs_league | "
                    "mma_mixed_martial_arts | americanfootball_nfl | icehockey_nhl | baseball_mlb"
                ),
            },
            "team_name": {
                "type": "string",
                "description": "Team or fighter name to filter results for",
            },
            "num_games": {
                "type": "integer",
                "description": "Max results to return (default 3)",
                "default": 3,
            },
        },
        "required": ["sport", "team_name"],
    },
}

TOOL_GET_OPEN_TRADES = {
    "name": "get_open_trades",
    "description": (
        "Read all currently open bets from the database. "
        "Returns trade IDs, selections, leagues, amounts, entry prices, and timestamps. "
        "Always call this first when the user asks about open positions or current bets."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

TOOL_SETTLE_TRADE = {
    "name": "settle_trade",
    "description": (
        "Force-settle a specific trade as WIN or LOSS and write the result to the database. "
        "Use this when the user tells you a game result, or when Polymarket hasn't updated yet "
        "but the outcome is known. Calculates PnL automatically from the entry price. "
        "IMPORTANT: Call get_open_trades first to confirm the trade_id before settling."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "trade_id": {
                "type": "integer",
                "description": "Numeric ID of the trade to settle (get it from get_open_trades)",
            },
            "outcome": {
                "type": "string",
                "enum": ["WIN", "LOSS"],
                "description": "WIN if the bet won, LOSS if it lost",
            },
        },
        "required": ["trade_id", "outcome"],
    },
}

TOOL_GET_LIVE_SCORES = {
    "name": "get_live_scores",
    "description": (
        "Fetch in-progress game scores from The Odds API. Returns games from the last 24h "
        "that are still live (not yet completed but scores are populated). "
        "Use when the user asks 'what's the score?' or 'how's my open bet doing?'. "
        "If sport is omitted, queries all tracked sports."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sport": {
                "type": "string",
                "description": (
                    "Odds API sport key, e.g. basketball_nba, soccer_epl. "
                    "Leave empty to query all tracked sports."
                ),
                "default": "",
            },
        },
        "required": [],
    },
}

TOOL_WRITE_LESSON = {
    "name": "write_lesson",
    "description": (
        "Append a new row to the LEARNINGS.md mistake log. "
        "Call this when you notice a clear pattern across this batch "
        "(e.g. all 5 skips were lumi_abort, edge was consistently 2-3%). "
        "Do NOT call for a single observation — minimum 3 events needed for a pattern."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Which agent or pipeline stage: Max, Nova, Lumi, Sage, or Pipeline",
            },
            "what_went_wrong": {
                "type": "string",
                "description": "1-2 sentences describing the pattern observed",
            },
            "correction": {
                "type": "string",
                "description": "What should be done differently",
            },
            "rule_generated": {
                "type": "string",
                "description": "Actionable rule in imperative form, e.g. 'Always X when Y'",
            },
        },
        "required": ["agent_name", "what_went_wrong", "correction", "rule_generated"],
    },
}

TOOL_UPDATE_BRAIN = {
    "name": "update_brain",
    "description": (
        "Update a named section in agents/BRAIN.md. "
        "Use this to revise 'Current Focus' or 'Active Hypotheses' when this batch "
        "changes your view on what the team should be tracking."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "description": "Exact section header name, e.g. 'Current Focus', 'Active Hypotheses'",
            },
            "content": {
                "type": "string",
                "description": "New markdown content to replace the section (bullet list format)",
            },
        },
        "required": ["section", "content"],
    },
}

TOOL_GET_API_FOOTBALL_H2H = {
    "name": "get_api_football_h2h",
    "description": (
        "Fetch the last 5-10 head-to-head results between two soccer teams from API-Football. "
        "Returns date, venue, score, and winner for each meeting. "
        "Use this for ALL soccer H2H lookups (EPL, UCL, MLS, La Liga, etc.) before falling back to web_search. "
        "Requires API_FOOTBALL_KEY environment variable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "home_team": {"type": "string", "description": "Home team name, e.g. 'Arsenal', 'Manchester City'"},
            "away_team": {"type": "string", "description": "Away team name, e.g. 'Chelsea', 'Liverpool'"},
        },
        "required": ["home_team", "away_team"],
    },
}

TOOL_GET_API_FOOTBALL_FORM = {
    "name": "get_api_football_form",
    "description": (
        "Fetch the last 5 fixtures for a soccer team from API-Football. "
        "Returns form string (WWDLL), goals_for, goals_against, and per-game details. "
        "Use this for ALL soccer recent form — faster and more reliable than web_search. "
        "Requires API_FOOTBALL_KEY environment variable."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "Team name, e.g. 'Arsenal', 'Real Madrid'"},
            "season": {
                "type": "integer",
                "description": "Season year (default 2025)",
                "default": 2025,
            },
        },
        "required": ["team_name"],
    },
}

TOOL_GET_SLEEPER_INJURIES = {
    "name": "get_sleeper_injuries",
    "description": (
        "Fetch injury status for all players on a team from the Sleeper API. "
        "Returns per-player injury_status, injury_body_part, news_updated, and practice_participation. "
        "Completely free, no API key needed. Covers NBA, NFL, NHL. "
        "Use this as a FIRST call for NBA/NFL/NHL injuries — more granular than ESPN. "
        "Sport slugs: nba | nfl | nhl"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "Team name, e.g. 'Lakers', 'Chiefs', 'Bruins'"},
            "sport": {
                "type": "string",
                "description": "Sport slug: nba | nfl | nhl",
                "default": "nba",
            },
        },
        "required": ["team_name"],
    },
}

TOOL_GET_SPORTSDB_H2H = {
    "name": "get_sportsdb_h2h",
    "description": (
        "Fetch the last 5 head-to-head results between two teams from TheSportsDB. "
        "Works multi-sport: NBA, soccer, NFL, NHL, MLB, MMA, etc. "
        "Completely free, no API key needed. "
        "Use this as a FALLBACK H2H for sports not covered by get_api_football_h2h (e.g. NBA, MMA)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "home_team": {"type": "string", "description": "Home team name"},
            "away_team": {"type": "string", "description": "Away team name"},
        },
        "required": ["home_team", "away_team"],
    },
}


TOOL_EXTRACT_URL = {
    "name": "extract_url",
    "description": (
        "Fetch and read the full text content of a specific URL using Tavily Extract. "
        "Use this when a web_search result returns a promising article URL and the snippet "
        "is too short to get the full picture — e.g. an ESPN injury report, a beat reporter's "
        "article, or a team news page. Returns cleaned markdown content of the page.\n"
        "WHEN TO USE: after web_search returns a relevant URL, call extract_url to read the "
        "full article. Budget: no more than 5 calls per batch (each costs 1 Tavily credit).\n"
        "REQUIRES: TAVILY_API_KEY must be set. Returns empty content if not set."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to extract content from (must start with https://)",
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional: your research question. Tavily uses this to rank and filter "
                    "the most relevant chunks from the page. E.g. 'Is Jaylen Brown playing tonight?'"
                ),
            },
        },
        "required": ["url"],
    },
}


def extract_url(url: str, query: str = "") -> dict:
    """
    Fetch full text content of a URL using Tavily Extract API.
    Returns cleaned markdown content, or an error dict if unavailable.
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return {"found": False, "error": "TAVILY_API_KEY not set — extract_url unavailable"}

    try:
        payload = {
            "urls":          [url],
            "extract_depth": "basic",
            "format":        "markdown",
        }
        if query:
            payload["query"]             = query
            payload["chunks_per_source"] = 3  # return 3 most relevant chunks

        r = requests.post(
            "https://api.tavily.com/extract",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        results = data.get("results", [])
        if not results:
            failed = data.get("failed_results", [])
            reason = failed[0].get("error", "no content returned") if failed else "no content returned"
            return {"found": False, "url": url, "error": reason}

        content = results[0].get("raw_content", "") or ""
        return {
            "found":   True,
            "url":     url,
            "content": content[:6000],  # cap at ~6k chars to avoid bloating context
        }
    except Exception as e:
        logger.debug(f"[TOOLS] extract_url failed for {url}: {e}")
        return {"found": False, "url": url, "error": str(e)}


# ─── Name matching ─────────────────────────────────────────────────────────────

def _names_match(a: str, b: str, threshold: float = 0.6) -> bool:
    """
    Fuzzy team name match. Handles short vs full names:
    "Rockets" matches "Houston Rockets", "76ers" matches "Philadelphia 76ers".
    """
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    # Substring containment (handles "Rockets" ↔ "Houston Rockets")
    if a in b or b in a:
        return True
    # Last-word match (team nickname without city)
    a_last = a.split()[-1] if a.split() else a
    b_last = b.split()[-1] if b.split() else b
    if len(a_last) > 3 and a_last == b_last:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


# ─── web_search ───────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5, topic: str = "general", time_range: str = "") -> list:
    """
    Search the web. Uses Tavily if TAVILY_API_KEY is set, else DuckDuckGo.

    Args:
        query:      Search query string.
        max_results: Max results to return (default 5).
        topic:      'news' for breaking injury/lineup articles, 'general' for historical context.
        time_range: 'day' | 'week' | 'month' | 'year' — filter by recency (Tavily only).
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if api_key:
        results = _tavily_search(query, max_results, api_key, topic=topic, time_range=time_range)
        if results:
            return results
        logger.debug("[TOOLS] Tavily failed, falling back to DuckDuckGo")
    else:
        logger.debug("[TOOLS] TAVILY_API_KEY not set, using DuckDuckGo")
    return _ddg_search(query, max_results)


def _tavily_search(query: str, max_results: int, api_key: str, topic: str = "general", time_range: str = "") -> list:
    try:
        payload = {
            "api_key":     api_key,
            "query":       query,
            "max_results": max_results,
            "topic":       topic,
        }
        if time_range:
            payload["time_range"] = time_range
        r = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title":   item.get("title", ""),
                "url":     item.get("url", ""),
                "content": (item.get("content", "") or "")[:800],
            })
        return results
    except Exception as e:
        logger.debug(f"[TOOLS] Tavily error: {e}")
        return []


def _ddg_search(query: str, max_results: int) -> list:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        # Session + homepage warmup avoids DuckDuckGo's 202 challenge response
        session = requests.Session()
        session.get("https://duckduckgo.com/", headers=headers, timeout=10)
        time.sleep(0.5)
        r = session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=headers,
            timeout=12,
        )
        if r.status_code != 200:
            logger.debug(f"[TOOLS] DDG returned status {r.status_code}")
            return []
        clean = lambda s: re.sub(r"<[^>]+>", "", s).strip()
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text, re.DOTALL)
        # DDG uses <a class="result__snippet"> — closes with </a>
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
        results = []
        for i in range(min(max_results, len(titles))):
            results.append({
                "title": clean(titles[i]),
                "url": "",
                "content": clean(snippets[i]) if i < len(snippets) else "",
            })
        return results
    except Exception as e:
        logger.debug(f"[TOOLS] DuckDuckGo fallback failed: {e}")
        return [{"title": "Search unavailable", "url": "", "content": f"Error: {e}"}]


# ─── get_nba_game_log ────────────────────────────────────────────────────────

_NBA_TEAM_IDS = {
    "atlanta hawks": 1610612737,      "hawks": 1610612737,
    "boston celtics": 1610612738,     "celtics": 1610612738,
    "brooklyn nets": 1610612751,      "nets": 1610612751,
    "charlotte hornets": 1610612766,  "hornets": 1610612766,
    "chicago bulls": 1610612741,      "bulls": 1610612741,
    "cleveland cavaliers": 1610612739,"cavaliers": 1610612739, "cavs": 1610612739,
    "dallas mavericks": 1610612742,   "mavericks": 1610612742, "mavs": 1610612742,
    "denver nuggets": 1610612743,     "nuggets": 1610612743,
    "detroit pistons": 1610612765,    "pistons": 1610612765,
    "golden state warriors": 1610612744, "warriors": 1610612744,
    "houston rockets": 1610612745,    "rockets": 1610612745,
    "indiana pacers": 1610612754,     "pacers": 1610612754,
    "los angeles clippers": 1610612746, "clippers": 1610612746, "la clippers": 1610612746,
    "los angeles lakers": 1610612747, "lakers": 1610612747,    "la lakers": 1610612747,
    "memphis grizzlies": 1610612763,  "grizzlies": 1610612763,
    "miami heat": 1610612748,         "heat": 1610612748,
    "milwaukee bucks": 1610612749,    "bucks": 1610612749,
    "minnesota timberwolves": 1610612750, "timberwolves": 1610612750, "wolves": 1610612750,
    "new orleans pelicans": 1610612740, "pelicans": 1610612740,
    "new york knicks": 1610612752,    "knicks": 1610612752,
    "oklahoma city thunder": 1610612760, "thunder": 1610612760, "okc": 1610612760,
    "orlando magic": 1610612753,      "magic": 1610612753,
    "philadelphia 76ers": 1610612755, "76ers": 1610612755,     "sixers": 1610612755,
    "phoenix suns": 1610612756,       "suns": 1610612756,
    "portland trail blazers": 1610612757, "trail blazers": 1610612757, "blazers": 1610612757,
    "sacramento kings": 1610612758,   "kings": 1610612758,
    "san antonio spurs": 1610612759,  "spurs": 1610612759,
    "toronto raptors": 1610612761,    "raptors": 1610612761,
    "utah jazz": 1610612762,          "jazz": 1610612762,
    "washington wizards": 1610612764, "wizards": 1610612764,
}


def _get_nba_season() -> str:
    """Returns current NBA season string, e.g. '2025-26'. Season starts in October."""
    now = datetime.now(timezone.utc)
    year = now.year
    if now.month < 10:
        return f"{year - 1}-{str(year)[2:]}"
    return f"{year}-{str(year + 1)[2:]}"


def get_nba_game_log(team_name: str, num_games: int = 5) -> dict:
    """
    Fetch last N completed game results for an NBA team from the official NBA Stats API.
    Returns structured form data: date, opponent, home/away, W/L, points.
    No API key required — free public endpoint.
    """
    team_key = team_name.lower().strip()
    team_id = _NBA_TEAM_IDS.get(team_key)

    if not team_id:
        # Fuzzy fallback
        for name, tid in _NBA_TEAM_IDS.items():
            if _names_match(team_key, name):
                team_id = tid
                break

    if not team_id:
        return {"found": False, "error": f"Unknown NBA team: {team_name!r}"}

    season = _get_nba_season()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://stats.nba.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://stats.nba.com",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true",
    }

    try:
        r = requests.get(
            "https://stats.nba.com/stats/teamgamelog",
            params={
                "TeamID": team_id,
                "Season": season,
                "SeasonType": "Regular Season",
                "LeagueID": "00",
            },
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug(f"[TOOLS] NBA game log failed for {team_name}: {e}")
        return {"found": False, "error": str(e)}

    result_sets = data.get("resultSets", [])
    if not result_sets:
        return {"found": False, "error": "Empty NBA API response"}

    col_headers = result_sets[0].get("headers", [])
    rows = result_sets[0].get("rowSet", [])
    if not rows:
        return {"found": False, "error": "No game log rows returned"}

    try:
        idx_date      = col_headers.index("GAME_DATE")
        idx_matchup   = col_headers.index("MATCHUP")
        idx_wl        = col_headers.index("WL")
        idx_pts       = col_headers.index("PTS")
        idx_plusminus = col_headers.index("PLUS_MINUS") if "PLUS_MINUS" in col_headers else None
    except ValueError as e:
        return {"found": False, "error": f"Unexpected API columns: {e}"}

    games = []
    for row in rows[:num_games]:
        matchup   = row[idx_matchup]  # e.g. "HOU vs. LAL" or "HOU @ LAL"
        is_home   = " vs. " in matchup
        opponent  = matchup.split(" vs. ")[-1] if is_home else matchup.split(" @ ")[-1]
        pts = row[idx_pts]
        # Compute opponent pts and combined total via PLUS_MINUS:
        # PLUS_MINUS = team_pts - opp_pts  →  opp_pts = team_pts - PLUS_MINUS
        opp_pts = None
        combined_total = None
        if idx_plusminus is not None:
            try:
                pm = float(row[idx_plusminus])
                opp_pts = int(round(float(pts) - pm))
                combined_total = int(pts) + opp_pts
            except (TypeError, ValueError):
                pass
        games.append({
            "date":           row[idx_date],
            "matchup":        matchup,
            "home_away":      "home" if is_home else "away",
            "opponent":       opponent.strip(),
            "result":         row[idx_wl],
            "pts_scored":     pts,
            "opp_pts":        opp_pts,
            "combined_total": combined_total,
        })

    wins   = sum(1 for g in games if g["result"] == "W")
    losses = len(games) - wins
    return {
        "found":         True,
        "team":          team_name,
        "season":        season,
        "record_last_n": f"{wins}W-{losses}L",
        "games":         games,
    }


# ─── get_recent_results ──────────────────────────────────────────────────────

def get_recent_results(sport: str, team_name: str, num_games: int = 3) -> dict:
    """
    Fetch recent completed game results for a team from The Odds API scores endpoint.
    Results per sport are cached (30-minute TTL) — multiple team lookups in the same
    sport only cost one API request, and results survive across consecutive batches.
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"found": False, "error": "ODDS_API_KEY not configured"}

    # ── Fetch (or reuse cached) all results for this sport ───────────────────
    with _scores_cache_lock:
        all_events = _scores_cache.get(sport)

    if all_events is None:
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/scores/",
                params={"apiKey": api_key, "daysFrom": 3, "dateFormat": "iso"},
                timeout=15,
            )
            r.raise_for_status()
            all_events = r.json()
            with _scores_cache_lock:
                _scores_cache[sport] = all_events
            logger.debug(f"[TOOLS] Scores cache populated for {sport}: {len(all_events)} events")
        except requests.exceptions.HTTPError as e:
            code = e.response.status_code if e.response else 0
            if code == 422:
                return {"found": False, "error": f"Sport '{sport}' not available on scores endpoint"}
            return {"found": False, "error": str(e)}
        except Exception as e:
            return {"found": False, "error": str(e)}
    else:
        logger.debug(f"[TOOLS] Scores cache hit for {sport}")

    # ── Filter to completed games involving this team ────────────────────────
    matches = []
    for event in all_events:
        if not event.get("completed"):
            continue
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not (_names_match(team_name, home) or _names_match(team_name, away)):
            continue

        scores = event.get("scores") or []
        score_map = {s["name"]: s["score"] for s in scores if s.get("name") and s.get("score")}
        home_score = score_map.get(home, "?")
        away_score = score_map.get(away, "?")

        # Determine result from this team's perspective
        try:
            h = float(home_score)
            a = float(away_score)
            team_is_home = _names_match(team_name, home)
            team_score = h if team_is_home else a
            opp_score  = a if team_is_home else h
            result = "W" if team_score > opp_score else ("L" if team_score < opp_score else "D")
        except (ValueError, TypeError):
            result = "?"
            team_is_home = _names_match(team_name, home)

        opponent = away if _names_match(team_name, home) else home
        matches.append({
            "date":       event.get("commence_time", "")[:10],
            "home_team":  home,
            "away_team":  away,
            "score":      f"{home_score}-{away_score}",
            "home_away":  "home" if team_is_home else "away",
            "opponent":   opponent,
            "result":     result,
        })

    # Most recent first
    matches.sort(key=lambda x: x["date"], reverse=True)
    matches = matches[:num_games]

    if not matches:
        return {
            "found": False,
            "message": (
                f"No completed results for {team_name!r} in the past 3 days. "
                "Use web_search for longer-term form."
            ),
        }

    wins   = sum(1 for m in matches if m["result"] == "W")
    losses = sum(1 for m in matches if m["result"] == "L")
    draws  = sum(1 for m in matches if m["result"] == "D")
    record = f"{wins}W-{losses}L" + (f"-{draws}D" if draws else "")

    return {
        "found":         True,
        "team":          team_name,
        "sport":         sport,
        "record_last_n": record,
        "results":       matches,
        "note":          "3-day window only — use web_search for full 5-game form history",
    }


# ─── get_live_scores ─────────────────────────────────────────────────────────

# Sports queried when no specific sport is requested
_LIVE_SCORE_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "mma_mixed_martial_arts",
]


def get_live_scores(sport: str = "") -> dict:
    """
    Fetch in-progress game scores from The Odds API scores endpoint.
    Filters to games where completed is False but scores are non-null (game is live).
    Results cached with 2-minute TTL — live data changes.

    Args:
        sport: Odds API sport key. If empty, queries all _LIVE_SCORE_SPORTS.

    Returns:
        {
          "found": True/False,
          "live_games": [
            {
              "sport": "basketball_nba",
              "home_team": "...", "away_team": "...",
              "home_score": "...", "away_score": "...",
              "commence_time": "YYYY-MM-DD HH:MM",
              "status": "in_progress"
            }
          ],
          "count": N
        }
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"found": False, "error": "ODDS_API_KEY not configured", "live_games": [], "count": 0}

    sports_to_query = [sport] if sport else _LIVE_SCORE_SPORTS
    live_games = []

    for s in sports_to_query:
        with _live_scores_cache_lock:
            all_events = _live_scores_cache.get(s)

        if all_events is None:
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{s}/scores/",
                    params={"apiKey": api_key, "daysFrom": 1, "dateFormat": "iso"},
                    timeout=15,
                )
                r.raise_for_status()
                all_events = r.json()
                with _live_scores_cache_lock:
                    _live_scores_cache[s] = all_events
                logger.debug(f"[TOOLS] Live scores fetched for {s}: {len(all_events)} events")
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code if e.response else 0
                if code == 422:
                    logger.debug(f"[TOOLS] Sport '{s}' not available on scores endpoint")
                    continue
                logger.debug(f"[TOOLS] Live scores HTTP error for {s}: {e}")
                continue
            except Exception as e:
                logger.debug(f"[TOOLS] Live scores error for {s}: {e}")
                continue
        else:
            logger.debug(f"[TOOLS] Live scores cache hit for {s}")

        # In-progress = not completed AND scores are populated
        for event in all_events:
            if event.get("completed"):
                continue
            scores = event.get("scores") or []
            if not scores:
                continue  # Not started yet

            home = event.get("home_team", "")
            away = event.get("away_team", "")
            score_map = {
                sc["name"]: sc["score"]
                for sc in scores
                if sc.get("name") and sc.get("score") is not None
            }
            home_score = score_map.get(home, "?")
            away_score = score_map.get(away, "?")

            live_games.append({
                "sport":          s,
                "home_team":      home,
                "away_team":      away,
                "home_score":     home_score,
                "away_score":     away_score,
                "commence_time":  event.get("commence_time", "")[:16].replace("T", " "),
                "status":         "in_progress",
            })

    if not live_games:
        return {
            "found":      False,
            "message":    "No in-progress games found right now.",
            "live_games": [],
            "count":      0,
        }

    return {
        "found":      True,
        "live_games": live_games,
        "count":      len(live_games),
    }


# ─── get_sharp_odds ──────────────────────────────────────────────────────────

SHARP_BOOKS = ["pinnacle", "betfair_ex_eu", "betfair_ex_au", "sport888", "draftkings", "fanduel"]


def get_sharp_odds(sport: str, home_team: str, away_team: str) -> dict:
    """Fetch devigged sharp book probabilities for a matchup."""
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"error": "ODDS_API_KEY not configured", "found": False, "books": []}

    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
            params={
                "apiKey": api_key,
                "regions": "us,uk,eu,au",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "bookmakers": ",".join(SHARP_BOOKS),
            },
            timeout=15,
        )
        r.raise_for_status()
        events = r.json()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else 0
        if code == 422:
            return {"error": f"Sport '{sport}' not available", "found": False, "books": []}
        return {"error": str(e), "found": False, "books": []}
    except Exception as e:
        return {"error": str(e), "found": False, "books": []}

    # Find matching event
    target = None
    swapped = False
    for event in events:
        h = event.get("home_team", "")
        a = event.get("away_team", "")
        if _names_match(h, home_team) and _names_match(a, away_team):
            target = event
            break
        if _names_match(a, home_team) and _names_match(h, away_team):
            target = event
            swapped = True
            break

    if not target:
        return {
            "found": False,
            "message": f"No event found for {home_team} vs {away_team} in {sport}",
            "books": [],
        }

    actual_home = target["away_team"] if swapped else target["home_team"]
    actual_away = target["home_team"] if swapped else target["away_team"]

    # Parse odds per book
    books = []
    for bm in target.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            raw_probs = [1.0 / o["price"] for o in outcomes if o.get("price", 0) > 0]
            total_raw = sum(raw_probs)
            if total_raw <= 0:
                continue
            probs = {}
            for outcome in outcomes:
                if outcome.get("price", 0) <= 0:
                    continue
                probs[outcome["name"]] = round((1.0 / outcome["price"]) / total_raw, 4)
            books.append({"book": bm["key"], "probs": probs})

    if not books:
        return {
            "found": True,
            "event_id": target["id"],
            "home_team": actual_home,
            "away_team": actual_away,
            "commence_time": target.get("commence_time", ""),
            "message": "Event found but no sharp book odds available",
            "books": [],
        }

    # Consensus: average across books
    home_probs = [b["probs"].get(actual_home, 0) for b in books if actual_home in b["probs"]]
    away_probs = [b["probs"].get(actual_away, 0) for b in books if actual_away in b["probs"]]

    # If exact name doesn't match, try fuzzy
    if not home_probs:
        for b in books:
            for name, prob in b["probs"].items():
                if _names_match(name, actual_home, 0.5):
                    home_probs.append(prob)
                elif _names_match(name, actual_away, 0.5):
                    away_probs.append(prob)

    home_consensus = round(sum(home_probs) / len(home_probs), 4) if home_probs else 0.0
    away_consensus = round(sum(away_probs) / len(away_probs), 4) if away_probs else 0.0

    return {
        "found": True,
        "event_id": target["id"],
        "home_team": actual_home,
        "away_team": actual_away,
        "commence_time": target.get("commence_time", ""),
        "books": books,
        "consensus": {
            "home_team": actual_home,
            "away_team": actual_away,
            "home_prob": home_consensus,
            "away_prob": away_consensus,
            "books_used": len(books),
        },
    }


# ─── get_sharp_odds_totals ────────────────────────────────────────────────────

def get_sharp_odds_totals(sport: str, home_team: str, away_team: str) -> dict:
    """
    Fetch devigged Over/Under probabilities from sharp sportsbooks for a matchup.
    Uses The Odds API with markets=totals.
    Totals outcomes: {"name": "Over", "price": 1.91, "point": 221.5}
    Returns: {found, over_prob, under_prob, ou_line, books_used}
    """
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"found": False, "error": "ODDS_API_KEY not configured", "books_used": 0}

    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
            params={
                "apiKey": api_key,
                "regions": "us,uk,eu,au",
                "markets": "totals",
                "oddsFormat": "decimal",
                "bookmakers": ",".join(SHARP_BOOKS),
            },
            timeout=15,
        )
        r.raise_for_status()
        events = r.json()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else 0
        if code == 422:
            return {"found": False, "error": f"Sport '{sport}' totals not available", "books_used": 0}
        return {"found": False, "error": str(e), "books_used": 0}
    except Exception as e:
        return {"found": False, "error": str(e), "books_used": 0}

    # Find matching event
    target = None
    for event in events:
        h = event.get("home_team", "")
        a = event.get("away_team", "")
        if (_names_match(h, home_team) and _names_match(a, away_team)) or \
           (_names_match(a, home_team) and _names_match(h, away_team)):
            target = event
            break

    if not target:
        return {
            "found": False,
            "message": f"No totals line found for {home_team} vs {away_team} in {sport}",
            "books_used": 0,
        }

    # Parse Over/Under per book — devig
    over_probs = []
    under_probs = []
    ou_line = None

    for bm in target.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "totals":
                continue
            outcomes = market.get("outcomes", [])
            raw_probs = [1.0 / o["price"] for o in outcomes if o.get("price", 0) > 0]
            total_raw = sum(raw_probs)
            if total_raw <= 0:
                continue
            for o in outcomes:
                if o.get("price", 0) <= 0:
                    continue
                devigged = round((1.0 / o["price"]) / total_raw, 4)
                name = o.get("name", "").lower()
                if "over" in name:
                    over_probs.append(devigged)
                    if ou_line is None and o.get("point"):
                        ou_line = float(o["point"])
                elif "under" in name:
                    under_probs.append(devigged)

    if not over_probs or not under_probs:
        return {
            "found": False,
            "message": "Event found but no sharp totals odds available",
            "books_used": 0,
        }

    over_consensus  = round(sum(over_probs)  / len(over_probs),  4)
    under_consensus = round(sum(under_probs) / len(under_probs), 4)

    return {
        "found":      True,
        "over_prob":  over_consensus,
        "under_prob": under_consensus,
        "ou_line":    ou_line,
        "books_used": len(over_probs),
    }


# ─── get_polymarket_market ────────────────────────────────────────────────────

def get_polymarket_market(home_team: str, away_team: str, sport: str = "") -> dict:
    """Search Polymarket Gamma API for an active market matching these teams."""
    base = "https://gamma-api.polymarket.com"
    session = requests.Session()
    session.headers["User-Agent"] = "SportsBettingBot/1.0"

    search_terms = [
        f"{home_team} {away_team}",
        home_team,
        away_team,
    ]

    for term in search_terms:
        try:
            r = session.get(
                f"{base}/markets",
                params={"q": term, "limit": 10, "active": "true", "closed": "false"},
                timeout=10,
            )
            r.raise_for_status()
            markets = r.json()

            for m in markets:
                # Parse prices
                prices_raw = m.get("outcomePrices", "")
                if isinstance(prices_raw, str):
                    try:
                        prices_raw = json.loads(prices_raw)
                    except Exception:
                        continue
                prices = [float(p) for p in prices_raw] if prices_raw else []
                if not prices:
                    continue

                # Parse outcomes
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

                # Check if teams appear in question
                q = m.get("question", "").lower()
                h_tokens = [t for t in home_team.lower().split() if len(t) > 3]
                a_tokens = [t for t in away_team.lower().split() if len(t) > 3]
                home_match = any(t in q for t in h_tokens)
                away_match = any(t in q for t in a_tokens)
                if not (home_match or away_match):
                    continue

                # Find home/away prices by outcome name matching
                home_price = None
                away_price = None
                for i, outcome in enumerate(outcomes):
                    if i >= len(prices):
                        break
                    if _names_match(outcome, home_team, 0.45):
                        home_price = prices[i]
                    elif _names_match(outcome, away_team, 0.45):
                        away_price = prices[i]

                return {
                    "found": True,
                    "slug": m.get("slug", ""),
                    "question": m.get("question", ""),
                    "outcomes": outcomes,
                    "prices": prices,
                    "home_price": home_price,
                    "away_price": away_price,
                    "volume": float(m.get("volume", 0) or 0),
                }

            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"[TOOLS] Polymarket search failed for '{term}': {e}")

    return {
        "found": False,
        "message": f"No active Polymarket market found for {home_team} vs {away_team}",
    }


# ─── fetch_polymarket_events ──────────────────────────────────────────────────

# Polymarket tag IDs per sport (from GET /sports endpoint).
# NBA/NFL/NHL removed — confirmed structurally efficient, excluded from pipeline.
_SPORT_TAGS = {
    "EPL":   "82",
    "UCL":   "306",      # UEFA Champions League
    "MLB":   "100381",
    "MMA":   "100639",   # generic sports tag — catches UFC/boxing/etc.
}


def fetch_polymarket_events(hours_ahead: int = 48) -> list:
    """
    Fetch all upcoming sports events from Polymarket's Gamma API.

    Uses the /events endpoint with sport-specific tag IDs — the only reliable
    way to find individual game markets (moneyline, spread) as opposed to
    season-long futures. Verified to surface e.g. Rockets vs. Hornets.

    Returns a list of dicts sorted by volume (highest first). Each dict:
      league, title, end_date, team_a, team_b, markets (list of market dicts),
      total_volume, moneyline_prices (dict team→price), slug
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours_ahead)

    all_events = []
    seen_titles = set()

    for league, tag_id in _SPORT_TAGS.items():
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/events",
                params={"tag_id": tag_id, "limit": 100, "active": "true", "closed": "false"},
                headers={"User-Agent": "SportsBettingBot/1.0"},
                timeout=15,
            )
            r.raise_for_status()
            raw_events = r.json()
        except Exception as e:
            logger.debug(f"[TOOLS] fetch_polymarket_events: {league} fetch failed: {e}")
            continue

        for e in raw_events:
            title = e.get("title", "")
            end_str = e.get("endDate", "")

            # Skip dupes (same event can appear under multiple tags)
            if title in seen_titles:
                continue

            # Timing filter: skip past events and far-future (futures/season markets)
            if end_str:
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt < now:
                        continue
                    if end_dt > cutoff:
                        continue
                except Exception:
                    pass
            else:
                continue  # no date = skip

            seen_titles.add(title)
            markets_raw = e.get("markets", [])

            # Aggregate volume across all markets in this event
            total_vol = sum(float(m.get("volume", 0) or 0) for m in markets_raw)

            # Find the moneyline (h2h) market — the one with exactly 2 outcomes (team names)
            moneyline = None
            moneyline_prices = {}
            slug = ""
            home_win_slug = ""   # EPL: per-team Yes/No win market slug
            away_win_slug = ""   # EPL: per-team Yes/No win market slug
            for m in markets_raw:
                outs_raw = m.get("outcomes", "[]")
                if isinstance(outs_raw, str):
                    try:
                        outs = json.loads(outs_raw)
                    except Exception:
                        continue
                else:
                    outs = outs_raw

                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    try:
                        prices_list = [float(p) for p in json.loads(prices_raw)]
                    except Exception:
                        continue
                else:
                    prices_list = [float(p) for p in prices_raw]

                if len(outs) == 2 and len(prices_list) == 2:
                    # Simple team vs team = moneyline
                    q_lower = m.get("question", "").lower()
                    if "spread" not in q_lower and "o/u" not in q_lower and "over" not in q_lower:
                        moneyline = m
                        moneyline_prices = dict(zip(outs, prices_list))
                        slug = m.get("slug", "")
                        break

            # ── EPL-specific: parse Yes/No win markets ────────────────────────────
            # EPL uses 3 separate binary markets ("Will [team] win?", draw?)
            # rather than a standard team-vs-team binary. Extract the Yes price
            # from each team's win market and synthesize a moneyline_prices dict.
            if league == "EPL" and not moneyline_prices and " vs. " in title:
                home_team_name = title.split(" vs. ", 1)[0].strip()
                away_team_name = title.split(" vs. ", 1)[1].strip()
                _home_yes_price = None
                _away_yes_price = None
                for m in markets_raw:
                    q = m.get("question", "")
                    q_lower = q.lower()
                    if "draw" in q_lower or "end in" in q_lower:
                        continue
                    outs_raw_m = m.get("outcomes", "[]")
                    if isinstance(outs_raw_m, str):
                        try:
                            outs_m = json.loads(outs_raw_m)
                        except Exception:
                            continue
                    else:
                        outs_m = list(outs_raw_m)
                    if "Yes" not in outs_m:
                        continue
                    prices_raw_m = m.get("outcomePrices", "[]")
                    if isinstance(prices_raw_m, str):
                        try:
                            pl_m = [float(p) for p in json.loads(prices_raw_m)]
                        except Exception:
                            continue
                    else:
                        pl_m = [float(p) for p in prices_raw_m]
                    if len(pl_m) != len(outs_m):
                        continue
                    yes_idx = outs_m.index("Yes")
                    yes_price = pl_m[yes_idx]
                    m_slug = m.get("slug", "")
                    if home_team_name.lower() in q_lower:
                        _home_yes_price = yes_price
                        home_win_slug = m_slug
                    elif away_team_name.lower() in q_lower:
                        _away_yes_price = yes_price
                        away_win_slug = m_slug
                if _home_yes_price is not None and _away_yes_price is not None:
                    moneyline_prices = {home_team_name: _home_yes_price, away_team_name: _away_yes_price}
                    slug = home_win_slug  # arbitrary default; Nova overrides per edge direction

            if not moneyline_prices and markets_raw:
                # Fallback: just use first market's outcomes
                m0 = markets_raw[0]
                outs_raw = m0.get("outcomes", "[]")
                if isinstance(outs_raw, str):
                    try:
                        outs = json.loads(outs_raw)
                    except Exception:
                        outs = []
                else:
                    outs = outs_raw
                prices_raw = m0.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    try:
                        pl = [float(p) for p in json.loads(prices_raw)]
                    except Exception:
                        pl = []
                else:
                    pl = [float(p) for p in prices_raw]
                moneyline_prices = dict(zip(outs, pl))
                slug = m0.get("slug", "")

            # ── Totals market extraction ─────────────────────────────────────
            totals_prices = {}
            totals_slug = ""
            ou_line = None
            for m in markets_raw:
                q_t = m.get("question", "").lower()
                if "over" not in q_t and "o/u" not in q_t and "under" not in q_t:
                    continue
                outs_raw = m.get("outcomes", "[]")
                if isinstance(outs_raw, str):
                    try:
                        t_outs = json.loads(outs_raw)
                    except Exception:
                        continue
                else:
                    t_outs = outs_raw
                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    try:
                        t_prices = [float(p) for p in json.loads(prices_raw)]
                    except Exception:
                        continue
                else:
                    t_prices = [float(p) for p in prices_raw]
                if len(t_outs) == 2 and len(t_prices) == 2:
                    totals_prices = dict(zip(t_outs, t_prices))
                    totals_slug = m.get("slug", "")
                    # Extract O/U line from outcome labels or question
                    for o in t_outs:
                        m2 = re.search(r"(\d+\.?\d*)", o)
                        if m2:
                            ou_line = float(m2.group(1))
                            break
                    if ou_line is None:
                        m2 = re.search(r"(\d+\.?\d*)", q_t)
                        if m2:
                            ou_line = float(m2.group(1))
                    break

            # Reject non-matchup markets: any event where "team" names are
            # generic outcome words (Yes/No/Over/Under) is a prop or futures
            # market that slipped through — not a real matchup.
            _NON_TEAM_WORDS = {"yes", "no", "over", "under", "draw", "other"}
            teams = list(moneyline_prices.keys())
            if any(t.strip().lower() in _NON_TEAM_WORDS for t in teams):
                continue

            event_dict = {
                "league":           league,
                "title":            title,
                "end_date":         end_str[:16],
                "team_a":           teams[0] if len(teams) > 0 else "",
                "team_b":           teams[1] if len(teams) > 1 else "",
                "moneyline_prices": moneyline_prices,
                "total_volume":     total_vol,
                "slug":             slug,
                "totals_prices":    totals_prices,
                "totals_slug":      totals_slug,
                "ou_line":          ou_line,
                "home_win_slug":    home_win_slug,
                "away_win_slug":    away_win_slug,
                "markets":          markets_raw,
            }
            all_events.append(event_dict)

    # Sort by total volume — highest volume = most important game
    all_events.sort(key=lambda x: x["total_volume"], reverse=True)
    logger.info(f"[TOOLS] fetch_polymarket_events: {len(all_events)} upcoming games found")
    return all_events


# ─── get_injury_report ────────────────────────────────────────────────────────

# Session-level player→team cache for cross-call corruption detection.
# Maps player_name → first team_name that claimed them this batch.
# Reset at the start of each runner batch via reset_injury_session().
_injury_session: dict = {}

# Scores cache: sport_key → list of raw event dicts from Odds API.
# TTLCache with 30-minute expiry — survives across batch boundaries to reduce
# API quota usage when the same sport is requested in consecutive batches.
_scores_cache: TTLCache = TTLCache(maxsize=32, ttl=1800)
_scores_cache_lock = threading.RLock()

# Live scores: 2-minute TTL — data changes during games.
_live_scores_cache: TTLCache = TTLCache(maxsize=16, ttl=120)
_live_scores_cache_lock = threading.RLock()

# API-Football team name → team ID (24h TTL — IDs don't change).
_api_football_team_cache: TTLCache = TTLCache(maxsize=256, ttl=86400)
_api_football_team_cache_lock = threading.RLock()

# Sleeper bulk player data: sport_key → full player list (1h TTL — expensive bulk call).
_sleeper_players_cache: TTLCache = TTLCache(maxsize=8, ttl=3600)
_sleeper_players_cache_lock = threading.RLock()

# TheSportsDB team name → team ID (24h TTL).
_sportsdb_team_cache: TTLCache = TTLCache(maxsize=256, ttl=86400)
_sportsdb_team_cache_lock = threading.RLock()


def reset_injury_session() -> None:
    """Call at the start of each batch to clear the cross-call corruption tracker."""
    global _injury_session
    _injury_session = {}
    logger.debug("[get_injury_report] Injury session cache reset")


_ESPN_SPORT_MAP = {
    "basketball_nba":          ("basketball", "nba"),
    "americanfootball_nfl":    ("football",   "nfl"),
    "icehockey_nhl":           ("hockey",     "nhl"),
    "baseball_mlb":            ("baseball",   "mlb"),
    "americanfootball_ncaaf":  ("football",   "college-football"),
    "basketball_ncaab":        ("basketball", "mens-college-basketball"),
}


def get_injury_report(team_name: str, sport: str = "basketball_nba") -> dict:
    """
    Fetch today's injury report for a team from ESPN's public API.
    Returns a structured list of injured/questionable players.
    """
    espn_sport, espn_league = _ESPN_SPORT_MAP.get(sport, ("basketball", "nba"))
    url = f"https://site.api.espn.com/apis/site/v2/sports/{espn_sport}/{espn_league}/injuries"

    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"team": team_name, "found": False, "error": str(e), "players": []}

    team_name_lower = team_name.lower().strip()
    matched_team = None

    for team_entry in data.get("injuries", []):
        # ESPN API now puts team name at the top level of each entry, not inside team{}
        t_name = (
            team_entry.get("displayName")
            or team_entry.get("shortDisplayName")
            or team_entry.get("team", {}).get("displayName")  # legacy fallback
            or ""
        )
        if t_name and _names_match(team_name_lower, t_name.lower()):
            matched_team = team_entry
            break

    if not matched_team:
        # ESPN only lists teams WITH active injuries. A missing team = clean bill of health.
        logger.debug(f"[get_injury_report] '{team_name}' not in ESPN report — no injuries on record")
        return {
            "team": team_name,
            "found": True,
            "players": [],
            "summary": "0 OUT, 0 Questionable",
            "message": "No injuries on record — team not listed in ESPN injury report (clean bill of health).",
        }

    # Resolve the canonical ESPN team name once — used for caching below
    espn_team_name = (
        matched_team.get("displayName")
        or matched_team.get("team", {}).get("displayName")
        or team_name
    )

    players = []
    for injury in matched_team.get("injuries", []):
        athlete = injury.get("athlete", {})
        details = injury.get("details", {})
        players.append({
            "name":        athlete.get("displayName", "?"),
            "position":    athlete.get("position", {}).get("abbreviation", "?"),
            "status":      injury.get("status", "?"),
            "injury_type": details.get("type", "?"),
            "return_date": details.get("returnDate", "unknown"),
        })

    # Sort by impact: Out first, then Questionable, then rest
    status_order = {"Out": 0, "Doubtful": 1, "Questionable": 2, "Probable": 3}
    players.sort(key=lambda p: status_order.get(p["status"], 9))

    # ── Corruption check ─────────────────────────────────────────────────────
    # Cache key is the resolved ESPN team name, NOT the raw query string.
    # This prevents "Wizards" and "Washington Wizards" from flagging each other
    # as corruption since they both resolve to "Washington Wizards".
    cross_team_dupes = [
        p["name"] for p in players
        if p["name"] in _injury_session and _injury_session[p["name"]] != espn_team_name
    ]
    if len(cross_team_dupes) >= 2:
        logger.warning(
            f"[get_injury_report] Corrupt data for '{team_name}' — "
            f"{cross_team_dupes[:3]} already seen for other teams. Skipping."
        )
        return {
            "team": team_name,
            "found": False,
            "error": "corrupted_data",
            "players": [],
            "message": (
                f"Injury data appears corrupted — {cross_team_dupes[0]} and "
                f"{len(cross_team_dupes)-1} other(s) already appeared for different teams. "
                "Use web search for injury information instead."
            ),
        }

    # Cache by resolved ESPN name so aliases ("Wizards"/"Washington Wizards") share the same key
    for p in players:
        _injury_session.setdefault(p["name"], espn_team_name)

    return {
        "team":    espn_team_name,
        "found":   True,
        "players": players,
        "summary": f"{sum(1 for p in players if p['status']=='Out')} OUT, "
                   f"{sum(1 for p in players if p['status']=='Questionable')} Questionable",
    }


# ─── get_open_trades ─────────────────────────────────────────────────────────

def get_open_trades() -> dict:
    """
    Read all currently open sage_agent bets from sports_trades.db.
    Returns {"trades": [...], "count": N}
    """
    import sqlite3
    db_path = "sports_trades.db"
    if not os.path.exists(db_path):
        return {"trades": [], "count": 0, "message": "No trades database found yet — no bets placed"}

    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT id, selection, league, home_team, away_team, amount, price, "
            "polymarket_slug, created_at, notes "
            "FROM trades WHERE status='open' AND source='sage_agent' ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
    except Exception as e:
        return {"trades": [], "count": 0, "error": str(e)}

    trades = []
    for row in rows:
        tid, selection, league, home, away, amount, price, slug, created, notes = row
        is_paper = (notes or "").startswith("PAPER")
        trades.append({
            "id":              tid,
            "selection":       selection,
            "league":          league or "?",
            "home_team":       home or "?",
            "away_team":       away or "?",
            "amount_usd":      amount,
            "entry_price":     price,
            "polymarket_slug": slug or "",
            "opened_at":       (created or "")[:16].replace("T", " ") + " UTC",
            "mode":            "paper" if is_paper else "live",
        })

    msg = f"{len(trades)} open bet(s)" if trades else "No open bets right now"
    return {"trades": trades, "count": len(trades), "message": msg}


# ─── settle_trade ─────────────────────────────────────────────────────────────

def settle_trade(trade_id: int, outcome: str) -> dict:
    """
    Force-settle a specific trade by ID. Calculates PnL and writes to DB.
    Used when the user confirms a result or Polymarket API hasn't updated yet.

    Returns a dict with settled status, PnL, and trade details.
    """
    import sqlite3
    outcome = outcome.upper().strip()
    if outcome not in ("WIN", "LOSS"):
        return {"settled": False, "error": "outcome must be WIN or LOSS"}

    db_path = "sports_trades.db"
    if not os.path.exists(db_path):
        return {"settled": False, "error": "sports_trades.db not found"}

    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT id, selection, league, price, amount, notes, status "
            "FROM trades WHERE id=? AND source='sage_agent'",
            (int(trade_id),),
        ).fetchone()
        conn.close()
    except Exception as e:
        return {"settled": False, "error": str(e)}

    if not row:
        return {"settled": False, "error": f"Trade #{trade_id} not found"}

    tid, selection, league, entry_price, amount, notes, current_status = row

    if current_status != "open":
        return {
            "settled": False,
            "error":   f"Trade #{trade_id} ({selection}) is already {current_status}",
        }

    is_paper = (notes or "").startswith("PAPER")
    fee      = amount * 0.015 if is_paper else 0.0
    pnl      = round(amount * (1.0 / entry_price - 1.0) - fee, 4) if outcome == "WIN" \
               else round(-amount - fee, 4)

    new_status = "won" if outcome == "WIN" else "lost"
    now_ts     = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE trades SET status=?, pnl=?, resolved_at=? WHERE id=?",
            (new_status, pnl, now_ts, tid),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        return {"settled": False, "error": f"DB write failed: {e}"}

    logger.info(
        f"[TOOLS] settle_trade: #{tid} {league} {selection} → {outcome} | PnL: ${pnl:+.2f}"
    )
    return {
        "settled":     True,
        "trade_id":    tid,
        "selection":   selection,
        "league":      league or "?",
        "outcome":     outcome,
        "entry_price": entry_price,
        "amount_usd":  amount,
        "pnl":         pnl,
        "mode":        "paper" if is_paper else "live",
        "message":     f"✅ Settled: {selection} → {outcome} | PnL ${pnl:+.2f}",
    }


# ─── write_lesson ─────────────────────────────────────────────────────────────

def write_lesson(
    agent_name: str,
    what_went_wrong: str,
    correction: str,
    rule_generated: str,
) -> dict:
    """
    Append a new row to agents/LEARNINGS.md mistake log table.
    Called by Sage (via tool) or by sports_bot._reflect_on_outcome (direct Python call).

    Returns:
        {"written": True, "row": "<markdown row>"} or {"written": False, "error": "..."}
    """
    learnings_path = _AGENTS_DIR / "LEARNINGS.md"
    if not learnings_path.exists():
        return {"written": False, "error": "LEARNINGS.md not found"}

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _esc(s: str) -> str:
        return str(s).replace("|", "\\|")

    row = (
        f"| {date_str} | {_esc(agent_name)} | {_esc(what_went_wrong)} "
        f"| {_esc(correction)} | {_esc(rule_generated)} |"
    )

    text = learnings_path.read_text(encoding="utf-8", errors="replace")

    # Replace the placeholder row if it's still there
    placeholder = "| — | — | *(no mistakes logged yet — log starts after first real batch)* | — | — |"
    if placeholder in text:
        text = text.replace(placeholder, row)
    else:
        # Append after the last table row
        lines = text.splitlines()
        last_table_idx = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 4:
                last_table_idx = i
        if last_table_idx >= 0:
            lines.insert(last_table_idx + 1, row)
            text = "\n".join(lines) + "\n"
        else:
            text = text.rstrip() + "\n" + row + "\n"

    learnings_path.write_text(text, encoding="utf-8")
    logger.info(f"[TOOLS] write_lesson: appended row for {agent_name}")
    return {"written": True, "row": row}


# ─── update_brain ─────────────────────────────────────────────────────────────

def update_brain(section: str, content: str) -> dict:
    """
    Replace a named section's content in agents/BRAIN.md.
    Finds the ## {section} header, replaces everything between it and the next ## header.

    Returns:
        {"updated": True, "section": section} or {"updated": False, "error": "..."}
    """
    brain_path = _AGENTS_DIR / "BRAIN.md"
    if not brain_path.exists():
        return {"updated": False, "error": "BRAIN.md not found"}

    text = brain_path.read_text(encoding="utf-8")

    # Find the section header (case-sensitive first, then insensitive)
    marker = f"## {section}\n"
    start_idx = text.find(marker)
    if start_idx < 0:
        lower_idx = text.lower().find(f"## {section.lower()}\n")
        if lower_idx < 0:
            return {"updated": False, "error": f"Section '## {section}' not found in BRAIN.md"}
        start_idx = lower_idx

    content_start = start_idx + len(marker)
    end_idx = text.find("\n## ", content_start)
    if end_idx < 0:
        end_idx = len(text)

    # Preserve the --- separator between sections if present
    segment = text[content_start:end_idx]
    has_separator = segment.rstrip().endswith("---")
    new_segment = f"\n{content.strip()}\n\n---\n\n" if has_separator else f"\n{content.strip()}\n\n"

    text = text[:content_start] + new_segment + text[end_idx:]
    brain_path.write_text(text, encoding="utf-8")
    logger.info(f"[TOOLS] update_brain: updated section '{section}'")
    return {"updated": True, "section": section}


# ─── get_api_football_h2h ─────────────────────────────────────────────────────

_API_FOOTBALL_BASE = "https://v3.football.api-sports.io"


def _api_football_search_team(team_name: str, api_key: str) -> int | None:
    """
    Resolve a team name to an API-Football team ID.
    Uses a 24h cache to avoid duplicate lookups across H2H + form calls.
    Returns team ID int, or None if not found.
    """
    cache_key = team_name.lower().strip()
    with _api_football_team_cache_lock:
        cached = _api_football_team_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{_API_FOOTBALL_BASE}/teams",
            headers={"x-apisports-key": api_key},
            params={"search": team_name},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("response", [])
        if not results:
            return None
        # Pick the closest name match
        team_id = None
        for item in results:
            name = item.get("team", {}).get("name", "")
            if _names_match(team_name, name, threshold=0.55):
                team_id = item["team"]["id"]
                break
        if team_id is None:
            team_id = results[0]["team"]["id"]  # fallback: first result
        with _api_football_team_cache_lock:
            _api_football_team_cache[cache_key] = team_id
        return team_id
    except Exception as e:
        logger.debug(f"[TOOLS] api_football team search failed for '{team_name}': {e}")
        return None


def get_api_football_h2h(home_team: str, away_team: str) -> dict:
    """
    Fetch the last 10 H2H fixtures between two soccer teams from API-Football.
    Returns last 5-10 results with date, venue, score, winner.
    """
    api_key = os.getenv("API_FOOTBALL_KEY", "")
    if not api_key:
        return {"found": False, "error": "API_FOOTBALL_KEY not configured"}

    home_id = _api_football_search_team(home_team, api_key)
    away_id = _api_football_search_team(away_team, api_key)

    if home_id is None:
        return {"found": False, "error": f"Team not found: {home_team}"}
    if away_id is None:
        return {"found": False, "error": f"Team not found: {away_team}"}

    try:
        r = requests.get(
            f"{_API_FOOTBALL_BASE}/fixtures/headtohead",
            headers={"x-apisports-key": api_key},
            params={"h2h": f"{home_id}-{away_id}", "last": 10},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        fixtures = data.get("response", [])
    except Exception as e:
        return {"found": False, "error": str(e)}

    if not fixtures:
        return {"found": False, "message": f"No H2H history found for {home_team} vs {away_team}"}

    results = []
    for f in fixtures[:10]:
        fixture = f.get("fixture", {})
        teams = f.get("teams", {})
        goals = f.get("goals", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        winner = (
            home.get("name") if home.get("winner")
            else away.get("name") if away.get("winner")
            else "draw"
        )
        results.append({
            "date":       fixture.get("date", "")[:10],
            "venue":      fixture.get("venue", {}).get("name", ""),
            "home_team":  home.get("name", ""),
            "away_team":  away.get("name", ""),
            "home_goals": goals.get("home"),
            "away_goals": goals.get("away"),
            "winner":     winner,
        })

    return {
        "found":   True,
        "h2h":     results,
        "count":   len(results),
        "home_team": home_team,
        "away_team": away_team,
    }


def get_api_football_form(team_name: str, season: int = 2025) -> dict:
    """
    Fetch the last 5 fixtures for a soccer team from API-Football.
    Returns form string (WWDLL), goals_for, goals_against, per-fixture details.
    """
    api_key = os.getenv("API_FOOTBALL_KEY", "")
    if not api_key:
        return {"found": False, "error": "API_FOOTBALL_KEY not configured"}

    team_id = _api_football_search_team(team_name, api_key)
    if team_id is None:
        return {"found": False, "error": f"Team not found: {team_name}"}

    try:
        r = requests.get(
            f"{_API_FOOTBALL_BASE}/fixtures",
            headers={"x-apisports-key": api_key},
            params={"team": team_id, "last": 5},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        fixtures = data.get("response", [])
    except Exception as e:
        return {"found": False, "error": str(e)}

    if not fixtures:
        return {"found": False, "message": f"No recent fixtures found for {team_name}"}

    form_chars = []
    goals_for = 0
    goals_against = 0
    fixture_details = []

    for f in fixtures:
        fixture = f.get("fixture", {})
        teams = f.get("teams", {})
        goals = f.get("goals", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        is_home = home.get("id") == team_id

        if is_home:
            gf = goals.get("home") or 0
            ga = goals.get("away") or 0
            opponent = away.get("name", "")
            won = home.get("winner")
        else:
            gf = goals.get("away") or 0
            ga = goals.get("home") or 0
            opponent = home.get("name", "")
            won = away.get("winner")

        goals_for += gf
        goals_against += ga

        if won is True:
            result = "W"
        elif won is False:
            result = "L"
        else:
            result = "D"
        form_chars.append(result)

        fixture_details.append({
            "date":     fixture.get("date", "")[:10],
            "opponent": opponent,
            "venue":    "home" if is_home else "away",
            "score":    f"{gf}-{ga}",
            "result":   result,
        })

    return {
        "found":          True,
        "team":           team_name,
        "form":           "".join(form_chars),
        "goals_for":      goals_for,
        "goals_against":  goals_against,
        "fixtures":       fixture_details,
    }


# ─── get_sleeper_injuries ─────────────────────────────────────────────────────

# Sleeper team abbreviation map: common name → Sleeper abbreviation
_SLEEPER_TEAM_ABBREV = {
    # NBA
    "hawks": "ATL", "celtics": "BOS", "nets": "BKN", "hornets": "CHA",
    "bulls": "CHI", "cavaliers": "CLE", "mavericks": "DAL", "nuggets": "DEN",
    "pistons": "DET", "warriors": "GSW", "rockets": "HOU", "pacers": "IND",
    "clippers": "LAC", "lakers": "LAL", "grizzlies": "MEM", "heat": "MIA",
    "bucks": "MIL", "timberwolves": "MIN", "pelicans": "NOP", "knicks": "NYK",
    "thunder": "OKC", "magic": "ORL", "76ers": "PHI", "suns": "PHX",
    "trail blazers": "POR", "blazers": "POR", "kings": "SAC", "spurs": "SAS",
    "raptors": "TOR", "jazz": "UTA", "wizards": "WAS",
    # NFL
    "bears": "CHI", "bengals": "CIN", "browns": "CLE", "cowboys": "DAL",
    "broncos": "DEN", "lions": "DET", "packers": "GB", "texans": "HOU",
    "colts": "IND", "jaguars": "JAX", "chiefs": "KC", "raiders": "LV",
    "chargers": "LAC", "rams": "LA", "dolphins": "MIA", "vikings": "MIN",
    "patriots": "NE", "saints": "NO", "giants": "NYG", "jets": "NYJ",
    "eagles": "PHI", "steelers": "PIT", "49ers": "SF", "seahawks": "SEA",
    "buccaneers": "TB", "titans": "TEN", "commanders": "WAS", "ravens": "BAL",
    "bills": "BUF", "falcons": "ATL", "panthers": "CAR", "cardinals": "ARI",
    # NHL
    "coyotes": "ARI", "bruins": "BOS", "sabres": "BUF", "flames": "CGY",
    "hurricanes": "CAR", "blackhawks": "CHI", "avalanche": "COL",
    "blue jackets": "CBJ", "stars": "DAL", "red wings": "DET",
    "oilers": "EDM", "panthers": "FLA", "kings": "LA", "wild": "MIN",
    "canadiens": "MTL", "predators": "NSH", "devils": "NJ", "islanders": "NYI",
    "rangers": "NYR", "senators": "OTT", "flyers": "PHI", "penguins": "PIT",
    "blues": "STL", "lightning": "TB", "maple leafs": "TOR", "canucks": "VAN",
    "golden knights": "VGK", "capitals": "WSH", "jets": "WPG", "kraken": "SEA",
    "sharks": "SJS", "ducks": "ANA",
}


def _sleeper_resolve_abbrev(team_name: str) -> str | None:
    """Map a team name to its Sleeper abbreviation. Returns None if unknown."""
    lower = team_name.lower().strip()
    # Direct lookup by last word (nickname)
    parts = lower.split()
    for length in range(len(parts), 0, -1):
        candidate = " ".join(parts[-length:])
        if candidate in _SLEEPER_TEAM_ABBREV:
            return _SLEEPER_TEAM_ABBREV[candidate]
    # Partial match
    for key, abbrev in _SLEEPER_TEAM_ABBREV.items():
        if key in lower or lower in key:
            return abbrev
    return None


def get_sleeper_injuries(team_name: str, sport: str = "nba") -> dict:
    """
    Fetch injury status for all players on a team from the Sleeper API.
    Uses a 1h bulk cache per sport to avoid re-fetching the large player list.
    """
    sport = sport.lower().strip()
    if sport not in ("nba", "nfl", "nhl"):
        return {"found": False, "error": f"Sleeper supports nba/nfl/nhl only, got: {sport}"}

    # Resolve team abbreviation
    abbrev = _sleeper_resolve_abbrev(team_name)
    if abbrev is None:
        return {"found": False, "error": f"Could not resolve team abbreviation for: {team_name}"}

    # Fetch (or reuse cached) full player list for this sport
    with _sleeper_players_cache_lock:
        all_players = _sleeper_players_cache.get(sport)

    if all_players is None:
        try:
            r = requests.get(
                f"https://api.sleeper.app/v1/players/{sport}",
                timeout=20,
            )
            r.raise_for_status()
            all_players = r.json()
            with _sleeper_players_cache_lock:
                _sleeper_players_cache[sport] = all_players
            logger.debug(f"[TOOLS] Sleeper players cached for {sport}: {len(all_players)} players")
        except Exception as e:
            return {"found": False, "error": f"Sleeper API error: {e}"}
    else:
        logger.debug(f"[TOOLS] Sleeper cache hit for {sport}")

    # Filter to players on this team with an injury status
    injured = []
    for player_id, p in all_players.items():
        if p.get("team") != abbrev:
            continue
        status = p.get("injury_status") or p.get("status", "")
        if not status or status.lower() in ("active", ""):
            continue
        injured.append({
            "player":                p.get("full_name") or p.get("last_name", player_id),
            "position":              p.get("position", ""),
            "injury_status":         status,
            "injury_body_part":      p.get("injury_body_part", ""),
            "injury_notes":          p.get("injury_notes", ""),
            "practice_participation": p.get("practice_participation", ""),
            "news_updated":          p.get("news_updated"),
        })

    if not injured:
        return {
            "found":   True,
            "team":    team_name,
            "abbrev":  abbrev,
            "sport":   sport,
            "message": "No injury-listed players found",
            "players": [],
        }

    return {
        "found":   True,
        "team":    team_name,
        "abbrev":  abbrev,
        "sport":   sport,
        "count":   len(injured),
        "players": injured,
    }


# ─── get_sportsdb_h2h ─────────────────────────────────────────────────────────

_SPORTSDB_BASE = "https://www.thesportsdb.com/api/v1/json/3"


def _sportsdb_search_team(team_name: str) -> int | None:
    """
    Resolve a team name to a TheSportsDB team ID.
    Uses a 24h cache.
    """
    cache_key = team_name.lower().strip()
    with _sportsdb_team_cache_lock:
        cached = _sportsdb_team_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            f"{_SPORTSDB_BASE}/searchteams.php",
            params={"t": team_name},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        teams = data.get("teams") or []
        if not teams:
            return None
        # Find closest match
        team_id = None
        for t in teams:
            name = t.get("strTeam", "")
            if _names_match(team_name, name, threshold=0.55):
                team_id = int(t["idTeam"])
                break
        if team_id is None:
            team_id = int(teams[0]["idTeam"])
        with _sportsdb_team_cache_lock:
            _sportsdb_team_cache[cache_key] = team_id
        return team_id
    except Exception as e:
        logger.debug(f"[TOOLS] sportsdb team search failed for '{team_name}': {e}")
        return None


def _sportsdb_event_to_h2h(e: dict, home_team: str, away_team: str) -> dict:
    """Convert a TheSportsDB event dict to a standard H2H result dict."""
    hs_raw = e.get("intHomeScore")
    as_raw = e.get("intAwayScore")
    try:
        home_score = int(hs_raw) if hs_raw is not None else None
        away_score = int(as_raw) if as_raw is not None else None
    except (ValueError, TypeError):
        home_score = away_score = None

    if home_score is not None and away_score is not None:
        if home_score > away_score:
            winner = e.get("strHomeTeam", "")
        elif home_score < away_score:
            winner = e.get("strAwayTeam", "")
        else:
            winner = "draw"
    else:
        winner = "unknown"

    return {
        "date":       e.get("dateEvent", ""),
        "venue":      e.get("strVenue", ""),
        "home_team":  e.get("strHomeTeam", ""),
        "away_team":  e.get("strAwayTeam", ""),
        "home_score": home_score,
        "away_score": away_score,
        "winner":     winner,
    }


def get_sportsdb_h2h(home_team: str, away_team: str) -> dict:
    """
    Fetch the last 5 H2H results between two teams from TheSportsDB.
    Works multi-sport (NBA, soccer, NFL, NHL, MLB, MMA).

    Uses eventslast.php (free tier) for each team, then intersects the results
    to find shared matchups — avoids the paywalled eventsh2h.php endpoint.
    """
    home_id = _sportsdb_search_team(home_team)
    away_id = _sportsdb_search_team(away_team)

    if home_id is None:
        return {"found": False, "error": f"Team not found: {home_team}"}
    if away_id is None:
        return {"found": False, "error": f"Team not found: {away_team}"}

    # Fetch last 15 events for each team, then find H2H matches by team-name
    def fetch_last(team_id: int) -> list:
        try:
            r = requests.get(
                f"{_SPORTSDB_BASE}/eventslast.php",
                params={"id": team_id},
                timeout=12,
            )
            r.raise_for_status()
            return r.json().get("results") or []
        except Exception as e:
            logger.debug(f"[TOOLS] sportsdb eventslast failed for id {team_id}: {e}")
            return []

    home_events = fetch_last(home_id)
    away_events = fetch_last(away_id)

    # Find events where both teams appear, scanning by name (handles data lag better than ID match)
    away_event_ids = {e.get("idEvent") for e in away_events if e.get("idEvent")}

    def _involves_both(e: dict) -> bool:
        """Event involves both teams — match by event ID or by team names."""
        if e.get("idEvent") in away_event_ids:
            return True
        ht = e.get("strHomeTeam", "")
        at = e.get("strAwayTeam", "")
        return _names_match(away_team, ht, 0.55) or _names_match(away_team, at, 0.55)

    h2h_events = [e for e in home_events if _involves_both(e)]

    # Also scan away_events for H2H matches not in home_events
    home_h2h_ids = {e.get("idEvent") for e in h2h_events}
    for e in away_events:
        if e.get("idEvent") in home_h2h_ids:
            continue
        ht = e.get("strHomeTeam", "")
        at = e.get("strAwayTeam", "")
        if _names_match(home_team, ht, 0.55) or _names_match(home_team, at, 0.55):
            h2h_events.append(e)

    # Sort by date descending
    h2h_events.sort(key=lambda e: e.get("dateEvent", ""), reverse=True)

    if not h2h_events:
        return {"found": False, "message": f"No H2H history found for {home_team} vs {away_team}"}

    results = [_sportsdb_event_to_h2h(e, home_team, away_team) for e in h2h_events[:5]]

    return {
        "found":     True,
        "h2h":       results,
        "count":     len(results),
        "home_team": home_team,
        "away_team": away_team,
    }


# ─── Tool dispatcher ──────────────────────────────────────────────────────────

def dispatch(name: str, input_data: dict) -> dict:
    """Route an agent tool call to the appropriate Python function."""
    if name == "web_search":
        results = web_search(
            query=input_data.get("query", ""),
            max_results=input_data.get("max_results", 5),
            topic=input_data.get("topic", "general"),
            time_range=input_data.get("time_range", ""),
        )
        return {"results": results, "count": len(results)}
    elif name == "extract_url":
        return extract_url(
            url=input_data.get("url", ""),
            query=input_data.get("query", ""),
        )
    elif name == "get_sharp_odds":
        return get_sharp_odds(
            sport=input_data.get("sport", ""),
            home_team=input_data.get("home_team", ""),
            away_team=input_data.get("away_team", ""),
        )
    elif name == "get_sharp_odds_totals":
        return get_sharp_odds_totals(
            sport=input_data.get("sport", ""),
            home_team=input_data.get("home_team", ""),
            away_team=input_data.get("away_team", ""),
        )
    elif name == "get_polymarket_market":
        return get_polymarket_market(
            home_team=input_data.get("home_team", ""),
            away_team=input_data.get("away_team", ""),
            sport=input_data.get("sport", ""),
        )
    elif name == "get_injury_report":
        return get_injury_report(
            team_name=input_data.get("team_name", ""),
            sport=input_data.get("sport", "basketball_nba"),
        )
    elif name == "get_nba_game_log":
        return get_nba_game_log(
            team_name=input_data.get("team_name", ""),
            num_games=input_data.get("num_games", 5),
        )
    elif name == "get_recent_results":
        return get_recent_results(
            sport=input_data.get("sport", ""),
            team_name=input_data.get("team_name", ""),
            num_games=input_data.get("num_games", 3),
        )
    elif name == "get_open_trades":
        return get_open_trades()
    elif name == "settle_trade":
        return settle_trade(
            trade_id=input_data.get("trade_id"),
            outcome=input_data.get("outcome", ""),
        )
    elif name == "get_live_scores":
        return get_live_scores(
            sport=input_data.get("sport", ""),
        )
    elif name == "write_lesson":
        return write_lesson(
            agent_name=input_data.get("agent_name", "Unknown"),
            what_went_wrong=input_data.get("what_went_wrong", ""),
            correction=input_data.get("correction", ""),
            rule_generated=input_data.get("rule_generated", ""),
        )
    elif name == "update_brain":
        return update_brain(
            section=input_data.get("section", ""),
            content=input_data.get("content", ""),
        )
    elif name == "get_api_football_h2h":
        return get_api_football_h2h(
            home_team=input_data.get("home_team", ""),
            away_team=input_data.get("away_team", ""),
        )
    elif name == "get_api_football_form":
        return get_api_football_form(
            team_name=input_data.get("team_name", ""),
            season=input_data.get("season", 2025),
        )
    elif name == "get_sleeper_injuries":
        return get_sleeper_injuries(
            team_name=input_data.get("team_name", ""),
            sport=input_data.get("sport", "nba"),
        )
    elif name == "get_sportsdb_h2h":
        return get_sportsdb_h2h(
            home_team=input_data.get("home_team", ""),
            away_team=input_data.get("away_team", ""),
        )
    else:
        return {"error": f"Unknown tool: {name}"}


# ─── Agentic loop ─────────────────────────────────────────────────────────────

def _api_call_with_retry(client, **kwargs):
    """
    Retry wrapper for Anthropic API calls.
    Retries on transient server errors (500, 529 overloaded, network failures).
    Fails immediately on client errors (4xx) — those won't fix themselves.
    Backoff: 10s → 30s → 60s → 120s → 240s → 480s (7 attempts total, ~16 min total wait).
    529 overloads can last many minutes — the longer schedule handles real outages.
    """
    delays = [10, 30, 60, 120, 240, 480]
    max_attempts = len(delays) + 1  # 7
    for attempt in range(max_attempts):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            # Determine if this error is worth retrying
            status = getattr(e, "status_code", None)
            is_transient = (
                status in (500, 529, 503, 408)  # server-side / overloaded / timeout
                or status is None               # network-level error (no HTTP status)
            )
            if not is_transient:
                raise  # 400/401/403/404 etc — won't fix on retry

            if attempt < max_attempts - 1:
                delay = delays[attempt]
                logger.warning(
                    f"[AGENT] Anthropic API error (attempt {attempt + 1}/{max_attempts}, "
                    f"status={status}): {e} — retrying in {delay}s"
                )
                time.sleep(delay)
            else:
                logger.error(f"[AGENT] Anthropic API failed after {max_attempts} attempts: {e}")
                raise


def run_agent(
    client,
    model: str,
    system: str,
    user_prompt: str,
    tools_schema: list = None,
    execute_fn=None,
    max_tool_calls: int = 15,
) -> str:
    """
    Run an agentic loop until stop_reason is end_turn or max_tool_calls is reached.
    Returns the final text from Claude.

    If tools_schema is empty/None, makes a single completion call (no loop).
    """
    messages = [{"role": "user", "content": user_prompt}]
    tool_calls_used = 0
    last_response = None

    while True:
        use_tools = (tools_schema or []) if tool_calls_used < max_tool_calls else []

        kwargs = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if use_tools:
            kwargs["tools"] = use_tools

        last_response = _api_call_with_retry(client, **kwargs)

        if last_response.stop_reason != "tool_use" or not use_tools:
            break

        # Extract and execute tool calls
        tool_use_blocks = [b for b in last_response.content if b.type == "tool_use"]
        tool_calls_used += len(tool_use_blocks)

        messages.append({"role": "assistant", "content": last_response.content})

        tool_results = []
        for block in tool_use_blocks:
            try:
                fn = execute_fn or dispatch
                result = fn(block.name, block.input)
            except Exception as e:
                result = {"error": str(e)}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

        if tool_calls_used >= max_tool_calls:
            logger.warning(f"[AGENT] Tool cap ({max_tool_calls}) reached — forcing final response")
            messages.append({
                "role": "user",
                "content": (
                    "IMPORTANT: You have used the maximum number of tool calls. "
                    "Please output your final JSON analysis now based on what you have gathered."
                ),
            })

    # Extract text from final response
    if last_response:
        for block in last_response.content:
            if hasattr(block, "text"):
                return block.text
    return ""


# ─── OpenAI-compatible tool schema helper ────────────────────────────────────

def _to_openai_tool(schema: dict) -> dict:
    """Convert an Anthropic-format tool schema to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema["input_schema"],
        },
    }


# ─── Gemini (Google OpenAI-compatible endpoint) ───────────────────────────────

@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.InternalServerError,
        openai.APITimeoutError,
    )),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _gemini_chat(messages: list, model: str, tools: list = None, max_tokens: int = 8192):
    """Single call to Google's OpenAI-compatible Gemini endpoint. Retries on transient errors."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set in environment")

    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    kwargs: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        kwargs["tools"]       = tools
        kwargs["tool_choice"] = "auto"

    return client.chat.completions.create(**kwargs)


def run_agent_gemini(
    system: str,
    user_prompt: str,
    tools_schema: list = None,
    execute_fn=None,
    max_tool_calls: int = 8,
    model: str = "gemini-2.5-flash",
    tool_call_limits: dict = None,
    max_tokens: int = 8192,
    cap_message: str = None,
) -> str:
    """
    Agentic loop using Google's Gemini via their OpenAI-compatible endpoint.
    Same tool-call pattern as run_agent_local — drop-in replacement.
    Requires GEMINI_API_KEY in environment.

    tool_call_limits: optional per-tool cap, e.g. {"get_injury_report": 4}.
    When a tool hits its limit, a budget-exceeded error is returned instead of
    calling the function, so remaining calls are preserved for other tools.

    cap_message: custom message injected when max_tool_calls is reached.
    Should include the expected output schema so the model knows the exact format.
    Defaults to a generic "output your JSON now" prompt if not provided.
    """
    openai_tools    = [_to_openai_tool(t) for t in (tools_schema or [])]
    messages        = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_prompt},
    ]
    tool_calls_used  = 0
    per_tool_counts  = {}   # tracks calls per tool name this run
    last_content     = ""

    while True:
        use_tools    = openai_tools if (tool_calls_used < max_tool_calls and openai_tools) else None
        response     = _gemini_chat(messages, model, tools=use_tools, max_tokens=max_tokens)
        msg          = response.choices[0].message
        tool_calls   = msg.tool_calls or []
        last_content = msg.content or ""

        if not tool_calls or not use_tools:
            break

        tool_calls_used += len(tool_calls)

        # Add assistant turn (SDK message object preserves tool_calls for Gemini context)
        messages.append(msg)

        for tc in tool_calls:
            fn_name = tc.function.name
            per_tool_counts[fn_name] = per_tool_counts.get(fn_name, 0) + 1

            # Per-tool budget check
            limit = (tool_call_limits or {}).get(fn_name)
            if limit is not None and per_tool_counts[fn_name] > limit:
                logger.warning(
                    f"[GEMINI] Per-tool cap for '{fn_name}' ({limit}) reached — "
                    "returning budget error to agent"
                )
                result = {
                    "error": "tool_budget_exceeded",
                    "message": (
                        f"'{fn_name}' call limit ({limit}) reached for this batch. "
                        "Use web_search as a fallback for any remaining research."
                    ),
                }
            else:
                try:
                    args   = json.loads(tc.function.arguments)
                    fn     = execute_fn or dispatch
                    result = fn(fn_name, args)
                except Exception as e:
                    result = {"error": str(e)}

            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      json.dumps(result, default=str),
            })

        if tool_calls_used >= max_tool_calls:
            logger.warning(f"[GEMINI] Tool cap ({max_tool_calls}) reached — forcing final response")
            messages.append({
                "role":    "user",
                "content": cap_message or (
                    "IMPORTANT: You have used the maximum number of tool calls. "
                    "Output your final JSON analysis now based on what you have gathered."
                ),
            })

    return last_content


# ─── Grok (xAI OpenAI-compatible endpoint) ───────────────────────────────────

GROK_MODEL = "grok-3-mini"


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.InternalServerError,
        openai.APITimeoutError,
    )),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _grok_chat(messages: list, model: str, tools: list = None):
    """Single call to xAI's Grok endpoint. Retries on transient errors."""
    api_key = os.getenv("GROK_API_KEY")
    if not api_key:
        raise ValueError("GROK_API_KEY not set")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
    )
    kwargs: dict = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"]       = tools
        kwargs["tool_choice"] = "auto"

    return client.chat.completions.create(**kwargs)


def run_agent_grok(
    system: str,
    user_prompt: str,
    tools_schema: list = None,
    execute_fn=None,
    max_tool_calls: int = 4,
    model: str = GROK_MODEL,
) -> str:
    """
    Agentic loop using xAI's Grok via their OpenAI-compatible endpoint.
    Requires GROK_API_KEY in environment.

    Used by Max as a breaking-news pre-pass (X/Twitter real-time data).
    Same tool-call pattern as run_agent_gemini.
    """
    openai_tools    = [_to_openai_tool(t) for t in (tools_schema or [])]
    messages        = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_prompt},
    ]
    tool_calls_used = 0
    last_content    = ""

    while True:
        use_tools    = openai_tools if (tool_calls_used < max_tool_calls and openai_tools) else None
        response     = _grok_chat(messages, model, tools=use_tools)
        msg          = response.choices[0].message
        tool_calls   = msg.tool_calls or []
        last_content = msg.content or ""

        if not tool_calls or not use_tools:
            break

        tool_calls_used += len(tool_calls)
        messages.append(msg)

        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                args   = json.loads(tc.function.arguments)
                fn     = execute_fn or dispatch
                result = fn(fn_name, args)
            except Exception as e:
                result = {"error": str(e)}

            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      json.dumps(result, default=str),
            })

        if tool_calls_used >= max_tool_calls:
            logger.warning(f"[GROK] Tool cap ({max_tool_calls}) reached — summarising")
            messages.append({
                "role":    "user",
                "content": "Summarise the key findings from your research so far.",
            })

    return last_content


# ─── JSON extractor ───────────────────────────────────────────────────────────

def extract_json(text: str):
    """
    Robustly extract a JSON object or array from LLM response text.
    Handles markdown code blocks and surrounding prose.
    Returns parsed dict/list or {} on failure.
    """
    if not text:
        return {}
    text = text.strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract from ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Find the largest {...} block
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Find the largest [...] block
    match = re.search(r"(\[[\s\S]+\])", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    return {}
