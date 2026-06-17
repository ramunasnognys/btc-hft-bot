#!/usr/bin/env python3
"""
Phase 1 — Chainlink feed + strike capture (NO TRADING).

What this does:
  * Subscribes to Polymarket's Chainlink BTC/USD price stream (the same feed
    that SETTLES the 5-minute markets) over a websocket, in a background thread.
  * Tracks the current 5-minute window and records each window's OPENING price
    as its strike — Polymarket never publishes the strike, so we capture it
    ourselves by watching the stream straddle the window boundary.
  * Only marks a window "tradeable" if we were connected BEFORE it opened
    (so we genuinely know its open price). Windows we joined mid-way are
    marked UNTRADEABLE and would be skipped by later phases.
  * Runs a periodic spot health-check (Binance/Coinbase) to confirm the
    Chainlink number is sane. Spot is NEVER used for decisions.
  * Logs everything to phase1_log.csv and prints a live status line.

Run:  python3 btc_feed.py
Stop: Ctrl+C

This module is built to be reused by the later phases (the ChainlinkFeed and
WindowTracker classes are the data layer for the whole bot).
"""

import csv
import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

try:
    import websockets  # provided by the polymarket SDK's deps; pip install websockets
except ImportError:
    raise SystemExit("Missing dependency: pip install websockets")

import asyncio

# ─── CONFIG ──────────────────────────────────────────────────────────────────
RTDS_WS_URL          = "wss://ws-live-data.polymarket.com"
CHAINLINK_WIRE_TOPIC = "crypto_prices_chainlink"
SYMBOL               = "btc/usd"
WINDOW_SECONDS       = 300                # 5-minute markets
RING_MAXLEN          = 400                # ~400s of BTC ticks (1/sec)
MAX_FEED_STALENESS_S = 3.0                # tick older than this => feed stale
STRIKE_DECISION_GAP  = 1.5                # secs after open to decide tradeable/not
HEALTH_CHECK_EVERY_S = 30                 # spot vs chainlink comparison cadence
STATUS_PRINT_EVERY_S = 5                  # console status cadence
LOG_FILE             = "phase1_log.csv"


