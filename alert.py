"""Stock mover alert: posts to Slack when watched tickers make big moves, or
when both the S&P 500 and the Nasdaq Composite move sharply the same way.

Triggers (per US trading day)
-----------------------------
* A watched ticker (watchlist.txt) is more than 5% away from the previous
  session's close AND one of these is true:
    - it spent MORE THAN 90 cumulative minutes beyond 5% during the regular
      session (measured on 1-minute bars), OR
    - the session closed beyond 5%.
  A brief spike past 5% that quickly retraces does NOT alert.
* ^GSPC and ^IXIC both up > 1.5%, or both down > 1%.

How it runs (and why)
---------------------
GitHub's cron scheduler silently drops most slots (2026-10-07: ~70 slots
scheduled, 6 ran, first one at 15:13 ET), so a "run every 15 minutes" design
alerted hours late. Now a single run WATCHES the whole session: it loops every
CHECK_INTERVAL_MIN minutes from the open until shortly after the close. The
many cron entries are only there to make sure at least one run starts; the
workflow's lock step makes every extra run exit within seconds.

Self-healing
------------
* Catch-up: every run first re-checks the PREVIOUS session with its final
  1-minute bars. If no run happened after yesterday's close (scheduler drop,
  outage) the alert is still delivered, tagged "补报".
* Early closes (half-days) use the real NYSE close time, not 16:00.
* A Slack post that fails is retried on the next loop; the alert is only
  marked as sent after Slack accepts it.
* If market data is unavailable for many tickers for several loops in a row,
  one warning is posted to Slack per day so a silent outage can't hide.

"Previous close" resolution
---------------------------
The reference close is the close of the EXACT previous NYSE session (from the
NYSE calendar): daily bar for that date -> rebuilt from intraday bars ->
fast_info previous_close (only for today's reference). If all fail the ticker
is skipped; an older session's close is never substituted.

De-duplication: each (ticker, trigger-type) is sent once per trading day.
State lives in state/alerted-YYYY-MM-DD.json, carried between runs by
actions/cache (latest cache wins, so yesterday's file is available for
catch-up).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

# ----------------------------------------------------------------- settings
DEFAULT_WATCHLIST = [
    "NVDA", "AMZN", "META", "TEM", "TSLA", "NFLX", "TSM", "GOOGL", "INTC",
    "CAT", "SNOW", "ANET", "HOOD", "SHOP", "ORCL", "GS", "SPCX", "MSFT",
    "PLTR", "AAPL", "COST", "AVGO", "MU", "CRWD",
]
WATCHLIST_FILE = Path(__file__).with_name("watchlist.txt")
SP500 = "^GSPC"
NASDAQ_COMP = "^IXIC"

STOCK_THRESHOLD_PCT = 5.0        # |move| >= 5%
INDEX_UP_THRESHOLD_PCT = 1.5     # both indices up > 1.5%
INDEX_DOWN_THRESHOLD_PCT = 1.0   # both indices down > 1%
SUSTAINED_MINUTES_REQUIRED = 90  # cumulative minutes beyond 5%

CHECK_INTERVAL_MIN = 3           # loop cadence during the session
MAX_PRE_OPEN_WAIT_MIN = 75       # started earlier than this before the open -> exit
POST_CLOSE_GRACE_MIN = 20        # keep watching this long after the close
MAX_LOOP_MINUTES = 335           # stay under the job's 350-min timeout
FETCH_TIMEOUT_S = 20             # per HTTP call to Yahoo
ITERATION_TIMEOUT_S = 240        # give up on stragglers within one loop
BAD_DATA_SHARE = 1 / 3           # share of tickers without data = "bad loop"
BAD_DATA_LOOPS_BEFORE_WARN = 5

STATE_DIR = Path("state")
STATE_KEEP_DAYS = 14
ET = ZoneInfo("America/New_York")


# ------------------------------------------------------------------- basics
def now_et() -> datetime:
    return datetime.now(ET)


def log(msg: str, err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout, flush=True)


def load_watchlist() -> list[str]:
    try:
        out = []
        for line in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines():
            t = line.split("#", 1)[0].strip().upper()
            if t and t not in out:
                out.append(t)
        if out:
            return out
    except FileNotFoundError:
        pass
    log("[warn] watchlist.txt missing/empty; using built-in list", err=True)
    return list(DEFAULT_WATCHLIST)


def _is_num(x) -> bool:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return not math.isnan(f) and f > 0


def fmt_pct(p):
    if p is None:
        return "n/a"
    return f"{'+' if p >= 0 else ''}{p:.2f}%"


def pct_change(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


# ----------------------------------------------------------- NYSE calendar
_SESSION_CACHE: dict[tuple[date, date], list[tuple[date, datetime, datetime]]] = {}


def _sessions(start: date, end: date):
    """[(day, open_et, close_et)] for NYSE sessions in [start, end].

    Falls back to Mon-Fri 09:30-16:00 if the calendar library fails.
    """
    key = (start, end)
    if key in _SESSION_CACHE:
        return _SESSION_CACHE[key]
    out = []
    try:
        import pandas_market_calendars as mcal
        sched = mcal.get_calendar("NYSE").schedule(
            start_date=start.isoformat(), end_date=end.isoformat())
        for idx, row in sched.iterrows():
            out.append((idx.date(),
                        row["market_open"].tz_convert(ET).to_pydatetime(),
                        row["market_close"].tz_convert(ET).to_pydatetime()))
    except Exception as e:  # pragma: no cover - defensive
        log(f"[warn] NYSE calendar unavailable ({e}); weekday fallback", err=True)
        d = start
        while d <= end:
            if d.weekday() < 5:
                out.append((d, datetime.combine(d, dtime(9, 30), ET),
                            datetime.combine(d, dtime(16, 0), ET)))
            d += timedelta(days=1)
    _SESSION_CACHE[key] = out
    return out


def session_for(day: date):
    s = _sessions(day, day)
    return s[0] if s else None


def previous_session(day: date):
    s = _sessions(day - timedelta(days=14), day - timedelta(days=1))
    return s[-1] if s else None


# ------------------------------------------------------------- market data
def _to_et_index(hist):
    idx = hist.index
    hist.index = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(ET)
    return hist


_DAILY_CACHE: dict[str, dict[date, float]] = {}


def daily_closes(ticker: str) -> dict[date, float]:
    """{session date -> close} from daily bars (cached per run)."""
    if ticker in _DAILY_CACHE:
        return _DAILY_CACHE[ticker]
    out: dict[date, float] = {}
    try:
        start = (now_et().date() - timedelta(days=20)).isoformat()
        hist = yf.Ticker(ticker).history(start=start, interval="1d",
                                         auto_adjust=False,
                                         timeout=FETCH_TIMEOUT_S)
        if hist is not None and not hist.empty:
            for stamp, close in zip(hist.index, hist["Close"].tolist()):
                if _is_num(close):
                    out[stamp.date()] = float(close)
    except Exception as e:
        log(f"[warn] daily_closes {ticker} failed: {e}", err=True)
    if out:  # don't cache failures; next loop retries
        _DAILY_CACHE[ticker] = out
    return out


def minute_bars(ticker: str, day: date):
    """Regular-session 1-minute bars for `day` (ET index), or None."""
    try:
        hist = yf.Ticker(ticker).history(
            start=day.isoformat(), end=(day + timedelta(days=1)).isoformat(),
            interval="1m", auto_adjust=False, prepost=False,
            timeout=FETCH_TIMEOUT_S)
        if hist is None or hist.empty:
            return None
        hist = _to_et_index(hist)
        hist = hist[hist.index.date == day]
        hist = hist[[_is_num(c) for c in hist["Close"].tolist()]]
        return None if hist.empty else hist
    except Exception as e:
        log(f"[warn] minute_bars {ticker} {day} failed: {e}", err=True)
        return None


def close_from_intraday(ticker: str, day: date):
    for interval in ("30m", "1h", "1m"):
        try:
            hist = yf.Ticker(ticker).history(
                start=day.isoformat(),
                end=(day + timedelta(days=1)).isoformat(),
                interval=interval, auto_adjust=False, prepost=False,
                timeout=FETCH_TIMEOUT_S)
            if hist is None or hist.empty:
                continue
            hist = _to_et_index(hist)
            hist = hist[hist.index.date == day]
            hist = hist[[_is_num(c) for c in hist["Close"].tolist()]]
            if not hist.empty:
                close = float(hist["Close"].iloc[-1])
                log(f"[info] {ticker}: rebuilt {day} close from {interval} -> {close:.4f}")
                return close
        except Exception as e:
            log(f"[warn] close_from_intraday {ticker} {day} {interval}: {e}", err=True)
    return None


def fast_info(ticker: str):
    """(last_price, previous_close) from yfinance fast_info; Nones on failure."""
    try:
        fi = yf.Ticker(ticker).fast_info
        last = fi["last_price"]
        prev = fi["previous_close"]
        return (float(last) if _is_num(last) else None,
                float(prev) if _is_num(prev) else None)
    except Exception as e:
        log(f"[warn] fast_info {ticker} failed: {e}", err=True)
        return None, None


_CLOSE_CACHE: dict[tuple[str, date], float] = {}


def close_on(ticker: str, day: date, allow_fast_info: bool = False):
    """Close of `ticker` on the exact session `day`, or None. Never older."""
    key = (ticker, day)
    if key in _CLOSE_CACHE:
        return _CLOSE_CACHE[key], "cache"
    closes = daily_closes(ticker)
    val, src = closes.get(day), "daily"
    if val is None:
        log(f"[warn] {ticker}: daily bar for {day} missing; rebuilding from intraday", err=True)
        val, src = close_from_intraday(ticker, day), "intraday"
    if val is None and allow_fast_info:
        _, prev = fast_info(ticker)
        val, src = prev, "fast_info"
        if val is not None:
            log(f"[warn] {ticker}: using fast_info previous_close {val:.4f} for {day}", err=True)
    if val is None:
        log(f"[warn] {ticker}: no close for {day}; skipping (never using an older session)", err=True)
        return None, "unavailable"
    _CLOSE_CACHE[key] = val
    return val, src


# ---------------------------------------------------------------- the rule
def evaluate(bars, prev_close: float, session_close: datetime, now: datetime):
    """Apply the 5% rule to one session's 1-minute bars.

    Returns dict(fired, pct, price, kind, minutes) where kind is
    "close" / "sustained" / None.
    """
    res = {"fired": False, "pct": None, "price": None, "kind": None,
           "minutes": 0, "close_pct": None}
    if bars is None or not prev_close:
        return res
    highs = bars["High"].astype(float)
    lows = bars["Low"].astype(float)
    hi_p = (highs - prev_close) / prev_close * 100.0
    lo_p = (lows - prev_close) / prev_close * 100.0
    take_hi = hi_p.abs() >= lo_p.abs()
    peak_pct = hi_p.where(take_hi, lo_p)
    peak_px = highs.where(take_hi, lows)
    over = peak_pct[peak_pct.abs() >= STOCK_THRESHOLD_PCT]
    res["minutes"] = int(len(over))

    # Session is over once we're past the real (possibly early) close AND
    # the bars reach the closing minute (a lagging feed must not be read as
    # the close).
    if (now >= session_close + timedelta(minutes=1)
            and bars.index[-1] >= session_close - timedelta(minutes=2)):
        last_px = float(bars["Close"].astype(float).iloc[-1])
        cp = (last_px - prev_close) / prev_close * 100.0
        res["close_pct"] = cp
        if abs(cp) >= STOCK_THRESHOLD_PCT:
            res.update(fired=True, pct=cp, price=last_px, kind="close")
            return res
    if res["minutes"] >= SUSTAINED_MINUTES_REQUIRED and not over.empty:
        i = over.abs().idxmax()
        res.update(fired=True, pct=float(over.loc[i]),
                   price=float(peak_px.loc[i]), kind="sustained")
    return res


# ------------------------------------------------------------------- state
def _state_file(day: date) -> Path:
    return STATE_DIR / f"alerted-{day.isoformat()}.json"


def load_state(day: date) -> set[str]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = _state_file(day)
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text()))
    except Exception:
        return set()


def save_state(day: date, alerted: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(day).write_text(json.dumps(sorted(alerted)))


def prune_state(today: date) -> None:
    cutoff = today - timedelta(days=STATE_KEEP_DAYS)
    for f in STATE_DIR.glob("alerted-*.json"):
        try:
            if date.fromisoformat(f.stem.replace("alerted-", "")) < cutoff:
                f.unlink()
        except ValueError:
            pass


# ------------------------------------------------------------------- slack
def post_slack(webhook: str, text: str) -> bool:
    if webhook == "DRY_RUN":
        log("----- [dry-run slack] -----\n" + text + "\n---------------------------")
        return True
    for attempt in range(3):
        try:
            r = requests.post(webhook, json={"text": text}, timeout=15)
            if r.status_code < 300:
                return True
            log(f"[err] slack post failed {r.status_code}: {r.text}", err=True)
        except Exception as e:
            log(f"[err] slack post error: {e}", err=True)
        _time.sleep(5 * (attempt + 1))
    return False


def stock_line(ticker, res, prev, prev_date, tag=""):
    arrow = "🚀" if res["pct"] > 0 else "🔻"
    peak = " (intraday peak)" if res["kind"] == "sustained" else ""
    return (f"{arrow} *{ticker}* moved *{fmt_pct(res['pct'])}*{peak}{tag} "
            f"(ref ${res['price']:.2f} vs prev close {prev_date} ${prev:.2f})")


def index_line(direction, sp, nq, tag=""):
    if direction == "up":
        return (f"📈 *Broad rally*{tag}: S&P 500 {fmt_pct(sp)} & "
                f"Nasdaq Composite {fmt_pct(nq)} (both > +1.5%)")
    return (f"📉 *Broad selloff*{tag}: S&P 500 {fmt_pct(sp)} & "
            f"Nasdaq Composite {fmt_pct(nq)} (both < -1%)")


def send(webhook, title, pending: list[tuple[str, str]], alerted: set[str]) -> int:
    """Post [(state_key, line)] as one message; mark keys only on success."""
    if not pending:
        return 0
    text = f"*{title}* — {now_et().strftime('%Y-%m-%d %H:%M ET')}\n" + \
        "\n".join(line for _, line in pending)
    if post_slack(webhook, text):
        alerted.update(k for k, _ in pending)
        return len(pending)
    log("[err] Slack rejected the message; will retry next loop", err=True)
    return 0


# ---------------------------------------------------------- one evaluation
def check_session(tickers, day, sess_close, prev_day, alerted, now,
                  allow_fast_info):
    """Evaluate all tickers for session `day`. Returns (pending, n_bad)."""
    todo = [t for t in tickers if f"stock:{t}" not in alerted]

    def work(t):
        prev, src = close_on(t, prev_day, allow_fast_info=allow_fast_info)
        if prev is None:
            return t, None, None, src
        return t, prev, minute_bars(t, day), src

    results = []
    pool = ThreadPoolExecutor(max_workers=6)
    futs = [pool.submit(work, t) for t in todo]
    done, not_done = wait(futs, timeout=ITERATION_TIMEOUT_S)
    pool.shutdown(wait=False, cancel_futures=True)
    for f in done:
        try:
            results.append(f.result())
        except Exception as e:
            log(f"[warn] worker failed: {e}", err=True)
    n_bad = len(not_done) + len(futs) - len(done)

    pending = []
    for t, prev, bars, src in sorted(results):
        if prev is None or bars is None:
            n_bad += 1
            continue
        res = evaluate(bars, prev, sess_close, now)
        if res["fired"]:
            log(f"[info] {t}: ALERT {res['pct']:+.2f}% ({res['kind']}, prev {prev:.4f} on {prev_day} [{src}])")
            pending.append((t, res, prev))
        else:
            log(f"[info] {t}: ok (prev {prev:.2f} on {prev_day} [{src}], "
                f"minutes_over={res['minutes']}, close_pct={fmt_pct(res['close_pct'])})")
    return pending, n_bad


def index_moves(day, prev_day, final: bool):
    """(sp_pct, nq_pct). final=True uses daily closes for a past session."""
    out = []
    for sym in (SP500, NASDAQ_COMP):
        prev, _ = close_on(sym, prev_day, allow_fast_info=not final)
        if final:
            cur, _ = close_on(sym, day)
        else:
            cur, _ = fast_info(sym)
            if cur is None:
                bars = minute_bars(sym, day)
                cur = float(bars["Close"].iloc[-1]) if bars is not None else None
        out.append(pct_change(cur, prev))
    return tuple(out)


def index_pending(sp, nq, alerted, tag=""):
    pend = []
    if sp is None or nq is None:
        return pend
    if ("index:both-up" not in alerted and sp > INDEX_UP_THRESHOLD_PCT
            and nq > INDEX_UP_THRESHOLD_PCT):
        pend.append(("index:both-up", index_line("up", sp, nq, tag)))
    if ("index:both-down" not in alerted and sp < -INDEX_DOWN_THRESHOLD_PCT
            and nq < -INDEX_DOWN_THRESHOLD_PCT):
        pend.append(("index:both-down", index_line("down", sp, nq, tag)))
    return pend


# -------------------------------------------------------------- catch-up
def catch_up(webhook, tickers, today: date):
    """Re-check the previous session with its final bars; send what's missing."""
    prev = previous_session(today)
    if prev is None:
        return
    p_day, _, p_close = prev
    pp = previous_session(p_day)
    if pp is None:
        return
    pp_day = pp[0]
    alerted = load_state(p_day)
    log(f"[info] catch-up: re-checking {p_day} (already sent: {sorted(alerted) or 'none'})")
    after_close = p_close + timedelta(minutes=30)
    pending, _ = check_session(tickers, p_day, p_close, pp_day, alerted,
                               after_close, allow_fast_info=False)
    tag = f" ⏪补报{p_day.strftime('%m/%d')}"
    lines = [(f"stock:{t}", stock_line(t, res, pv, pp_day, tag))
             for t, res, pv in pending]
    sp, nq = index_moves(p_day, pp_day, final=True)
    lines += index_pending(sp, nq, alerted, tag)
    if lines:
        n = send(webhook, f"Stock mover alert (补报 {p_day})", lines, alerted)
        save_state(p_day, alerted)
        log(f"[info] catch-up posted {n} alert(s) for {p_day}")
    else:
        log(f"[info] catch-up: nothing missed for {p_day}")


