#!/usr/bin/env python3
"""
Phase 4 — Reporting & expectancy.

Turns the bot's logs into an honest P&L / expectancy verdict. Pure stdlib, so it
runs with any Python (no venv needed) and never touches the network or money.

Usage:
    python3 report.py                      # reads trades_live.csv
    python3 report.py trades_live.csv      # explicit live/dry trade log
    python3 report.py phase2b_probe.csv    # backtest the liquidity-probe log

It auto-detects which kind of file it is:
  * a TRADE log  (from btc_live.py)      -> realized win rate, net P&L, expectancy,
                                            fill rate, skip reasons, by-day, and
                                            whether it BEAT the market's implied odds.
  * a PROBE log  (from phase2b_probe.py)  -> reconstructs outcomes from consecutive
                                            window strikes and backtests the
                                            buy-the-favorite rule by entry time.
"""

import csv
import math
import sys
from collections import defaultdict


# ─── shared helpers ──────────────────────────────────────────────────────────
def fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def wilson(k, n, z=1.96):
    """95% confidence interval for a win rate (flags small-sample uncertainty)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, c - h), min(1, c + h))


def load(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def bar(label, value, width=28):
    return f"  {label:<22} {value}"


# ─── TRADE-LOG REPORT (btc_live.py output) ───────────────────────────────────
def report_trades(rows):
    print("=" * 60)
    print("  TRADE REPORT  (from btc_live.py)")
    print("=" * 60)

    by_mode = defaultdict(list)
    for r in rows:
        by_mode[r.get("mode", "?")].append(r)

    for mode, mrows in by_mode.items():
        print(f"\n### MODE: {mode}   ({len(mrows)} log rows)")
        events = defaultdict(int)
        for r in mrows:
            events[r["event"]] += 1

        fills = {int(r["window"]): r for r in mrows if r["event"] == "FILL"}
        resolves = [r for r in mrows if r["event"] == "RESOLVE"]
        nofills = events.get("NOFILL", 0)

        # join FILL (has ask) with RESOLVE (has pnl/outcome) by window
        trades = []
        for r in resolves:
            w = int(r["window"])
            ask = fl(fills[w]["ask"]) if w in fills else None
            trades.append({"window": w, "side": r["side"],
                           "outcome": r["outcome"], "pnl": fl(r["pnl"]),
                           "ask": ask})

        n_fill = len(fills)
        n_res = len(trades)
        fill_rate = (n_fill / (n_fill + nofills)) if (n_fill + nofills) else 0.0

        print(bar("decisions", dict(events)))
        print(bar("fills / nofills", f"{n_fill} / {nofills}  (fill rate {fill_rate:.0%})"))
        print(bar("open / unresolved", f"{n_fill - n_res}"))

        if not trades:
            print("  (no resolved trades yet — nothing to score)")
            continue

        wins = sum(1 for t in trades if t["outcome"] == "WIN")
        net = sum(t["pnl"] for t in trades if t["pnl"] is not None)
        asks = [t["ask"] for t in trades if t["ask"] is not None]
        avg_ask = sum(asks) / len(asks) if asks else None
        wr = wins / n_res
        lo, hi = wilson(wins, n_res)

        print(bar("resolved trades", n_res))
        print(bar("win rate", f"{wins}/{n_res} = {wr:.0%}  (95% CI {lo:.0%}-{hi:.0%})"))
        if avg_ask is not None:
            verdict = "BEATS" if wr > avg_ask else "BELOW"
            print(bar("implied breakeven", f"{avg_ask:.0%}  -> realized {verdict} market"))
        print(bar("net realized P&L", f"${net:+.2f}"))
        print(bar("expectancy / trade", f"${net / n_res:+.3f}"))

        # by-day
        days = defaultdict(lambda: [0, 0, 0.0])  # day -> [trades, wins, pnl]
        for r in resolves:
            day = r["iso_time"][:10]
            days[day][0] += 1
            days[day][1] += (r["outcome"] == "WIN")
            days[day][2] += fl(r["pnl"]) or 0.0
        print("\n  By day:")
        for day in sorted(days):
            t, w, p = days[day]
            print(f"    {day}: {w}/{t} won, P&L ${p:+.2f}")

        # skip reasons (reason col, fall back to side for STAND_DOWN etc.)
        skips = defaultdict(int)
        for r in mrows:
            if r["event"] in ("FILL", "RESOLVE"):
                continue
            why = r.get("reason") or r.get("side") or r["event"]
            skips[why] += 1
        if skips:
            print("\n  Why it didn't trade (top reasons):")
            for why, c in sorted(skips.items(), key=lambda x: -x[1])[:8]:
                print(f"    {why:<18} {c}")

    print("\n" + "-" * 60)
    print("Reminder: dry-run P&L is simulated (assumes fills at the price cap).")
    print("Only LIVE rows reflect real fills, slippage, and outcomes.")


# ─── PROBE-LOG BACKTEST (phase2b_probe.py output) ────────────────────────────
def report_probe(rows):
    print("=" * 60)
    print("  PROBE BACKTEST  (from phase2b_probe.py)")
    print("=" * 60)

    # clean: drop late-join backfill (secs_left inconsistent with checkpoint) + dedupe
    clean, seen = [], set()
    for r in rows:
        sl = fl(r["secs_left"])
        if sl is None or abs(sl - int(r["checkpoint_s"])) > 8:
            continue
        k = (r["window_id"], r["checkpoint_s"])
        if k in seen:
            continue
        seen.add(k)
        clean.append(r)

    strike = {}
    for r in clean:
        s = fl(r["strike"])
        if s is not None:
            strike.setdefault(int(r["window_id"]), s)
    windows = sorted(strike)
    outc = {w: ("UP" if (w + 300 in strike and strike[w + 300] >= strike[w])
                else ("DOWN" if w + 300 in strike else None)) for w in windows}
    resolved = [w for w in windows if outc[w]]
    ups = sum(outc[w] == "UP" for w in resolved)

    print(f"\n  windows: {len(windows)} | with reconstructed outcome: {len(resolved)}")
    if resolved:
        print(f"  base rate: UP {ups}/{len(resolved)} = {ups / len(resolved):.0%}  "
              f"(balanced ~50% means no one-way trend bias)")
    print(f"  BTC drift over sample: "
          f"{strike[windows[0]]:.0f} -> {strike[windows[-1]]:.0f} "
          f"({strike[windows[-1]] - strike[windows[0]]:+.0f})")

    def sim(filt, label, fee=0.02, slip=0.01):
        per = {}
        for r in clean:
            if not filt(r):
                continue
            w = int(r["window_id"]); cp = int(r["checkpoint_s"]); ask = fl(r["fav_best_ask"])
            if outc.get(w) is None or ask is None:
                continue
            if w not in per or cp > per[w][0]:
                per[w] = (cp, r)
        bets = []
        for w, (cp, r) in per.items():
            ask = min(fl(r["fav_best_ask"]) + slip, 0.99)
            won = (r["favorite"] == outc[w])
            bets.append((won, ((1 / ask) if won else 0) - 1 - fee))
        n = len(bets)
        if not n:
            print(f"    {label:<26} no bets")
            return
        k = sum(b[0] for b in bets); pnl = sum(b[1] for b in bets)
        lo, hi = wilson(k, n)
        print(f"    {label:<26} n={n:<3} win={k/n:>4.0%} (CI {lo:.0%}-{hi:.0%}) "
              f"| $P&L ${pnl:+.2f}  (${pnl/n:+.3f}/bet)")

    print("\n  Buy-the-favorite by entry time (2% fee + 1-tick slippage):")
    for cp in ("120", "90", "60", "30", "15"):
        sim(lambda r, cp=cp: r["checkpoint_s"] == cp and r["fav_has_book"] == "True",
            f"entry @{cp}s")

    print("\n  Effect of the cushion filter at 120s entry:")
    for cm in (0, 25, 50, 75):
        sim(lambda r, cm=cm: (r["checkpoint_s"] == "120" and r["fav_has_book"] == "True"
                              and abs(fl(r["cushion"]) or 0) >= cm),
            f"cushion >= ${cm}")

    print("\n  Signals flagged fillable_edge=True:")
    sim(lambda r: r["fillable_edge"] == "True", "fillable_edge rule")

    print("\n" + "-" * 60)
    print("Caveat: outcomes are reconstructed from the NEXT window's open price,")
    print("and this is in-sample over a limited period. Treat high win rates as")
    print("regime-dependent until confirmed live.")


# ─── dispatch ────────────────────────────────────────────────────────────────
def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "trades_live.csv"
    try:
        rows = load(path)
    except FileNotFoundError:
        print(f"No file '{path}'. Run the bot first, or pass a log filename.")
        return
    if not rows:
        print(f"'{path}' is empty.")
        return
    header = set(rows[0].keys())
    if "fillable_edge" in header:
        report_probe(rows)
    elif "event" in header:
        report_trades(rows)
    else:
        print(f"Unrecognized log format in '{path}'. "
              "Expected a btc_live or phase2b_probe CSV.")


if __name__ == "__main__":
    main()
