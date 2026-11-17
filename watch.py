#!/usr/bin/env python3
"""美投异动提醒 v3 —— 跟踪股票一有大动静，几分钟内发到 Slack。

规则（每条都对着「相对上一个收盘价的涨跌幅」）
------------------------------------------------
* 个股：涨跌超过 5%，并且连续 2 次报价都在线外（盘前盘后成交稀，要 3 次）就发。
  之后扩大到 8% / 12% / 20% / 30% 各追发一条「升级」。
* 报的永远是现价和现在的涨跌幅，不是盘中极值。
* 时段：盘前 07:00 起、盘中、盘后到 20:00（美东）。
  盘前、盘中对的是昨收；盘后对的是今收；第二天盘前接着对同一个收盘价，
  所以「盘后财报跌 7%」只报一次，第二天早上不会重复。
* 大盘：标普 500 和纳指同涨超 1.5% 或同跌超 1%（沿用旧版），升级档见下面常量。
  大盘日里，比纳指多动不到 3 个点的个股合成一行，不单独点名。
* 收盘后发一条小结：今天提醒过的股票收在哪里；外加「缓涨缓跌」
  （近 5 个交易日累计超 10%、单日都没超 5%，沿用旧 stock-alert 的规则）。
* 漏发补报：收盘价是定值，任何时候醒来发现「收盘超线但没报过」都会补一条。

为什么这么跑
------------
旧版慢，是因为 GitHub 的定时器常常到下午才启动。现在改成接力：
每一轮跑 5 个半小时，结束时自己启动下一轮（workflow 里的 hand-off 步骤），
定时器只当保险。非监控时段这一轮只是睡着等开盘。

数据：Yahoo 报价接口，一次请求拿全部股票（含盘前盘后价、上一收盘价、财报时间）。
状态：存在仓库 `state` 分支的 state.json 里（发之前先登记，登记成功才发，避免重复）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------ settings
LEVELS = [5.0, 8.0, 12.0, 20.0, 30.0]      # 个股档位（%）
INDEX_UP_LEVELS = [1.5, 3.0, 5.0]          # 标普、纳指同涨
INDEX_DOWN_LEVELS = [1.0, 2.0, 3.0, 5.0]   # 标普、纳指同跌
CONFIRM_REGULAR = 2                        # 盘中：连续几次报价确认
CONFIRM_EXTENDED = 3                       # 盘前盘后
EXCESS_PTS = 3.0                           # 比纳指多动不到这么多点 = 跟着大盘
LOOP_SECONDS = 60
WINDOW_START = dtime(7, 0)                 # 美东
WINDOW_END = dtime(20, 5)
STALE_REGULAR_S = 600                      # 盘中报价超过这么久没更新 = 不算数
STALE_EXTENDED_S = 900
SUMMARY_DELAY_MIN = 5                      # 收盘后几分钟发小结
SLOW_WINDOW_DAYS = 5
SLOW_CUM_PCT = 10.0
SLOW_DAILY_CAP = 5.0
SLOW_REPEAT_DAYS = 7                       # 同一只同方向几天内不重复提
RUN_BUDGET_MIN = 330                       # 一轮跑多久后交棒（GitHub 单轮上限 6 小时）
BAD_LOOPS_BEFORE_WARN = 5
STATE_KEEP_DAYS = 12
NEWS_MAX_AGE_H = 24
NEWS_COUNT = 3

SP500, NASDAQ = "^GSPC", "^IXIC"
ET = ZoneInfo("America/New_York")
HERE = Path(__file__).parent
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
WEEKDAYS = "一二三四五六日"


def now_et() -> datetime:
    return datetime.now(ET)


def log(msg: str) -> None:
    print(f"[{now_et().strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None


def fmt_pct(p: float, digits: int = 1) -> str:
    return f"{p:+.{digits}f}%"


def fmt_px(p: float) -> str:
    return f"${p:,.2f}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------- watchlist
def load_watchlist(path: Path | None = None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """一行一只：「NVDA 英伟达 | Nvidia」（代码 中文名 | 找新闻用的英文关键词，逗号分隔）。
    # 后面是注释。返回 ({代码: 中文名}, {代码: [关键词]})。"""
    names: dict[str, str] = {}
    keywords: dict[str, list[str]] = {}
    for line in (path or HERE / "watchlist.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        left, _, kw = line.partition("|")
        parts = left.split(None, 1)
        sym = parts[0].upper()
        names[sym] = parts[1].strip() if len(parts) > 1 else ""
        keywords[sym] = [k.strip() for k in kw.split(",") if k.strip()]
    if not names:
        raise SystemExit("watchlist.txt 是空的")
    return names, keywords


def load_owners() -> dict[str, str]:
    """环境变量 OWNERS：`NVDA:U08XXXX,META:U08XXXX`（Slack 用户 ID）。没配就不 @ 人。"""
    out = {}
    for item in os.environ.get("OWNERS", "").replace("\n", ",").split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            if k.strip() and v.strip():
                out[k.strip().upper()] = v.strip()
    return out


# ------------------------------------------------------------- NYSE calendar
class Cal:
    """真实的 NYSE 交易日（节假日排除）；日历库不可用时退回周一到周五。"""

    def __init__(self, center: date | None = None):
        center = center or now_et().date()
        start, end = center - timedelta(days=40), center + timedelta(days=15)
        self.days: list[date] = []
        try:
            import pandas_market_calendars as mcal
            sched = mcal.get_calendar("NYSE").schedule(
                start_date=start.isoformat(), end_date=end.isoformat())
            self.days = [ts.date() for ts in sched.index]
        except Exception as e:  # pragma: no cover - defensive
            log(f"[warn] NYSE 日历不可用（{e}），退回工作日判断")
        if not self.days:
            d = start
            while d <= end:
                if d.weekday() < 5:
                    self.days.append(d)
                d += timedelta(days=1)
        self._set = set(self.days)

    def is_session(self, d: date) -> bool:
        return d in self._set

    def prev(self, d: date) -> date:
        """d 之前最近的一个交易日。"""
        earlier = [x for x in self.days if x < d]
        return earlier[-1] if earlier else d - timedelta(days=1)

    def next_on_or_after(self, d: date) -> date:
        later = [x for x in self.days if x >= d]
        return later[0] if later else d

    def last_n(self, d: date, n: int) -> list[date]:
        return [x for x in self.days if x <= d][-n:]


def in_window(now: datetime, cal: Cal) -> bool:
    return cal.is_session(now.date()) and WINDOW_START <= now.time() < WINDOW_END


def next_window_start(now: datetime, cal: Cal) -> datetime:
    d = now.date()
    if not (cal.is_session(d) and now.time() < WINDOW_START):
        d = cal.next_on_or_after(d + timedelta(days=1))
    return datetime.combine(d, WINDOW_START, ET)


# --------------------------------------------------------------- market data
def _yf_get(url: str, params: dict):
    from yfinance.data import YfData
    r = YfData().get(url, params=params, timeout=20)
    if getattr(r, "status_code", 200) >= 400:
        raise RuntimeError(f"Yahoo {r.status_code}")
    return r.json()


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """一次请求拿全部股票的报价。"""
    j = _yf_get(QUOTE_URL, {"symbols": ",".join(symbols)})
    return {q["symbol"]: q for q in j["quoteResponse"]["result"]}


def fetch_news(sym: str, keywords: list[str], count: int = NEWS_COUNT) -> list[dict]:
    """最近 24 小时内、标题里直接提到这家公司的新闻，最多 count 条。
    先 Yahoo，不够再 Google News。标题没提到公司名的一律不要（宁缺毋滥）。"""
    now = time.time()
    words = [k.lower() for k in keywords] + [sym.lower()]

    def relevant(title: str) -> bool:
        t = title.lower()
        return any(w in t for w in words)

    out: list[dict] = []
    seen: set[str] = set()

    def add(title: str, src: str, ts: float, url: str) -> None:
        key = title.lower()[:40]
        if title and relevant(title) and now - ts <= NEWS_MAX_AGE_H * 3600 and key not in seen:
            seen.add(key)
            out.append({"title": title, "src": src, "ts": ts, "url": url})

    try:
        j = _yf_get(SEARCH_URL, {"q": sym, "newsCount": 12, "quotesCount": 0})
        for n in j.get("news", []):
            add(n.get("title") or "", n.get("publisher") or "",
                n.get("providerPublishTime") or 0, n.get("link") or "")
    except Exception as e:
        log(f"[warn] {sym} Yahoo 新闻拿不到：{e}")
    if len(out) < count:
        try:
            from email.utils import parsedate_to_datetime
            q = (keywords[0] if keywords else sym) + " stock when:1d"
            r = requests.get("https://news.google.com/rss/search", timeout=15,
                             params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                             headers={"User-Agent": "Mozilla/5.0"})
            for it in ElementTree.fromstring(r.content).findall("./channel/item"):
                title = it.findtext("title") or ""
                src = ""
                if " - " in title:
                    title, src = title.rsplit(" - ", 1)
                add(title, src, parsedate_to_datetime(it.findtext("pubDate")).timestamp(),
                    it.findtext("link") or "")
        except Exception as e:
            log(f"[warn] {sym} Google 新闻拿不到：{e}")
    out.sort(key=lambda n: -n["ts"])
    return out[:count]


# ------------------------------------------------------- quote -> observation
def observations(q: dict, now: datetime, cal: Cal) -> list[dict]:
    """把一条 Yahoo 报价拆成 0～2 个观察值：

    * 实时的（盘前 / 盘中 / 盘后）：需要连续确认；
    * 已收盘的常规时段（收盘）：定值，不用确认，用来兜底和补报。

    每个观察值都带 `ref_day`：它对的那个收盘价属于哪个交易日。去重就按
    (ref_day, 股票, 方向, 档位)，所以盘后和第二天盘前不会重复报同一件事。
    """
    out: list[dict] = []
    state = q.get("marketState") or ""
    px, prev = num(q.get("regularMarketPrice")), num(q.get("regularMarketPreviousClose"))
    rt = q.get("regularMarketTime")
    if not (px and prev and rt):
        return out
    now_ts = now.timestamp()
    today = now.date()
    sess_day = datetime.fromtimestamp(rt, ET).date()   # 这个常规时段价格属于哪天
    reg_pct = (px - prev) / prev * 100.0
    base = {"sess_day": sess_day, "day_low": num(q.get("regularMarketDayLow")),
            "day_high": num(q.get("regularMarketDayHigh"))}

    if state == "REGULAR":
        if sess_day == today:   # 开盘头几秒报价还停在昨天，跳过
            out.append({**base, "kind": "盘中", "live": True, "pct": reg_pct, "px": px,
                        "ref": prev, "ref_day": cal.prev(sess_day), "t": rt,
                        "fresh": now_ts - rt <= STALE_REGULAR_S, "need": CONFIRM_REGULAR})
        return out

    # 常规时段已经结束：收盘值是定值
    out.append({**base, "kind": "收盘", "live": False, "pct": reg_pct, "px": px,
                "ref": prev, "ref_day": cal.prev(sess_day), "t": rt,
                "fresh": True, "need": 1})

    if state == "PRE":
        ext_px, ext_t, kind = num(q.get("preMarketPrice")), q.get("preMarketTime"), "盘前"
    elif state in ("POST", "POSTPOST"):
        ext_px, ext_t, kind = num(q.get("postMarketPrice")), q.get("postMarketTime"), "盘后"
    else:
        ext_px, ext_t, kind = None, None, ""
    if ext_px and ext_t and datetime.fromtimestamp(ext_t, ET).date() == today:
        out.append({**base, "kind": kind, "live": True,
                    "pct": (ext_px - px) / px * 100.0, "px": ext_px, "ref": px,
                    "ref_day": sess_day, "t": ext_t,
                    "fresh": now_ts - ext_t <= STALE_EXTENDED_S, "need": CONFIRM_EXTENDED})
    return out


def index_observations(q_sp: dict | None, q_nq: dict | None, now: datetime, cal: Cal) -> list[dict]:
    """两个指数合成一个观察值：同向时取动得少的那个（它决定有没有「都过线」），不同向记 0。"""
    out = []
    if not (q_sp and q_nq):
        return out
    sp = {o["kind"]: o for o in observations(q_sp, now, cal)}
    nq = {o["kind"]: o for o in observations(q_nq, now, cal)}
    for kind in ("盘中", "收盘"):
        a, b = sp.get(kind), nq.get(kind)
        if not (a and b) or a["ref_day"] != b["ref_day"]:
            continue
        if a["pct"] > 0 and b["pct"] > 0:
            pct = min(a["pct"], b["pct"])
        elif a["pct"] < 0 and b["pct"] < 0:
            pct = max(a["pct"], b["pct"])
        else:
            pct = 0.0
        out.append({**a, "pct": pct, "sp": a["pct"], "nq": b["pct"],
                    "t": max(a["t"], b["t"]), "fresh": a["fresh"] and b["fresh"]})
    return out


# ------------------------------------------------------------------ the rule
def sent_key(ref_day: date, sym: str, direction: str, level: float) -> str:
    return f"{ref_day.isoformat()}|{sym}|{direction}|{level:g}"


class Engine:
    """连续确认 + 档位去重。`sent` 是已经发过的 key（来自 state）。"""

    def __init__(self, sent: dict, confirm_override: int | None = None):
        self.sent = sent
        self.streak: dict[tuple, int] = {}
        self.last_t: dict[tuple, float] = {}
        self.confirm_override = confirm_override

    def observe(self, sym: str, o: dict, levels: list[float]) -> float | None:
        """喂一个观察值；返回这次应该发的档位（没有就 None）。"""
        direction = "up" if o["pct"] > 0 else "down"
        other = "down" if direction == "up" else "up"
        need = self.confirm_override or o["need"]
        mag = abs(o["pct"])
        fired = None
        for lv in levels:
            k = (sym, o["ref_day"], direction, lv)
            if o["live"] and (o["fresh"] or mag < lv):
                self.streak[(sym, o["ref_day"], other, lv)] = 0
            if mag >= lv:
                if not o["live"]:
                    self.streak[k] = need
                elif o["fresh"] and o["t"] != self.last_t.get(k):
                    self.streak[k] = self.streak.get(k, 0) + 1
                    self.last_t[k] = o["t"]
            elif o["fresh"] or not o["live"]:
                self.streak[k] = 0
            if (self.streak.get(k, 0) >= need
                    and sent_key(o["ref_day"], sym, direction, lv) not in self.sent):
                fired = lv
        return fired

    def prior(self, sym: str, o: dict, levels: list[float]) -> dict | None:
        """同一件事（同参考收盘价、同方向）之前发过的最早那条。"""
        direction = "up" if o["pct"] > 0 else "down"
        hits = [self.sent[k] for lv in levels
                if (k := sent_key(o["ref_day"], sym, direction, lv)) in self.sent]
        hits = [h for h in hits if isinstance(h, dict) and h.get("ts")]
        return min(hits, key=lambda h: h["ts"]) if hits else None


# --------------------------------------------------------------------- state
class LocalStore:
    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict = {}

    def load(self) -> dict:
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}
        return self.data

    def commit(self, note: str = "") -> bool:
        if self.path:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        return True


