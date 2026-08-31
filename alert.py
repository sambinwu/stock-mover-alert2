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

"Previous close" resolution
---------------------------
The reference close MUST be the close of the exact previous NYSE session --
never "whatever the most recent daily bar happens to be". Yahoo has been
observed to serve daily OHLC bars where an entire session is null/missing
(e.g. Fri 2026-08-28 came back empty for most tickers). The old code walked
back to the next-most-recent bar, silently comparing against a close from two
sessions ago; that produced a bogus -8.26% HOOD alert and swallowed a real
+5.5% TSLA move on the same day.

So now:
  1. The expected previous session date comes from the NYSE calendar.
  2. We take the daily bar for THAT EXACT DATE.
  3. If it is missing/NaN, we rebuild that day's close from intraday bars.
  4. If that also fails, we fall back to fast_info['previous_close'].
  5. If everything fails we SKIP the ticker. We never substitute an older
     session's close.

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
import math
import os
import sys
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

WATCHLIST = [
    "NVDA", "META", "TSLA", "PLTR", "MSFT", "TEM", "TSM",
    "NFLX", "GOOGL", "AMZN", "GS", "COST", "INTC", "ORCL", "AAPL", "SNOW",
    "HOOD", "SPCX", "SHOP"
]
SP500 = "^GSPC"
NASDAQ_COMP = "^IXIC"

STOCK_THRESHOLD_PCT = 5.0        # |move| > 5%
INDEX_UP_THRESHOLD_PCT = 1.5     # both indices up >1.5%
INDEX_DOWN_THRESHOLD_PCT = 1.0   # both indices down >1%

# A >5% move must persist for MORE THAN this many cumulative minutes during the
# session to qualify as a "sustained" alert. (Non-consecutive minutes count.)
SUSTAINED_MINUTES_REQUIRED = 90

# If fast_info['previous_close'] disagrees with the resolved previous-session
# close by more than this percent, emit a [warn] line so the divergence is
# auditable.
PREV_CLOSE_DRIFT_WARN_PCT = 0.05

STATE_DIR = Path("state")
ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def trading_day_key() -> str:
    return now_et().strftime("%Y-%m-%d")


def _nyse_sessions(start: date, end: date) -> list[date]:
    """NYSE session dates in [start, end], or [] if the calendar is unusable."""
    import pandas_market_calendars as mcal
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if sched.empty:
        return []
    return [ts.date() for ts in sched.index]


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
        return bool(_nyse_sessions(now.date(), now.date()))
    except Exception as e:
        print(f"[warn] pandas_market_calendars unavailable ({e}); "
              "falling back to weekday check", file=sys.stderr)
        return now.weekday() < 5


def previous_trading_day(today: date | None = None) -> date | None:
    """The most recent NYSE session strictly before `today`.

    This is the single source of truth for which date the alert compares
    against. Never infer it from whatever bars a data provider happened to
    return.
    """
    if today is None:
        today = now_et().date()
    try:
        sessions = _nyse_sessions(today - timedelta(days=12),
                                  today - timedelta(days=1))
        if sessions:
            return sessions[-1]
    except Exception as e:
        print(f"[warn] previous_trading_day: calendar unavailable ({e}); "
              "falling back to weekday arithmetic", file=sys.stderr)
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


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


def _is_num(x) -> bool:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return not math.isnan(f) and f > 0


def _to_et_index(hist):
    idx = hist.index
    if idx.tz is None:
        hist.index = idx.tz_localize("UTC").tz_convert(ET)
    else:
        hist.index = idx.tz_convert(ET)
    return hist


