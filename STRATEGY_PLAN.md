# BTC 5-Minute Late-Window Confirmation Bot — Implementation Plan

*Status: PLAN — nothing built yet. Review and approve before I write code.*

## 1. What this bot does, in one paragraph

For each Polymarket "Bitcoin Up or Down" 5-minute market, the bot waits until the
**final 30 seconds** of the window. By then BTC is usually already clearly above or
below the window's opening price. The bot buys the side that is *already winning*
(the "late favorite") — but only if a stack of guards pass: enough cushion from the
strike, momentum not reversing, acceptable orderbook price/spread/liquidity, and a
positive estimated edge. It risks $1 per trade, stops for the day after $5 of losses,
and logs every decision so we can judge it on realized P&L, not vibes.

This is **not** a prediction strategy. It does not forecast BTC. It confirms a
near-settled outcome and pays a small premium to collect the rest.

## 2. Key facts established during research (these shape the whole design)

- **Settlement feed = Chainlink BTC/USD**, explicitly *not* spot. The market text says:
  *"this market is about the price according to Chainlink data stream BTC/USD, not
  according to other sources or spot markets."* We use Polymarket's own Chainlink
  stream (`prices.crypto.chainlink`, symbol `btc/usd`) via the SDK.
- **The strike/open price is not published.** Resolution rule: *Up wins if the price
  at window end ≥ the price at window start.* So the strike = the Chainlink price at
  the window's opening second. **The bot must record this itself** by watching the
  stream. Consequence: the bot can only trade windows whose open it actually observed.
- **Chainlink price arrives over a websocket** (`AsyncPublicClient.subscribe(...)`),
  not a REST endpoint. We run that subscription in a background thread.
- **Slug format `btc-updown-5m-{window_start_unix}` is correct**; the timestamp is the
  window *start*, aligned to 5-minute boundaries (confirmed against `eventStartTime`).
- **Spot feeds (Binance/Coinbase/Kraken) are used only as a health check** — if spot
  and Chainlink diverge wildly, that signals a feed problem and the bot stands down.
- SDK provides everything for execution: `get_order_book`, `get_spread`, `get_price`,
  `place_limit_order`, `list_positions`.

## 3. Architecture

Four cooperating parts in one process:

1. **Chainlink price thread** (async, background): subscribes to the Chainlink BTC/USD
   stream, keeps the latest `(price, timestamp)` in a thread-safe holder, and flags the
   feed "stale" if no tick arrives for >3s.
2. **Spot health-check** (lightweight): polls one or two spot exchanges occasionally
   only to sanity-check the Chainlink number. Not used for decisions.
3. **Window manager** (main loop): tracks the current 5-minute window, captures its
   opening Chainlink price as the strike, resolves the slug → tokens, and at the right
   time runs the guard stack to make a decision.
4. **Executor + risk** : places the limit order, tracks exposure / open positions /
   daily P&L, enforces the kill switches, and writes the trade log.

A separate **reporter** reads the log and prints net P&L and expectancy.

## 4. Per-window state machine

```
NEW WINDOW (ts = floor(now/300)*300)
  └─ capture strike = first Chainlink tick at/after window open
     (if bot joined mid-window and missed the open → mark window UNTRADEABLE, skip)

WAIT
  └─ do nothing until seconds_to_close <= 30

EVALUATE  (only in the 3s … 30s-to-close band; <3s left = too late to fill, skip)
  ├─ Guard 1  Feed fresh?         Chainlink tick age <= 3s, else STAND DOWN
  ├─ Guard 2  Strike known?       window open was observed, else SKIP
  ├─ Guard 3  Pick favorite       price>strike → UP ; price<strike → DOWN ; ==' → SKIP
  ├─ Guard 4  Cushion             |price - strike| >= $50, else SKIP
  ├─ Guard 5  Momentum            recent velocity not driving price back toward
  │                               strike fast enough to plausibly cross before close
  ├─ Guard 6  Spread/liquidity    spread <= 0.05 AND best-ask depth >= our size
  ├─ Guard 7  Price cap           best ask <= 0.98
  ├─ Guard 8  Edge                est_win_prob - ask >= 0.05
  └─ Guard 9  Risk free?          no open position, daily loss cap not hit,
                                  not already bet this window
  → If ALL pass: BUY favorite, $1, marketable limit @ min(best_ask, cap), fill-or-kill
  → else: log the exact guard that blocked, and the numbers

AFTER CLOSE
  └─ record resolution (win/loss), realized P&L, update daily total
```

