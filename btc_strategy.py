#!/usr/bin/env python3
"""
Phase 2 — Decision engine in DRY-RUN (NO ORDERS).

Builds on Phase 1 (btc_feed.py). For each 5-minute window, once we're inside the
final 30 seconds, it runs the full guard stack and logs a decision:
    WOULD_BUY <UP|DOWN>   or   SKIP (<which guard blocked>)
…with every number behind the decision. It places NO orders. The point is to
watch real windows and confirm the logic and the win-probability model behave
sanely before any money is armed in Phase 3.

Guard stack (in order):
  1. FEED      Chainlink tick fresh (age <= MAX_FEED_STALENESS_S)
  2. STRIKE    window open was observed (tradeable)
  3. FAVORITE  price vs strike picks the side already winning (tie -> skip)
  4. CUSHION   |price - strike| >= MIN_CUSHION_USD
  5. MOMENTUM  recent velocity not projecting a cross back over the strike
  6. BOOK      spread <= MAX_SPREAD, best ask exists, enough depth for the stake
  7. PRICE     best ask <= MAX_ASK
  8. EDGE      est_win_prob - best_ask >= MIN_EDGE
  9. RISK      not already decided this window (Phase 3 adds position/loss caps)

Run:  python3 btc_strategy.py
Stop: Ctrl+C
"""

import csv
import math
import time
from datetime import datetime, timezone

import requests

from btc_feed import (
    ChainlinkFeed, WindowTracker, iso,
    MAX_FEED_STALENESS_S, WINDOW_SECONDS,
)

# ─── CONFIG (Phase 2 — your chosen values) ───────────────────────────────────
MAX_ENTRY_SECONDS   = 30      # start evaluating with <= this many secs left
MIN_ENTRY_SECONDS   = 3       # too late to act with fewer secs left
MIN_CUSHION_USD     = 50.0    # BTC must be >= this far past the strike
MIN_EDGE            = 0.05    # est_win_prob - best_ask must be >= this
MAX_ASK             = 0.98    # never pay more than this per share
MAX_SPREAD          = 0.05    # skip wide / illiquid books
STAKE_USDC          = 1.0     # spend per (would-be) trade

VOL_LOOKBACK_S      = 60      # window for volatility estimate
VEL_LOOKBACK_S      = 10      # window for momentum/velocity estimate
VOL_INFLATE         = 1.3     # inflate vol => pessimistic win prob (conservative)
VOL_FLOOR_PER_S     = 0.5     # $/sec floor so prob isn't artificially ~1.0
TOKEN_PRELOAD_SECS  = 75      # fetch token ids when this many secs remain

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"
LOG_FILE = "phase2_log.csv"


# ─── MARKET RESOLUTION (REST, read-only) ─────────────────────────────────────
class MarketResolver:
    """Resolves window_start_ts -> {up, down token ids, condition_id}. Cached."""

    def __init__(self):
        self._cache = {}   # window_ts -> dict | None (None = looked up, not found)

    def slug(self, window_ts):
        return f"btc-updown-5m-{window_ts}"

    def get(self, window_ts):
        if window_ts in self._cache:
            return self._cache[window_ts]
        result = None
        try:
            r = requests.get(f"{GAMMA}/events",
                             params={"slug": self.slug(window_ts)}, timeout=8)
            evs = r.json()
            if evs and evs[0].get("markets"):
                m = evs[0]["markets"][0]
                cid = m.get("conditionId")
                accepting = m.get("acceptingOrders", False)
                mk = requests.get(f"{CLOB}/markets/{cid}", timeout=8).json()
                toks = mk.get("tokens", [])
                up = next((t["token_id"] for t in toks
                           if t["outcome"].lower() == "up"), None)
                down = next((t["token_id"] for t in toks
                             if t["outcome"].lower() == "down"), None)
                if up and down:
                    result = {"condition_id": cid, "up": up, "down": down,
                              "accepting": accepting}
        except Exception as e:
            print(f"  [resolver] error: {e}")
            return None  # transient — don't cache, allow retry
        self._cache[window_ts] = result
        return result


# ─── ORDERBOOK (REST, read-only) ─────────────────────────────────────────────
def read_book(token_id):
    """Return {best_ask, ask_depth, best_bid, spread, tick_size, min_order_size}
    or None. asks come high->low, bids low->high, so best ask = min ask price,
    best bid = max bid price."""
    try:
        bk = requests.get(f"{CLOB}/book", params={"token_id": token_id},
                          timeout=8).json()
    except Exception as e:
        print(f"  [book] error: {e}")
        return None
    asks = bk.get("asks") or []
    bids = bk.get("bids") or []
    if not asks:
        return None
    best_ask = min(float(a["price"]) for a in asks)
    ask_depth = sum(float(a["size"]) for a in asks
                    if abs(float(a["price"]) - best_ask) < 1e-9)
    best_bid = max((float(b["price"]) for b in bids), default=0.0)
    return {
        "best_ask": best_ask,
        "ask_depth": ask_depth,
        "best_bid": best_bid,
        "spread": round(best_ask - best_bid, 4),
        "tick_size": float(bk.get("tick_size", 0.001)),
        "min_order_size": float(bk.get("min_order_size", 0) or 0),
    }


