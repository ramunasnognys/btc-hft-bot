# BTC 5-Minute Late-Window Confirmation Bot

> **Status: In Development — Phase 2 complete, Phase 3 (live execution) in progress**

A trading bot for Polymarket's [Bitcoin Up or Down 5-minute markets](https://polymarket.com). Instead of predicting BTC direction, it waits until the **final 30 seconds** of each window, confirms the current winner, and bets on it — only if a multi-guard evaluation stack approves the trade.

Settlement is determined by Polymarket's **Chainlink BTC/USD feed**, not spot prices. The bot subscribes directly to that same feed.

---

## How It Works

Each 5-minute window has an opening (strike) price. At close, whichever side (Up or Down) the Chainlink price is on wins. The bot:

1. Captures the opening strike price by watching the Chainlink stream across the window boundary
2. Waits until 3–30 seconds remain
3. Runs a 9-guard evaluation stack before committing any money
4. Buys the current favorite — only if all guards pass

**This is not a prediction strategy.** It confirms a near-settled outcome and pays a small premium to collect the rest of it.

### Guard Stack

| # | Guard | Condition |
|---|-------|-----------|
| 1 | Feed | Chainlink tick age ≤ 3s |
| 2 | Strike | Window open was observed (bot was connected before it opened) |
| 3 | Favorite | Price clearly on one side of the strike (no tie) |
| 4 | Cushion | \|price − strike\| ≥ $50 |
| 5 | Momentum | Recent velocity not projecting a cross back over the strike |
| 6 | Book | Spread ≤ 0.05, enough depth for the stake |
| 7 | Price cap | Best ask ≤ 0.98 |
| 8 | Edge | `est_win_prob − ask ≥ 0.05` |
| 9 | Risk | Not already decided this window; daily loss cap not hit |

---

## Project Structure

```
btc_feed.py       # Phase 1 — Chainlink feed + strike capture (no trading)
btc_strategy.py   # Phase 2 — Full decision engine in dry-run (no orders)
btc_bot.py        # Original prototype (Binance price + Polymarket, simpler logic)
STRATEGY_PLAN.md  # Full architecture and design decisions
requirements.txt  # Python dependencies
phase2_log.csv    # Decision log output from Phase 2
```

---

## Build Phases

- [x] **Phase 1** — Chainlink websocket feed, window tracking, strike capture, CSV logging
- [x] **Phase 2** — Full guard stack + win-probability model, dry-run decisions logged
- [ ] **Phase 3** — Live order execution (`--live` flag), position tracking, daily loss cap
- [ ] **Phase 4** — Resolution tracking, realized P&L report, expectancy per trade

---

## Setup

### Requirements

- Python 3.10+
- Polymarket account with a funded wallet

### Install

```bash
git clone https://github.com/ramunasnognys/btc-hft-bot.git
cd btc-hft-bot

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

To activate the venv in future sessions:

```bash
source .venv/bin/activate
```

Your prompt will show `(.venv)` when it's active. Use `deactivate` to exit it.

### Environment Variables

Create a `.env` file in the project root (never committed):

```env
POLY_PRIVATE_KEY=0x...         # Your wallet private key
POLY_RELAYER_KEY=...           # Optional: Polymarket relayer API key
POLY_RELAYER_ADDRESS=0x...     # Optional: relayer wallet address
BET_SIZE=1.0                   # USDC per trade (used by btc_bot.py)
```

---

## Running

### Phase 1 — Feed + strike capture (safe, no trades)

Subscribes to the Chainlink BTC/USD stream and captures each window's opening price. Run this first to verify connectivity and confirm the data layer is working.

```bash
python3 btc_feed.py
```

Output: live status line + `phase1_log.csv`

### Phase 2 — Decision engine dry-run (safe, no trades)

Runs the full guard stack for each window and logs `WOULD_BUY` or `SKIP (guard N)` with all the numbers behind the decision. No orders are placed.

```bash
python3 btc_strategy.py
```

Output: live decisions + `phase2_log.csv`

---

## Key Parameters

All tunable in the `CONFIG` block at the top of each file:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STAKE_USDC` | `1.0` | USDC spent per trade |
| `DAILY_LOSS_CAP` | `5.0` | Bot halts for the day after this net loss |
| `MAX_ENTRY_SECONDS` | `30` | Start evaluating at ≤30s left |
| `MIN_ENTRY_SECONDS` | `3` | Don't enter with <3s left |
| `MIN_CUSHION_USD` | `50` | BTC must be ≥$50 past the strike |
| `MIN_EDGE` | `0.05` | Minimum `est_win_prob − ask` |
| `MAX_ASK` | `0.98` | Never pay more than 98¢ per share |
| `MAX_SPREAD` | `0.05` | Skip illiquid or wide books |
| `MAX_FEED_STALENESS_S` | `3.0` | Stand down if Chainlink tick is older than this |

---

## Risks & Caveats

- **Thin edge after fees and slippage.** Buying a near-certain favorite at 95¢+ to win $1 is a narrow margin. One bad fill or reversal can erase many wins.
- **Late-window liquidity.** Spreads widen exactly when the bot enters. The fill-or-kill order protects price but means many signals won't fill.
- **Strike basis risk.** The bot measures against the Chainlink feed (the same source Polymarket uses for settlement), which minimizes this risk — but if the feed drops, the bot stands down rather than guess.
- **Windows joined mid-way are skipped.** If the bot wasn't connected before a window opened, it can't know the true strike and will not trade that window.

---

## License

MIT