# ─── CHAINLINK FEED (background websocket thread) ────────────────────────────
class ChainlinkFeed:
    """Maintains the latest Chainlink BTC/USD price and a short history ring.

    Thread-safe. Call start(), then latest()/lookup_open()/age_seconds().
    """

    def __init__(self, symbol=SYMBOL):
        self.symbol = symbol
        self._lock = threading.Lock()
        self._ring = deque(maxlen=RING_MAXLEN)   # (payload_ts_ms, value) chronological
        self._latest = None                       # (value, payload_ts_ms)
        self._last_recv_mono = None               # monotonic time of last tick
        self._stop = threading.Event()
        self._thread = None
        self.connected = False

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _thread_main(self):
        asyncio.run(self._run())

    async def _run(self):
        backoff = 1
        sub = {
            "action": "subscribe",
            "subscriptions": [{"topic": CHAINLINK_WIRE_TOPIC, "type": "update"}],
        }
        while not self._stop.is_set():
            try:
                async with websockets.connect(RTDS_WS_URL, open_timeout=15,
                                              ping_interval=15, ping_timeout=15) as ws:
                    await ws.send(json.dumps(sub))
                    self.connected = True
                    backoff = 1
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        self._handle(raw)
            except Exception as e:
                self.connected = False
                if self._stop.is_set():
                    break
                print(f"  [feed] disconnected ({e!r}); reconnecting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 15)

    def _handle(self, raw):
        if not raw:
            return  # server sends periodic empty keepalive frames
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if msg.get("topic") != CHAINLINK_WIRE_TOPIC:
            return
        pl = msg.get("payload") or {}
        if pl.get("symbol") != self.symbol:
            return
        try:
            value = float(pl["value"])
            ts_ms = int(pl["timestamp"])
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            self._ring.append((ts_ms, value))
            self._latest = (value, ts_ms)
            self._last_recv_mono = time.monotonic()

    def latest(self):
        with self._lock:
            return self._latest

    def age_seconds(self):
        with self._lock:
            if self._last_recv_mono is None:
                return math.inf
            return time.monotonic() - self._last_recv_mono

    def is_stale(self, max_age=MAX_FEED_STALENESS_S):
        return self.age_seconds() > max_age

    def ring_snapshot(self):
        """Return a chronological copy of recent (payload_ts_ms, value) ticks."""
        with self._lock:
            return list(self._ring)

    def lookup_open(self, boundary_ms):
        """Return (open_value_or_None, have_prior_tick).

        open_value = price of the first tick at/after the window boundary.
        have_prior = we also saw a tick BEFORE the boundary (proof we were
        connected across the open, so the captured price is trustworthy).
        """
        with self._lock:
            have_prior = False
            open_val = None
            for ts_ms, val in self._ring:
                if ts_ms < boundary_ms:
                    have_prior = True
                else:
                    open_val = val
                    break
        return open_val, have_prior


# ─── WINDOW + STRIKE TRACKER ─────────────────────────────────────────────────
class WindowTracker:
    """Tracks 5-minute windows and captures each one's opening (strike) price."""

    def __init__(self):
        self.strikes = {}        # window_start_ts -> strike price
        self.untradeable = set() # window_start_ts we joined mid-way (open unknown)

    @staticmethod
    def window_start(now):
        return int(now // WINDOW_SECONDS) * WINDOW_SECONDS

    @staticmethod
    def seconds_to_close(now):
        return ((int(now) // WINDOW_SECONDS) + 1) * WINDOW_SECONDS - now

    def poll(self, feed, now):
        """Resolve the strike for the current window when possible. Returns the
        current window_start_ts."""
        w = self.window_start(now)
        if w in self.strikes or w in self.untradeable:
            return w
        open_val, have_prior = feed.lookup_open(w * 1000)
        if not have_prior and (now - w) > STRIKE_DECISION_GAP:
            # We were not connected before this window opened => can't trust the
            # strike. Stand down for this window.
            self.untradeable.add(w)
        elif open_val is not None and have_prior:
            self.strikes[w] = open_val
        # else: connected across the open but the post-open tick hasn't arrived
        # yet (sub-second) — keep waiting.
        return w

    def status_for(self, w, now):
        if w in self.strikes:
            return "TRADEABLE"
        if w in self.untradeable:
            return "UNTRADEABLE"
        return "CAPTURING"


# ─── SPOT HEALTH CHECK (sanity only, never used for decisions) ───────────────
def get_spot_btc():
    """Best-effort median spot BTC from Binance + Coinbase. Returns float or None."""
    prices = []
    for url, key in [
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", "price"),
        ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", "price"),
    ]:
        try:
            r = requests.get(url, timeout=6, headers={"User-Agent": "btc-bot"})
            prices.append(float(r.json()[key]))
        except Exception:
            pass
    if not prices:
        return None
    prices.sort()
    n = len(prices)
    return prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2


# ─── MAIN (Phase 1 runner) ───────────────────────────────────────────────────
def iso(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run():
    print("Phase 1 — Chainlink feed + strike capture (NO TRADING)")
    print("Connecting to Chainlink stream...")

    feed = ChainlinkFeed()
    feed.start()

    # Wait for first tick
    t0 = time.time()
    while feed.latest() is None:
        if time.time() - t0 > 20:
            raise SystemExit("No Chainlink ticks received in 20s — check connectivity.")
        time.sleep(0.2)
    v0, _ = feed.latest()
    print(f"✓ Feed live. BTC/USD = ${v0:,.2f}\n")
    print("Watching windows. The first (joined mid-way) will be UNTRADEABLE; "
          "the next full window onward will capture a strike.\n")

    tracker = WindowTracker()

    # CSV setup
    new_file = not _file_exists(LOG_FILE)
    f = open(LOG_FILE, "a", newline="")
    writer = csv.writer(f)
    if new_file:
        writer.writerow([
            "iso_time", "window_id", "window_open_iso", "secs_to_close",
            "chainlink_price", "strike", "cushion_usd", "favorite",
            "spot_price", "divergence_usd", "feed_age_s", "status",
        ])
        f.flush()

    last_print = 0.0
    last_health = 0.0
    last_logged_window = None
    spot = None
    divergence = None

    try:
        while True:
            now = time.time()
            w = tracker.poll(feed, now)
            secs_left = tracker.seconds_to_close(now)
            latest = feed.latest()
            price = latest[0] if latest else None
            strike = tracker.strikes.get(w)
            age = feed.age_seconds()
            stale = feed.is_stale()
            status = "STALE_FEED" if stale else tracker.status_for(w, now)

            cushion = fav = None
            if price is not None and strike is not None:
                cushion = price - strike
                fav = "UP" if cushion >= 0 else "DOWN"

            # Periodic spot health check
            if now - last_health >= HEALTH_CHECK_EVERY_S:
                spot = get_spot_btc()
                if spot and price:
                    divergence = price - spot
                last_health = now

            # Log on every window change, plus a heartbeat row each print cycle
            window_changed = (w != last_logged_window)
            if window_changed or now - last_print >= STATUS_PRINT_EVERY_S:
                writer.writerow([
                    iso(now), w, iso(w), f"{secs_left:.1f}",
                    f"{price:.2f}" if price is not None else "",
                    f"{strike:.2f}" if strike is not None else "",
                    f"{cushion:+.2f}" if cushion is not None else "",
                    fav or "",
                    f"{spot:.2f}" if spot else "",
                    f"{divergence:+.2f}" if divergence is not None else "",
                    f"{age:.1f}", status,
                ])
                f.flush()
                last_logged_window = w

            if now - last_print >= STATUS_PRINT_EVERY_S:
                cush_str = f"{cushion:+.1f}" if cushion is not None else "  --"
                strike_str = f"{strike:,.2f}" if strike is not None else "(capturing)"
                div_str = f"{divergence:+.1f}" if divergence is not None else "n/a"
                price_str = f"${price:,.2f}" if price is not None else "  --"
                print(f"[{iso(now)[11:19]}] BTC {price_str} | "
                      f"strike {strike_str} | cushion {cush_str} {fav or ''} | "
                      f"{secs_left:4.0f}s left | spotΔ {div_str} | "
                      f"age {age:.1f}s | {status}")
                last_print = now

            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n\nStopping.")
    finally:
        feed.stop()
        f.close()
        print(f"Captured strikes for {len(tracker.strikes)} window(s); "
              f"{len(tracker.untradeable)} untradeable. Log: {LOG_FILE}")


def _file_exists(path):
    try:
        open(path).close()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    run()
