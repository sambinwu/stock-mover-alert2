"""规则自测：python tests/test_rules.py（不联网）。"""
import sys
from datetime import date, datetime
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


def quote(state, px, prev, rt, **kw):
    return {"marketState": state, "regularMarketPrice": px, "regularMarketPreviousClose": prev,
            "regularMarketTime": rt, **kw}


def feed(engine, sym, q, now, levels=w.LEVELS):
    fired = []
    for o in w.observations(q, now, CAL):
        lv = engine.observe(sym, o, levels)
        if lv:
            fired.append((o["kind"], lv, o))
    return fired


def mark(engine, sym, o, lv, levels=w.LEVELS):
    d = "up" if o["pct"] > 0 else "down"
    for x in levels:
        if x <= lv:
            engine.sent.setdefault(w.sent_key(o["ref_day"], sym, d, x), {"ts": o["t"], "pct": o["pct"]})


def at(h, m, s=0, day=D):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=ET)


def kinds(fired):
    return [(k, lv) for k, lv, _ in fired]


def test_regular_needs_two_distinct_quotes():
    e = w.Engine({})
    assert feed(e, "X", quote("REGULAR", 96.0, 100, ts(D, 10, 0)), at(10, 0, 5)) == []     # -4%
    assert feed(e, "X", quote("REGULAR", 94.8, 100, ts(D, 10, 1)), at(10, 1, 5)) == []     # 第一次过线
    assert feed(e, "X", quote("REGULAR", 94.8, 100, ts(D, 10, 1)), at(10, 2, 5)) == []     # 同一笔报价不算
    f = feed(e, "X", quote("REGULAR", 94.5, 100, ts(D, 10, 3)), at(10, 3, 5))
    assert kinds(f) == [("盘中", 5.0)], f
    assert abs(f[0][2]["pct"] - (-5.5)) < 1e-9      # 报的是现价涨跌幅


def test_spike_that_retraces_does_not_fire():
    e = w.Engine({})
    feed(e, "X", quote("REGULAR", 94.0, 100, ts(D, 10, 1)), at(10, 1, 5))
    assert feed(e, "X", quote("REGULAR", 97.0, 100, ts(D, 10, 2)), at(10, 2, 5)) == []
    assert feed(e, "X", quote("REGULAR", 94.0, 100, ts(D, 10, 3)), at(10, 3, 5)) == []     # 重新数


def test_gap_fires_once_at_top_level_then_escalates():
    e = w.Engine({})
    feed(e, "X", quote("REGULAR", 69.0, 100, ts(D, 9, 31)), at(9, 31, 5))
    f = feed(e, "X", quote("REGULAR", 68.5, 100, ts(D, 9, 32)), at(9, 32, 5))
    assert kinds(f) == [("盘中", 30.0)]
    mark(e, "X", f[0][2], 30.0)
    assert feed(e, "X", quote("REGULAR", 68.0, 100, ts(D, 9, 33)), at(9, 33, 5)) == []     # 不重复
    e2 = w.Engine({})
    feed(e2, "Y", quote("REGULAR", 94.0, 100, ts(D, 10, 1)), at(10, 1, 5))
    f = feed(e2, "Y", quote("REGULAR", 94.0, 100, ts(D, 10, 2)), at(10, 2, 5))
    mark(e2, "Y", f[0][2], 5.0)
    feed(e2, "Y", quote("REGULAR", 91.5, 100, ts(D, 11, 1)), at(11, 1, 5))
    f = feed(e2, "Y", quote("REGULAR", 91.0, 100, ts(D, 11, 2)), at(11, 2, 5))
    assert kinds(f) == [("盘中", 8.0)]
    assert e2.prior("Y", f[0][2], w.LEVELS)["pct"] == -6.0


