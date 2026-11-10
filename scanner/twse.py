"""證交所加權指數（TAIEX）資料：日 OHLC（含前日收盤）與盤中即時值。"""
from __future__ import annotations

import datetime as dt
import json
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


def day_info(date: dt.date, archive_dir: Path, mode: str = "refresh") -> dict | None:
    """當日 OHLC 與前一交易日收盤；任何一個拿不到就回傳 None。"""
    try:
        cur = month_ohlc(date.year, date.month, archive_dir, mode)
        prev_m = (date.replace(day=1) - dt.timedelta(days=1))
        older = {d: v for d, v in cur.items() if d < date}
        if not older:
            older = month_ohlc(prev_m.year, prev_m.month, archive_dir, mode)
    except (requests.RequestException, ValueError) as e:
        print(f"  [警告] 加權指數資料取得失敗：{e}")
        return None
    today = cur.get(date)
    if not today or not older:
        return None
    prev_close = older[max(older)]["close"]
    pct = lambda x: round((x / prev_close - 1) * 100, 2) if x and prev_close else None
    return {"date": date.isoformat(), **today, "prev_close": prev_close,
            "pct": pct(today["close"]), "high_pct": pct(today["high"]), "low_pct": pct(today["low"])}


def realtime() -> dict | None:
    """盤中加權指數（證交所基本市況報導，約 5 秒更新）。"""
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
            "high": _num(m.get("h")), "low": _num(m.get("l")), "pct": round((last / prev - 1) * 100, 2)}