# ─── STATS: volatility, velocity, win probability ────────────────────────────
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _ticks_in(snapshot, now_ms, lookback_s):
    cutoff = now_ms - lookback_s * 1000
    return [(ts, v) for ts, v in snapshot if ts >= cutoff]


def volatility_per_sec(snapshot, now_ms):
    """Std-dev of 1-second BTC price changes ($) over the lookback, floored."""
    pts = _ticks_in(snapshot, now_ms, VOL_LOOKBACK_S)
    if len(pts) < 5:
        return VOL_FLOOR_PER_S
    # bucket to ~1s by taking consecutive diffs normalized by dt
    diffs = []
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dt = (t1 - t0) / 1000.0
        if dt > 0:
            diffs.append((v1 - v0) / math.sqrt(dt))  # scale to per-1s std
    if len(diffs) < 4:
        return VOL_FLOOR_PER_S
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
    return max(math.sqrt(var), VOL_FLOOR_PER_S)


def velocity_per_sec(snapshot, now_ms):
    """Recent signed drift ($/sec) over VEL_LOOKBACK_S."""
    pts = _ticks_in(snapshot, now_ms, VEL_LOOKBACK_S)
    if len(pts) < 2:
        return 0.0
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    dt = (t1 - t0) / 1000.0
    return (v1 - v0) / dt if dt > 0 else 0.0


def win_probability(cushion_abs, sigma_per_sec, seconds_left):
    """P(favorite still on its side at close), end-price based.
    change over remaining t ~ N(0, sigma_t); favorite loses only if it moves
    against by more than the cushion. Conservative (vol inflated)."""
    t = max(seconds_left, 1.0)
    sigma_t = sigma_per_sec * VOL_INFLATE * math.sqrt(t)
    if sigma_t <= 0:
        return 1.0
    return norm_cdf(cushion_abs / sigma_t)


