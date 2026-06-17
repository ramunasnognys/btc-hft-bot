#!/usr/bin/env python3
"""
Polymarket BTC Up/Down 5-Minute Bot  (uses new polymarket-client SDK)
Finds each 5-min window, reads BTC momentum, bets Up or Down.
"""

import os
import time
import requests
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BET_SIZE_USDC   = Decimal(os.getenv("BET_SIZE", "1.0"))
MIN_MOVE_PCT    = 0.03   # minimum BTC % move to trigger a bet
PRICE_READINGS  = 3      # how many readings to compare (taken 10s apart)

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST  = "https://clob.polymarket.com"

# ─── CONNECT ─────────────────────────────────────────────────────────────────
from polymarket import SecureClient, RelayerApiKey

PRIVATE_KEY    = os.getenv("POLY_PRIVATE_KEY")
RELAYER_KEY    = os.getenv("POLY_RELAYER_KEY")
RELAYER_ADDR   = os.getenv("POLY_RELAYER_ADDRESS")

if not PRIVATE_KEY:
    print("❌  Missing POLY_PRIVATE_KEY in .env")
    exit(1)

print("Connecting to Polymarket...")
try:
    api_key = None
    if RELAYER_KEY and RELAYER_ADDR:
        api_key = RelayerApiKey(key=RELAYER_KEY, address=RELAYER_ADDR)

    client = SecureClient.create(
        private_key=PRIVATE_KEY,
        api_key=api_key,
    )
    print("✓ Connected\n")
except Exception as e:
    print(f"❌  Connection failed: {e}")
    exit(1)

# ─── BTC PRICE FEED ──────────────────────────────────────────────────────────
price_history = []

def get_btc_price():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=5
        )
        return float(r.json()["price"])
    except Exception as e:
        print(f"  ⚠ Price error: {e}")
        return None

def get_signal():
    """Returns 'Up', 'Down', or None."""
    if len(price_history) < 2:
        return None
    pct = (price_history[-1] - price_history[0]) / price_history[0] * 100
    print(f"  BTC move: {pct:+.3f}%  (need ±{MIN_MOVE_PCT}%)")
    if pct >= MIN_MOVE_PCT:
        return "Up"
    if pct <= -MIN_MOVE_PCT:
        return "Down"
    return None

# ─── MARKET FINDER ───────────────────────────────────────────────────────────
def get_current_slug():
    ts = (int(time.time()) // 300) * 300
    return f"btc-updown-5m-{ts}", ts

def fetch_token_ids(slug, verbose=False):
    """Returns (up_token_id, down_token_id) or (None, None).

    Set verbose=True to log exactly which step failed — useful for the
    startup preflight, since a wrong slug format fails silently otherwise.
    """
    def log(msg):
        if verbose:
            print(msg)
    try:
        r = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug}, timeout=10)
        events = r.json()
        if not events:
            log(f"  ✗ No event found for slug '{slug}' (slug format may be wrong)")
            return None, None
        markets = events[0].get("markets", [])
        if not markets:
            log("  ✗ Event found but it has no markets")
            return None, None
        condition_id = markets[0].get("conditionId")
        r2 = requests.get(f"{CLOB_HOST}/markets/{condition_id}", timeout=10)
        tokens = r2.json().get("tokens", [])
        up   = next((t["token_id"] for t in tokens if t["outcome"].lower() == "up"),   None)
        down = next((t["token_id"] for t in tokens if t["outcome"].lower() == "down"), None)
        if not up or not down:
            outcomes = [t.get("outcome") for t in tokens]
            log(f"  ✗ Market found but no Up/Down tokens. Outcomes seen: {outcomes}")
        return up, down
    except Exception as e:
        log(f"  ⚠ Market fetch error: {e}")
        return None, None


def verify_market():
    """Startup preflight: confirm the slug format actually resolves to a
    live market with Up/Down tokens. Without this, a wrong slug means the
    bot loops forever and silently never trades.
    Returns True if a tradeable market was found, False otherwise.
    """
    slug, _ = get_current_slug()
    print(f"Preflight: resolving current market slug '{slug}'...")
    up, down = fetch_token_ids(slug, verbose=True)
    if up and down:
        print("✓ Market resolved — Up/Down tokens found. Slug format looks correct.\n")
        return True
    print(
        "⚠ Preflight could NOT resolve a tradeable market.\n"
        "  The bot will keep retrying, but if every window fails the slug\n"
        "  format in get_current_slug() is likely wrong for Polymarket's\n"
        "  current schema. Verify a real event slug before leaving it running.\n"
    )
    return False

# ─── ORDER PLACEMENT ─────────────────────────────────────────────────────────
def place_bet(token_id, outcome, size):
    try:
        result = client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=size,       # USDC to spend
            order_type="FAK",  # Fill and Kill (partial fills ok)
        )
        return result
    except Exception as e:
        print(f"  ⚠ Order error: {e}")
        return None

# ─── TIMING ──────────────────────────────────────────────────────────────────
def seconds_left():
    now = int(time.time())
    return ((now // 300) + 1) * 300 - now

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
def run():
    print(f"🤖 BTC 5-Min Bot  |  Bet: ${BET_SIZE_USDC} USDC  |  Min move: {MIN_MOVE_PCT}%")

    # Confirm the market slug actually resolves before we trust the loop.
    verify_market()

    print("Press Ctrl+C to stop\n")

    last_bet_window = None

    while True:
        now_str = datetime.now().strftime("%H:%M:%S")
        secs    = seconds_left()

        price = get_btc_price()
        if price:
            price_history.append(price)
            if len(price_history) > PRICE_READINGS:
                price_history.pop(0)
            print(f"[{now_str}] BTC ${price:,.2f}  |  {secs}s left in window")

        slug, window_ts = get_current_slug()

        # Already bet this window
        if window_ts == last_bet_window:
            time.sleep(10)
            continue

        # Too close to window end
        if secs < 120:
            time.sleep(10)
            continue

        if len(price_history) < 2:
            print("  Collecting price data...")
            time.sleep(10)
            continue

        signal = get_signal()
        if not signal:
            time.sleep(10)
            continue

        # Get token IDs
        print(f"  Signal: {signal} | Finding market {slug}...")
        up_id, down_id = fetch_token_ids(slug)
        if not up_id or not down_id:
            print("  Market not found yet — retrying")
            time.sleep(10)
            continue

        token_id = up_id if signal == "Up" else down_id
        print(f"  Placing ${BET_SIZE_USDC} on {signal}...")
        result = place_bet(token_id, signal, BET_SIZE_USDC)

        if result:
            print(f"  ✓ Order result: {result}")
            last_bet_window = window_ts
        else:
            print("  ✗ Order failed")

        time.sleep(10)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n\nBot stopped.")
    finally:
        client.close()
