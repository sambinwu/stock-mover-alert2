"""Stock mover alert: posts to Slack when watched tickers move >5% intraday,
or when both S&P 500 and Nasdaq Composite move sharply in the same direction.

Triggers (per US trading day):
  * Any watched ticker's intraday % change vs. previous close has |%| > 5
    AND one of the following is also true:
      - |%| > 5 was sustained for MORE THAN 90 cumulative minutes during the
        regular session (measured on 1-minute bars), OR
      - The session's closing price itself is still |%| > 5 vs. previous close.
    A brief spike above 5% that quickly retraces (and does not close >5%) does
    NOT alert.
  * Both ^GSPC and ^IXIC are simultaneously up > 1.5%, OR
  * Both ^GSPC and ^IXIC are simultaneously down > 1%

"Previous close" is resolved from the daily-OHLC history (interval='1d'):
specifically, the Close of the most recent NYSE trading day strictly before
today (ET). yfinance's fast_info['previous_close'] has been observed to
return a stale value on some days, so we treat the daily-bar Close as the
authoritative reference. fast_info is only used as a last-resort fallback
if the daily-bar query fails outright.

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
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

WATCHLIST = [
    "NVDA", "META", "TSLA", "PLTR", "MSFT", "TEM", "TSM",
    "NFLX", "GOOGL", "AMZN", "GS", "COST", "INTC", "ORCL", "AAPL", "SNOW"
]
SP500 = "^GSPC"
NASDAQ_COMP = "^IXIC"

STOCK_THRESHOLD_PCT = 5.0          # |move| > 5%
INDEX_UP_THRESHOLD_PCT = 1.5       # both indices up >1.5%
INDEX_DOWN_THRESHOLD_PCT = 1.0     # both indices down >1%

# A >5% move must persist for MORE THAN this many cumulative minutes during the
# session to qualify as a "sustained" alert. (Non-consecutive minutes count.)
SUSTAINED_MINUTES_REQUIRED = 90

# If fast_info['previous_close'] disagrees with the daily-bar Close by more
# than this percent, emit a [warn] line so the divergence is auditable.
PREV_CLOSE_DRIFT_WARN_PCT = 0.05

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
    """Return (last_price, prev_close_fast_info, day_high, day_low).

    The second value is yfinance fast_info['previous_close'], which is NOT
    authoritative -- it has been observed to return a stale value (e.g. the
    Close from two sessions ago) on some days. Always prefer resolve_prev_close().
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


