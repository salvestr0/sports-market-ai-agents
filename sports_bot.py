"""
Polymarket Sports Betting Bot v1.0
===================================
Value betting on Polymarket sports markets using:
  1. Sharp book reference odds (The Odds API) — find where Polymarket is mispriced
  2. Friend's scraper picks (JSON files in scraper_data/) — model probabilities
  3. Execute on Polymarket CLOB (same execution layer as crypto bot)

Strategy:
  - Only bet when BOTH sharp book AND scraper model agree Polymarket is underpricing
  - Edge = min(sharp_prob, model_prob) - polymarket_price
  - Size = fractional Kelly (25%) on bankroll
  - Paper mode by default; --live for real money

Setup:
  pip install requests py-clob-client web3==6.14.0 python-dotenv flask

  .env file:
    POLYMARKET_PRIVATE_KEY=0xYourKey
    POLYMARKET_FUNDER=0xYourFunderAddress
    ODDS_API_KEY=your_odds_api_key   # from the-odds-api.com (free tier: 500 req/month)
    SPORTS_DRY_RUN=true

Usage:
  python sports_bot.py              # paper mode
  python sports_bot.py --live       # live trading
  python sports_bot.py --single-scan # one scan then exit

Scraper integration:
  - Drop JSON pick files into scraper_data/ folder
  - Bot auto-reads them every scan
  - See scraper_data/SPEC.md for file format
"""

import json
import time
import logging
import sqlite3
import requests
import os
import glob
import difflib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Logging ---
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sports_bot.log", mode="a", encoding="utf-8"),
    ]
)
logger = logging.getLogger("SportsBot")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

@dataclass
class SportsConfig:
    # Polymarket
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"

    # The Odds API (reference prices)
    odds_api_key: str = ""
    odds_api_url: str = "https://api.the-odds-api.com/v4"

    # Sharp books to use as reference (ordered by sharpness)
    sharp_books: list = field(default_factory=lambda: [
        "pinnacle", "betfair_ex_eu", "betfair_ex_au",
        "sport888", "williamhill", "draftkings", "fanduel"
    ])

    # Sports to monitor (Odds API sport keys)
    sports: list = field(default_factory=lambda: [
        "americanfootball_nfl",
        "basketball_nba",
        "soccer_epl",
        "soccer_uefa_champs_league",
        "soccer_usa_mls",
        "tennis_atp_french_open",
        "tennis_wta_french_open",
        "mma_mixed_martial_arts",
    ])

    # Scraper
    scraper_folder: str = "scraper_data"

    # Strategy thresholds
    min_edge_pct: float = 5.0          # minimum edge vs sharp prob to bet (%)
    min_model_prob: float = 0.55       # scraper model must give >= this probability
    min_sharp_prob: float = 0.50       # sharp book must agree selection has > 50% true prob
    require_both: bool = True          # require BOTH sharp + scraper to agree (safer)
    max_hours_before_event: float = 48 # don't bet too early (lines move)
    min_hours_before_event: float = 0.25  # don't bet in last 15 minutes

    # Sizing
    bankroll_usd: float = 200.0
    kelly_fraction: float = 0.25       # 25% Kelly (conservative)
    max_bet_usd: float = 25.0
    min_bet_usd: float = 2.0
    max_open_bets: int = 10            # max simultaneous open positions
    max_daily_bets: int = 20

    # Execution
    dry_run: bool = True
    use_limit_orders: bool = True      # maker (0% fee) by default for sports
    limit_order_timeout_s: int = 60    # wait longer for sports (less HFT)
    polymarket_fee_pct: float = 0.02   # 2% taker fee estimate for sports markets
    max_retries: int = 2

    # Timing
    scan_interval_seconds: int = 300   # scan every 5 minutes (sports don't need 30s)
    db_path: str = "sports_trades.db"
    status_file: str = "sports_status.json"

    def __post_init__(self):
        env_map = {
            "POLYMARKET_PRIVATE_KEY": ("polymarket_private_key", str),
            "POLYMARKET_FUNDER": ("polymarket_funder", str),
            "ODDS_API_KEY": ("odds_api_key", str),
            "SPORTS_DRY_RUN": ("dry_run", bool),
            "SPORTS_BANKROLL": ("bankroll_usd", float),
            "SPORTS_MAX_BET": ("max_bet_usd", float),
            "SPORTS_MIN_EDGE": ("min_edge_pct", float),
        }
        for env_key, (attr, typ) in env_map.items():
            val = os.getenv(env_key)
            if val is not None:
                if typ == bool:
                    setattr(self, attr, val.lower() not in ("0", "false", "no"))
                elif typ == float:
                    setattr(self, attr, float(val))
                else:
                    setattr(self, attr, val)


# ─────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────

@dataclass
class ScraperPick:
    """A pick from the friend's research scraper."""
    event_id: str          # unique ID the scraper assigns
    sport: str             # e.g. "basketball_nba"
    league: str            # e.g. "NBA"
    home_team: str
    away_team: str
    market_type: str       # "moneyline", "spread", "totals"
    selection: str         # team name or "over"/"under"
    model_prob: float      # scraper's estimated win probability (0-1)
    confidence: str        # "high" / "medium" / "low"
    notes: str
    event_start: str       # ISO8601 datetime
    file_timestamp: str    # when this file was generated
    polymarket_slug: str = ""  # if scraper provides it directly (optional)


@dataclass
class SharpLine:
    """Reference odds from a sharp book."""
    book: str
    selection: str
    odds_decimal: float    # e.g. 2.10
    implied_prob: float    # raw implied prob (includes vig)
    true_prob: float       # devigged probability


@dataclass
class PolymarketSportsMarket:
    """An active Polymarket sports market."""
    slug: str
    question: str
    outcomes: list         # e.g. ["Yes", "No"] or ["Team A", "Team B"]
    prices: list           # matching prices [0.55, 0.45]
    token_ids: list
    end_time: datetime
    seconds_to_close: int
    volume: float = 0.0


