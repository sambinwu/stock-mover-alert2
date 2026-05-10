"""Stock mover alert: posts to Slack when watched tickers move >5% intraday,
or when both S&P 500 and Nasdaq Composite move sharply in the same direction.

Triggers (per US trading day):
  * Any watched ticker's intraday % change vs. previous close has |%| > 5
  * Both ^GSPC and ^IXIC are simultaneously up > 1.5%, OR
  * Both ^GSPC and ^IXIC are simultaneously down > 1%

De-duplication: each (ticker, trigger-type) is only sent once per ET trading
day. State is persisted in state/alerted-YYYY-MM-DD.json and cached between
GitHub Actions runs via actions/cache (keyed by date).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

WATCHLIST = [
    "NVDA", "META", "TSLA", "PLTR", "MSFT", "TEM", "TSM",
    "NFLX", "GOOGL", "AMZN", "GS", "COST", "INTC", "ORCL",
]
SP500 = "^GSPC"
NASDAQ_COMP = "^IXIC"

STOCK_THRESHOLD_PCT = 5.0       # |move| > 5%
INDEX_UP_THRESHOLD_PCT = 1.5    # both indices up >1.5%
INDEX_DOWN_THRESHOLD_PCT = 1.0  # both indices down >1%

STATE_DIR = Path("state")
ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def trading_day_key() -> str:
    return now_et().strftime("%Y-%m-%d")


def fetch_quote(ticker: str):
    """Return (last_price, prev_close) using yfinance fast_info, with daily fallback."""
    try:
        t = yf.Ticker(ticker)
        last = None
        prev = None
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            try:
                last = float(fi["last_price"])
            except Exception:
                last = None
            try:
                prev = float(fi["previous_close"])
            except Exception:
                prev = None
        if last is None or prev is None:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 2:
                if prev is None:
                    prev = float(hist["Close"].iloc[-2])
                if last is None:
                    last = float(hist["Close"].iloc[-1])
        return last, prev
    except Exception as e:
        print(f"[warn] fetch_quote {ticker} failed: {e}", file=sys.stderr)
        return None, None


def pct_change(last, prev):
    if last is None or prev is None or prev == 0:
        return None
    return (last - prev) / prev * 100.0


def load_state(day_key: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / f"alerted-{day_key}.json"
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text()))
    except Exception:
        return set()


def save_state(day_key: str, alerted):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = STATE_DIR / f"alerted-{day_key}.json"
    f.write_text(json.dumps(sorted(alerted)))


def post_slack(webhook: str, text: str):
    r = requests.post(webhook, json={"text": text}, timeout=15)
    if r.status_code >= 300:
        print(f"[err] slack post failed {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()


def fmt_pct(p):
    if p is None:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def build_stock_message(ticker, last, prev, pct):
    arrow = "🚀" if pct > 0 else "🔻"
    return (
        f"{arrow} *{ticker}* moved *{fmt_pct(pct)}* intraday  "
        f"(last ${last:.2f} vs prev close ${prev:.2f})"
    )


def build_index_message(direction, sp_pct, nq_pct):
    if direction == "up":
        return (
            f"📈 *Broad rally*: S&P 500 {fmt_pct(sp_pct)} & "
            f"Nasdaq Composite {fmt_pct(nq_pct)} (both > +1.5%)"
        )
    return (
        f"📉 *Broad selloff*: S&P 500 {fmt_pct(sp_pct)} & "
        f"Nasdaq Composite {fmt_pct(nq_pct)} (both < -1%)"
    )


def main() -> int:
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        print("[err] SLACK_WEBHOOK_URL not set", file=sys.stderr)
        return 2

    day_key = trading_day_key()
    alerted = load_state(day_key)
    new_alerts = []

    for ticker in WATCHLIST:
        key = f"stock:{ticker}"
        if key in alerted:
            continue
        last, prev = fetch_quote(ticker)
        pct = pct_change(last, prev)
        if pct is None:
            continue
        if abs(pct) > STOCK_THRESHOLD_PCT:
            new_alerts.append(build_stock_message(ticker, last, prev, pct))
            alerted.add(key)

    sp_last, sp_prev = fetch_quote(SP500)
    nq_last, nq_prev = fetch_quote(NASDAQ_COMP)
    sp_pct = pct_change(sp_last, sp_prev)
    nq_pct = pct_change(nq_last, nq_prev)

    if sp_pct is not None and nq_pct is not None:
        up_key = "index:both-up"
        down_key = "index:both-down"
        if (up_key not in alerted
                and sp_pct > INDEX_UP_THRESHOLD_PCT
                and nq_pct > INDEX_UP_THRESHOLD_PCT):
            new_alerts.append(build_index_message("up", sp_pct, nq_pct))
            alerted.add(up_key)
        if (down_key not in alerted
                and sp_pct < -INDEX_DOWN_THRESHOLD_PCT
                and nq_pct < -INDEX_DOWN_THRESHOLD_PCT):
            new_alerts.append(build_index_message("down", sp_pct, nq_pct))
            alerted.add(down_key)

    if new_alerts:
        header = f"*Stock mover alert* — {now_et().strftime('%Y-%m-%d %H:%M ET')}"
        body = "\n".join(new_alerts)
        post_slack(webhook, f"{header}\n{body}")
        save_state(day_key, alerted)
        print(f"posted {len(new_alerts)} alert(s)")
    else:
        print("no new alerts")

    return 0


if __name__ == "__main__":
    sys.exit(main())
