#!/usr/bin/env python3
"""Nokia sell-window tracker.

Pulls daily prices from Yahoo Finance, scores how "stretched" the price is,
and decides HOLD / SELL TRANCHE / SELL ALL for a fixed 30-day exit window.
Stdlib only, so it runs on a bare GitHub Actions runner.

Output: a markdown report on stdout, and (when GITHUB_OUTPUT is set) the
fields `action`, `price`, and `signal_key` for the workflow to act on.
"""
import json
import math
import os
import sys
import urllib.request
from datetime import date, datetime, timezone

SYMBOL = os.environ.get("SYMBOL", "NOKIA.HE")          # NOK for the NYSE ADR
SHARES = int(os.environ.get("SHARES", "900"))
TRANCHES = int(os.environ.get("TRANCHES", "3"))
START = date.fromisoformat(os.environ.get("START_DATE", "2026-10-21"))
END = date.fromisoformat(os.environ.get("END_DATE", "2026-11-20"))
TARGET = float(os.environ["TARGET_PRICE"]) if os.environ.get("TARGET_PRICE") else None
TRAIL_PCT = float(os.environ.get("TRAIL_PCT", "6"))    # drop from window high that forces exit
FINAL_DAYS = int(os.environ.get("FINAL_DAYS", "5"))    # trading days before END to stop waiting


def fetch(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    rows = [
        (datetime.fromtimestamp(t, timezone.utc).date(), c, h)
        for t, c, h in zip(res["timestamp"], q["close"], q["high"])
        if c is not None
    ]
    live = res["meta"].get("regularMarketPrice") or rows[-1][1]
    return rows, live, res["meta"].get("currency", "")


def rsi(closes, n=14):
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def business_days(a, b):
    return sum(1 for i in range((b - a).days) if date.fromordinal(a.toordinal() + i + 1).weekday() < 5)


def main():
    rows, price, ccy = fetch(SYMBOL)
    closes = [c for _, c, _ in rows]
    today = datetime.now(timezone.utc).date()

    sma20 = sum(closes[-20:]) / 20
    sd20 = math.sqrt(sum((c - sma20) ** 2 for c in closes[-20:]) / 20)
    upper_bb = sma20 + 2 * sd20
    sma50 = sum(closes[-50:]) / 50
    r = rsi(closes + [price] if rows[-1][0] != today else closes)
    hi52 = max(h for _, _, h in rows if h is not None)
    window = [c for d, c, _ in rows if d >= START] + [price]
    win_hi, start_px = max(window), window[0]
    drawdown = (win_hi - price) / win_hi * 100
    left = business_days(today, END)

    # Stretch score: each condition is a sign the price sits near a short-term top.
    checks = [
        ("RSI(14) >= 70 (overbought)", r >= 70),
        ("Price above upper Bollinger band (20d, 2σ)", price >= upper_bb),
        ("Within 3% of 52-week high", price >= hi52 * 0.97),
        ("Price >= 8% above 50-day average", price >= sma50 * 1.08),
        (f"At/above target {TARGET}" if TARGET else "Target price (not set)", bool(TARGET and price >= TARGET)),
    ]
    score = sum(ok for _, ok in checks)

    if today > END:
        action, why = "EXPIRED", "30-day window closed."
    elif left <= FINAL_DAYS:
        action, why = "SELL ALL", f"Only {left} trading days left; stop waiting for a better print."
    elif drawdown >= TRAIL_PCT and win_hi > start_px:
        action, why = "SELL ALL", f"Price fell {drawdown:.1f}% from the window high {win_hi:.2f}; trailing stop hit."
    elif score >= 3:
        action, why = "SELL TRANCHE", f"{score}/5 top signals firing; sell {SHARES // TRANCHES} shares."
    elif score == 2:
        action, why = "WATCH", "2/5 top signals; get your order ready."
    else:
        action, why = "HOLD", f"{score}/5 top signals; no stretch in the price yet."

    report = f"""### Nokia ({SYMBOL}) — {action}
**{why}**

| Metric | Value |
|---|---|
| Price | {price:.3f} {ccy} |
| Position value ({SHARES} sh) | {price * SHARES:,.0f} {ccy} |
| Window start / high | {start_px:.3f} / {win_hi:.3f} ({drawdown:.1f}% off high) |
| 52-week high | {hi52:.3f} |
| RSI(14) | {r:.1f} |
| 20d SMA / upper band | {sma20:.3f} / {upper_bb:.3f} |
| 50d SMA | {sma50:.3f} |
| Trading days left (to {END}) | {left} |

| Top signal | Firing |
|---|---|
""" + "\n".join(f"| {n} | {'✅' if ok else '—'} |" for n, ok in checks) + \
        f"\n\n_Checked {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._\n"
    print(report)

    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"action={action}\nprice={price:.3f}\nsignal_key={today}-{action}\n")
            f.write("report<<EOF\n" + report + "\nEOF\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # surface fetch failures in the workflow log
        print(f"Tracker failed: {e}", file=sys.stderr)
        sys.exit(1)
