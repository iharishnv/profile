#!/usr/bin/env python3
"""Private price watch for a fixed exit window.

Pulls daily prices from Yahoo Finance, scores how "stretched" the price is,
and decides HOLD / WATCH / SELL LOT / SELL ALL. Stdlib only.

Everything personal comes from the WATCH_CONFIG secret (JSON) and the report
goes out only by email, so nothing about the position reaches the public
repo or its Actions logs:

  {"symbol": "...", "shares": 0, "lots": 3, "start": "YYYY-MM-DD",
   "end": "YYYY-MM-DD", "target": null, "floor": null, "trail_pct": 6,
   "final_days": 5, "email": "you@gmail.com"}

GMAIL_APP_PASSWORD holds a Gmail app password for that address.
"""
import json
import math
import os
import smtplib
import sys
import urllib.request
from datetime import date, datetime, timezone
from email.message import EmailMessage

CFG = json.loads(os.environ["WATCH_CONFIG"])
SYMBOL = CFG["symbol"]
SHARES = int(CFG.get("shares", 0))
LOTS = int(CFG.get("lots", 3))
START = date.fromisoformat(CFG["start"])
END = date.fromisoformat(CFG["end"])
TARGET = CFG.get("target")
FLOOR = CFG.get("floor")
TRAIL_PCT = float(CFG.get("trail_pct", 6))    # drop from window high that forces exit
FINAL_DAYS = int(CFG.get("final_days", 5))    # trading days before END to stop waiting
ALWAYS_EMAIL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"


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


def evaluate():
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
    lot = SHARES // LOTS

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
        action, why = "EXPIRED", "Exit window closed. Disable the workflow."
    elif left <= FINAL_DAYS:
        action, why = "SELL ALL", f"Only {left} trading days left; stop waiting for a better print."
    elif FLOOR and price <= FLOOR:
        action, why = "SELL ALL", f"Price {price:.2f} at/below your floor {FLOOR}."
    elif drawdown >= TRAIL_PCT and win_hi > start_px:
        action, why = "SELL ALL", f"Price fell {drawdown:.1f}% from the window high {win_hi:.2f}; trailing stop hit."
    elif score >= 3:
        action, why = "SELL LOT", f"{score}/5 top signals firing; sell one lot ({lot} shares)."
    elif score == 2:
        action, why = "WATCH", "2/5 top signals; get your order ready."
    else:
        action, why = "HOLD", f"{score}/5 top signals; no stretch in the price yet."

    lines = [
        f"{SYMBOL}: {action}",
        why,
        "",
        f"Price               {price:.3f} {ccy}",
        f"Position ({SHARES} sh)  {price * SHARES:,.0f} {ccy}",
        f"Window start / high {start_px:.3f} / {win_hi:.3f} ({drawdown:.1f}% off high)",
        f"52-week high        {hi52:.3f}",
        f"RSI(14)             {r:.1f}",
        f"20d SMA / upper BB  {sma20:.3f} / {upper_bb:.3f}",
        f"50d SMA             {sma50:.3f}",
        f"Target / floor      {TARGET or '-'} / {FLOOR or '-'}",
        f"Trading days left   {left} (to {END})",
        "",
    ] + [f"[{'x' if ok else ' '}] {n}" for n, ok in checks] + [
        "",
        f"Checked {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.",
    ]
    return action, price, ccy, "\n".join(lines)


def send(subject, body):
    msg = EmailMessage()
    msg["From"] = msg["To"] = CFG["email"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(CFG["email"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    action, price, ccy, body = evaluate()
    # Scheduled runs email only when there is something to do; manual runs always report.
    if ALWAYS_EMAIL or action in ("WATCH", "SELL LOT", "SELL ALL"):
        send(f"[{action}] {SYMBOL} at {price:.2f} {ccy}", body)
    print("ok")  # deliberately nothing else: Actions logs on a public repo are public


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"price watch failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