# ─── GUARD STACK ─────────────────────────────────────────────────────────────
def evaluate(feed, resolver, window_ts, now):
    """Run all guards for the current window. Returns (decision, reason, metrics).
    decision in {"WOULD_BUY", "SKIP", "STAND_DOWN"}."""
    m = {"window": window_ts}
    secs_left = ((int(now) // WINDOW_SECONDS) + 1) * WINDOW_SECONDS - now
    m["secs_left"] = round(secs_left, 1)

    # 1. FEED
    age = feed.age_seconds()
    m["feed_age"] = round(age, 2)
    if age > MAX_FEED_STALENESS_S:
        return "STAND_DOWN", "FEED_STALE", m

    latest = feed.latest()
    if latest is None:
        return "STAND_DOWN", "NO_PRICE", m
    price, price_ts_ms = latest
    m["price"] = round(price, 2)

    # 2. STRIKE
    strike = None
    # tracker holds strikes; we pass it via resolver-independent lookup below
    strike = STRIKES.get(window_ts)
    if strike is None:
        return "SKIP", "NO_STRIKE", m
    m["strike"] = round(strike, 2)

    # 3. FAVORITE
    cushion = price - strike
    m["cushion"] = round(cushion, 2)
    if cushion == 0:
        return "SKIP", "AT_STRIKE", m
    favorite = "UP" if cushion > 0 else "DOWN"
    m["favorite"] = favorite
    cushion_abs = abs(cushion)

    # 4. CUSHION
    if cushion_abs < MIN_CUSHION_USD:
        return "SKIP", f"CUSHION<{MIN_CUSHION_USD:.0f}", m

    # 5. MOMENTUM — reject if recent drift projects a cross back over strike
    snap = feed.ring_snapshot()
    vel = velocity_per_sec(snap, price_ts_ms)
    m["vel_per_s"] = round(vel, 3)
    projected_end = price + vel * secs_left
    m["proj_end"] = round(projected_end, 2)
    if favorite == "UP" and projected_end < strike:
        return "SKIP", "MOMENTUM_DOWN", m
    if favorite == "DOWN" and projected_end > strike:
        return "SKIP", "MOMENTUM_UP", m

    # 6. BOOK — resolve tokens + read favorite's book
    mkt = resolver.get(window_ts)
    if not mkt:
        return "SKIP", "NO_MARKET", m
    if not mkt.get("accepting", True):
        return "SKIP", "NOT_ACCEPTING", m
    token_id = mkt["up"] if favorite == "UP" else mkt["down"]
    book = read_book(token_id)
    if not book:
        return "SKIP", "NO_BOOK", m
    m["best_ask"] = book["best_ask"]
    m["spread"] = book["spread"]
    m["ask_depth"] = round(book["ask_depth"], 1)
    if book["spread"] > MAX_SPREAD:
        return "SKIP", f"SPREAD>{MAX_SPREAD}", m
    shares_needed = STAKE_USDC / book["best_ask"]
    if book["ask_depth"] < shares_needed:
        return "SKIP", "THIN_DEPTH", m

    # 7. PRICE cap
    if book["best_ask"] > MAX_ASK:
        return "SKIP", f"ASK>{MAX_ASK}", m

    # 8. EDGE
    sigma = volatility_per_sec(snap, price_ts_ms)
    m["sigma_per_s"] = round(sigma, 3)
    p_win = win_probability(cushion_abs, sigma, secs_left)
    m["p_win"] = round(p_win, 4)
    edge = p_win - book["best_ask"]
    m["edge"] = round(edge, 4)
    if edge < MIN_EDGE:
        return "SKIP", f"EDGE<{MIN_EDGE}", m

    # 9. RISK (per-window dedupe handled by caller; full caps in Phase 3)
    return "WOULD_BUY", favorite, m


# shared strike map populated by the tracker each loop (kept simple for Phase 2)
STRIKES = {}


# ─── MAIN (Phase 2 dry-run) ──────────────────────────────────────────────────
def run():
    print("Phase 2 — decision engine in DRY-RUN (NO ORDERS)")
    print("Connecting to Chainlink stream...")
    feed = ChainlinkFeed()
    feed.start()
    t0 = time.time()
    while feed.latest() is None:
        if time.time() - t0 > 20:
            raise SystemExit("No Chainlink ticks in 20s — check connectivity.")
        time.sleep(0.2)
    print(f"✓ Feed live. BTC/USD = ${feed.latest()[0]:,.2f}\n")
    print(f"Evaluating each window in its final {MAX_ENTRY_SECONDS}s. "
          "Decisions are logged; no orders are placed.\n")

    tracker = WindowTracker()
    resolver = MarketResolver()

    new_file = not _exists(LOG_FILE)
    f = open(LOG_FILE, "a", newline="")
    writer = csv.writer(f)
    if new_file:
        writer.writerow(["iso_time", "window_id", "decision", "reason",
                         "secs_left", "price", "strike", "cushion", "favorite",
                         "best_ask", "spread", "p_win", "edge",
                         "sigma_per_s", "vel_per_s"])
        f.flush()

    decided = {}        # window_ts -> True once a final decision is logged
    last_status = 0.0

    try:
        while True:
            now = time.time()
            w = tracker.poll(feed, now)
            STRIKES.clear(); STRIKES.update(tracker.strikes)
            secs_left = tracker.seconds_to_close(now)

            # Preload tokens a bit before the entry window
            if secs_left <= TOKEN_PRELOAD_SECS and w not in resolver._cache:
                resolver.get(w)

            # Evaluate once per window, inside the entry band
            in_band = MIN_ENTRY_SECONDS <= secs_left <= MAX_ENTRY_SECONDS
            if in_band and w not in decided and w in tracker.strikes:
                decision, reason, m = evaluate(feed, resolver, w, now)
                # Only finalize a SKIP late in the band; keep trying earlier in
                # case the book/price improves before the cutoff.
                finalize = (decision == "WOULD_BUY") or (secs_left <= MIN_ENTRY_SECONDS + 4)
                if finalize:
                    decided[w] = True
                    writer.writerow([
                        iso(now), w, decision, reason, m.get("secs_left"),
                        m.get("price", ""), m.get("strike", ""), m.get("cushion", ""),
                        m.get("favorite", ""), m.get("best_ask", ""), m.get("spread", ""),
                        m.get("p_win", ""), m.get("edge", ""),
                        m.get("sigma_per_s", ""), m.get("vel_per_s", ""),
                    ])
                    f.flush()
                    tag = "✅" if decision == "WOULD_BUY" else "⏭️ "
                    print(f"{tag} [{iso(now)[11:19]}] window {w}: {decision} "
                          f"{reason} | px {m.get('price')} strike {m.get('strike')} "
                          f"cush {m.get('cushion')} ask {m.get('best_ask','-')} "
                          f"edge {m.get('edge','-')} pwin {m.get('p_win','-')}")

            # Heartbeat status every 15s
            if now - last_status >= 15:
                strike = tracker.strikes.get(w)
                px = feed.latest()[0] if feed.latest() else None
                cush = (px - strike) if (px and strike) else None
                print(f"   [{iso(now)[11:19]}] BTC ${px:,.2f} | "
                      f"strike {('%.2f'%strike) if strike else '(capturing)'} | "
                      f"cushion {('%+.1f'%cush) if cush is not None else '--'} | "
                      f"{secs_left:4.0f}s left | feed {feed.age_seconds():.1f}s")
                last_status = now

            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n\nStopping.")
    finally:
        feed.stop()
        f.close()
        n_buy = sum(1 for v in decided.values() if v)
        print(f"Evaluated {len(decided)} window(s). Decisions logged to {LOG_FILE}")


def _exists(path):
    try:
        open(path).close()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    run()
