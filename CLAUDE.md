# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Model Routing — IMPORTANT

Pick the cheapest model that fits the task. This is a HARD requirement — do not default to Opus for everything.

| Model | When to use | Examples |
|-------|-------------|---------|
| **Haiku** | Simple, repetitive, low-stakes tasks | Cron jobs, file renames, formatting, running scripts, simple git ops, status checks, reading logs, quick lookups |
| **Sonnet** | Conversational, moderate reasoning | Answering questions, explaining code, light refactors, reviewing diffs, writing docs, config changes, small bug fixes |
| **Opus** | Complex reasoning, architecture, heavy code | New features, multi-file refactors, strategy analysis, debugging complex issues, performance optimization, trade analysis |

When spawning subagents via Task tool, always set the `model` parameter to match:
- `model: "haiku"` for search/grep/file-reading tasks, running tests, simple checks
- `model: "sonnet"` for moderate code edits, explanations, reviews
- `model: "opus"` only when the task genuinely requires deep reasoning

If unsure, start with Sonnet. Escalate to Opus only if the task involves multi-step logic, architectural decisions, or complex debugging.

## Project Overview

Python-based automated trading bot for Polymarket prediction markets. Trades 15-minute binary up/down markets for BTC, ETH, and SOL. Uses Binance price feeds for signal generation and Polymarket's CLOB API for order execution.

## Running the Bot

```bash
# Install dependencies
pip install requests>=2.31.0 py-clob-client web3==6.14.0 python-dotenv flask>=3.0.0

# Paper trading (dry run)
python bot.py

# Live trading
python bot.py --live

# Single scan (no loop)
python bot.py --single-scan

# Test CLOB connection
python bot.py --test-connection

# Filter strategies
python bot.py --arb-only
python bot.py --momentum-only

# Custom parameters
python bot.py --live --coins btc eth sol --bankroll 100 --max-trade 5 --interval 30

# Web dashboard (starts bot from UI)
python server.py
# or double-click start_dashboard.bat
```

### Utility Scripts

```bash
python tg_bot.py              # Telegram alerts (runs alongside bot)
python redeem.py              # Redeem winning positions (--check, --loop)
python check_costs.py         # Orderbook spread/slippage analysis
python performance.py         # Performance dashboard
python server.py              # Web dashboard on http://localhost:8050
```

## Architecture

All core logic lives in `bot.py` (~2000 lines) with these key classes:

- **`Config`** — Dataclass holding all settings. Reads from `.env` + CLI args.
- **`TradeDB`** — SQLite wrapper (`trades_15m.db`). Tables: `trades`, `scans`. Auto-migrates schema.
- **`PriceClient`** — Binance API client. Provides `get_price()`, `get_klines()`, `compute_rsi()`, `compute_momentum()`. No auth needed.
- **`MarketDiscovery`** — Finds active 15-minute markets via Polymarket's Gamma API (`gamma-api.polymarket.com`). Returns `Market15m` dataclass objects.
- **`StrategyEngine`** — Four strategies that return `Signal` objects:
  1. **Arbitrage** — Buy both sides if combined price < $1.00 (risk-free)
  2. **Trend Rider** — Buy strong side when Binance momentum confirms direction
  3. **Early Scalp** — Trade near 50/50 when Binance shows early directional move
  4. **Strong Reversal** — Contrarian play on massive Binance reversals only
- **`Executor`** — Places orders via `py-clob-client`. Supports market (taker, ~1.5% fee) and limit (maker, 0% fee) orders.
- **`Bot15m`** — Main orchestrator. Loop: discover markets → analyze signals → execute trades → check early exits → resolve outcomes. Scans every 30 seconds.

### Web Dashboard (`server.py`)

Flask app serving a vanilla JS dashboard on `localhost:8050`. Manages `bot.py` and `tg_bot.py` as subprocesses. API endpoints: `/api/status`, `/api/trades`, `/api/performance`, `/api/bot/start|stop`, `/api/tg/start|stop`, `/api/logs`.

### Execution Flow

```
Bot15m.run() loop (every 30s):
  discover_markets() → list of Market15m
  strategy.analyze(market) → list of Signal
  execute(signal) → place order, record in TradeDB
  check_early_exits() → sell winners at 0.88+, stop-loss mid-window
  resolve_outcomes() → mark WIN/LOSS, calculate PnL
```

### Signal Filters & Sizing (v6.1)

- **Flat $5 sizing** — no tiered sizing until 50+ trades validate tiers
- **UP bets**: net_from_open >= 0.20% required (weak UP signals lose money)
- **DOWN bets**: net_from_open >= 0.15% required (cheaper entry = better R:R)
- **Max entry price**: 0.91 (orderbook best_ask, not midpoint)
- **Edge recalculation**: edge recomputed after orderbook check; rejected if below min_edge
- **Fair value scaling**: 0.70 (weak) / 0.78 (medium) / 0.85 (strong) by net strength

### Early Exit System

- **Win exit**: Sell at ANY time if token midpoint >= 0.88 (locks in ~60% of max profit, eliminates late-reversal risk)
- **Stop-loss**: Mid-window (300-600s remaining), if token < 0.45 AND Binance net has flipped against position, sell to recover partial capital

### Risk Controls

- Min edge filter (default 3%), daily trade limit (default 50)
- Max 1 trade per 15-minute market window
- Trades only placed 1-14 min into market (avoids early noise and late liquidity issues)
- Consecutive loss pause threshold (5 losses → circuit breaker)
- 15-minute cooldown after any loss
- 60-second minimum before market close

## Configuration

Copy `env.template` to `.env`. Key variables:
- `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIG_TYPE` — Wallet credentials
- `BOT_BANKROLL`, `BOT_MAX_TRADE`, `BOT_MIN_EDGE`, `BOT_DRY_RUN` — Trading parameters
- `TG_BOT_TOKEN`, `TG_CHAT_ID` — Telegram alerts

## Data Files

- `trades_15m.db` — SQLite trade history (primary)
- `trades.db` — Legacy database
- `bot.log` — Execution log
- `bot_status.json` — Real-time status snapshot

---

## Workflow Orchestration

### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity
- **Before writing any code, explicitly decide: is this plan overbuilt, underbuilt, or engineered enough?** State the answer out loud. Overbuilt = unnecessary abstractions. Underbuilt = will break in production. Engineered enough = solves the problem cleanly with no excess.

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `tasks/lessons.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project context

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
- **Aggressively review test coverage, edge cases, and failure modes** — before calling anything done, enumerate: what happens on empty input, network failure, bad data, concurrent access, and boundary values? If any failure mode is unhandled and realistic, fix it.

### 5. Demand Elegance (Balanced)
- For non-trivial changes, pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it
- **Look for performance risks, scaling issues, and refactoring opportunities** — ask: does this break under 10x load? Is there an N+1 query, a growing list that should be a set, or a blocking call that should be async? Flag these even if not fixing them now.

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

---

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plans**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections

---

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
