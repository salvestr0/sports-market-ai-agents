# Sports Market AI Agents

A 4-agent AI pipeline that finds value bets on [Polymarket](https://polymarket.com) sports prediction markets. Agents research upcoming events, compare odds against sharp bookmakers, assess risk, and make final BET/SKIP decisions — all autonomously.

---

## How It Works

The pipeline runs four agents in sequence:

| Agent | Model | Role |
|-------|-------|------|
| **Max** | Gemini 2.5 Flash | Researches upcoming sports events, injuries, form, and news |
| **Nova** | Gemini 2.5 Flash | Fetches sharp book odds (Pinnacle/Betfair via The Odds API) and calculates edge vs Polymarket prices |
| **Lumi** | Gemini 2.5 Flash | Devil's advocate risk assessor — challenges every pick |
| **Sage** | Claude Sonnet | Final decision maker — writes approved picks for execution |

Picks are executed by `sports_bot.py` via Polymarket's CLOB API using limit orders (0% maker fee).

---

## Why Polymarket Sports?

- Polymarket sports participants are less sophisticated than sharp bettors
- Sharp books (Pinnacle, Betfair) provide excellent reference prices
- Events resolve cleanly — no oracle divergence
- No HFT bots dominating sports markets

---

## Architecture

```
agents/runner.py          — Pipeline orchestrator (manual /run trigger via Telegram)
agents/max_agent.py       — Research agent (web search, injuries, recent form)
agents/nova_agent.py      — Odds analysis (sharp book vs Polymarket edge calc)
agents/lumi_agent.py      — Risk assessment (bankroll health, red flags)
agents/sage_agent.py      — Final BET/SKIP decisions, writes pick files
agents/tools.py           — Shared tools: web search, odds API, Polymarket lookup
agents/telegram_listener.py — Two-way Telegram interface (/run, /status, Q&A)
agents/notifier.py        — Telegram notifications
sports_bot.py             — Executes picks via Polymarket CLOB API
sports_server.py          — Web dashboard (localhost:8050)
```

**Pick flow:**
```
/run (Telegram)
  → Max researches events
  → Nova checks sharp odds + calculates edge
  → Lumi challenges each pick
  → Sage writes approved picks → scraper_data/agents_{ts}.json
  → sports_bot.py executes via Polymarket CLOB
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp env.template .env
```

Fill in your `.env`:

```env
# Telegram
TG_BOT_TOKEN=        # from @BotFather
TG_CHAT_ID=          # your Telegram chat ID

# AI APIs
ANTHROPIC_API_KEY=   # console.anthropic.com (Sage)
GEMINI_API_KEY=      # aistudio.google.com (Max, Nova, Lumi)
GROK_API_KEY=        # console.x.ai (Max breaking-news pass)

# Data
ODDS_API_KEY=        # the-odds-api.com (free tier: 500 req/month)
TAVILY_API_KEY=      # app.tavily.com (optional — falls back to DuckDuckGo)

# Polymarket
POLYMARKET_PRIVATE_KEY=
POLYMARKET_FUNDER=
POLYMARKET_SIG_TYPE=2

# Bot config
BOT_BANKROLL=100
BOT_MAX_TRADE=25
SPORTS_DRY_RUN=true   # set to false for live execution
```

### 3. Run the pipeline

```bash
# Start the agent pipeline (waits for /run command via Telegram)
python -m agents.runner

# Or run a single batch immediately
python -m agents.runner --once

# Test all API connections
python -m agents.runner --test-tools

# Web dashboard
python sports_server.py
```

---

## Telegram Commands

Once `runner.py` is running, control everything from Telegram:

| Command | Action |
|---------|--------|
| `/run` | Trigger a full pipeline batch |
| `/status` | Show last batch results |
| `/reports` | Detailed agent reports |
| `/help` | List all commands |
| Any text | Ask the agent team a question |

Questions are automatically routed to Haiku (simple lookups) or Sonnet (analysis/strategy) to minimise API costs.

---

## Sizing & Risk

- **Kelly criterion** — 25% fractional Kelly, capped at $25/bet
- **Min edge** — 5% edge required (sharp book vs Polymarket)
- **Model probability** — >= 0.55 required
- **Event timing** — only bets 0.25–48h before event start
- **Limit orders** — 0% maker fee on Polymarket CLOB

---

## Tech Stack

- **Python 3.9+**
- [Anthropic API](https://console.anthropic.com) — Claude Sonnet (Sage)
- [Google Gemini API](https://aistudio.google.com) — Gemini 2.5 Flash (Max, Nova, Lumi)
- [xAI Grok API](https://console.x.ai) — Grok 3 Mini (breaking news)
- [The Odds API](https://the-odds-api.com) — Sharp book reference prices
- [Polymarket CLOB API](https://docs.polymarket.com) — Order execution
- [Tavily](https://app.tavily.com) — Web search (optional)
- Flask — Web dashboard
- SQLite — Trade history

---

## Disclaimer

This project is for educational purposes. Sports betting and prediction market trading carry financial risk. Past performance does not guarantee future results.