def fetch_quote(ticker: str):
    """Return (last_price, prev_close_fast_info, day_high, day_low).

    The second value is yfinance fast_info['previous_close']. It is only a
    fallback for resolve_prev_close(); it has been observed stale on some
    days, so it is never the primary reference.
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


def daily_closes(ticker: str) -> dict[date, float]:
    """{session date -> close} from daily OHLC bars. NaN/empty rows dropped."""
    out: dict[date, float] = {}
    try:
        t = yf.Ticker(ticker)
        start_dt = (now_et().date() - timedelta(days=14)).isoformat()
        hist = t.history(start=start_dt, interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return out
        for stamp, close in zip(hist.index, hist["Close"].tolist()):
            if not _is_num(close):
                continue
            try:
                d = stamp.date()
            except Exception:
                d = stamp.to_pydatetime().date()
            out[d] = float(close)
    except Exception as e:
        print(f"[warn] daily_closes {ticker} failed: {e}", file=sys.stderr)
    return out


def close_from_intraday(ticker: str, day: date) -> float | None:
    """Rebuild `day`'s regular-session close from intraday bars.

    Used when the daily bar for that exact session is missing or NaN. Yahoo
    retains 1m bars for ~30 days and 30m/1h bars for ~60 days, which comfortably
    covers "yesterday".
    """
    for interval in ("30m", "1h", "1m"):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(start=day.isoformat(),
                             end=(day + timedelta(days=1)).isoformat(),
                             interval=interval, auto_adjust=False, prepost=False)
            if hist is None or hist.empty:
                continue
            hist = _to_et_index(hist)
            hist = hist[hist.index.date == day]
            hist = hist[[_is_num(c) for c in hist["Close"].tolist()]]
            if hist.empty:
                continue
            close = float(hist["Close"].iloc[-1])
            if _is_num(close):
                print(f"[info] {ticker}: rebuilt {day} close from {interval} "
                      f"bars -> {close:.4f}")
                return close
        except Exception as e:
            print(f"[warn] close_from_intraday {ticker} {day} {interval} "
                  f"failed: {e}", file=sys.stderr)
    return None


def resolve_prev_close(ticker: str, fast_info_prev: float | None = None):
    """Return (prev_close, prev_close_date, source).

    prev_close_date is ALWAYS the exact previous NYSE session (or None on
    failure). We never silently fall back to an older session's close --
    that is the bug this function exists to prevent.
    """
    expected = previous_trading_day()
    if expected is None:
        print(f"[warn] {ticker}: cannot determine previous trading day",
              file=sys.stderr)
        return None, None, "unavailable"

    closes = daily_closes(ticker)
    if expected in closes:
        return closes[expected], expected, "daily"

    print(f"[warn] {ticker}: daily bar for {expected} is missing/NaN "
          f"(have {sorted(closes)[-4:] if closes else []}); "
          "falling back to intraday reconstruction", file=sys.stderr)

    close = close_from_intraday(ticker, expected)
    if close is not None:
        return close, expected, "intraday"

    if _is_num(fast_info_prev):
        print(f"[warn] {ticker}: using fast_info previous_close "
              f"{float(fast_info_prev):.4f} for {expected}", file=sys.stderr)
        return float(fast_info_prev), expected, "fast_info"

    print(f"[warn] {ticker}: no previous close for {expected}; skipping "
          "(refusing to compare against an older session)", file=sys.stderr)
    return None, None, "unavailable"


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

        hist = _to_et_index(hist)

        today = now_et().date()
        hist = hist[hist.index.date == today]
        if hist.empty:
            return out

        closes = hist["Close"].astype(float)
        highs = hist["High"].astype(float)
        lows = hist["Low"].astype(float)

        high_pcts = (highs - prev_close) / prev_close * 100.0
        low_pcts = (lows - prev_close) / prev_close * 100.0

        # For each bar take whichever extreme is further from the reference,
        # and keep the PRICE that produced it so the reported price and the
        # reported percentage always describe the same tick.
        take_high = high_pcts.abs() >= low_pcts.abs()
        bar_peak_pct = high_pcts.where(take_high, low_pcts)
        bar_peak_price = highs.where(take_high, lows)

        over = bar_peak_pct[bar_peak_pct.abs() >= STOCK_THRESHOLD_PCT]
        out["sustained_minutes"] = int(len(over))
        if not over.empty:
            peak_idx = over.abs().idxmax()
            out["sustained_peak_pct"] = float(over.loc[peak_idx])
            out["ref_price_sustained"] = float(bar_peak_price.loc[peak_idx])

        last_ts = hist.index[-1]
        if last_ts.time() >= dtime(15, 59):
            out["is_session_closed"] = True
            out["close_pct"] = float(
                (closes.iloc[-1] - prev_close) / prev_close * 100.0)
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


def _audit_prev_close(ticker, fast_info_prev, resolved_prev):
    """Print a [warn] line if fast_info disagrees with the resolved
    previous-session close by more than PREV_CLOSE_DRIFT_WARN_PCT percent.
    """
    if fast_info_prev is None or resolved_prev is None or resolved_prev == 0:
        return
    drift = abs(fast_info_prev - resolved_prev) / resolved_prev * 100.0
    if drift > PREV_CLOSE_DRIFT_WARN_PCT:
        print(f"[warn] {ticker} prev_close drift: "
              f"fast_info={fast_info_prev:.4f} vs "
              f"resolved={resolved_prev:.4f} ({drift:.3f}%)",
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
    expected_prev = previous_trading_day()
    print(f"[info] run at {now_et().isoformat()} "
          f"(regular_hours={regular_hours}, prev_session={expected_prev})")

    day_key = trading_day_key()
    alerted = load_state(day_key)
    new_alerts = []

    for ticker in WATCHLIST:
        key = f"stock:{ticker}"
        if key in alerted:
            continue
        last, fi_prev, day_high, day_low = fetch_quote(ticker)

        prev, prev_date, prev_source = resolve_prev_close(ticker, fi_prev)
        if prev is None:
            # Deliberately no older-session fallback: a wrong reference is
            # worse than a missed run, and the next run will retry.
            continue
        _audit_prev_close(ticker, fi_prev, prev)

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
                  f"(prev_close={prev:.4f} on {prev_date} [{prev_source}], "
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

        print(f"[info] {ticker}: ALERT {pct:+.2f}% "
              f"(prev_close={prev:.4f} on {prev_date} [{prev_source}], "
              f"intraday_peak={intraday_peak})")

        new_alerts.append(
            build_stock_message(ticker, ref_price, prev, pct,
                                intraday_peak=intraday_peak,
                                prev_date=prev_date)
        )
        alerted.add(key)

    # --- Indices ---
    sp_last, sp_fi_prev, _, _ = fetch_quote(SP500)
    nq_last, nq_fi_prev, _, _ = fetch_quote(NASDAQ_COMP)
    sp_prev, sp_prev_date, sp_src = resolve_prev_close(SP500, sp_fi_prev)
    nq_prev, nq_prev_date, nq_src = resolve_prev_close(NASDAQ_COMP, nq_fi_prev)
    _audit_prev_close(SP500, sp_fi_prev, sp_prev)
    _audit_prev_close(NASDAQ_COMP, nq_fi_prev, nq_prev)

    sp_pct = pct_change(sp_last, sp_prev)
    nq_pct = pct_change(nq_last, nq_prev)
    print(f"[info] indices: S&P 500 {fmt_pct(sp_pct)} "
          f"(prev {sp_prev_date}={sp_prev} [{sp_src}]), "
          f"Nasdaq Composite {fmt_pct(nq_pct)} "
          f"(prev {nq_prev_date}={nq_prev} [{nq_src}])")

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
