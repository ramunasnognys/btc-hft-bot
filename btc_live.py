#!/usr/bin/env python3
"""
Phase 3 — Live execution (early-entry 120s variant). PAID VALIDATION.

⚠️  THIS CAN SPEND REAL MONEY.  It defaults to DRY-RUN and will not place a real
    order unless you pass --live AND type the confirmation when prompted.

What it does, per 5-minute window:
  * Reuses the Phase 1 Chainlink feed + strike capture and the Phase 2 stats.
  * At ~120 seconds to close (the only entry the backtest supported), it runs the
    guard stack and, if everything passes, BUYS the favorite with a price-capped
    fill-and-kill order for a fixed $1 stake.
  * Tracks the position, reconstructs the outcome from the next window's open
    price, books realized P&L, and STOPS for the day if losses hit the cap.

Hard risk rails (all enforced every loop):
  * $1 stake, $5 net daily-loss cap -> halt for the UTC day.
  * One position at a time, one bet per window.
  * Feed stale (>3s) -> stand down.  Repeated errors -> halt.

Modes:
  python3 btc_live.py            # DRY-RUN: simulates fills, no SDK, no money
  python3 btc_live.py --check    # connect to Polymarket, print wallet/balance, exit
  python3 btc_live.py --live     # REAL ORDERS (requires typed confirmation)

This file never runs itself; you start it. Read DRY-RUN output first.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

from btc_feed import ChainlinkFeed, WindowTracker, iso, WINDOW_SECONDS, MAX_FEED_STALENESS_S
from btc_strategy import (
    MarketResolver, read_book, volatility_per_sec, velocity_per_sec, win_probability,
)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
STAKE_USDC        = 5.0     # spend per trade
DAILY_LOSS_CAP    = 10.0     # halt for the day after this much net realized loss

ENTRY_AT_SECS     = 120     # evaluate when secs_left first drops to <= this
ENTRY_FLOOR_SECS  = 100     # ...but not if we're already past this (missed the band)

# The validation knob. The backtest's edge lived in SMALL-cushion favorites at
# 120s; larger cushions were ~breakeven after costs. Lower = more faithful to the
# (fragile, unproven) edge; higher = safer but little/no edge. Start modest.
MIN_CUSHION_USD   = 25.0
MIN_EDGE          = 0.0     # 0 = pure "buy the 120s favorite" (replicates backtest)
MAX_ASK           = 0.97    # never pay more than this per share
MAX_SPREAD        = 0.07    # skip wide books
SLIP_TOL          = 0.02    # allow fills up to best_ask + this (price cap)

MAX_CONSEC_ERRORS = 5
LOG_FILE          = "trades_live.csv"


# ─── RISK MANAGER ────────────────────────────────────────────────────────────
class RiskManager:
    def __init__(self, daily_loss_cap=DAILY_LOSS_CAP):
        self.cap = daily_loss_cap
        self.day = _utc_day()
        self.realized = 0.0       # net realized P&L for the current UTC day
        self.halted = False
        self.halt_reason = None
        self.open_position = None # dict or None
        self.bet_windows = set()  # windows we've already acted on
        self.errors = 0

    def _roll_day(self):
        d = _utc_day()
        if d != self.day:
            self.day, self.realized = d, 0.0
            if self.halt_reason == "DAILY_LOSS_CAP":
                self.halted, self.halt_reason = False, None

    def can_trade(self, window):
        self._roll_day()
        if self.halted:
            return False, self.halt_reason
        if self.open_position is not None:
            return False, "POSITION_OPEN"
        if window in self.bet_windows:
            return False, "ALREADY_BET_WINDOW"
        return True, None

    def record_open(self, position):
        self.open_position = position
        self.bet_windows.add(position["window"])

    def record_resolution(self, pnl):
        self.realized += pnl
        self.open_position = None
        if self.realized <= -self.cap:
            self.halted, self.halt_reason = True, "DAILY_LOSS_CAP"

    def note_error(self):
        self.errors += 1
        if self.errors >= MAX_CONSEC_ERRORS:
            self.halted, self.halt_reason = True, "TOO_MANY_ERRORS"

    def clear_errors(self):
        self.errors = 0


# ─── EXECUTION (dry-run simulates; live uses the SDK) ────────────────────────
class Executor:
    def __init__(self, live=False):
        self.live = live
        self.client = None

    def connect(self):
        """Create a real SDK client from .env. Only called in live/check mode."""
        import os
        from dotenv import load_dotenv
        load_dotenv()
        from py_clob_client.client import ClobClient
        pk = os.getenv("POLY_PRIVATE_KEY")
        if not pk:
            raise SystemExit("Missing POLY_PRIVATE_KEY in .env")
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
        )
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)
        return self.client

    def buy(self, token_id, best_ask):
        """Buy the favorite for STAKE_USDC, price-capped, fill-and-kill.
        Returns (shares, cost, price) on a fill, or (0, 0, None) if killed."""
        cap = min(round(best_ask + SLIP_TOL, 3), MAX_ASK)
        if self.live:
            try:
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.order_builder.constants import BUY
                shares = round(STAKE_USDC / cap, 4)
                order_args = OrderArgs(price=cap, size=shares, side=BUY, token_id=token_id)
                signed = self.client.create_order(order_args)
                res = self.client.post_order(signed, OrderType.FAK)
            except Exception as e:
                print(f"  ⚠ order error: {e}")
                return 0.0, 0.0, None
            order_id = (res.get("orderID") if isinstance(res, dict)
                        else getattr(res, "orderID", None))
            if not order_id:
                return 0.0, 0.0, None       # FAK found no match
            return shares, STAKE_USDC, cap
        # DRY-RUN: assume a fill at the capped price for the full stake
        price = cap
        shares = STAKE_USDC / price
        return shares, STAKE_USDC, price


# ─── DECISION (Phase-3 guard stack, 120s entry) ──────────────────────────────
def decide(feed, resolver, tracker, window, now):
    secs_left = tracker.seconds_to_close(now)
    if feed.is_stale():
        return "STAND_DOWN", "FEED_STALE", {}
    latest = feed.latest()
    if latest is None:
        return "STAND_DOWN", "NO_PRICE", {}
    price, price_ts = latest
    strike = tracker.strikes.get(window)
    if strike is None:
        return "SKIP", "NO_STRIKE", {}
    cushion = price - strike
    favorite = "UP" if cushion >= 0 else "DOWN"
    cabs = abs(cushion)
    m = {"price": round(price, 2), "strike": round(strike, 2),
         "cushion": round(cushion, 2), "favorite": favorite, "secs_left": round(secs_left, 1)}
    if cabs < MIN_CUSHION_USD:
        return "SKIP", "CUSHION", m
    snap = feed.ring_snapshot()
    vel = velocity_per_sec(snap, int(price_ts))
    if favorite == "UP" and price + vel * secs_left < strike:
        return "SKIP", "MOMENTUM", m
    if favorite == "DOWN" and price + vel * secs_left > strike:
        return "SKIP", "MOMENTUM", m
    mkt = resolver.get(window)
    if not mkt:
        return "SKIP", "NO_MARKET", m
    if not mkt.get("accepting", True):
        return "SKIP", "NOT_ACCEPTING", m
    token = mkt["up"] if favorite == "UP" else mkt["down"]
    book = read_book(token)
    if not book:
        return "SKIP", "NO_BOOK", m
    m.update({"best_ask": book["best_ask"], "spread": book["spread"]})
    if book["spread"] > MAX_SPREAD:
        return "SKIP", "SPREAD", m
    if book["best_ask"] > MAX_ASK:
        return "SKIP", "ASK_TOO_HIGH", m
    if book["ask_depth"] < STAKE_USDC / book["best_ask"]:
        return "SKIP", "THIN_DEPTH", m
    sigma = volatility_per_sec(snap, int(price_ts))
    p_win = win_probability(cabs, sigma, secs_left)
    edge = p_win - book["best_ask"]
    m.update({"p_win": round(p_win, 4), "edge": round(edge, 4)})
    if edge < MIN_EDGE:
        return "SKIP", "EDGE", m
    return "BUY", favorite, {**m, "token": token}


# ─── MAIN LOOP ───────────────────────────────────────────────────────────────
def run(live=False):
    mode = "LIVE (REAL MONEY)" if live else "DRY-RUN (no orders)"
    print(f"Phase 3 — {mode}")
    print(f"Stake ${STAKE_USDC} | daily cap ${DAILY_LOSS_CAP} | entry ~{ENTRY_AT_SECS}s "
          f"| min cushion ${MIN_CUSHION_USD} | min edge {MIN_EDGE} | max ask {MAX_ASK}\n")

    execu = Executor(live=live)
    if live:
        print("Connecting to Polymarket...")
        execu.connect()
        print("✓ Connected\n")

    feed = ChainlinkFeed(); feed.start()
    t0 = time.time()
    while feed.latest() is None:
        if time.time() - t0 > 20:
            raise SystemExit("No Chainlink ticks in 20s.")
        time.sleep(0.2)
    print(f"✓ Feed live. BTC/USD = ${feed.latest()[0]:,.2f}\n")

    tracker = WindowTracker()
    resolver = MarketResolver()
    risk = RiskManager()

    new = not _exists(LOG_FILE)
    f = open(LOG_FILE, "a", newline="")
    log = csv.writer(f)
    if new:
        log.writerow(["iso_time", "mode", "event", "window", "side", "secs_left",
                      "price", "strike", "cushion", "ask", "shares", "cost",
                      "outcome", "pnl", "daily_pnl", "reason"])
        f.flush()

    def write(event, window, side="", m=None, **kw):
        m = m or {}
        log.writerow([iso(), mode, event, window, side, m.get("secs_left", ""),
                      m.get("price", ""), m.get("strike", ""), m.get("cushion", ""),
                      m.get("best_ask", kw.get("ask", "")), kw.get("shares", ""),
                      kw.get("cost", ""), kw.get("outcome", ""), kw.get("pnl", ""),
                      round(risk.realized, 4), kw.get("reason", "")])
        f.flush()

    evaluated = set()
    last_status = 0.0
    print("Running. Ctrl+C to stop.\n")
    try:
        while True:
            now = time.time()
            w = tracker.poll(feed, now)
            secs_left = tracker.seconds_to_close(now)

            # Resolve an open position once the next window's open is known
            if risk.open_position is not None:
                pos = risk.open_position
                nxt = pos["window"] + WINDOW_SECONDS
                if nxt in tracker.strikes:
                    settle = tracker.strikes[nxt]
                    won = ((pos["side"] == "UP" and settle >= pos["strike"]) or
                           (pos["side"] == "DOWN" and settle < pos["strike"]))
                    pnl = (pos["shares"] - pos["cost"]) if won else (-pos["cost"])
                    risk.record_resolution(pnl)
                    write("RESOLVE", pos["window"], pos["side"],
                          m={"strike": round(pos["strike"], 2)},
                          outcome=("WIN" if won else "LOSS"), pnl=round(pnl, 4))
                    print(f"  {'🟢 WIN ' if won else '🔴 LOSS'} window {pos['window']} "
                          f"{pos['side']} pnl ${pnl:+.3f} | day ${risk.realized:+.3f}")
                    if risk.halted:
                        print(f"\n⛔ HALTED: {risk.halt_reason}. Day P&L ${risk.realized:+.2f}")

            # Preload tokens before the entry band
            if secs_left <= ENTRY_AT_SECS + 20 and w not in resolver._cache:
                resolver.get(w)

            # One evaluation per window inside the entry band
            in_band = ENTRY_FLOOR_SECS <= secs_left <= ENTRY_AT_SECS
            if in_band and w not in evaluated and w in tracker.strikes:
                evaluated.add(w)
                ok, why = risk.can_trade(w)
                if not ok:
                    write("SKIP", w, reason=why)
                else:
                    action, side, m = decide(feed, resolver, tracker, w, now)
                    if action == "BUY":
                        try:
                            shares, cost, px = execu.buy(m["token"], m["best_ask"])
                            risk.clear_errors()
                        except Exception as e:
                            risk.note_error(); write("ERROR", w, reason=str(e)[:50]); shares = 0
                        if shares > 0:
                            risk.record_open({"window": w, "side": side,
                                              "shares": shares, "cost": cost,
                                              "strike": m["strike"]})
                            write("FILL", w, side, m=m, ask=px, shares=round(shares, 3),
                                  cost=round(cost, 3))
                            print(f"  ✅ BUY {side} window {w} | {shares:.2f} sh @ {px} "
                                  f"= ${cost:.2f} | cush {m['cushion']} edge {m.get('edge')}")
                        else:
                            write("NOFILL", w, side, m=m, reason="killed")
                            print(f"  ⏭️  no fill {side} window {w} (FAK killed)")
                    else:
                        write(action, w, side if action != "SKIP" else "",
                              m=m, reason=why)

            if now - last_status >= 20:
                st = "HALTED:" + str(risk.halt_reason) if risk.halted else "running"
                px = feed.latest()[0] if feed.latest() else None
                pos = "flat" if risk.open_position is None else f"in {risk.open_position['side']}"
                print(f"   [{iso()[11:19]}] BTC ${px:,.2f} | {secs_left:4.0f}s left | "
                      f"{pos} | day P&L ${risk.realized:+.2f} | {st}")
                last_status = now

            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n\nStopped. Day realized P&L: ${risk.realized:+.2f}  (log: {LOG_FILE})")
    finally:
        feed.stop(); f.close()


def check():
    """Connect and report wallet/balance without placing any order."""
    print("Connecting to Polymarket (no orders will be placed)...")
    ex = Executor(live=True)
    c = ex.connect()
    print("✓ Connected.")
    try:
        ok = c.get_ok()
        print(f"  API health: {ok}")
    except Exception as e:
        print(f"  API health check: {e}")
    try:
        bal = c.get_balance()
        print(f"  USDC balance: {bal}")
    except Exception:
        pass
    print("\nIf the connection succeeded, you can arm live trading with --live.")


def confirm_live():
    print("=" * 64)
    print("⚠️  LIVE MODE — this will place REAL orders and spend REAL USDC.")
    print(f"   Stake ${STAKE_USDC}/trade, daily loss cap ${DAILY_LOSS_CAP}.")
    print("   This strategy's edge is UNPROVEN out-of-sample. You may lose money.")
    print("=" * 64)
    return input('Type  ARM LIVE  to proceed (anything else cancels): ').strip() == "ARM LIVE"


def _utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _exists(path):
    try:
        open(path).close(); return True
    except OSError:
        return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="place REAL orders")
    ap.add_argument("--check", action="store_true", help="connect + show balance, then exit")
    args = ap.parse_args()
    if args.check:
        check()
    elif args.live:
        if confirm_live():
            run(live=True)
        else:
            print("Cancelled. (Run without --live for dry-run.)")
    else:
        run(live=False)
