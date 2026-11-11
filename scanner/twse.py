"""加權指數（TAIEX）資料：日 OHLC（含前日收盤）與盤中即時值。

來源順序：證交所 → Yahoo Finance（^TWII）。證交所會擋部分海外 IP（GitHub Actions 主機即是），
所以雲端排程實際上多半走 Yahoo；本機或台灣主機可直接用證交所。結果快取在 archive/taiex/days.json。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path

import requests

HIST_URL = "https://www.twse.com.tw/rwd/zh/indicesReport/MI_5MINS_HIST"
LIVE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
HEADERS = {"User-Agent": "Mozilla/5.0 (txo-limit-arb)"}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def _roc(s: str) -> dt.date | None:
    m = re.fullmatch(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", str(s).strip())
    return dt.date(int(m[1]) + 1911, int(m[2]), int(m[3])) if m else None


def parse_month(js: dict) -> dict:
    """MI_5MINS_HIST 回傳 → {日期: {open, high, low, close}}。"""
    if js.get("stat") not in ("OK", "ok") or not js.get("data"):
        return {}
    fields = [str(f) for f in js.get("fields", [])]
    idx = lambda kw, default: next((i for i, f in enumerate(fields) if kw in f), default)
    i_d, i_o, i_h, i_l, i_c = idx("日期", 0), idx("開盤", 1), idx("最高", 2), idx("最低", 3), idx("收盤", 4)
    out = {}
    for row in js["data"]:
        d = _roc(row[i_d])
        if d:
            out[d] = {"open": _num(row[i_o]), "high": _num(row[i_h]), "low": _num(row[i_l]), "close": _num(row[i_c])}
    return out


def month_ohlc(year: int, month: int, archive_dir: Path, mode: str = "refresh") -> dict:
    p = archive_dir / "taiex" / f"{year}{month:02d}.json"
    today = dt.date.today()
    current = (year, month) == (today.year, today.month)
    if p.exists() and (mode != "refresh" or not current):
        js = json.loads(p.read_text("utf-8"))
    elif mode == "offline":
        return {}
    else:
        r = requests.get(HIST_URL, params={"date": f"{year}{month:02d}01", "response": "json"},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        js = r.json()
        time.sleep(3)                                     # 證交所有頻率限制
        if js.get("stat") in ("OK", "ok"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(js, ensure_ascii=False), "utf-8")
    return parse_month(js)


def _twse_day(date: dt.date, archive_dir: Path, mode: str) -> dict | None:
    cur = month_ohlc(date.year, date.month, archive_dir, mode)
    prev_m = date.replace(day=1) - dt.timedelta(days=1)
    older = {d: v for d, v in cur.items() if d < date}
    if not older:
        older = month_ohlc(prev_m.year, prev_m.month, archive_dir, mode)
    today = cur.get(date)
    if not today or not older:
        return None
    return {**today, "prev_close": older[max(older)]["close"], "source": "TWSE"}


def _yahoo_day(date: dt.date) -> dict | None:
    import yfinance as yf
    h = yf.Ticker("^TWII").history(start=date - dt.timedelta(days=20), end=date + dt.timedelta(days=1),
                                   interval="1d", auto_adjust=False)
    if h.empty:
        return None
    h.index = [i.date() for i in h.index]
    if date not in h.index:
        return None
    prev = h[h.index < date]
    if prev.empty:
        return None
    r = h.loc[date]
    return {"open": round(float(r["Open"]), 2), "high": round(float(r["High"]), 2),
            "low": round(float(r["Low"]), 2), "close": round(float(r["Close"]), 2),
            "prev_close": round(float(prev["Close"].iloc[-1]), 2), "source": "Yahoo ^TWII"}


def ensure_history(dates, archive_dir: Path) -> int:
    """回測前一次補齊加權指數日資料（Yahoo），寫進 archive/taiex/days.json；取不到就略過。"""
    cache_p = archive_dir / "taiex" / "days.json"
    cache = json.loads(cache_p.read_text("utf-8")) if cache_p.exists() else {}
    missing = sorted(d for d in dates if d.isoformat() not in cache)
    if not missing:
        return 0
    try:
        import yfinance as yf
        h = yf.Ticker("^TWII").history(start=missing[0] - dt.timedelta(days=20),
                                       end=missing[-1] + dt.timedelta(days=1), interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"  [警告] 補齊加權指數歷史失敗：{str(e)[:120]}")
        return 0
    if h.empty:
        return 0
    rows = [(i.date(), r) for i, r in h.iterrows()]
    n = 0
    for (d0, r0), (d1, r1) in zip(rows, rows[1:]):
        if d1.isoformat() in cache or d1 not in set(missing):
            continue
        cache[d1.isoformat()] = {"open": round(float(r1["Open"]), 2), "high": round(float(r1["High"]), 2),
                                 "low": round(float(r1["Low"]), 2), "close": round(float(r1["Close"]), 2),
                                 "prev_close": round(float(r0["Close"]), 2), "source": "Yahoo ^TWII"}
        n += 1
    cache_p.parent.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")
    print(f"  補齊加權指數 {n} 天")
    return n


def history_returns(archive_dir: Path, before: dt.date) -> list[float]:
    """進場日之前每個交易日的指數漲跌幅（收盤 ÷ 前一日收盤 − 1），依日期排序；只用過去資料。"""
    cache_p = archive_dir / "taiex" / "days.json"
    if not cache_p.exists():
        return []
    cache = json.loads(cache_p.read_text("utf-8"))
    out = []
    for d in sorted(cache):
        v = cache[d]
        if d < before.isoformat() and v.get("close") and v.get("prev_close") and v.get("source") != "TWSE-live":
            out.append(v["close"] / v["prev_close"] - 1)
    return out


def history_closes(archive_dir: Path, upto: dt.date) -> list[tuple[str, float]]:
    """到 upto（含）為止的加權指數日收盤 [(YYYY-MM-DD, close)]，依日期排序；不含盤中即時值。"""
    cache_p = archive_dir / "taiex" / "days.json"
    if not cache_p.exists():
        return []
    cache = json.loads(cache_p.read_text("utf-8"))
    return [(d, cache[d]["close"]) for d in sorted(cache)
            if d <= upto.isoformat() and cache[d].get("close") and cache[d].get("source") != "TWSE-live"]


def use_twse() -> bool:
    """TAIEX_SOURCE=twse|yahoo|auto（預設 auto：在 GitHub Actions 上跳過必定被擋的證交所）。"""
    src = os.environ.get("TAIEX_SOURCE", "auto").lower()
    if src in ("twse", "yahoo"):
        return src == "twse"
    return os.environ.get("GITHUB_ACTIONS") != "true"


def day_info(date: dt.date, archive_dir: Path, mode: str = "refresh") -> dict | None:
    """當日 OHLC 與前一交易日收盤；任何一個拿不到就回傳 None。"""
    cache_p = archive_dir / "taiex" / "days.json"
    cache = json.loads(cache_p.read_text("utf-8")) if cache_p.exists() else {}
    base = cache.get(date.isoformat()) if mode != "refresh" else None
    if base is None and mode != "offline":
        sources = [("TWSE", lambda: _twse_day(date, archive_dir, mode))] if use_twse() else []
        sources.append(("Yahoo", lambda: _yahoo_day(date)))
        for name, fn in sources:
            try:
                base = fn()
            except Exception as e:                       # 來源失效時改用下一個
                print(f"  [警告] 加權指數（{name}）取得失敗：{str(e)[:120]}")
                base = None
            if base:
                break
        if base:
            cache[date.isoformat()] = base
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")
    if not base:
        return None
    pc = base["prev_close"]
    pct = lambda x: round((x / pc - 1) * 100, 2) if x and pc else None
    return {"date": date.isoformat(), **base,
            "pct": pct(base["close"]), "high_pct": pct(base["high"]), "low_pct": pct(base["low"])}


def realtime() -> dict | None:
    """盤中加權指數：證交所基本市況報導（約 5 秒更新），失敗改用 Yahoo（可能延遲）。"""
    if use_twse():
        try:
            return _twse_realtime()
        except Exception as e:
            print(f"  [警告] 證交所即時指數失敗：{str(e)[:120]}，改用 Yahoo")
    import yfinance as yf
    t = yf.Ticker("^TWII")
    h = t.history(period="1d", interval="1m")
    d = t.history(period="5d", interval="1d")
    if h.empty or len(d) < 2:
        return None
    last = float(h["Close"].iloc[-1])
    today = h.index[-1].date()
    prev = float(d[[i.date() < today for i in d.index]]["Close"].iloc[-1])
    return {"time": h.index[-1].strftime("%Y%m%d %H:%M:%S"), "last": round(last, 2), "prev_close": round(prev, 2),
            "high": round(float(h["High"].max()), 2), "low": round(float(h["Low"].min()), 2),
            "pct": round((last / prev - 1) * 100, 2), "source": "Yahoo ^TWII（可能延遲）"}


def _twse_realtime() -> dict | None:
    r = requests.get(LIVE_URL, params={"ex_ch": "tse_t00.tw", "json": "1", "delay": "0"},
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    arr = r.json().get("msgArray") or []
    if not arr:
        return None
    m = arr[0]
    last, prev = _num(m.get("z")), _num(m.get("y"))
    if last is None or prev is None:
        return None
    return {"time": f"{m.get('d', '')} {m.get('t', '')}".strip(), "last": last, "prev_close": prev,
            "high": _num(m.get("h")), "low": _num(m.get("l")), "pct": round((last / prev - 1) * 100, 2),
            "source": "TWSE"}