@dataclass
class BetSignal:
    """A signal to place a bet."""
    pick: ScraperPick
    market: PolymarketSportsMarket
    selection_idx: int     # index into market.outcomes / prices
    polymarket_price: float
    sharp_prob: float      # devigged sharp book probability
    model_prob: float      # scraper model probability
    edge_pct: float        # min(sharp_prob, model_prob) - polymarket_price, as %
    kelly_size: float      # recommended bet size
    size_usd: float        # capped size
    sharp_book: str        # which book provided the reference
    reasoning: str


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

class SportsDB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                sport TEXT,
                league TEXT,
                event_id TEXT,
                home_team TEXT,
                away_team TEXT,
                market_type TEXT,
                selection TEXT,
                polymarket_slug TEXT,
                polymarket_price REAL,
                sharp_prob REAL,
                model_prob REAL,
                edge_pct REAL,
                size_usd REAL,
                token_id TEXT DEFAULT '',
                shares REAL DEFAULT 0.0,
                order_id TEXT DEFAULT '',
                status TEXT,
                resolved INTEGER DEFAULT 0,
                outcome TEXT DEFAULT '',
                pnl REAL DEFAULT 0.0,
                exit_price REAL DEFAULT 0.0,
                event_start TEXT,
                reasoning TEXT,
                sharp_book TEXT
            );

            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                picks_found INTEGER,
                markets_found INTEGER,
                signals_found INTEGER,
                bets_placed INTEGER
            );
        """)
        self.conn.commit()

    def save_bet(self, data: dict) -> int:
        cols = [c for c in data.keys() if c != "id"]
        self.conn.execute(
            f"INSERT INTO bets ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
            [data[c] for c in cols]
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def log_scan(self, picks: int, markets: int, signals: int, bets: int):
        self.conn.execute(
            "INSERT INTO scans (timestamp,picks_found,markets_found,signals_found,bets_placed) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), picks, markets, signals, bets)
        )
        self.conn.commit()

    def update_bet(self, bet_id: int, **kwargs):
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [bet_id]
        self.conn.execute(f"UPDATE bets SET {sets} WHERE id=?", vals)
        self.conn.commit()

    def open_bets(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM bets WHERE resolved=0 AND status IN ('live','paper')"
        ).fetchall()]

    def daily_bets(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = self.conn.execute(
            "SELECT COUNT(*) as c FROM bets WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()
        return r["c"] if r else 0

    def already_bet(self, event_id: str, selection: str) -> bool:
        r = self.conn.execute(
            "SELECT id FROM bets WHERE event_id=? AND selection=? AND resolved=0",
            (event_id, selection)
        ).fetchone()
        return r is not None

    def get_performance(self) -> dict:
        r = self.conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                   SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as pending,
                   COALESCE(SUM(pnl),0) as total_pnl,
                   COALESCE(SUM(size_usd),0) as volume
            FROM bets WHERE status IN ('live','paper','settled')
        """).fetchone()
        d = dict(r) if r else {}
        wins = d.get("wins") or 0
        losses = d.get("losses") or 0
        resolved = wins + losses
        d["win_rate"] = round(wins / resolved * 100, 1) if resolved else 0
        vol = d.get("volume") or 0
        pnl = d.get("total_pnl") or 0
        d["roi_pct"] = round(pnl / vol * 100, 1) if vol > 0 else 0
        return d

    def recent_bets(self, n: int = 20) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM bets ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()]

    def by_sport(self) -> list:
        rows = self.conn.execute("""
            SELECT sport,
                   COUNT(*) as bets,
                   SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl),0) as pnl
            FROM bets WHERE status IN ('live','paper','settled')
            GROUP BY sport ORDER BY bets DESC
        """).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# SHARP ODDS CLIENT (The Odds API)
# ─────────────────────────────────────────────

class SharpOddsClient:
    """
    Fetches odds from The Odds API (the-odds-api.com).
    Free tier: 500 requests/month. Paid plans available.
    Converts bookmaker odds to devigged true probabilities.
    """

    def __init__(self, api_key: str, sharp_books: list):
        self.api_key = api_key
        self.sharp_books = sharp_books
        self.base = "https://api.the-odds-api.com/v4"
        self.session = requests.Session()
        self._cache = {}       # (sport, event_id) -> (data, cache_time)
        self._events_cache = {}  # sport -> (list, cache_time)
        self.remaining_requests = None

    def _get(self, endpoint: str, params: dict) -> Optional[dict]:
        if not self.api_key:
            logger.debug("  [ODDS] No API key configured — skipping reference odds")
            return None
        params["apiKey"] = self.api_key
        try:
            r = self.session.get(f"{self.base}/{endpoint}", params=params, timeout=15)
            self.remaining_requests = r.headers.get("x-requests-remaining")
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error("  [ODDS] Invalid API key")
            elif e.response.status_code == 422:
                logger.debug(f"  [ODDS] Sport not available: {params.get('sports','')}")
            else:
                logger.error(f"  [ODDS] HTTP error: {e}")
            return None
        except Exception as e:
            logger.debug(f"  [ODDS] Request failed: {e}")
            return None

    def get_events(self, sport: str) -> list:
        """Get upcoming events for a sport. Cached for 10 minutes."""
        now = time.time()
        if sport in self._events_cache:
            cached, ts = self._events_cache[sport]
            if now - ts < 600:
                return cached

        data = self._get(f"sports/{sport}/events", {"dateFormat": "iso"})
        if not data:
            return []
        self._events_cache[sport] = (data, now)
        return data

    def get_odds(self, sport: str, event_id: str) -> Optional[list]:
        """Get odds for a specific event. Returns list of SharpLine objects."""
        cache_key = (sport, event_id)
        now = time.time()
        if cache_key in self._cache:
            cached, ts = self._cache[cache_key]
            if now - ts < 300:  # 5 min cache
                return cached

        data = self._get(
            f"sports/{sport}/odds",
            {
                "eventIds": event_id,
                "regions": "us,uk,eu,au",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "bookmakers": ",".join(self.sharp_books),
            }
        )

        if not data:
            return None

        lines = self._parse_odds(data)
        self._cache[cache_key] = (lines, now)
        return lines

    def _parse_odds(self, events_data: list) -> list:
        """Parse Odds API response into SharpLine objects."""
        lines = []
        for event in events_data:
            bookmakers = event.get("bookmakers", [])
            for book in bookmakers:
                book_key = book.get("key", "")
                if book_key not in self.sharp_books:
                    continue
                for market in book.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    outcomes = market.get("outcomes", [])
                    if not outcomes:
                        continue
                    # Devig: convert all outcomes to true probabilities
                    raw_probs = [1.0 / o["price"] for o in outcomes if o.get("price", 0) > 0]
                    total_raw = sum(raw_probs)
                    if total_raw <= 0:
                        continue
                    for i, outcome in enumerate(outcomes):
                        if outcome.get("price", 0) <= 0:
                            continue
                        raw_prob = 1.0 / outcome["price"]
                        true_prob = raw_prob / total_raw  # devig by normalisation
                        lines.append(SharpLine(
                            book=book_key,
                            selection=outcome["name"],
                            odds_decimal=outcome["price"],
                            implied_prob=raw_prob,
                            true_prob=round(true_prob, 4),
                        ))
        return lines

    def get_best_sharp_prob(self, sharp_lines: list, selection: str) -> Optional[SharpLine]:
        """
        Find the best (highest) sharp probability for a selection.
        We prefer Pinnacle > Betfair > others.
        """
        priority = {b: i for i, b in enumerate(self.sharp_books)}

        matching = [
            l for l in sharp_lines
            if self._names_match(l.selection, selection)
        ]
        if not matching:
            return None

        # Sort by book priority (sharpest first), then by true_prob
        matching.sort(key=lambda l: (priority.get(l.book, 99), -l.true_prob))
        return matching[0]

    @staticmethod
    def _names_match(a: str, b: str, threshold: float = 0.6) -> bool:
        """Fuzzy match two team/player names."""
        a_norm = a.lower().strip()
        b_norm = b.lower().strip()
        if a_norm == b_norm:
            return True
        ratio = difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
        return ratio >= threshold