class GitHubStore:
    """state.json 放在仓库的 `state` 分支；写的时候带上一次读到的版本号，
    版本对不上（别的进程先写了）就重读、这一轮不发，下一轮再判。"""

    def __init__(self, repo: str, token: str, branch: str = "state", path: str = "state.json"):
        self.repo, self.branch, self.path = repo, branch, path
        self.api = f"https://api.github.com/repos/{repo}"
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"}
        self.data: dict = {}
        self.sha: str | None = None

    def _ensure_branch(self) -> None:
        r = requests.get(f"{self.api}/git/ref/heads/{self.branch}", headers=self.h, timeout=20)
        if r.status_code == 200:
            return
        default = requests.get(self.api, headers=self.h, timeout=20).json()["default_branch"]
        sha = requests.get(f"{self.api}/git/ref/heads/{default}", headers=self.h,
                           timeout=20).json()["object"]["sha"]
        r = requests.post(f"{self.api}/git/refs", headers=self.h, timeout=20,
                          json={"ref": f"refs/heads/{self.branch}", "sha": sha})
        log(f"[info] 新建 {self.branch} 分支：{r.status_code}")

    def load(self) -> dict:
        r = requests.get(f"{self.api}/contents/{self.path}", headers=self.h, timeout=20,
                         params={"ref": self.branch})
        if r.status_code == 200:
            j = r.json()
            self.sha = j["sha"]
            try:
                self.data = json.loads(base64.b64decode(j["content"]).decode("utf-8"))
            except Exception:
                self.data = {}
        elif r.status_code == 404:
            self._ensure_branch()
            self.data, self.sha = {}, None
        else:
            raise RuntimeError(f"读 state 失败：{r.status_code} {r.text[:200]}")
        return self.data

    def commit(self, note: str = "") -> bool:
        body = {"message": f"state: {note}"[:120], "branch": self.branch,
                "content": base64.b64encode(
                    json.dumps(self.data, ensure_ascii=False, indent=1).encode("utf-8")).decode()}
        if self.sha:
            body["sha"] = self.sha
        r = requests.put(f"{self.api}/contents/{self.path}", headers=self.h, json=body, timeout=30)
        if r.status_code in (200, 201):
            self.sha = r.json()["content"]["sha"]
            return True
        log(f"[warn] 写 state 没成功（{r.status_code}），重读后下一轮再判")
        try:
            self.load()
        except Exception as e:
            log(f"[warn] 重读 state 失败：{e}")
        return False