def test_premarket_needs_three_and_shares_key_with_regular():
    e = w.Engine({})

    def pre(px, m):   # 昨收 100，盘前价 px
        return quote("PRE", 100.0, 98.0, ts(PREV, 16, 0), preMarketPrice=px, preMarketTime=ts(D, 8, m))

    assert kinds(feed(e, "X", pre(93.0, 1), at(8, 1, 30))) == []
    assert kinds(feed(e, "X", pre(93.2, 2), at(8, 2, 30))) == []
    f = [x for x in feed(e, "X", pre(93.1, 3), at(8, 3, 30)) if x[0] == "盘前"]
    assert kinds(f) == [("盘前", 5.0)], f
    assert f[0][2]["ref_day"] == PREV
    mark(e, "X", f[0][2], 5.0)
    # 开盘后还是 -6.9%：同一个昨收、同一方向、同一档 → 不再报
    feed(e, "X", quote("REGULAR", 93.1, 100, ts(D, 9, 31)), at(9, 31, 5))
    assert feed(e, "X", quote("REGULAR", 93.0, 100, ts(D, 9, 32)), at(9, 32, 5)) == []


def test_post_market_then_next_morning_no_repeat():
    e = w.Engine({})

    def post(px, m):
        return quote("POST", 100.0, 99.0, ts(D, 16, 0), postMarketPrice=px, postMarketTime=ts(D, 17, m))

    feed(e, "X", post(92.0, 1), at(17, 1, 30))
    feed(e, "X", post(92.5, 2), at(17, 2, 30))
    f = feed(e, "X", post(92.2, 3), at(17, 3, 30))
    assert kinds(f) == [("盘后", 5.0)], f
    assert f[0][2]["ref_day"] == D and f[0][2]["ref"] == 100.0
    mark(e, "X", f[0][2], 5.0)
    nxt = date(2026, 11, 18)
    for m in (1, 2, 3, 4):
        q = quote("PRE", 100.0, 99.0, ts(D, 16, 0), preMarketPrice=92.3, preMarketTime=ts(nxt, 8, m))
        assert feed(e, "X", q, at(8, m, 30, nxt)) == []


def test_stale_extended_print_does_not_confirm():
    e = w.Engine({})
    q = quote("PRE", 100.0, 99.5, ts(PREV, 16, 0), preMarketPrice=90.0, preMarketTime=ts(D, 7, 5))
    for m in (30, 31, 32, 33):     # 07:05 的一笔成交，之后没人交易
        assert feed(e, "X", q, at(7, m)) == []


def test_missed_close_is_caught_up_once():
    e = w.Engine({})
    q = quote("POST", 94.0, 100.0, ts(D, 16, 0))            # 今天收 -6%，盘中没报过
    f = feed(e, "X", q, at(16, 6))
    assert kinds(f) == [("收盘", 5.0)]
    mark(e, "X", f[0][2], 5.0)
    assert feed(e, "X", q, at(16, 7)) == []
    # 第二天早上才醒：照样能补，且标成补报
    e2 = w.Engine({})
    nxt = date(2026, 11, 18)
    f = feed(e2, "X", quote("PRE", 94.0, 100.0, ts(D, 16, 0)), at(7, 1, 0, nxt))
    assert f and f[0][2]["sess_day"] == D
    a = {"sym": "X", "obs": f[0][2], "level": 5.0, "levels": w.LEVELS, "quote": {}, "prior": None, "nq": None}
    assert "补报 11/17" in w.stock_block(a, {}, {}, at(7, 1, 0, nxt))


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


def test_message_renders():
    q = quote("REGULAR", 94.0, 100.0, ts(D, 10, 2), earningsTimestamp=ts(D, 16, 5))
    o = w.observations(q, at(10, 2, 5), CAL)[0]
    a = {"sym": "NVDA", "obs": o, "level": 5.0, "levels": w.LEVELS, "quote": q, "prior": None,
         "nq": -1.0, "news": [{"title": "A <b> & c", "src": "Reuters", "ts": ts(D, 9, 40), "url": "https://x.y/z"}]}
    text = w.build_message([], [a], [], {"NVDA": "英伟达"}, {"NVDA": "U123"}, at(10, 2, 5))
    assert "英伟达 NVDA　盘中 -6.0%" in text and "$94.00" in text and "昨收 $100.00" in text, text
    assert "比纳指多跌 5.0 个点（纳指 -1.0%）" in text and "<@U123>" in text, text
    assert "财报时间：11/17 16:05 ET" in text and "A &lt;b&gt; &amp; c" in text, text


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} tests passed")
