"""规则自测：python tests/test_rules.py（不联网）。"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watch as w  # noqa: E402

ET = w.ET
D = date(2026, 11, 17)          # 周二
PREV = date(2026, 11, 16)


class FakeCal:
    days = [date(2026, 11, d) for d in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]

    def is_session(self, d):
        return d in self.days

    def prev(self, d):
        return [x for x in self.days if x < d][-1]


CAL = FakeCal()


def ts(day, h, m, s=0):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET).timestamp()


def at(h, m, s=0, day=D):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)


def quote(state, px, prev, rt, **kw):
    return {"marketState": state, "regularMarketPrice": px, "regularMarketPreviousClose": prev,
            "regularMarketTime": rt, **kw}


def feed(engine, sym, q, now):
    """和正式流程一样：每个观察值交给 judge_stock。"""
    fired = []
    for o in w.observations(q, now, CAL):
        lv = w.judge_stock(engine, sym, o, w.LEVELS)
        if lv:
            fired.append((o["kind"], lv, o))
    return fired


def mark(engine, sym, o, lv):
    d = "up" if o["pct"] > 0 else "down"
    for x in w.LEVELS:
        if x <= lv:
            engine.sent.setdefault(w.sent_key(o["ref_day"], sym, d, x), {"ts": o["t"], "pct": o["pct"]})


def kinds(fired):
    return [(k, lv) for k, lv, _ in fired]


def run_minutes(engine, sym, px, start, minutes, prev=100.0):
    """从 start 起每分钟喂一次盘中报价（价格 px），共 minutes 次；返回第一次触发的 (第几分钟, fired)。"""
    t = start
    for i in range(minutes):
        f = feed(engine, sym, quote("REGULAR", px, prev, t.timestamp() - 3), t)
        if f:
            return i, f
        t += timedelta(minutes=1)
    return None, []


# ------------------------------------------------------------------ 盘中
def test_intraday_needs_5_5_pct_for_30_minutes():
    e = w.Engine({})
    i, f = run_minutes(e, "X", 94.0, at(10, 0), 45)            # -6%，一直在线外
    assert kinds(f) == [("盘中", 5.0)], f
    assert i == 30, i                                          # 第 30 分钟整才发（10:30）
    assert abs(f[0][2]["pct"] + 6.0) < 1e-9                    # 报的是现价涨跌幅
    assert e.minutes_out("X", f[0][2], 5.0) == 30


def test_intraday_5_3_pct_never_fires():
    e = w.Engine({})
    i, f = run_minutes(e, "X", 94.7, at(10, 0), 120)           # -5.3%：过了 5% 但没到 5.5%
    assert f == []


def test_intraday_time_is_cumulative_and_must_be_out_now():
    e = w.Engine({})
    i, f = run_minutes(e, "X", 94.0, at(10, 0), 21)            # 线外 20 分钟
    assert f == []
    i, f = run_minutes(e, "X", 97.0, at(10, 21), 30)           # 回到线内半小时：不发，也不清零
    assert f == []
    i, f = run_minutes(e, "X", 94.0, at(10, 51), 30)           # 再出线：接着之前的 20 分钟算
    assert kinds(f) == [("盘中", 5.0)] and i == 10, (i, f)      # 再过 10 分钟就够 30


def test_intraday_gap_in_quotes_is_not_counted():
    e = w.Engine({})
    run_minutes(e, "X", 94.0, at(10, 0), 11)                   # 线外 10 分钟
    # 监控断了 40 分钟再回来：中间这段不能算进累计
    i, f = run_minutes(e, "X", 94.0, at(10, 50), 19)
    assert f == [], f
    i, f = run_minutes(e, "X", 94.0, at(11, 9), 5)
    assert kinds(f) == [("盘中", 5.0)], f


def test_intraday_once_per_day_no_escalation():
    # Sam 2026-11-18：CRWD 11:24 报了 +6.2%，13:48 又因为过 8% 追了一条——不要第二条
    e = w.Engine({})
    i, f = run_minutes(e, "X", 106.0, at(10, 0), 31)
    mark(e, "X", f[0][2], 5.0)
    i, f = run_minutes(e, "X", 109.0, at(11, 0), 120)          # 扩大到 +9%，待满两小时
    assert f == [], f
    i, f = run_minutes(e, "X", 93.0, at(13, 0), 120)           # 反转成 -7%：今天也不再报
    assert f == [], f


def test_no_alert_after_hours_even_after_a_new_drop():
    # Sam 2026-11-19：只做盘中。盘中报过，盘后再跌 6%（比如收盘后出财报）也不单独报
    e = w.Engine({})
    i, f = run_minutes(e, "X", 94.0, at(10, 0), 31)
    mark(e, "X", f[0][2], 5.0)

    def post(px, m):
        return quote("POST", 94.0, 100.0, ts(D, 16, 0), postMarketPrice=px, postMarketTime=ts(D, 17, m))

    for m in range(1, 8):
        assert feed(e, "X", post(88.0 + m / 100, m), at(17, m, 30)) == []


def test_accum_survives_a_new_run():
    e = w.Engine({})
    run_minutes(e, "X", 94.0, at(10, 0), 26)                   # 线外 25 分钟，然后换了一轮
    saved = {k: round(v) for k, v in e.accum.items() if v >= 60}
    e2 = w.Engine({}, accum=dict(saved))
    i, f = run_minutes(e2, "X", 94.0, at(10, 27), 20)
    assert kinds(f) == [("盘中", 5.0)] and i <= 6, (i, f)       # 接着算，不用再等 30 分钟


def test_stale_quote_neither_counts_nor_fires():
    e = w.Engine({})
    old = ts(D, 10, 0)
    for m in range(1, 48):          # 报价时间一直停在 10:00（行情卡住）
        assert feed(e, "X", quote("REGULAR", 94.0, 100.0, old), at(10, 11 + m)) == []


# ------------------------------------------------------------ 盘前、盘后
def test_premarket_never_alerts_and_intraday_still_does():
    # Sam 2026-11-19：「盘前也不用提醒，我们只做盘中的提醒」
    e = w.Engine({})

    def pre(px, m):   # 昨收 100，盘前一直 -7%
        return quote("PRE", 100.0, 98.0, ts(PREV, 16, 0), preMarketPrice=px, preMarketTime=ts(D, 8, m))

    for m in range(1, 30):
        assert feed(e, "X", pre(93.0 + m / 100, m), at(8, m, 30)) == []
    # 开盘后还在 -7%：盘中照常按 5.5%、30 分钟报
    i, f = run_minutes(e, "X", 93.0, at(9, 31), 45)
    assert kinds(f) == [("盘中", 5.0)] and i == 30, (i, f)


def test_post_market_never_alerts():
    e = w.Engine({})

    def post(px, m):
        return quote("POST", 100.0, 99.0, ts(D, 16, 0), postMarketPrice=px, postMarketTime=ts(D, 17, m))

    for m in range(1, 10):
        assert feed(e, "X", post(92.0 + m / 100, m), at(17, m, 30)) == []


def test_stale_extended_print_does_not_confirm():
    e = w.Engine({})
    q = quote("PRE", 100.0, 99.5, ts(PREV, 16, 0), preMarketPrice=90.0, preMarketTime=ts(D, 7, 5))
    for m in (30, 31, 32, 33):     # 07:05 的一笔成交，之后没人交易
        assert feed(e, "X", q, at(7, m)) == []


def test_close_value_is_not_an_individual_alert():
    e = w.Engine({})
    q = quote("POST", 94.0, 100.0, ts(D, 16, 0))               # 收 -6%：归 16:05 那一条，不单独报
    assert feed(e, "X", q, at(16, 6)) == []


# ------------------------------------------------------------------ 大盘
def test_index_both_must_cross():
    e = w.Engine({})

    def idx(sp, nq, m):
        a = quote("REGULAR", 100 + sp, 100, ts(D, 11, m))
        b = quote("REGULAR", 100 + nq, 100, ts(D, 11, m))
        out = []
        for o in w.index_observations(a, b, at(11, m, 5), CAL):
            levels = w.INDEX_UP_LEVELS if o["pct"] > 0 else w.INDEX_DOWN_LEVELS
            lv = e.observe("INDEX", o, levels)
            if lv:
                out.append((lv, o))
        return out

    assert idx(-1.5, -0.8, 1) == [] and idx(-1.5, -0.8, 2) == []      # 只有一个过线
    assert idx(-1.2, 0.3, 3) == []
    assert idx(-1.2, -1.1, 4) == []
    f = idx(-1.3, -1.15, 5)
    assert f and f[0][0] == 1.0 and abs(f[0][1]["sp"] + 1.3) < 1e-9 and abs(f[0][1]["nq"] + 1.15) < 1e-9
    assert idx(1.2, 1.4, 6) == [] and idx(1.2, 1.4, 7) == []          # 上涨线是 1.5


# ------------------------------------------------------------ 16:05 那一条
def closed(px, prev=100.0):
    return quote("POST", px, prev, ts(D, 16, 0))


def test_close_report_lists_everything_beyond_5_pct():
    quotes = {"AAA": closed(94.9), "BBB": closed(105.2), "CCC": closed(95.1), "DDD": closed(97.0),
              w.SP500: closed(98.9), w.NASDAQ: closed(98.7)}
    names = {"AAA": "甲", "BBB": "乙", "CCC": "丙", "DDD": "丁"}
    alerted = {"DDD": {"first_pct": -6.1, "first_ts": ts(D, 11, 0), "kind": "盘中"},
               "AAA": {"first_pct": -5.8, "first_ts": ts(D, 12, 0), "kind": "盘中"}}
    text = w.build_close_report(D, D, quotes, list(names), names, alerted, [])
    assert "收盘异动 · 11/17（周二）" in text and "标普500 -1.1%｜纳指 -1.3%" in text, text
    assert "收盘涨跌超过 5% 的 2 只" in text, text
    assert "乙 BBB*　+5.2%　$105.20" in text and "甲 AAA*　-5.1%　$94.90" in text, text
    assert text.index("BBB") < text.index("AAA")                      # 幅度大的在前
    assert "CCC" not in text                                          # -4.9%：没过线、也没提醒过
    assert "今天提醒过、收盘回到 5% 以内的：丁 DDD 收 -3.0%" in text, text
    assert "http" not in text and "<@" not in text                    # 不带新闻、不 @ 人


def test_close_report_stays_silent_when_nothing_to_show():
    quotes = {"AAA": closed(99.0), w.SP500: closed(100.2), w.NASDAQ: closed(100.1)}
    assert w.build_close_report(D, D, quotes, ["AAA"], {"AAA": "甲"}, {}, []) is None


def test_close_report_stays_silent_when_only_an_earlier_alert_came_back():
    # 2026-11-17 实际发生的：HOOD 盘中提醒过、收 -4.0%，其余都在 5% 以内 → 不发
    quotes = {"HOOD": closed(96.0), "AAA": closed(98.0), w.SP500: closed(99.0), w.NASDAQ: closed(99.3)}
    alerted = {"HOOD": {"first_pct": -5.44, "first_ts": ts(D, 13, 49), "kind": "盘中"}}
    assert w.build_close_report(D, D, quotes, ["HOOD", "AAA"], {}, alerted, []) is None


def test_close_report_still_goes_out_for_slow_movers_alone():
    quotes = {"AAA": closed(98.0)}
    text = w.build_close_report(D, D, quotes, ["AAA"], {"AAA": "甲"}, {}, ["• 甲 AAA +11.2%（11/11→11/17）"])
    assert text and "缓涨缓跌" in text and "收盘涨跌超过" not in text, text


def test_close_report_marks_catch_up():
    quotes = {"AAA": closed(93.0)}
    text = w.build_close_report(D, date(2026, 11, 18), quotes, ["AAA"], {"AAA": "甲"}, {}, [])
    assert "补报 收盘异动 · 11/17" in text, text


# ------------------------------------------------------------------ 文字
def test_message_renders():
    q = quote("REGULAR", 94.0, 100.0, ts(D, 10, 2), earningsTimestamp=ts(D, 16, 5))
    o = w.observations(q, at(10, 2, 5), CAL)[0]
    a = {"sym": "NVDA", "obs": o, "level": 5.0, "levels": w.LEVELS, "quote": q, "prior": None,
         "nq": -1.0, "thr": 5.5, "minutes": 31,
         "news": [{"title": "A <b> & c", "src": "Reuters", "ts": ts(D, 9, 40), "url": "https://x.y/z"}]}
    text = w.build_message([], [a], [], {"NVDA": "英伟达"}, {"NVDA": "U123"}, at(10, 2, 5))
    # Sam 2026-11-20：只要涨跌幅和 @，不要新闻、现价、累计时长、比纳指、财报时间
    assert text.endswith("🔻 *英伟达 NVDA　盘中 -6.0%*　<@U123>"), text
    for gone in ("$94.00", "线外累计", "纳指", "财报", "Reuters", "x.y/z"):
        assert gone not in text, (gone, text)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