### Win-probability model (for the edge guard)

Conservative and simple: estimate how far BTC could move over the remaining `t`
seconds from recent Chainlink volatility (standard deviation of 1-second returns,
scaled by √t), then `est_win_prob ≈ Φ(cushion / expected_move)`. We inflate the
volatility estimate slightly so the edge number is pessimistic, not optimistic.
With a $50 cushion and ~25s left this is typically very high, but the guard still
refuses to pay nearly the whole dollar for it (price cap + edge minimum).

## 5. Parameters (your chosen values)

| Parameter | Value | Meaning |
|---|---|---|
| `STAKE_USDC` | `1` | spend per trade |
| `DAILY_LOSS_CAP` | `5` | net realized loss that stops the bot for the day |
| `MAX_ENTRY_SECONDS` | `30` | start evaluating with ≤30s left |
| `MIN_ENTRY_SECONDS` | `3` | don't enter with <3s left (can't fill safely) |
| `MIN_CUSHION_USD` | `50` | BTC must be ≥$50 past strike |
| `MIN_EDGE` | `0.05` | est_win_prob − ask must be ≥0.05 |
| `MAX_ASK` | `0.98` | never pay more than 98¢ |
| `MAX_SPREAD` | `0.05` | skip illiquid/wide books |
| `MAX_FEED_STALENESS_S` | `3` | stand down if Chainlink tick older than this |
| `MAX_CONCURRENT` | `1` | one position at a time |

All live in a single CONFIG block at the top of the file, easy to tune.

## 6. Risk controls / hard stops

The bot halts (and logs why) on any of: daily loss cap hit, Chainlink feed stale or
disconnected, spot-vs-Chainlink divergence beyond a threshold, an order error or
rejected fill, or any unhandled exception. It never holds more than one position and
never bets the same window twice. The private key stays in `.env` (now git-ignored).

## 7. Logging & reporting

Every skip, signal, order, fill, and resolution is appended to `trades.csv` with the
window id, strike, live price, cushion, ask, spread, est edge, the blocking guard (if
skipped), and the realized outcome. A `report.py` prints: trades taken, win rate,
gross/net P&L, average edge captured, and expectancy per trade. We judge the strategy
only on filled trades after slippage.

## 8. Build phases (each one reviewable before the next)

- **Phase 1 — Feed + strike capture, no trading.** Chainlink stream thread, window
  tracking, strike recording, spot health-check, logging. Run it and watch it
  correctly capture each window's open and track the live price. Proves the data layer.
- **Phase 2 — Decision engine in DRY-RUN.** Add slug/token resolution, orderbook reads,
  the full guard stack and win-prob model. It logs "WOULD BUY / SKIPPED (guard N)" but
  places no orders. Watch a handful of windows to confirm the decisions look sane.
- **Phase 3 — Arm live execution.** Wire in `place_limit_order`, exposure tracking,
  daily loss cap, and the kill switches. Controlled by an explicit `--live` flag that
  defaults to OFF, so the first run is always dry even though your intent is live; you
  flip it on when Phase 2 looks right.
- **Phase 4 — Resolution tracking + report.** Record outcomes, compute realized P&L and
  expectancy.

> Note: you chose "go live small now." I'll honor that — but I'm building a dry-run
> default into Phase 3 so you can eyeball one or two real windows of decisions before
> any money moves. Flipping `--live` is one flag. If you'd rather it arm immediately,
> say so and I'll default it on.

## 9. Honest risks / caveats

- **The edge may not survive fees + slippage.** Buying a near-certain favorite at 95¢+
  to win $1 is thin. One reversal or one bad fill erases many wins. Expectancy after
  real fills is the only thing that matters — Phase 4 will tell the truth.
- **Late-window liquidity is thin and spreads widen** — exactly when you enter. The
  fill-or-kill limit protects price but means many signals won't fill at all.
- **Strike basis risk.** We measure against Polymarket's Chainlink feed, which is the
  settlement source, so this is minimized — but if that stream lags or drops, the bot
  stands down rather than guess.
- **Final-3s cutoff** means some "sure" windows are skipped because there isn't time to
  fill safely. That's intentional.
```