def prune_state(data: dict, today: date) -> None:
    cutoff = (today - timedelta(days=STATE_KEEP_DAYS)).isoformat()
    sent = data.setdefault("sent", {})
    for k in [k for k in sent if k.split("|", 1)[0] < cutoff and k[:4].isdigit()]:
        del sent[k]
    days = data.setdefault("days", {})
    for d in [d for d in days if d < cutoff]:
        del days[d]


# --------------------------------------------------------------------- slack
def post_slack(webhook: str, text: str) -> bool:
    if webhook == "DRY":
        # 仓库是公开的，日志谁都能看：试跑时不把 Slack 用户 ID 打出来
        shown = re.sub(r"<@[A-Z0-9]+>", "<@负责人>", text)
        print("\n----- [不发 Slack，只打印] -----\n" + shown + "\n--------------------------------\n",
              flush=True)
        return True
    for attempt in range(3):
        try:
            r = requests.post(webhook, json={"text": text}, timeout=15)
            if r.status_code < 300:
                return True
            log(f"[err] Slack 拒收 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"[err] Slack 发送出错：{e}")
        time.sleep(4 * (attempt + 1))
    return False


def label(sym: str, names: dict) -> str:
    name = names.get(sym) or ""
    return sym if not name or name.upper() == sym else f"{name} {sym}"


def ago(ts: float) -> str:
    m = max(0, int((time.time() - ts) / 60))
    return f"{m} 分钟前" if m < 60 else f"{m // 60} 小时前"


def earnings_tag(q: dict, now: datetime) -> str:
    ts = q.get("earningsTimestamp") or q.get("earningsTimestampStart")
    if not ts:
        return ""
    delta_h = (ts - now.timestamp()) / 3600
    when = datetime.fromtimestamp(ts, ET)
    est = "（预计）" if q.get("isEarningsDateEstimate") else ""
    if -20 <= delta_h <= 0:
        return f"📅 财报已在 {when.strftime('%m/%d %H:%M')} ET 公布{est}"
    if 0 < delta_h <= 20:
        return f"📅 财报时间：{when.strftime('%m/%d %H:%M')} ET{est}"
    return ""


REF_LABEL = {"盘前": "昨收", "盘中": "昨收", "盘后": "今收", "收盘": "前收"}


def stock_block(a: dict, names: dict, owners: dict, now: datetime, with_news: bool = True) -> str:
    o, sym = a["obs"], a["sym"]
    up = o["pct"] > 0
    icon = "🚀" if up else "🔻"
    word = "涨" if up else "跌"
    title = label(sym, names)
    kind = o["kind"]
    late = ""
    if kind == "收盘" and o["sess_day"] != now.date():
        late = f"⏪ 补报 {o['sess_day'].strftime('%m/%d')}："
    if a["prior"]:
        first_at = datetime.fromtimestamp(a["prior"]["ts"], ET).strftime("%H:%M")
        head = (f"{icon} {late}*{title} {word}幅扩大到 {fmt_pct(o['pct'])}*　{kind} {fmt_px(o['px'])}"
                f"（{first_at} 首次提醒时 {fmt_pct(a['prior']['pct'])}）")
    else:
        head = (f"{icon} {late}*{title}　{kind} {fmt_pct(o['pct'])}*　{fmt_px(o['px'])}"
                f"（{REF_LABEL[kind]} {fmt_px(o['ref'])}）")
    lines = [head]
    meta = []
    if a.get("nq") is not None:
        diff = o["pct"] - a["nq"]
        if abs(diff) >= 0.05:
            rel = (f"比纳指多{word} {abs(diff):.1f} 个点" if (diff > 0) == up
                   else f"比纳指少{word} {abs(diff):.1f} 个点")
            meta.append(f"{rel}（纳指 {fmt_pct(a['nq'])}）")
    if owners.get(sym):
        meta.append(f"<@{owners[sym]}>")
    if meta:
        lines.append("　" + "｜".join(meta))
    tag = earnings_tag(a["quote"], now)
    if tag:
        lines.append("　" + tag)
    if with_news and a.get("news") == []:
        lines.append("　（24 小时内没找到标题直接提到它的新闻）")
    if with_news:
        for n in a.get("news") or []:
            src = f" — {esc(n['src'])}" if n["src"] else ""
            link = f"<{n['url']}|{esc(n['title'])}>" if n["url"] else esc(n["title"])
            lines.append(f"　• {link}{src} · {ago(n['ts'])}")
    return "\n".join(lines)


def index_block(a: dict, now: datetime) -> str:
    o = a["obs"]
    up = o["pct"] > 0
    late = ""
    if o["kind"] == "收盘" and o["sess_day"] != now.date():
        late = f"⏪ 补报 {o['sess_day'].strftime('%m/%d')}："
    what = ("大盘涨幅扩大" if up else "大盘跌幅扩大") if a["prior"] else "大盘异动"
    return (f"{'📈' if up else '📉'} {late}*{what}（{o['kind']}）：标普500 {fmt_pct(o['sp'])}"
            f"｜纳指 {fmt_pct(o['nq'])}*")


def build_message(index_alerts: list, full: list, follow: list, names: dict, owners: dict,
                  now: datetime) -> str:
    parts = [f"*异动提醒* · {now.strftime('%m/%d %H:%M')} ET"]
    parts += [index_block(a, now) for a in index_alerts]
    parts += [stock_block(a, names, owners, now) for a in full]
    if follow:
        items = "、".join(f"{label(a['sym'], names)} {fmt_pct(a['obs']['pct'])}" for a in follow)
        parts.append(f"跟着大盘动的（比纳指多动不到 {EXCESS_PTS:g} 个点）：{items}")
    return "\n\n".join(parts)


# -------------------------------------------------------- 缓涨缓跌（收盘后）
def slow_movers(symbols: list[str], quotes: dict, cal: Cal, today: date) -> list[dict]:
    """近 5 个交易日累计涨跌超 10%，且每一天都没超 5%。日期必须正好命中交易日，
    缺一天就跳过这只，不拿别的日期顶替（沿用旧 stock-alert 的做法）。"""
    days = cal.last_n(today, SLOW_WINDOW_DAYS + 1)
    if len(days) < SLOW_WINDOW_DAYS + 1 or days[-1] != today:
        return []
    import yfinance as yf
    df = yf.download(symbols, start=(days[0] - timedelta(days=3)).isoformat(),
                     end=(today + timedelta(days=1)).isoformat(), interval="1d",
                     auto_adjust=False, progress=False, group_by="ticker", threads=True)
    out = []
    for sym in symbols:
        try:
            ser = df[sym]["Close"].dropna()
            by_day = {ts.date(): float(v) for ts, v in ser.items()}
        except Exception:
            continue
        q = quotes.get(sym) or {}
        if num(q.get("regularMarketPrice")) and q.get("regularMarketTime") and \
                datetime.fromtimestamp(q["regularMarketTime"], ET).date() == today:
            by_day[today] = float(q["regularMarketPrice"])
        closes = [by_day.get(d) for d in days]
        if any(c is None for c in closes):
            log(f"[info] 缓涨缓跌：{sym} 缺某天的收盘价，跳过")
            continue
        daily = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100 for i in range(1, len(closes))]
        cum = (closes[-1] - closes[0]) / closes[0] * 100
        if abs(cum) >= SLOW_CUM_PCT and all(abs(d) < SLOW_DAILY_CAP for d in daily):
            out.append({"sym": sym, "cum": cum, "from": days[1], "to": days[-1]})
    return out


