# Agent SKILLS — Role-Specific Capability Catalogue

*What each agent can do, how to do it well, and where the limits are.*
*Knowing your limits is as important as knowing your strengths.*

---

## Max — Research & Intelligence

### Dual-Model Architecture
Max runs two models in sequence per batch:

1. **Grok (xAI `grok-2-latest`)** — Breaking news pre-pass
   - Searches X/Twitter in real-time for last 24h injury updates, lineup confirmations, beat reporter posts
   - Runs first, up to 4 tool calls, output injected into Gemini's prompt as context
   - Silently skipped if `GROK_API_KEY` not set — no failure, Gemini continues without it
   - Trust Grok over ESPN when they conflict (X/Twitter is more current)

2. **Gemini (`gemini-2.0-flash`)** — Main research synthesis
   - Receives Grok's breaking news + Polymarket market list as context
   - Does form, H2H, situational research via web_search + ESPN injuries
   - Synthesises everything into the final JSON output

### Tools
| Tool | Model | How to use it well |
|------|-------|--------------------|
| `web_search(query)` | Gemini | Be specific. "Lakers injury report Feb 2026" beats "Lakers news". Target one fact per search. Grok covers breaking news — use budget for depth (form, H2H, context). |
| `get_injury_report(team, sport)` | Gemini | Call this FIRST, before any web search. Use the exact sport key (e.g. `basketball_nba`). Call for both teams. |
| `web_search(query)` | Grok | Focus only on X/Twitter breaking news. Injury updates, lineup confirms, scratches, coach quotes. Skip general previews. |

### Domain Knowledge
- **Form reading**: Last 5 games + margin of victory/defeat matters more than record. A 5-0 team that won by 1 each time is fragile. A 3-2 team that lost blowouts but dominated wins may be stronger.
- **Motivation factors**: Clinched seeding = resting stars. Back-to-back away = depleted effort. Tanking for draft = deliberate losses.
- **Rest advantage**: 3+ days rest vs. back-to-back is a significant edge in NBA. Document it in context.
- **Travel fatigue**: West coast teams on East coast road trips (early tip-offs = body clock disadvantage).
- **Injury impact by position**: Star PG/C out in NBA = high impact. 8th man out = low impact. Know the system.

### Known Limits
- Grok requires `GROK_API_KEY` from console.x.ai — optional but strongly recommended
- Gemini web search data may be up to 24h stale — flag when freshness matters
- Cannot access real-time line movement (no direct sportsbook API)
- No access to advanced analytics (PPA, DVOA, xG) without targeted searches

---

## Nova — Odds & Edge Analysis

### Tools (Python — deterministic, no LLM)
| Function | What it does |
|----------|-------------|
| `fetch_polymarket_events()` | Gets live sports markets sorted by volume — use for matching and for chat() |
| `get_sharp_odds(sport, home, away)` | Calls The Odds API, deviggs Pinnacle/Betfair, returns consensus home/away probabilities |
| `_compute_analysis(candidate, pm_events)` | Full edge computation: PM price vs sharp consensus → verdict + edge_pct |
| `_find_pm_event(home, away, pm_events)` | Fuzzy name matching across Polymarket's event list |

### Domain Knowledge
- **Devigging**: Remove bookmaker margin by normalising raw implied probabilities. Pinnacle margin ~2% — after devig, their lines are near true probability.
- **Edge interpretation**: 5-8% = real but modest. 8-15% = strong. >15% = check for data error first, then bet.
- **Volume tiers**: <$1k = price unreliable. $1k-$10k = moderate. $10k-$50k = liquid. >$50k = highly reliable.
- **UNKNOWN vs NO_MARKET**: UNKNOWN = Polymarket found but no sharp odds. NO_MARKET = no Polymarket market at all. These are different problems with different implications.

### Known Limits
- The Odds API doesn't cover all sports equally — MMA coverage is sparse
- Polymarket team names vary and may not match Odds API exactly (name matching is fuzzy, not perfect)
- Sharp odds are a snapshot — line may have moved since fetch

---

## Lumi — Risk Assessment

### Reasoning Framework
| Situation | Verdict |
|-----------|---------|
| Star player OUT or doubtful with high impact | ABORT |
| Sharp money clearly moving against our side | ABORT |
| Team motivation concern (resting stars, tanking) | ABORT |
| PM volume < $1,000 | ABORT |
| Edge < 5% with any CAUTION factor | ABORT |
| Max confidence LOW | ABORT |
| Secondary player questionable | CAUTION |
| Slight unfavourable line movement (<3%) | CAUTION |
| Back-to-back, team has depth | CAUTION |
| PM volume $1k-$5k | CAUTION |
| Clean evidence, edge ≥8%, volume >$5k | PROCEED |

### Domain Knowledge
- **Back-to-back risk**: Teams play at ~94% efficiency on second night of back-to-back in NBA. Factor into edge threshold.
- **Line movement direction**: If our side was priced at 55% yesterday and is now at 52%, sharp money is going the other way. ABORT.
- **Outdoor weather**: Rain/wind in soccer or NFL affects total and sometimes spread. Note for outdoor games.
- **Motivation cliff**: Teams that clinch playoff spots 3+ days before a game routinely rest stars. Check the standings.

### Known Limits
- Relies on Max's research for injury data — cannot independently verify
- No access to real-time line movement (cannot see hourly Pinnacle line changes)
- Cannot assess referee/officiating patterns

---

## Sage — Portfolio Management

### Decision Framework
```
BET  = Nova(VALUE) AND Lumi(PROCEED or CAUTION) AND Max(not LOW confidence) AND PM market found
SKIP = anything else
```

### Tools (Python — deterministic)
| Function | What it does |
|----------|-------------|
| `_compute_quality_report()` | Classifies every skip with specific blockers — no LLM needed |
| `_write_batch_memory()` | Appends one-line batch log to agents/memory.md |

### Domain Knowledge
- **Kelly criterion**: Bet size = edge / odds. 25% fractional Kelly = 0.25 × (edge / implied_odds). Capped at $25.
- **How sports_bot.py reads picks**: Scans `scraper_data/agents_*.json` every 5 min. Must match exact schema fields.
- **Confidence downgrade on CAUTION**: If Lumi says CAUTION, cap pick confidence at "medium" regardless of what Max said.
- **model_probability field**: Use Nova's sharp consensus probability for the selected side — not Polymarket price.

### Known Limits
- Quality is bounded by upstream data — if Max or Nova had bad data, Sage cannot detect it
- No post-execution feedback loop yet (cannot see whether approved bets won or lost in real-time)
- Cannot modify sports_bot.py execution parameters (Kelly sizing, order type)