# ─────────────────────────────────────────────
# SCRAPER READER
# ─────────────────────────────────────────────

class ScraperReader:
    """
    Reads pick files from the scraper_data/ folder.
    Files are JSON, format defined in scraper_data/SPEC.md.
    Polls for new files each scan.
    """

    def __init__(self, folder: str):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self._write_spec()

    def _write_spec(self):
        """Write the data format spec so the scraper knows what to produce."""
        spec_path = os.path.join(self.folder, "SPEC.md")
        if os.path.exists(spec_path):
            return
        spec = """# Scraper Data Format

Drop JSON pick files into this folder. The bot reads them automatically.

## File naming
  picks_YYYYMMDD_HHMMSS.json   (timestamped, one file per run)
  picks_latest.json             (or a fixed name the bot always reads)

## JSON Format

```json
{
  "generated_at": "2026-02-18T10:00:00Z",
  "picks": [
    {
      "event_id": "nba_lakers_celtics_20260218",
      "sport": "basketball_nba",
      "league": "NBA",
      "home_team": "Boston Celtics",
      "away_team": "Los Angeles Lakers",
      "market_type": "moneyline",
      "selection": "Boston Celtics",
      "model_probability": 0.64,
      "confidence": "high",
      "notes": "Celtics strong at home, Lakers missing AD",
      "event_start": "2026-02-18T20:00:00Z",
      "polymarket_slug": ""
    }
  ]
}
```

## Fields
- event_id: unique string you assign per event (used to avoid duplicate bets)
- sport: Odds API sport key (americanfootball_nfl, basketball_nba, soccer_epl, etc.)
- league: human readable (NFL, NBA, EPL, etc.)
- home_team / away_team: full team names (must match sportsbook names for odds lookup)
- market_type: moneyline | spread | totals
- selection: the thing you're backing (team name, "over", "under")
- model_probability: YOUR model's estimated win probability (0.0-1.0)
- confidence: high / medium / low
- notes: any research notes (shown in dashboard)
- event_start: ISO8601 UTC datetime
- polymarket_slug: (optional) if you know the exact Polymarket market slug

## Confidence → bet filter
  high   → bot bets if edge >= min_edge_pct
  medium → bot bets if edge >= min_edge_pct + 2%
  low    → bot skips (never bet low confidence)
"""
        with open(spec_path, "w") as f:
            f.write(spec)
        logger.info(f"  [SCRAPER] Wrote pick format spec to {spec_path}")

    def read_picks(self) -> list:
        """Read all pick files and return deduplicated list of ScraperPick objects."""
        picks = []
        seen_ids = set()

        # Find all JSON files (newest first)
        pattern = os.path.join(self.folder, "*.json")
        files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

        for filepath in files:
            try:
                with open(filepath) as f:
                    data = json.load(f)
                gen_time = data.get("generated_at", "")
                for p in data.get("picks", []):
                    eid = p.get("event_id", "")
                    sel = p.get("selection", "")
                    key = f"{eid}:{sel}"
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)

                    # Filter by confidence: skip "low"
                    confidence = p.get("confidence", "medium").lower()
                    if confidence == "low":
                        continue

                    # Filter stale picks: event must be in the future
                    event_start_str = p.get("event_start", "")
                    if event_start_str:
                        try:
                            event_start = datetime.fromisoformat(
                                event_start_str.replace("Z", "+00:00")
                            )
                            if event_start < datetime.now(timezone.utc):
                                continue  # event already started/finished
                        except ValueError:
                            pass

                    picks.append(ScraperPick(
                        event_id=eid,
                        sport=p.get("sport", ""),
                        league=p.get("league", ""),
                        home_team=p.get("home_team", ""),
                        away_team=p.get("away_team", ""),
                        market_type=p.get("market_type", "moneyline"),
                        selection=sel,
                        model_prob=float(p.get("model_probability", 0)),
                        confidence=confidence,
                        notes=p.get("notes", ""),
                        event_start=event_start_str,
                        file_timestamp=gen_time,
                        polymarket_slug=p.get("polymarket_slug", ""),
                    ))
            except Exception as e:
                logger.warning(f"  [SCRAPER] Failed to read {filepath}: {e}")

        logger.info(f"  [SCRAPER] {len(picks)} valid picks from {len(files)} file(s)")
        return picks


# ─────────────────────────────────────────────
# POLYMARKET SPORTS DISCOVERY
# ─────────────────────────────────────────────