def resolve_prev_close(ticker: str):
    """Return (prev_close, prev_close_date) for the most recent NYSE trading
    day strictly before today (ET).

    Uses daily-OHLC bars (interval='1d') and picks the most recent bar whose
    date is < today. This is the authoritative prior-trading-day close;
    yfinance's fast_info['previous_close'] is unreliable and should not be
    used for the alert comparison.

    Returns (None, None) on failure.
    """
    try:
        t = yf.Ticker(ticker)
        # 7 calendar days easily covers weekends + any 3-day holiday gap.
        start_dt = (now_et().date() - timedelta(days=7)).isoformat()
        hist = t.history(start=start_dt, interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None, None

        idx = hist.index
        dates: list[date] = []
        for d in idx:
            try:
                # Daily bars: the index date IS the trading day; take it as-is.
                if getattr(d, "tzinfo", None) is not None:
                    dates.append(d.date())
                else:
                    dates.append(d.date())
            except Exception:
                # Fall back to pydatetime path
                dates.append(d.to_pydatetime().date())

        closes = [float(c) for c in hist["Close"].tolist()]
        today = now_et().date()

        for d, c in zip(reversed(dates), reversed(closes)):
            if d < today:
                return c, d
        return None, None
    except Exception as e:
        print(f"[warn] resolve_prev_close {ticker} failed: {e}",
              file=sys.stderr)
        return None, None


def pct_change(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def evaluate_sustained_and_close(ticker: str, prev_close):
    """Inspect today's 1-minute bars and decide whether the >5% rule fires.

    Returns a dict:
      {
        "sustained_minutes": int,
        "sustained_peak_pct": float|None,
        "ref_price_sustained": float|None,
        "is_session_closed": bool,
        "close_pct": float|None,
        "ref_price_close": float|None,
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

        idx = hist.index
        if idx.tz is None:
            hist.index = idx.tz_localize("UTC").tz_convert(ET)
        else:
            hist.index = idx.tz_convert(ET)

        today = now_et().date()
        hist = hist[hist.index.date == today]
        if hist.empty:
            return out

        closes = hist["Close"].astype(float); highs = hist["High"].astype(float); lows = hist["Low"].astype(float)
        pcts = (closes - prev_close) / prev_close * 100.0; high_pcts = (highs - prev_close) / prev_close * 100.0; low_pcts = (lows - prev_close) / prev_close * 100.0; bar_peak_pct = high_pcts.where(high_pcts.abs() >= low_pcts.abs(), low_pcts)

        over = bar_peak_pct[bar_peak_pct.abs() >= STOCK_THRESHOLD_PCT]
        out["sustained_minutes"] = int(len(over))
        if not over.empty:
            peak_idx = over.abs().idxmax()
            out["sustained_peak_pct"] = float(over.loc[peak_idx])
            out["ref_price_sustained"] = float(closes.loc[peak_idx])

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


def build_stock_message(ticker, ref_price, prev, pct, *, intraday_peak=False,
                        prev_date=None):
    arrow = "🚀" if pct > 0 else "🔻"
    tag = " (intraday peak)" if intraday_peak else ""
    prev_tag = f" {prev_date}" if prev_date else ""
    return (
        f"{arrow} *{ticker}* moved *{fmt_pct(pct)}*{tag} "
        f"(ref ${ref_price:.2f} vs prev close{prev_tag} ${prev:.2f})"
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


def _audit_prev_close(ticker, fast_info_prev, daily_prev):
    """Print a [warn] line if fast_info disagrees with the daily-bar Close
    by more than PREV_CLOSE_DRIFT_WARN_PCT percent.
    """
    if fast_info_prev is None or daily_prev is None or daily_prev == 0:
        return
    drift = abs(fast_info_prev - daily_prev) / daily_prev * 100.0
    if drift > PREV_CLOSE_DRIFT_WARN_PCT:
        print(f"[warn] {ticker} prev_close drift: "
              f"fast_info={fast_info_prev:.4f} vs "
              f"daily={daily_prev:.4f} ({drift:.3f}%)",
              file=sys.stderr)


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
        last, fi_prev, day_high, day_low = fetch_quote(ticker)

        # Authoritative prior-trading-day Close from daily bars.
        prev, prev_date = resolve_prev_close(ticker)
        _audit_prev_close(ticker, fi_prev, prev)
        if prev is None:
            # Last-resort fallback: only used if daily history is totally unavailable.
            prev = fi_prev
        if prev is None:
            print(f"[warn] {ticker}: no prev_close available, skipping",
                  file=sys.stderr)
            continue

        info = evaluate_sustained_and_close(ticker, prev)

        sustained_fired = (
            info["sustained_minutes"] >= SUSTAINED_MINUTES_REQUIRED
            and info["sustained_peak_pct"] is not None
        )
        close_fired = (
            info["is_session_closed"]
            and info["close_pct"] is not None
            and abs(info["close_pct"]) >= STOCK_THRESHOLD_PCT
        )

        if not (sustained_fired or close_fired):
            print(f"[info] {ticker}: no alert "
                  f"(prev_close={prev:.4f} on {prev_date}, "
                  f"sustained_minutes={info['sustained_minutes']}, "
                  f"is_session_closed={info['is_session_closed']}, "
                  f"close_pct={info['close_pct']})")
            continue

        if close_fired:
            pct = info["close_pct"]
            ref_price = info["ref_price_close"]
            if ref_price is None:
                ref_price = last if last is not None else prev
            intraday_peak = False
        else:
            pct = info["sustained_peak_pct"]
            ref_price = info["ref_price_sustained"]
            if ref_price is None:
                ref_price = last if last is not None else prev
            intraday_peak = True
        
        new_alerts.append(
            build_stock_message(ticker, ref_price, prev, pct,
                                intraday_peak=intraday_peak,
                                prev_date=prev_date)
        )
        alerted.add(key)

    # --- Indices ---
    sp_last, sp_fi_prev, _, _ = fetch_quote(SP500)
    nq_last, nq_fi_prev, _, _ = fetch_quote(NASDAQ_COMP)
    sp_prev, sp_prev_date = resolve_prev_close(SP500)
    nq_prev, nq_prev_date = resolve_prev_close(NASDAQ_COMP)
    _audit_prev_close(SP500, sp_fi_prev, sp_prev)
    _audit_prev_close(NASDAQ_COMP, nq_fi_prev, nq_prev)
    if sp_prev is None:      
        sp_prev = sp_fi_prev
    if nq_prev is None:        
        nq_prev = nq_fi_prev

    sp_pct = pct_change(sp_last, sp_prev)
    nq_pct = pct_change(nq_last, nq_prev)
    print(f"[info] indices: S&P 500 {fmt_pct(sp_pct)} "
          f"(prev {sp_prev_date}={sp_prev}), "
          f"Nasdaq Composite {fmt_pct(nq_pct)} "
          f"(prev {nq_prev_date}={nq_prev})")

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
