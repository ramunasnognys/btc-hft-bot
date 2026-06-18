# BTC 5-Minute Late-Window Confirmation Bot

> **Status: Phase 4 complete — full pipeline built (dry-run default, `--live` arms real orders, `report.py` scores results)**

A trading bot for Polymarket's [Bitcoin Up or Down 5-minute markets](https://polymarket.com). Instead of predicting BTC direction, it waits until ~120 seconds remain in each window, confirms the current winner, and bets on it — only if a multi-guard evaluation stack approves the trade.

Settlement is determined by Polymarket's **Chainlink BTC/USD feed**, not spot prices. The bot subscribes directly to that same feed.

---

## How It Works

Each 5-minute window has an opening (strike) price. At close, whichever side (Up or Down) the Chainlink price is on wins. The bot:

1. Captures the opening strike price by watching the Chainlink stream across the window boundary
2. Waits until ~120 seconds remain (the entry band the backtest supported)
3. Runs a 9-guard evaluation stack before committing any money
4. Buys the current favorite — only if all guards pass

**This is not a prediction strategy.** It confirms a near-settled outcome and pays a small premium to collect the rest of it.

### Guard Stack

| # | Guard | Condition |
|---|-------|-----------|
| 1 | Feed | Chainlink tick age ≤ 3s |
| 2 | Strike | Window open was observed (bot was connected before it opened) |
| 3 | Favorite | Price clearly on one side of the strike (no tie) |
| 4 | Cushion | \|price − strike\| ≥ $25 |
| 5 | Momentum | Recent velocity not projecting a cross back over the strike |
| 6 | Book | Spread ≤ 0.07, enough depth for the stake |
| 7 | Price cap | Best ask ≤ 0.97 |
| 8 | Edge | `est_win_prob − ask ≥ 0.0` |
| 9 | Risk | One position at a time; daily loss cap not hit |

---

## Project Structure

```
btc_feed.py       # Phase 1 — Chainlink feed + strike capture (no trading)
btc_strategy.py   # Phase 2 — Full decision engine in dry-run (no orders)
phase2b_probe.py  # Phase 2b — Liquidity probe across entry checkpoints (no orders)
btc_live.py       # Phase 3 — Live execution (dry-run default, --live for real orders)
report.py         # Phase 4 — Resolution tracking, P&L + expectancy report
btc_bot.py        # Original prototype (Binance price + Polymarket, simpler logic)
setup_env.sh      # One-shot venv builder (run this first)
requirements.txt  # Python dependencies
STRATEGY_PLAN.md  # Full architecture and design decisions
phase2_log.csv    # Decision log output from Phase 2
phase2b_probe.csv # Liquidity-probe log (input to report.py backtest)
trades_live.csv   # Trade log output from Phase 3 (input to report.py)
```

---

## Build Phases

- [x] **Phase 1** — Chainlink websocket feed, window tracking, strike capture, CSV logging
- [x] **Phase 2** — Full guard stack + win-probability model, dry-run decisions logged
- [x] **Phase 3** — Live order execution (`--live` flag), position tracking, daily loss cap
- [x] **Phase 4** — Resolution tracking, realized P&L report, expectancy per trade (`report.py`)

---

## Setup

### Requirements

- Python 3.11+
- Polymarket account with a funded Polygon wallet

### Install

```bash
git clone https://github.com/ramunasnognys/btc-hft-bot.git
cd btc-hft-bot
bash setup_env.sh
```

`setup_env.sh` finds the best available Python 3.11+, creates `.venv`, installs all dependencies, and verifies the SDK import. Safe to re-run.

### Environment Variables

Create a `.env` file in the project root (never committed):

```env
POLY_PRIVATE_KEY=0x...   # Your Polygon wallet private key (required)
```

---

## Running

### Phase 1 — Feed + strike capture (safe, no trades)

Subscribes to the Chainlink BTC/USD stream and captures each window's opening price.

```bash
./.venv/bin/python btc_feed.py
```

Output: live status line + `phase1_log.csv`

### Phase 2 — Decision engine dry-run (safe, no trades)

Runs the full guard stack and logs `WOULD_BUY` or `SKIP (reason)` for every window.

```bash
./.venv/bin/python btc_strategy.py
```

Output: live decisions + `phase2_log.csv`

### Phase 3 — Live execution

```bash
./.venv/bin/python btc_live.py              # dry-run: simulates fills, no SDK needed
./.venv/bin/python btc_live.py --check      # connect to Polymarket, print balance, exit
./.venv/bin/python btc_live.py --live       # REAL ORDERS — asks for typed confirmation
```

Output: live status + `trades_live.csv`

> **Read the dry-run output for at least one session before arming `--live`.**

### Phase 4 — Reporting & expectancy (safe, read-only)

Scores the logs: realized win rate, net P&L, expectancy per trade, fill rate, and
whether the realized win rate **beat the market's implied odds**. Auto-detects the
file type (trade log vs probe log). Pure stdlib — runs with any Python.

```bash
python3 report.py                      # reads trades_live.csv
python3 report.py phase2b_probe.csv    # backtests the liquidity-probe log
```

> Dry-run P&L is simulated (assumes fills at the price cap). Only `LIVE` rows
> reflect real fills and slippage.

---

## Key Parameters

All tunable in the `CONFIG` block at the top of each file:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STAKE_USDC` | `5.0` | USDC spent per trade |
| `DAILY_LOSS_CAP` | `10.0` | Bot halts for the day after this net loss |
| `ENTRY_AT_SECS` | `120` | Evaluate when ≤120s remain |
| `ENTRY_FLOOR_SECS` | `100` | Don't enter if already past this (missed the band) |
| `MIN_CUSHION_USD` | `25` | BTC must be ≥$25 past the strike |
| `MIN_EDGE` | `0.0` | Minimum `est_win_prob − ask` (0 = pure favorite buy) |
| `MAX_ASK` | `0.97` | Never pay more than 97¢ per share |
| `MAX_SPREAD` | `0.07` | Skip illiquid or wide books |
| `SLIP_TOL` | `0.02` | Price cap = `best_ask + SLIP_TOL` |
| `MAX_FEED_STALENESS_S` | `3.0` | Stand down if Chainlink tick is older than this |

---

## Risks & Caveats

- **Thin edge after fees and slippage.** Buying a near-certain favorite at 95¢+ to win $1 is a narrow margin. One bad fill or reversal can erase many wins.
- **Late-window liquidity.** Spreads widen exactly when the bot enters. The fill-or-kill order protects price but means many signals won't fill.
- **Strike basis risk.** The bot measures against the Chainlink feed (the same source Polymarket uses for settlement), which minimizes this risk — but if the feed drops, the bot stands down rather than guess.
- **Windows joined mid-way are skipped.** If the bot wasn't connected before a window opened, it can't know the true strike and will not trade that window.
- **Unproven out-of-sample.** The 120s entry timing is derived from backtest data. Live performance may differ.

---

## License

MIT