class SportsMarketDiscovery:
    """
    Finds active sports markets on Polymarket.
    Uses keyword search + slug matching on the Gamma API.
    """

    def __init__(self, config: SportsConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SportsBettingBot/1.0"

    def find_market_for_pick(self, pick: ScraperPick) -> Optional[PolymarketSportsMarket]:
        """Find the Polymarket market that matches a scraper pick."""

        # If scraper provided a direct slug, use it
        if pick.polymarket_slug:
            m = self._fetch_by_slug(pick.polymarket_slug)
            if m:
                return m

        # Otherwise search by keyword
        return self._search_by_teams(pick)

    def _fetch_by_slug(self, slug: str) -> Optional[PolymarketSportsMarket]:
        try:
            r = self.session.get(
                f"{self.config.polymarket_gamma_url}/markets",
                params={"slug": slug},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return self._parse_market(data[0])
        except Exception as e:
            logger.debug(f"  [DISCOVERY] Slug fetch failed for {slug}: {e}")
        return None

    def _search_by_teams(self, pick: ScraperPick) -> Optional[PolymarketSportsMarket]:
        """Search Gamma API for a market matching home/away team names."""
        search_terms = [
            f"{pick.home_team} {pick.away_team}",
            pick.home_team,
            pick.away_team,
        ]

        for term in search_terms:
            try:
                r = self.session.get(
                    f"{self.config.polymarket_gamma_url}/markets",
                    params={
                        "q": term,
                        "limit": 10,
                        "active": "true",
                        "closed": "false",
                    },
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()

                for m_data in data:
                    market = self._parse_market(m_data)
                    if not market:
                        continue

                    # Check end time is near the event start
                    if pick.event_start:
                        try:
                            event_dt = datetime.fromisoformat(
                                pick.event_start.replace("Z", "+00:00")
                            )
                            # Market should end within 24h of event start
                            if abs((market.end_time - event_dt).total_seconds()) > 86400:
                                continue
                        except ValueError:
                            pass

                    # Check team names appear in question
                    q_lower = market.question.lower()
                    home_lower = pick.home_team.lower().split()
                    away_lower = pick.away_team.lower().split()
                    # At least one token from each team name should appear
                    home_match = any(t in q_lower for t in home_lower if len(t) > 3)
                    away_match = any(t in q_lower for t in away_lower if len(t) > 3)
                    if home_match or away_match:
                        logger.info(
                            f"  [DISCOVERY] Matched: '{pick.home_team} vs {pick.away_team}'"
                            f" → '{market.question[:60]}'"
                        )
                        return market

                time.sleep(0.2)  # rate limit
            except Exception as e:
                logger.debug(f"  [DISCOVERY] Search failed for '{term}': {e}")

        return None

    def _parse_market(self, m: dict) -> Optional[PolymarketSportsMarket]:
        """Parse a Gamma API market dict into PolymarketSportsMarket."""
        try:
            # Prices
            prices_raw = m.get("outcomePrices", "")
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            prices = [float(p) for p in prices_raw] if prices_raw else []
            if not prices:
                return None

            # Token IDs
            token_ids_raw = m.get("clobTokenIds", "")
            if isinstance(token_ids_raw, str):
                token_ids_raw = json.loads(token_ids_raw)
            token_ids = token_ids_raw or []

            # Outcomes (outcome names)
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

            # End time
            end_str = m.get("endDate", "")
            if not end_str:
                return None
            end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if end_time < now:
                return None

            seconds_to_close = int((end_time - now).total_seconds())

            return PolymarketSportsMarket(
                slug=m.get("slug", ""),
                question=m.get("question", ""),
                outcomes=outcomes,
                prices=prices,
                token_ids=token_ids,
                end_time=end_time,
                seconds_to_close=seconds_to_close,
                volume=float(m.get("volume", 0) or 0),
            )
        except Exception as e:
            logger.debug(f"  [DISCOVERY] Parse failed: {e}")
            return None

    def find_all_active_sports(self) -> list:
        """
        Browse all active markets and return sports-looking ones.
        Used for dashboard display and manual review.
        """
        sports_keywords = [
            "beat", "win", "vs", "championship", "final", "league",
            "nfl", "nba", "nhl", "mlb", "premier", "ufc", "mma",
            "tennis", "score", "goal", "match", "game",
        ]
        markets = []
        try:
            r = self.session.get(
                f"{self.config.polymarket_gamma_url}/markets",
                params={"limit": 200, "active": "true", "closed": "false"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            now = datetime.now(timezone.utc)
            for m in data:
                q = m.get("question", "").lower()
                slug = m.get("slug", "").lower()
                if any(k in q or k in slug for k in sports_keywords):
                    market = self._parse_market(m)
                    if market and market.seconds_to_close > 0:
                        markets.append(market)
        except Exception as e:
            logger.debug(f"  [DISCOVERY] Browse failed: {e}")
        return markets


# ─────────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────────

class SportsSignalEngine:
    """
    Finds value: compares Polymarket price vs sharp book probability.
    Only generates signals when edge is clear and model agrees.
    """

    def __init__(self, config: SportsConfig, odds_client: SharpOddsClient):
        self.config = config
        self.odds = odds_client

    def analyze(
        self,
        pick: ScraperPick,
        market: PolymarketSportsMarket,
    ) -> Optional[BetSignal]:
        """
        Try to find a value bet for this pick.
        Returns BetSignal if edge found, None otherwise.
        """
        # Timing checks
        if pick.event_start:
            try:
                event_dt = datetime.fromisoformat(
                    pick.event_start.replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                hours_to_event = (event_dt - now).total_seconds() / 3600

                if hours_to_event < self.config.min_hours_before_event:
                    logger.info(
                        f"  [SIGNAL] {pick.selection} skip: "
                        f"only {hours_to_event:.1f}h to event (min {self.config.min_hours_before_event}h)"
                    )
                    return None

                if hours_to_event > self.config.max_hours_before_event:
                    logger.info(
                        f"  [SIGNAL] {pick.selection} skip: "
                        f"{hours_to_event:.1f}h to event > max {self.config.max_hours_before_event}h"
                    )
                    return None
            except ValueError:
                pass

        # Minimum model confidence
        if pick.model_prob < self.config.min_model_prob:
            logger.info(
                f"  [SIGNAL] {pick.selection} skip: "
                f"model_prob={pick.model_prob:.2f} < min {self.config.min_model_prob}"
            )
            return None

        # Confidence-based edge adjustment
        confidence_adj = {
            "high": 0.0,
            "medium": 0.02,   # require 2% extra edge for medium confidence
        }
        extra_edge = confidence_adj.get(pick.confidence, 0.02)
        effective_min_edge = self.config.min_edge_pct + extra_edge * 100

        # Find which outcome on Polymarket matches the selection
        selection_idx, polymarket_price = self._match_outcome(pick.selection, market)
        if selection_idx is None:
            logger.info(
                f"  [SIGNAL] {pick.selection} skip: "
                f"no outcome match in market '{market.question[:50]}'"
            )
            return None

        # Get sharp odds
        sharp_line = None
        if self.config.odds_api_key:
            sharp_lines = self.odds.get_odds(pick.sport, pick.event_id)
            if sharp_lines:
                sharp_line = self.odds.get_best_sharp_prob(sharp_lines, pick.selection)

        if sharp_line is None and self.config.require_both:
            logger.info(
                f"  [SIGNAL] {pick.selection} skip: "
                f"no sharp odds found (require_both=True)"
            )
            return None

        # Calculate edge
        sharp_prob = sharp_line.true_prob if sharp_line else None

        if self.config.require_both and sharp_prob:
            # Conservative: use the lower of the two estimates
            consensus_prob = min(sharp_prob, pick.model_prob)
        elif sharp_prob:
            consensus_prob = sharp_prob
        else:
            consensus_prob = pick.model_prob

        edge_pct = (consensus_prob - polymarket_price) * 100

        if edge_pct < effective_min_edge:
            book_str = f" | sharp={sharp_prob:.3f}" if sharp_prob else ""
            logger.info(
                f"  [SIGNAL] {pick.selection} skip: "
                f"edge={edge_pct:.1f}% < {effective_min_edge:.1f}% | "
                f"poly={polymarket_price:.3f} model={pick.model_prob:.3f}{book_str}"
            )
            return None

        # Sharp book must also think this is > 50% (avoid backing longshots)
        if sharp_prob and sharp_prob < self.config.min_sharp_prob:
            logger.info(
                f"  [SIGNAL] {pick.selection} skip: "
                f"sharp_prob={sharp_prob:.3f} < {self.config.min_sharp_prob} (book disagrees)"
            )
            return None

        # Calculate Kelly size
        # Kelly formula for binary: f = (p*(1/price) - 1) / (1/price - 1)
        # where p = true probability, price = cost per share (= polymarket_price)
        # Simplified: f = (p - polymarket_price) / (1 - polymarket_price)
        kelly_full = (consensus_prob - polymarket_price) / (1 - polymarket_price)
        kelly_size = kelly_full * self.config.kelly_fraction * self.config.bankroll_usd
        size_usd = max(
            self.config.min_bet_usd,
            min(kelly_size, self.config.max_bet_usd)
        )
        size_usd = round(size_usd, 2)

        book_str = sharp_line.book if sharp_line else "model_only"
        reasoning = (
            f"{pick.league}: {pick.home_team} vs {pick.away_team} | "
            f"Backing: {pick.selection} | "
            f"Poly={polymarket_price:.3f} | "
            f"Sharp({book_str})={sharp_prob:.3f if sharp_prob else 'N/A'} | "
            f"Model={pick.model_prob:.3f} | "
            f"Edge={edge_pct:.1f}% | "
            f"Confidence={pick.confidence} | "
            f"Notes: {pick.notes[:60]}"
        )

        logger.info(f"  [SIGNAL] *** VALUE BET FOUND *** {reasoning}")

        return BetSignal(
            pick=pick,
            market=market,
            selection_idx=selection_idx,
            polymarket_price=polymarket_price,
            sharp_prob=sharp_prob or 0.0,
            model_prob=pick.model_prob,
            edge_pct=round(edge_pct, 2),
            kelly_size=round(kelly_size, 2),
            size_usd=size_usd,
            sharp_book=book_str,
            reasoning=reasoning,
        )

    def _match_outcome(
        self, selection: str, market: PolymarketSportsMarket
    ) -> tuple:
        """
        Match a selection string to a market outcome.
        Returns (index, price) or (None, None).
        """
        for i, outcome in enumerate(market.outcomes):
            if SharpOddsClient._names_match(outcome, selection, threshold=0.5):
                if i < len(market.prices):
                    return i, market.prices[i]
        return None, None


# ─────────────────────────────────────────────
# EXECUTOR
# ─────────────────────────────────────────────

class SportsExecutor:
    """Executes bets on Polymarket via CLOB. Same mechanism as crypto bot."""

    def __init__(self, config: SportsConfig):
        self.config = config
        self.clob_client = None
        self._init_attempted = False

    def _ensure_client(self) -> bool:
        if self.clob_client is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True

        pk = self.config.polymarket_private_key
        funder = self.config.polymarket_funder

        if not pk or pk.startswith("<"):
            logger.warning("  [EXEC] No private key — live execution disabled")
            return False

        try:
            from py_clob_client.client import ClobClient
            sig_type = int(os.getenv("POLYMARKET_SIG_TYPE", "2"))
            kwargs = {"host": "https://clob.polymarket.com", "key": pk, "chain_id": 137, "signature_type": sig_type}
            if funder:
                kwargs["funder"] = funder
            self.clob_client = ClobClient(**kwargs)
            self.clob_client.set_api_creds(self.clob_client.create_or_derive_api_creds())
            logger.info(f"  [EXEC] CLOB ready (sig_type={sig_type})")
            return True
        except Exception as e:
            logger.error(f"  [EXEC] CLOB init failed: {e}")
            return False

    def execute(self, signal: BetSignal, db: SportsDB) -> bool:
        """Place a bet (or simulate in paper mode)."""
        pick = signal.pick
        market = signal.market
        token_id = (
            market.token_ids[signal.selection_idx]
            if signal.selection_idx < len(market.token_ids)
            else ""
        )

        bet_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sport": pick.sport,
            "league": pick.league,
            "event_id": pick.event_id,
            "home_team": pick.home_team,
            "away_team": pick.away_team,
            "market_type": pick.market_type,
            "selection": pick.selection,
            "polymarket_slug": market.slug,
            "polymarket_price": signal.polymarket_price,
            "sharp_prob": signal.sharp_prob,
            "model_prob": signal.model_prob,
            "edge_pct": signal.edge_pct,
            "size_usd": signal.size_usd,
            "token_id": token_id,
            "shares": 0.0,
            "order_id": "",
            "status": "paper" if self.config.dry_run else "pending",
            "event_start": pick.event_start,
            "reasoning": signal.reasoning,
            "sharp_book": signal.sharp_book,
        }

        mode = "[PAPER]" if self.config.dry_run else "[LIVE]"
        logger.info(
            f"  {mode} BET | {pick.league} | {pick.selection} @ {signal.polymarket_price:.3f} | "
            f"${signal.size_usd:.2f} | Edge {signal.edge_pct:.1f}% | "
            f"Sharp={signal.sharp_prob:.3f} Model={signal.model_prob:.3f}"
        )

        if self.config.dry_run:
            # Paper: simulate fill at the market price (no real order)
            shares = signal.size_usd / signal.polymarket_price
            bet_data["shares"] = round(shares, 4)
            bet_data["status"] = "paper"
            fee_sim = signal.size_usd * self.config.polymarket_fee_pct
            logger.info(
                f"  >>> [PAPER FILL] {pick.selection} | "
                f"${signal.size_usd:.2f} → {shares:.2f} shares @ {signal.polymarket_price:.4f} | "
                f"Simulated fee: ${fee_sim:.3f}"
            )
            db.save_bet(bet_data)
            return True

        # Live execution
        if not self._ensure_client():
            logger.error("  [EXEC] CLOB not ready for live trade")
            bet_data["status"] = "failed"
            db.save_bet(bet_data)
            return False

        if not token_id:
            logger.error(f"  [EXEC] No token ID for selection index {signal.selection_idx}")
            bet_data["status"] = "failed"
            db.save_bet(bet_data)
            return False

        result = None
        for attempt in range(1, self.config.max_retries + 1):
            if self.config.use_limit_orders:
                result = self._place_limit(token_id, signal.polymarket_price, signal.size_usd)
            else:
                result = self._place_market(token_id, signal.size_usd)

            if result:
                break
            if attempt < self.config.max_retries:
                logger.warning(f"  [RETRY] Attempt {attempt} failed...")
                time.sleep(2)

        if result:
            bet_data["status"] = "live"
            bet_data["order_id"] = result.get("orderID", "")
            taking = result.get("takingAmount", "?")
            making = result.get("makingAmount", "?")
            try:
                shares = float(taking)
                cost = float(making)
                bet_data["shares"] = round(shares, 4)
                bet_data["size_usd"] = round(cost, 4)
                logger.info(f"  >>> FILLED: {shares:.2f} shares @ ${cost:.4f}")
            except (ValueError, TypeError):
                bet_data["shares"] = round(signal.size_usd / signal.polymarket_price, 4)
        else:
            bet_data["status"] = "failed"
            logger.error(f"  [FAIL] All attempts failed for {pick.selection}")

        db.save_bet(bet_data)
        return result is not None

    def _place_market(self, token_id: str, amount_usd: float) -> Optional[dict]:
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            args = MarketOrderArgs(token_id=token_id, amount=round(amount_usd, 2), side=BUY)
            signed = self.clob_client.create_market_order(args)
            resp = self.clob_client.post_order(signed, OrderType.FOK)
            return resp if resp and resp.get("success") else None
        except Exception as e:
            logger.error(f"  [ORDER] Market order error: {e}")
            return None

    def _place_limit(self, token_id: str, price: float, amount_usd: float) -> Optional[dict]:
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            shares = round(amount_usd / price, 2)
            args = OrderArgs(token_id=token_id, price=round(price, 3), size=shares, side=BUY)
            signed = self.clob_client.create_order(args)
            resp = self.clob_client.post_order(signed, OrderType.GTC)
            if not resp or not resp.get("success"):
                return None
            order_id = resp.get("orderID", "")
            logger.info(f"  [ORDER] Limit GTC posted @ {price:.3f} x {shares} shares | {order_id[:16]}...")
            # Poll for fill
            for _ in range(self.config.limit_order_timeout_s // 5):
                time.sleep(5)
                try:
                    status = self.clob_client.get_order(order_id)
                    if status and (status.get("status") == "MATCHED" or
                                   float(status.get("size_matched", 0)) >= shares * 0.95):
                        logger.info(f"  [ORDER] Limit filled")
                        return resp
                    if status and status.get("status") in ("CANCELLED", "EXPIRED"):
                        return None
                except Exception:
                    pass
            logger.warning(f"  [ORDER] Limit timeout — cancelling")
            try:
                self.clob_client.cancel(order_id)
            except Exception:
                pass
            return None
        except Exception as e:
            logger.error(f"  [ORDER] Limit order error: {e}")
            return None


# ─────────────────────────────────────────────
# RESOLUTION
# ─────────────────────────────────────────────

class SportsResolver:
    """
    Resolves settled bets by checking Polymarket market outcomes.
    """

    def __init__(self, config: SportsConfig, db: SportsDB):
        self.config = config
        self.db = db
        self.session = requests.Session()

    def resolve_pending(self):
        """Check all open bets and settle those whose events have finished."""
        open_bets = self.db.open_bets()
        if not open_bets:
            return

        now = datetime.now(timezone.utc)
        resolved = 0

        for bet in open_bets:
            slug = bet.get("polymarket_slug", "")
            if not slug:
                continue

            # Check if event has ended (with 30 min buffer for settlement)
            event_start_str = bet.get("event_start", "")
            if event_start_str:
                try:
                    event_start = datetime.fromisoformat(
                        event_start_str.replace("Z", "+00:00")
                    )
                    # Sports events typically last 2-4 hours; use 6h as safe buffer
                    if now < event_start + timedelta(hours=6):
                        continue
                except ValueError:
                    pass

            # Fetch market resolution from Polymarket
            outcome = self._check_resolution(slug, bet["selection"])
            if outcome is None:
                continue

            size_usd = bet["size_usd"]
            entry_price = bet["polymarket_price"]
            fee = size_usd * self.config.polymarket_fee_pct if bet["status"] == "paper" else 0

            if outcome == "WIN":
                pnl = size_usd * (1.0 / entry_price - 1.0) - fee
            else:
                pnl = -size_usd - fee

            self.db.update_bet(
                bet["id"],
                resolved=1,
                outcome=outcome,
                pnl=round(pnl, 4),
                status="settled",
            )
            resolved += 1

            emoji = "+++" if outcome == "WIN" else "---"
            logger.info(
                f"  [{emoji}] {outcome} | {bet['league']} {bet['selection']} @ {entry_price:.3f} | "
                f"PnL: ${pnl:+.2f}"
            )

            # Post-resolution reflection: write a 1-line lesson to LEARNINGS.md
            try:
                _reflect_on_outcome(bet, outcome, entry_price)
            except Exception as _e:
                logger.debug(f"  [REFLECT] Reflection skipped: {_e}")

        if resolved > 0:
            perf = self.db.get_performance()
            logger.info(
                f"  [SETTLED] {resolved} bets | "
                f"W:{perf.get('wins',0)} L:{perf.get('losses',0)} | "
                f"PnL: ${perf.get('total_pnl',0):+.2f}"
            )

    def _check_resolution(self, slug: str, selection: str) -> Optional[str]:
        """
        Fetch market from Polymarket and check if it's resolved.
        Returns "WIN", "LOSS", or None if still pending.
        """
        try:
            r = self.session.get(
                f"{self.config.polymarket_gamma_url}/markets",
                params={"slug": slug},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                return None

            m = data[0]
            is_resolved = m.get("resolved", False)
            if not is_resolved:
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

            # Find which outcome resolved to 1 (winner)
            for i, (outcome, price) in enumerate(zip(outcomes, prices)):
                if price >= 0.95:
                    # This outcome won. Did we bet on it?
                    if SharpOddsClient._names_match(outcome, selection, threshold=0.5):
                        return "WIN"
                    else:
                        return "LOSS"

            return None  # Not fully resolved yet
        except Exception as e:
            logger.debug(f"  [RESOLVE] Failed for {slug}: {e}")
            return None


# ─────────────────────────────────────────────
# POST-RESOLUTION REFLECTION
# ─────────────────────────────────────────────

def _reflect_on_outcome(bet: dict, outcome: str, entry_price: float) -> None:
    """
    Use Claude Haiku to generate a 1-line lesson from a resolved trade and write
    it to agents/LEARNINGS.md. Called after every WIN/LOSS in resolve_pending().

    Uses Haiku (cheap) — just a single non-agentic completion call.
    Silently skips if ANTHROPIC_API_KEY not set or agents/LEARNINGS.md not found.
    """
    from pathlib import Path as _Path
    if not _Path("agents/LEARNINGS.md").exists():
        return

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return

    selection    = bet.get("selection", "?")
    league       = bet.get("league", "?")
    confidence   = bet.get("confidence", "?")
    edge_pct     = bet.get("edge_pct", None)
    edge_str     = f", edge={edge_pct:.1f}%" if edge_pct is not None else ""

    prompt = (
        f"A sports value bet just resolved.\n"
        f"  League: {league}\n"
        f"  Selection: {selection}\n"
        f"  Entry price: {entry_price:.3f}{edge_str}\n"
        f"  Confidence: {confidence}\n"
        f"  Result: {outcome}\n\n"
        f"In exactly 1 sentence, what is the single most important thing to remember "
        f"about a trade like this? Focus on what the entry price or confidence level "
        f"tells us, not just 'we won' or 'we lost'. Be specific and actionable."
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        lesson_text = resp.content[0].text.strip() if resp.content else ""
    except Exception as e:
        logger.debug(f"  [REFLECT] Haiku call failed: {e}")
        return

    if not lesson_text:
        return

    correction = (
        "Monitor entries near this price level more carefully"
        if outcome == "LOSS"
        else "This entry profile is working — reinforce these conditions"
    )
    rule = f"{'Avoid' if outcome == 'LOSS' else 'Favour'} {selection}-style entries at {entry_price:.2f} with {confidence} confidence"

    try:
        from agents.tools import write_lesson
        result = write_lesson(
            agent_name="Pipeline",
            what_went_wrong=lesson_text if outcome == "LOSS" else f"(WIN) {lesson_text}",
            correction=correction,
            rule_generated=rule,
        )
        if result.get("written"):
            logger.debug(f"  [REFLECT] Lesson written for {outcome} on {selection}")
    except Exception as e:
        logger.debug(f"  [REFLECT] write_lesson call failed: {e}")


# ─────────────────────────────────────────────
# MAIN BOT
# ─────────────────────────────────────────────

class SportsBot:
    def __init__(self, config: SportsConfig = None):
        self.config = config or SportsConfig()
        self.db = SportsDB(self.config.db_path)
        self.scraper = ScraperReader(self.config.scraper_folder)
        self.odds_client = SharpOddsClient(self.config.odds_api_key, self.config.sharp_books)
        self.discovery = SportsMarketDiscovery(self.config)
        self.signal_engine = SportsSignalEngine(self.config, self.odds_client)
        self.executor = SportsExecutor(self.config)
        self.resolver = SportsResolver(self.config, self.db)

    def scan(self) -> dict:
        logger.info("-" * 65)
        logger.info(f"Scan at {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

        # Daily bet limit
        daily = self.db.daily_bets()
        if daily >= self.config.max_daily_bets:
            logger.info(f"  Daily bet limit reached ({daily}/{self.config.max_daily_bets})")
            return {"picks": 0, "markets": 0, "signals": 0, "bets": 0}

        # Max open bets
        open_count = len(self.db.open_bets())
        if open_count >= self.config.max_open_bets:
            logger.info(f"  Max open bets reached ({open_count}/{self.config.max_open_bets})")
            return {"picks": 0, "markets": 0, "signals": 0, "bets": 0}

        # 1. Read scraper picks
        picks = self.scraper.read_picks()
        if not picks:
            logger.info("  No picks from scraper yet. Waiting for scraper_data/*.json files.")
            self.db.log_scan(0, 0, 0, 0)
            return {"picks": 0, "markets": 0, "signals": 0, "bets": 0}

        # 2. Find matching Polymarket markets
        signals = []
        markets_found = 0

        for pick in picks:
            # Skip if already bet on this event + selection
            if self.db.already_bet(pick.event_id, pick.selection):
                logger.debug(f"  [SKIP] Already bet: {pick.event_id} | {pick.selection}")
                continue

            market = self.discovery.find_market_for_pick(pick)
            if not market:
                logger.info(
                    f"  [NO MARKET] {pick.league}: {pick.home_team} vs {pick.away_team} "
                    f"— no Polymarket match found"
                )
                continue

            markets_found += 1
            logger.info(
                f"  [MARKET] {pick.league} | {pick.selection} | "
                f"poly={market.prices} | "
                f"ends in {market.seconds_to_close//3600}h"
            )

            # 3. Analyse for value
            signal = self.signal_engine.analyze(pick, market)
            if signal:
                signals.append(signal)

        logger.info(f"  Picks: {len(picks)} | Markets matched: {markets_found} | Signals: {len(signals)}")

        # 4. Execute signals (highest edge first)
        bets_placed = 0
        for signal in sorted(signals, key=lambda s: s.edge_pct, reverse=True):
            if daily + bets_placed >= self.config.max_daily_bets:
                break
            if open_count + bets_placed >= self.config.max_open_bets:
                break
            if self.executor.execute(signal, self.db):
                bets_placed += 1

        self.db.log_scan(len(picks), markets_found, len(signals), bets_placed)

        return {
            "picks": len(picks),
            "markets": markets_found,
            "signals": len(signals),
            "bets": bets_placed,
        }

    def run(self):
        logger.info("=" * 65)
        logger.info("  POLYMARKET SPORTS BETTING BOT v1.0")
        logger.info("=" * 65)
        mode = "LIVE (REAL MONEY)" if not self.config.dry_run else "PAPER"
        logger.info(f"  Mode:         {mode}")
        logger.info(f"  Bankroll:     ${self.config.bankroll_usd:.0f}")
        logger.info(f"  Max bet:      ${self.config.max_bet_usd:.0f}")
        logger.info(f"  Min edge:     {self.config.min_edge_pct:.1f}%")
        logger.info(f"  Kelly frac:   {self.config.kelly_fraction*100:.0f}%")
        logger.info(f"  Require both: {self.config.require_both} (sharp + scraper must agree)")
        logger.info(f"  Scraper dir:  {self.config.scraper_folder}/")
        logger.info(f"  Odds API:     {'configured' if self.config.odds_api_key else 'NOT CONFIGURED (add ODDS_API_KEY to .env)'}")
        logger.info(f"  Scan every:   {self.config.scan_interval_seconds}s")
        logger.info(f"  Orders:       {'LIMIT/GTC (0% fee)' if self.config.use_limit_orders else 'MARKET/FOK'}")
        logger.info("=" * 65)

        if not self.config.dry_run:
            if not self.executor._ensure_client():
                logger.error("  Cannot start LIVE: CLOB client failed. Check .env.")
                return

        if not self.config.odds_api_key:
            logger.warning(
                "  WARNING: No ODDS_API_KEY set. "
                "Bot will only use scraper model probabilities (no sharp reference)."
            )
            if self.config.require_both:
                logger.warning(
                    "  require_both=True but no odds API — no bets will fire. "
                    "Set ODDS_API_KEY or set require_both=False in config."
                )

        cycle = 0
        while True:
            try:
                cycle += 1

                # Resolve completed bets
                self.resolver.resolve_pending()

                # Scan for new opportunities
                result = self.scan()

                # Save status for dashboard
                perf = self.db.get_performance()
                status = {
                    "last_scan": result,
                    "performance": perf,
                    "by_sport": self.db.by_sport(),
                    "recent_bets": self.db.recent_bets(10),
                    "daily_bets": self.db.daily_bets(),
                    "open_bets": len(self.db.open_bets()),
                    "odds_api_remaining": self.odds_client.remaining_requests,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                with open(self.config.status_file, "w") as f:
                    json.dump(status, f, indent=2, default=str)

                time.sleep(self.config.scan_interval_seconds)

            except KeyboardInterrupt:
                self.resolver.resolve_pending()
                logger.info("\nBot stopped.")
                perf = self.db.get_performance()
                logger.info(
                    f"  RESULTS: {perf.get('wins',0)}W / {perf.get('losses',0)}L "
                    f"({perf.get('win_rate',0):.1f}% WR) | "
                    f"PnL: ${perf.get('total_pnl',0):+.2f} | "
                    f"ROI: {perf.get('roi_pct',0):+.1f}%"
                )
                break
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(30)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Sports Betting Bot")
    parser.add_argument("--live", action="store_true", help="Live trading (real money)")
    parser.add_argument("--single-scan", action="store_true", help="Run one scan then exit")
    parser.add_argument("--bankroll", type=float, help="Bankroll in USD")
    parser.add_argument("--max-bet", type=float, help="Max bet per event in USD")
    parser.add_argument("--min-edge", type=float, help="Minimum edge %% to bet")
    parser.add_argument("--no-odds-api", action="store_true", help="Disable sharp odds requirement")
    parser.add_argument("--test-connection", action="store_true", help="Test CLOB connection")
    args = parser.parse_args()

    config = SportsConfig()

    if args.live:
        config.dry_run = False
    if args.bankroll:
        config.bankroll_usd = args.bankroll
    if args.max_bet:
        config.max_bet_usd = args.max_bet
    if args.min_edge:
        config.min_edge_pct = args.min_edge
    if args.no_odds_api:
        config.require_both = False

    if args.test_connection:
        executor = SportsExecutor(config)
        if executor._ensure_client():
            print("CLOB connection: OK")
        else:
            print("CLOB connection: FAILED — check .env")
        exit(0)

    bot = SportsBot(config)

    if args.single_scan:
        bot.resolver.resolve_pending()
        result = bot.scan()
        print(json.dumps(result, indent=2))
    else:
        bot.run()
