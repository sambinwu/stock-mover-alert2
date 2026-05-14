"""Stock mover alert: posts to Slack when watched tickers move >5% intraday,
or when both S&P 500 and Nasdaq Composite move sharply in the same direction.

Triggers (per US trading day):
  * Any watched ticker's intraday % change vs. previous close has |%| > 5
    AND one of the following is also true:
      - |%| > 5 was sustained for MORE THAN 60 cumulative minutes during the
        regular session (measured on 1-minute bars), OR
      - The session's closing price itself is still |%| > 5 vs. previous close.
    A brief spike above 5% that quickly retraces (and does not close >5%) does
    NOT alert.
  * Both ^GSPC and ^IXIC are simultaneously up > 1.5%, OR
  * Both ^GSPC and ^IXIC are simultaneously down > 1%

De-duplication: each (ticker, trigger-type) is only sent once per ET trading
day. State is persisted in state/alerted-YYYY-MM-DD.json and cached between
GitHub Actions runs via actions/cache (keyed by date).

Trading-day guard: the script will only send alerts on real NYSE trading
days (weekends and US market holidays are skipped). Within a trading day,
it runs whether or not the bell is open -- the after-hours run acts as a
safety net for cron slots GitHub Actions failed to schedule on time.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time as dtime
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

STOCK_THRESHOLD_PCT = 5.0          # |move| > 5%
INDEX_UP_THRESHOLD_PCT = 1.5       # both indices up >1.5%
INDEX_DOWN_THRESHOLD_PCT = 1.0     # both indices down >1%

# A >5% move must persist for MORE THAN this many cumulative minutes during the
# session to qualify as a "sustained" alert. (Non-consecutive minutes count.)
SUSTAINED_MINUTES_REQUIRED = 60

STATE_DIR = Path("state")
ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def trading_day_key() -> str:
    return now_et().strftime("%Y-%m-%d")


def is_trading_day(now: datetime | None = None) -> bool:
    """Return True if `now` falls on a real NYSE trading day.

    Uses pandas_market_calendars to skip weekends and US market holidays.
    We deliberately do NOT gate on regular-hours-only here: the script
    intentionally also runs after-hours, as a safety net for cron slots
    GitHub Actions failed to schedule during the day.
    """
    if now is None:
        now = now_et()
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(
            start_date=now.date().isoformat(),
            end_date=now.date().isoformat(),
        )
        return not sched.empty
    except Exception as e:
        print(f"[warn] pandas_market_calendars unavailable ({e}); "
              "falling back to weekday check", file=sys.stderr)
        return now.weekday() < 5


def is_regular_hours(now: datetime | None = None) -> bool:
    """Heuristic: are we inside 09:30-16:00 ET on a weekday?

    Only used for logging / message context. Holiday-aware gating is done
    by is_trading_day().
    """
    if now is None:
        now = now_et()
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


def fetch_quote(ticker: str):
    """Return (last_price, prev_close, day_high, day_low).

    Uses yfinance fast_info for the live last and previous close, plus
    the day's high/low. Any field that can't be resolved comes back as None.
    """
    last = None
    prev = None
    day_high = None
    day_low = None
    try:
        t = yf.Ticker(ticker)
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
            try:
                day_high = float(fi["day_high"])
            except Exception:
                day_high = None
            try:
                day_low = float(fi["day_low"])
            except Exception:
                day_low = None

        if last is None or prev is None or day_high is None or day_low is None:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) >= 2:
                if prev is None:
                    prev = float(hist["Close"].iloc[-2])
                if last is None:
                    last = float(hist["Close"].iloc[-1])
                if day_high is None:
                    day_high = float(hist["High"].iloc[-1])
                if day_low is None:
                    day_low = float(hist["Low"].iloc[-1])
    except Exception as e:
        print(f"[warn] fetch_quote {ticker} failed: {e}", file=sys.stderr)

    return last, prev, day_high, day_low


def pct_change(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def evaluate_sustained_and_close(ticker: str, prev_close):
    """Inspect today's 1-minute bars and decide whether the >5% rule fires.

    Returns a dict:
      {
        "sustained_minutes": int,          # cumulative minutes today with
                                           #   |pct| > STOCK_THRESHOLD_PCT
        "sustained_peak_pct": float|None,  # signed % at the single most
                                           #   extreme qualifying minute
        "ref_price_sustained": float|None, # price at that minute
        "is_session_closed": bool,         # True if a 15:59 ET (or later) bar
                                           #   is present, i.e. we have the close
        "close_pct": float|None,           # signed % of the last bar vs prev
        "ref_price_close": float|None,     # price of the last bar
      }
    """
    out = {
        "sustained_minutes": 0,
        "sustained_peak_pct": None,
        "ref_price_sustained": None,
        "is_session_closed": False,
        "close_pct": None,
        "ref_price_close": None,
    }
    if prev_close is None or prev_close == 0:
        return out
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1d", interval="1m",
                         auto_adjust=False, prepost=False)
        if hist is None or hist.empty:
            return out

        # Normalize index timezone to ET.
        idx = hist.index
        if idx.tz is None:
            hist.index = idx.tz_localize("UTC").tz_convert(ET)
        else:
            hist.index = idx.tz_convert(ET)

        today = now_et().date()
        hist = hist[hist.index.date == today]
        if hist.empty:
            return out

        closes = hist["Close"].astype(float)
        pcts = (closes - prev_close) / prev_close * 100.0

        # Sustained: cumulative minutes where |pct| > threshold.
        over = pcts[pcts.abs() > STOCK_THRESHOLD_PCT]
        out["sustained_minutes"] = int(len(over))
        if not over.empty:
            peak_idx = over.abs().idxmax()
            out["sustained_peak_pct"] = float(over.loc[peak_idx])
            out["ref_price_sustained"] = float(closes.loc[peak_idx])

        # Closing-price check: the 1-min bar labeled 15:59 ET covers the
        # final minute of the regular session, so its Close is effectively
        # the session close.
        last_ts = hist.index[-1]
        if last_ts.time() >= dtime(15, 59):
            out["is_session_closed"] = True
            out["close_pct"] = float(pcts.iloc[-1])
            out["ref_price_close"] = float(closes.iloc[-1])
    except Exception as e:
        print(f"[warn] evaluate_sustained_and_close {ticker} failed: {e}",
              file=sys.stderr)
    return out


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
        print(f"[err] slack post failed {r.status_code}: {r.text}",
              file=sys.stderr)
    r.raise_for_status()


def fmt_pct(p):
    if p is None:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def build_stock_message(ticker, ref_price, prev, pct, *, intraday_peak=False):
    arrow = "🚀" if pct > 0 else "🔻"
    tag = " (intraday peak)" if intraday_peak else ""
    return (
        f"{arrow} *{ticker}* moved *{fmt_pct(pct)}*{tag} "
        f"(ref ${ref_price:.2f} vs prev close ${prev:.2f})"
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

    if not is_trading_day():
        print(f"[skip] not a trading day at {now_et().isoformat()}; "
              "no alerts will be sent")
        return 0

    regular_hours = is_regular_hours()
    print(f"[info] run at {now_et().isoformat()} "
          f"(regular_hours={regular_hours})")

    day_key = trading_day_key()
    alerted = load_state(day_key)
    new_alerts = []

    for ticker in WATCHLIST:
        key = f"stock:{ticker}"
        if key in alerted:
            continue
        last, prev, day_high, day_low = fetch_quote(ticker)
        if prev is None:
            continue

        # New rule: a >5% move alone is NOT enough.
        # We require EITHER cumulative-minutes-above-5% > 60 during the
        # session, OR the closing price itself still > 5% off prev close.
        info = evaluate_sustained_and_close(ticker, prev)

        sustained_fired = (
            info["sustained_minutes"] > SUSTAINED_MINUTES_REQUIRED
            and info["sustained_peak_pct"] is not None
        )
        close_fired = (
            info["is_session_closed"]
            and info["close_pct"] is not None
            and abs(info["close_pct"]) > STOCK_THRESHOLD_PCT
        )

        if not (sustained_fired or close_fired):
            # Touched 5% but neither sustained >60 cumulative minutes nor
            # closed >5% off prev close -> do not alert.
            print(f"[info] {ticker}: no alert "
                  f"(sustained_minutes={info['sustained_minutes']}, "
                  f"is_session_closed={info['is_session_closed']}, "
                  f"close_pct={info['close_pct']})")
            continue

        # Prefer the close-based message once the bell has rung; otherwise
        # report the sustained-peak move.
        if close_fired:
            pct = info["close_pct"]
            ref_price = info["ref_price_close"]
            if ref_price is None:
                ref_price = last if last is not None else prev
            intraday_peak = False  # this IS the close
        else:
            pct = info["sustained_peak_pct"]
            ref_price = info["ref_price_sustained"]
            if ref_price is None:
                ref_price = last if last is not None else prev
            intraday_peak = True   # sustained peak during the session

        new_alerts.append(
            build_stock_message(ticker, ref_price, prev, pct,
                                intraday_peak=intraday_peak)
        )
        alerted.add(key)

    sp_last, sp_prev, _, _ = fetch_quote(SP500)
    nq_last, nq_prev, _, _ = fetch_quote(NASDAQ_COMP)
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