# ------------------------------------------------------------------ the loop
class Watcher:
    def __init__(self, args, store, webhook: str):
        self.args = args
        self.store = store
        self.webhook = webhook
        self.names, self.keywords = load_watchlist()
        self.symbols = list(self.names)
        self.owners = load_owners()
        self.cal = Cal()
        self.state = store.load()
        self.state.setdefault("sent", {})
        self.state.setdefault("days", {})
        self.levels = args.levels or LEVELS
        self.engine = Engine(self.state["sent"], args.confirm)
        self.bad_loops = 0

    # ---- state helpers
    def _rebind(self) -> None:
        """store 重读之后，把引擎指到新的 sent 上。"""
        self.state = self.store.data
        self.state.setdefault("sent", {})
        self.state.setdefault("days", {})
        self.engine.sent = self.state["sent"]

    def _claim_and_send(self, keys: dict, text: str, note: str) -> bool:
        """先登记、登记成功再发；Slack 没收下就撤销登记，下一轮重试。"""
        self.state["sent"].update(keys)
        if not self.store.commit(note):
            self._rebind()
            return False
        if post_slack(self.webhook, text):
            return True
        for k in keys:
            self.state["sent"].pop(k, None)
        if not self.store.commit(f"undo {note}"):
            self._rebind()
        return False

    # ---- one pass
    def tick(self) -> None:
        now = now_et()
        if self.cal.days[-1] < now.date() + timedelta(days=5):
            self.cal = Cal()
        quotes = fetch_quotes(self.symbols + [SP500, NASDAQ])
        missing = [s for s in self.symbols if s not in quotes]
        if missing:
            log(f"[warn] 这几只没拿到报价：{' '.join(missing)}")

        # 大盘
        idx_obs = index_observations(quotes.get(SP500), quotes.get(NASDAQ), now, self.cal)
        index_alerts, nq_by_kind = [], {}
        for o in idx_obs:
            nq_by_kind[(o["kind"], o["sess_day"])] = o["nq"]
            levels = INDEX_UP_LEVELS if o["pct"] > 0 else INDEX_DOWN_LEVELS
            lv = self.engine.observe("INDEX", o, levels)
            if lv:
                index_alerts.append({"sym": "INDEX", "obs": o, "level": lv, "levels": levels,
                                     "prior": self.engine.prior("INDEX", o, levels)})

        # 个股
        alerts, stale = [], 0
        for sym in self.symbols:
            q = quotes.get(sym)
            if not q:
                continue
            obs = observations(q, now, self.cal)
            if any(o["live"] and not o["fresh"] and o["kind"] == "盘中" for o in obs):
                stale += 1
            for o in obs:
                lv = self.engine.observe(sym, o, self.levels)
                if lv:
                    alerts.append({"sym": sym, "obs": o, "level": lv, "levels": self.levels,
                                   "quote": q, "prior": self.engine.prior(sym, o, self.levels),
                                   "nq": nq_by_kind.get((o["kind"], o["sess_day"]))})

        self._watchdog(now, len(missing) + stale)
        if index_alerts or alerts:
            self._send_alerts(now, index_alerts, alerts)
        self._maybe_summary(now, quotes)

    def _index_active(self, o: dict) -> bool:
        direction = "up" if o["pct"] > 0 else "down"
        levels = INDEX_UP_LEVELS if direction == "up" else INDEX_DOWN_LEVELS
        return any(sent_key(o["ref_day"], "INDEX", direction, lv) in self.state["sent"]
                   for lv in levels)

    def _send_alerts(self, now: datetime, index_alerts: list, alerts: list) -> None:
        keys = {}
        stamp = now.timestamp()
        for a in index_alerts + alerts:
            o = a["obs"]
            direction = "up" if o["pct"] > 0 else "down"
            for lv in a["levels"]:
                k = sent_key(o["ref_day"], a["sym"], direction, lv)
                if lv <= a["level"] and k not in self.state["sent"]:
                    keys[k] = {"ts": stamp, "pct": round(o["pct"], 2), "kind": o["kind"]}
        # 跟着大盘动的：大盘已经（或正在）报同方向，个股比纳指多动不到 3 个点
        index_dirs = {("up" if a["obs"]["pct"] > 0 else "down", a["obs"]["ref_day"])
                      for a in index_alerts}
        full, follow = [], []
        for a in alerts:
            o = a["obs"]
            direction = "up" if o["pct"] > 0 else "down"
            market_day = (direction, o["ref_day"]) in index_dirs or \
                (o["kind"] in ("盘中", "收盘") and self._index_active(o))
            if (market_day and a["nq"] is not None and (a["nq"] > 0) == (o["pct"] > 0)
                    and abs(o["pct"] - a["nq"]) < EXCESS_PTS):
                follow.append(a)
            else:
                full.append(a)
        for a in full:
            a["news"] = fetch_news(a["sym"], self.keywords.get(a["sym"], []),
                                   NEWS_COUNT if len(full) <= 4 else 2)
        text = build_message(index_alerts, full, follow, self.names, self.owners, now)
        note = " ".join(sorted({a["sym"] for a in index_alerts + alerts}))
        if self._claim_and_send(keys, text, note):
            log(f"[sent] {note}")
            day = self.state["days"].setdefault(now.date().isoformat(), {})
            for a in alerts:
                o = a["obs"]
                if o["kind"] in ("盘前", "盘中") or (o["kind"] == "收盘" and o["sess_day"] == now.date()):
                    day.setdefault(a["sym"], {"first_pct": round(o["pct"], 2), "first_ts": stamp,
                                              "kind": o["kind"]})
            self.store.commit("day record") or self._rebind()

    def _watchdog(self, now: datetime, n_bad: int) -> None:
        if n_bad >= max(1, len(self.symbols) // 3):
            self.bad_loops += 1
        else:
            self.bad_loops = 0
        key = f"{now.date().isoformat()}|WARN|data"
        if self.bad_loops >= BAD_LOOPS_BEFORE_WARN and key not in self.state["sent"]:
            self._claim_and_send(
                {key: {"ts": now.timestamp()}},
                f"⚠️ *异动提醒：行情数据不正常* — 连续 {self.bad_loops} 轮有 {n_bad} 只股票拿不到最新报价，"
                "这段时间的提醒可能不全。", "warn data")

    def _maybe_summary(self, now: datetime, quotes: dict) -> None:
        today = now.date()
        key = f"{today.isoformat()}|SUMMARY"
        if key in self.state["sent"] or not self.cal.is_session(today):
            return
        closed = [q for s in self.symbols if (q := quotes.get(s))
                  and q.get("marketState") != "REGULAR" and q.get("regularMarketTime")
                  and datetime.fromtimestamp(q["regularMarketTime"], ET).date() == today]
        if len(closed) < len(self.symbols) * 0.8:
            return
        last_close = max(q["regularMarketTime"] for q in closed)
        if now.timestamp() < last_close + SUMMARY_DELAY_MIN * 60:
            return
        lines = []
        alerted = self.state["days"].get(today.isoformat(), {})
        for sym, rec in sorted(alerted.items(), key=lambda kv: kv[1].get("first_ts", 0)):
            q = quotes.get(sym)
            if not q:
                continue
            px, prev = num(q.get("regularMarketPrice")), num(q.get("regularMarketPreviousClose"))
            if not (px and prev):
                continue
            pct = (px - prev) / prev * 100
            lo, hi = num(q.get("regularMarketDayLow")), num(q.get("regularMarketDayHigh"))
            ext = ""
            if rec.get("first_pct", 0) < 0 and lo:
                ext = f"盘中最低 {fmt_pct((lo - prev) / prev * 100)}，"
            elif rec.get("first_pct", 0) > 0 and hi:
                ext = f"盘中最高 {fmt_pct((hi - prev) / prev * 100)}，"
            first_at = datetime.fromtimestamp(rec["first_ts"], ET).strftime("%H:%M")
            lines.append(f"• {label(sym, self.names)} 收 {fmt_pct(pct)}"
                         f"（{ext}{first_at} 首次提醒时 {fmt_pct(rec['first_pct'])}）")
        slow_lines, slow_new = [], {}
        slow_sent = self.state.setdefault("slow", {})
        try:
            for s in slow_movers(self.symbols, quotes, self.cal, today):
                d = "up" if s["cum"] > 0 else "down"
                last = slow_sent.get(f"{s['sym']}|{d}")
                if last and (today - date.fromisoformat(last)).days < SLOW_REPEAT_DAYS:
                    continue
                slow_new[f"{s['sym']}|{d}"] = today.isoformat()
                slow_lines.append(f"• {label(s['sym'], self.names)} {fmt_pct(s['cum'])}"
                                  f"（{s['from'].strftime('%m/%d')}→{s['to'].strftime('%m/%d')}）")
        except Exception as e:
            log(f"[warn] 缓涨缓跌没算成：{e}")
        if not lines and not slow_lines:
            self.state["sent"][key] = {"ts": now.timestamp(), "empty": True}
            self.store.commit("summary (nothing)") or self._rebind()
            return
        parts = [f"*收盘小结 · {today.strftime('%m/%d')}（周{WEEKDAYS[today.weekday()]}）*"]
        sp, nq = quotes.get(SP500), quotes.get(NASDAQ)
        if sp and nq and num(sp.get("regularMarketPreviousClose")) and num(nq.get("regularMarketPreviousClose")):
            parts.append("大盘：标普500 {}｜纳指 {}".format(
                fmt_pct((sp["regularMarketPrice"] - sp["regularMarketPreviousClose"])
                        / sp["regularMarketPreviousClose"] * 100),
                fmt_pct((nq["regularMarketPrice"] - nq["regularMarketPreviousClose"])
                        / nq["regularMarketPreviousClose"] * 100)))
        if lines:
            parts.append(f"今天提醒过的 {len(lines)} 只：\n" + "\n".join(lines))
        if slow_lines:
            parts.append(f"🐢 缓涨缓跌（近 {SLOW_WINDOW_DAYS} 个交易日累计超 {SLOW_CUM_PCT:g}%、"
                         f"单日都没超 {SLOW_DAILY_CAP:g}%）：\n" + "\n".join(slow_lines))
        slow_sent.update(slow_new)
        if self._claim_and_send({key: {"ts": now.timestamp()}}, "\n\n".join(parts), "summary"):
            log("[sent] 收盘小结")
        else:
            for k in slow_new:
                self.state.get("slow", {}).pop(k, None)

    # ---- gap notice
    def gap_notice(self) -> None:
        """不是接力启动、又正好在监控时段里 = 之前断过，被保险定时器拉起来的。说一声。"""
        now = now_et()
        if not self.state.get("launched"):
            self.state["launched"] = now.isoformat(timespec="minutes")
            self.store.commit("first launch") or self._rebind()
            return
        if os.environ.get("HANDOFF_FROM") or not in_window(now, self.cal):
            return
        key = f"{now.date().isoformat()}|WARN|gap|{now.hour}"
        if key in self.state["sent"]:
            return
        since = ""
        try:
            repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
            r = requests.get(
                f"https://api.github.com/repos/{repo}/actions/workflows/watch.yml/runs",
                headers={"Authorization": f"Bearer {token}"}, timeout=20,
                params={"per_page": 20, "status": "completed"})
            me = os.environ.get("GITHUB_RUN_ID", "")
            for run in r.json().get("workflow_runs", []):
                if str(run["id"]) != me and run.get("conclusion") in ("success", "failure", "cancelled") \
                        and "dry" not in (run.get("display_title") or ""):
                    started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
                    ended = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
                    if (ended - started).total_seconds() > 300:   # 真正跑过监控的那一轮
                        since = f"上一轮结束于 {ended.astimezone(ET).strftime('%m/%d %H:%M')} ET，"
                        break
        except Exception as e:
            log(f"[warn] 查上一轮结束时间失败：{e}")
        self._claim_and_send(
            {key: {"ts": now.timestamp()}},
            f"⚠️ *异动提醒中断过，刚恢复* — {since}现在 {now.strftime('%H:%M')} ET 重新开始盯盘。"
            "中间超线的股票会在接下来几分钟补上。", "warn gap")

    # ---- main loop
    def run(self) -> int:
        started = time.time()
        budget_s = self.args.budget_min * 60
        if self.args.mode == "dry":
            self.tick()
            log("[info] 试跑一轮完成")
            return 0
        prune_state(self.state, now_et().date())
        self.gap_notice()
        last_idle_log = 0.0
        while time.time() - started < budget_s:
            now = now_et()
            if in_window(now, self.cal):
                t0 = time.time()
                try:
                    self.tick()
                except Exception as e:
                    log(f"[warn] 这一轮出错：{type(e).__name__}: {e}")
                    self.bad_loops += 1
                    try:
                        self._watchdog(now, len(self.symbols))
                    except Exception:
                        pass
                time.sleep(max(5.0, LOOP_SECONDS - (time.time() - t0)))
            else:
                nxt = next_window_start(now, self.cal)
                wait = (nxt - now).total_seconds()
                if time.time() - last_idle_log > 1800:
                    log(f"[idle] 不在监控时段，下一次开始：{nxt.strftime('%m/%d %H:%M')} ET")
                    last_idle_log = time.time()
                left = budget_s - (time.time() - started)
                time.sleep(max(1.0, min(wait, 300.0, left)))
        log("[info] 这一轮时间到，交棒给下一轮")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "dry"], default="live",
                    help="live=正式监控；dry=只跑一轮、打印不发、不写状态")
    ap.add_argument("--budget-min", type=float, default=RUN_BUDGET_MIN)
    ap.add_argument("--levels", type=lambda s: [float(x) for x in s.split(",") if x.strip()],
                    default=None, help="试跑用：临时改个股档位，如 0.5,1,2")
    ap.add_argument("--confirm", type=int, default=None, help="试跑用：临时改确认次数")
    ap.add_argument("--state-file", default=None, help="本地跑时把状态存到这个文件")
    args = ap.parse_args()

    if args.mode == "dry":
        webhook, store = "DRY", LocalStore(None)
        args.confirm = args.confirm or 1
    else:
        webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
        if not webhook:
            log("[err] 没有 SLACK_WEBHOOK_URL")
            return 2
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if args.state_file or not (repo and token):
            store = LocalStore(Path(args.state_file or "state.local.json"))
        else:
            store = GitHubStore(repo, token)
    w = Watcher(args, store, webhook)
    log(f"[info] 模式 {args.mode}；盯 {len(w.symbols)} 只：{' '.join(w.symbols)}；"
        f"配了负责人的 {len(w.owners)} 只")
    return w.run()


if __name__ == "__main__":
    sys.exit(main())