# ------------------------------------------------------------------- main
def run_once(webhook, tickers, sess, prev_day, alerted, data_state):
    day, _, close_dt = sess
    now = now_et()
    pending, n_bad = check_session(tickers, day, close_dt, prev_day, alerted,
                                   now, allow_fast_info=True)
    lines = [(f"stock:{t}", stock_line(t, res, pv, prev_day))
             for t, res, pv in pending]
    sp, nq = index_moves(day, prev_day, final=False)
    log(f"[info] indices: S&P 500 {fmt_pct(sp)}, Nasdaq Composite {fmt_pct(nq)}")
    lines += index_pending(sp, nq, alerted)
    n = send(webhook, "Stock mover alert", lines, alerted)

    # Data-outage watchdog: one Slack warning per day.
    todo = len([t for t in tickers if f"stock:{t}" not in alerted]) or 1
    if n_bad / todo >= BAD_DATA_SHARE:
        data_state["bad"] += 1
    else:
        data_state["bad"] = 0
    if (data_state["bad"] >= BAD_DATA_LOOPS_BEFORE_WARN
            and "warn:data" not in alerted):
        msg = [("warn:data", f"⚠️ 行情数据异常：连续{data_state['bad']}轮有"
                f"{n_bad}只股票拿不到数据，异动提醒可能不完整，请留意。")]
        send(webhook, "Stock mover alert 系统警告", msg, alerted)
    save_state(day, alerted)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="single check instead of watching the session")
    ap.add_argument("--no-catch-up", action="store_true")
    args = ap.parse_args()

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        log("[err] SLACK_WEBHOOK_URL not set", err=True)
        return 2

    started = now_et()
    today = started.date()
    tickers = load_watchlist()
    log(f"[info] run at {started.isoformat()}; watching {len(tickers)} tickers: {' '.join(tickers)}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    prune_state(today)

    if not args.no_catch_up:
        try:
            catch_up(webhook, tickers, today)
        except Exception as e:
            log(f"[warn] catch-up failed: {e}", err=True)

    sess = session_for(today)
    if sess is None:
        log(f"[skip] {today} is not an NYSE trading day")
        return 0
    _, open_dt, close_dt = sess
    prev = previous_session(today)
    if prev is None:
        log("[err] cannot determine previous session", err=True)
        return 1
    prev_day = prev[0]
    log(f"[info] session {today} {open_dt.strftime('%H:%M')}-{close_dt.strftime('%H:%M')} ET, prev session {prev_day}")

    alerted = load_state(today)
    data_state = {"bad": 0}
    end_watch = close_dt + timedelta(minutes=POST_CLOSE_GRACE_MIN)

    if args.once:
        run_once(webhook, tickers, sess, prev_day, alerted, data_state)
        return 0

    now = now_et()
    if now < open_dt:
        wait_min = (open_dt - now).total_seconds() / 60
        if wait_min > MAX_PRE_OPEN_WAIT_MIN:
            log(f"[skip] {wait_min:.0f} min before the open; a later run will watch")
            return 0
        log(f"[info] waiting {wait_min:.0f} min for the open")
        _time.sleep((open_dt - now).total_seconds() + 60)

    deadline = started + timedelta(minutes=MAX_LOOP_MINUTES)
    loops = 0
    while True:
        loops += 1
        t0 = now_et()
        log(f"[info] loop {loops} at {t0.strftime('%H:%M:%S')} ET")
        try:
            run_once(webhook, tickers, sess, prev_day, alerted, data_state)
        except Exception as e:
            log(f"[warn] loop {loops} failed: {e}", err=True)
        now = now_et()
        if now >= end_watch:
            log("[info] session over; final check done")
            break
        if now >= deadline:
            log("[info] loop time budget used; the next scheduled run takes over")
            break
        nxt = t0 + timedelta(minutes=CHECK_INTERVAL_MIN)
        # Make sure one check lands just after the close.
        if t0 < close_dt + timedelta(minutes=2) < nxt:
            nxt = close_dt + timedelta(minutes=2)
        _time.sleep(max(5, (nxt - now_et()).total_seconds()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
