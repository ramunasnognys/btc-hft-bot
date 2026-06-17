#!/usr/bin/env python3
"""
Phase 2b — Liquidity probe (enter-earlier experiment, NO ORDERS).

Question this answers: at 120 / 90 / 60 / 30 / 15 seconds before close, does the
FAVORITE side actually have asks you could buy — and when does that book empty?
Phase 2 showed the favorite is unbuyable at ~6s left. This maps the liquidity
decay across the final two minutes so we can see if an earlier entry is fillable.

For each window whose open we observed (strike known), at each checkpoint it
records the favorite's book AND the losing side's book, so we can confirm whether
liquidity simply shifts to the loser as everyone holds the winner to $1.

Logs to phase2b_probe.csv. Run:  python3 phase2b_probe.py   Stop: Ctrl+C
"""

import csv
import time

import requests

from btc_feed import ChainlinkFeed, WindowTracker, iso, WINDOW_SECONDS
from btc_strategy import (
    MarketResolver, CLOB, volatility_per_sec, win_probability, MIN_EDGE,
)

CHECKPOINTS_S = [120, 90, 60, 30, 15]   # seconds-to-close to sample
TOKEN_PRELOAD_SECS = 150
LOG_FILE = "phase2b_probe.csv"


def book_summary(token_id):
    """Return {has_ask, best_ask, best_bid, spread, ask_depth}. has_ask=False
    means the side is unbuyable (no resting asks)."""
    try:
        bk = requests.get(f"{CLOB}/book", params={"token_id": token_id},
                          timeout=8).json()
    except Exception:
        return None
    asks = bk.get("asks") or []
    bids = bk.get("bids") or []
    best_ask = min((float(a["price"]) for a in asks), default=None)
    best_bid = max((float(b["price"]) for b in bids), default=None)
    depth = 0.0
    if best_ask is not None:
        depth = sum(float(a["size"]) for a in asks
                    if abs(float(a["price"]) - best_ask) < 1e-9)
    return {
        "has_ask": best_ask is not None,
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": (round(best_ask - best_bid, 4)
                   if best_ask is not None and best_bid is not None else None),
        "ask_depth": round(depth, 1),
    }


def run():
    print("Phase 2b — liquidity probe (NO ORDERS)")
    print(f"Sampling favorite + loser books at {CHECKPOINTS_S}s before close.\n")
    feed = ChainlinkFeed(); feed.start()
    t0 = time.time()
    while feed.latest() is None:
        if time.time() - t0 > 20:
            raise SystemExit("No Chainlink ticks in 20s.")
        time.sleep(0.2)
    print(f"✓ Feed live. BTC/USD = ${feed.latest()[0]:,.2f}\n")

    tracker = WindowTracker()
    resolver = MarketResolver()

    new = not _exists(LOG_FILE)
    f = open(LOG_FILE, "a", newline="")
    w_csv = csv.writer(f)
    if new:
        w_csv.writerow(["iso_time", "window_id", "checkpoint_s", "secs_left",
                        "price", "strike", "cushion", "favorite",
                        "fav_has_book", "fav_best_ask", "fav_spread", "fav_depth",
                        "loser_best_ask", "loser_depth",
                        "our_p_win", "our_edge", "fillable_edge"])
        f.flush()

    sampled = {}   # window_ts -> set of checkpoints already recorded

    try:
        while True:
            now = time.time()
            w = tracker.poll(feed, now)
            secs_left = tracker.seconds_to_close(now)

            if secs_left <= TOKEN_PRELOAD_SECS and w not in resolver._cache:
                resolver.get(w)

            if w in tracker.strikes:
                done = sampled.setdefault(w, set())
                due = [c for c in CHECKPOINTS_S if c not in done and secs_left <= c]
                # only fire the nearest not-yet-sampled checkpoint at a time
                if due:
                    cp = max(due)   # the checkpoint band we just entered
                    done.add(cp)
                    _sample(feed, tracker, resolver, w, cp, secs_left, now, w_csv, f)

            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nStopping.")
    finally:
        feed.stop()
        f.close()
        print(f"Probe data logged to {LOG_FILE}")


def _sample(feed, tracker, resolver, w, cp, secs_left, now, w_csv, f):
    price = feed.latest()[0]
    strike = tracker.strikes[w]
    cushion = price - strike
    favorite = "UP" if cushion >= 0 else "DOWN"
    mkt = resolver.get(w)
    fav = loser = None
    if mkt:
        fav_tok = mkt["up"] if favorite == "UP" else mkt["down"]
        los_tok = mkt["down"] if favorite == "UP" else mkt["up"]
        fav = book_summary(fav_tok)
        loser = book_summary(los_tok)

    # Our model's view at this checkpoint: win prob vs the price we'd actually pay
    sigma = volatility_per_sec(feed.ring_snapshot(), int(feed.latest()[1]))
    p_win = win_probability(abs(cushion), sigma, secs_left)
    our_edge = None
    fillable_edge = False
    if fav and fav["best_ask"] is not None:
        our_edge = round(p_win - fav["best_ask"], 4)
        fillable_edge = our_edge >= MIN_EDGE
    w_csv.writerow([
        iso(now), w, cp, round(secs_left, 1), round(price, 2), round(strike, 2),
        round(cushion, 2), favorite,
        fav["has_ask"] if fav else "",
        fav["best_ask"] if fav and fav["best_ask"] is not None else "",
        fav["spread"] if fav and fav["spread"] is not None else "",
        fav["ask_depth"] if fav else "",
        loser["best_ask"] if loser and loser["best_ask"] is not None else "",
        loser["ask_depth"] if loser else "",
        round(p_win, 4), our_edge if our_edge is not None else "",
        fillable_edge,
    ])
    f.flush()
    fa = fav["best_ask"] if fav and fav["best_ask"] is not None else "none"
    flag = "  <<< FILLABLE EDGE" if fillable_edge else ""
    print(f"[{iso(now)[11:19]}] win {w} @ {cp:>3}s | cush {cushion:+7.1f} {favorite:<4} "
          f"| fav_ask {fa} depth {fav['ask_depth'] if fav else '-'} "
          f"| p_win {round(p_win,3)} edge {our_edge}{flag}")


def _exists(path):
    try:
        open(path).close(); return True
    except OSError:
        return False


if __name__ == "__main__":
    run()
