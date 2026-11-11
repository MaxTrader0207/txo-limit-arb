"""期貨狀態判斷、選擇權鏈整理。"""
from __future__ import annotations

import datetime as dt
import math
import re

import pandas as pd


def num(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def ok(x) -> bool:
    return x is not None and x > 0


# ------------------------------------------------------------------ 期貨
def tx_contracts(fut: pd.DataFrame) -> pd.DataFrame:
    """所有 TX 單式月契約（排除價差契約）。"""
    return fut[(fut["contract"] == "TX") & fut["expiry"].str.fullmatch(r"\d{6}")].sort_values("expiry")


def hedge_contracts(fut: pd.DataFrame) -> pd.DataFrame:
    """對沖用期貨：小台 MTX（月契約＋週三週契約，每點 50 元與 TXO 1:1）；沒有 MTX 資料時退回 TX 月契約。"""
    mtx = fut[(fut["contract"] == "MTX") & fut["expiry"].str.fullmatch(r"\d{6}(W\d)?")]
    return mtx.sort_values("expiry") if len(mtx) else tx_contracts(fut)


def front_futures(fut: pd.DataFrame) -> dict:
    """各交易時段的近月 TX。"""
    return {sess: g.iloc[0] for sess, g in tx_contracts(fut).groupby("session")}


def third_wednesday(ym: str) -> dt.date:
    d = dt.date(int(ym[:4]), int(ym[4:6]), 15)
    return d + dt.timedelta(days=(2 - d.weekday()) % 7)


def parse_ymd(s) -> dt.date | None:
    s = str(s or "").strip()
    return dt.datetime.strptime(s, "%Y%m%d").date() if re.fullmatch(r"\d{8}", s) else None


def nth_weekday(ym: str, weekday: int, n: int) -> dt.date:
    d = dt.date(int(ym[:4]), int(ym[4:6]), 1)
    return d + dt.timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def code_to_date(exp: str) -> dt.date | None:
    """後備：由代碼推算（未考慮假日順延）。202611→第3個週三、202611W2→第2個週三、202611F2→第2個週五。"""
    m = re.fullmatch(r"(\d{6})(?:([WF])(\d))?", str(exp))
    if not m:
        return None
    ym, kind, n = m.groups()
    if not kind:
        return third_wednesday(ym)
    return nth_weekday(ym, 2 if kind == "W" else 4, int(n))


def expiry_dates(opt: pd.DataFrame) -> dict:
    """{到期別: 結算日}。優先用 TXO 的「契約到期日」欄（含假日順延），沒有才由代碼推算。"""
    out = {}
    o = opt[opt["contract"] == "TXO"]
    for exp, g in o.groupby("expiry"):
        d = None
        if "exp_date" in g.columns:
            d = next((parse_ymd(x) for x in g["exp_date"] if parse_ymd(x)), None)
        d = d or code_to_date(exp)
        if d:
            out[exp] = d
    return out


def fut_settle_date(code: str, exp_dates: dict) -> dt.date:
    """期貨契約結算日：月契約與同月月選、小台週契約與同代碼週選同日結算（含假日順延）；查不到才由代碼推算。"""
    return exp_dates.get(code) or code_to_date(code)


tx_settle_date = fut_settle_date   # 舊名相容


def hedge_for(opt_settle: dt.date, futs: list[dict]) -> dict | None:
    """在同一交易時段的 TX 中，找結算日 ≥ 選擇權結算日的最近一口。"""
    cands = [f for f in futs if f["settle_date"] >= opt_settle]
    return min(cands, key=lambda f: f["settle_date"]) if cands else None


STATUS_LABEL = {
    "LIMIT_UP": "漲停鎖住", "LIMIT_DOWN": "跌停鎖住", "TOUCHED_UP": "盤中觸及漲停",
    "TOUCHED_DOWN": "盤中觸及跌停", "NEAR_UP": "接近漲停", "NEAR_DOWN": "接近跌停",
    "INTRA_NEAR_UP": "盤中接近漲停", "INTRA_NEAR_DOWN": "盤中接近跌停",
    "NORMAL": "正常", "UNKNOWN": "資料不足",
}
LIMIT_EVENTS = {"LIMIT_UP", "LIMIT_DOWN", "TOUCHED_UP", "TOUCHED_DOWN"}


def limit_status(row, cfg) -> dict:
    close = num(row.get("close")) or num(row.get("settle"))
    chg = num(row.get("chg"))
    hi, lo = num(row.get("high")), num(row.get("low"))
    res = {"expiry": row.get("expiry"), "close": close, "high": hi, "low": lo, "chg": chg}
    if close is None or chg is None:
        return {**res, "status": "UNKNOWN", "label": STATUS_LABEL["UNKNOWN"]}
    prev = close - chg                       # 期交所漲跌價以前一日結算價為基準
    up = math.floor(prev * (1 + cfg["limit_pct"]))
    dn = math.ceil(prev * (1 - cfg["limit_pct"]))
    pct = chg / prev if prev else 0.0
    tol = cfg["limit_tol_pts"]
    if close >= up - tol:
        s = "LIMIT_UP"
    elif close <= dn + tol:
        s = "LIMIT_DOWN"
    elif hi is not None and hi >= up - tol:
        s = "TOUCHED_UP"
    elif lo is not None and lo <= dn + tol:
        s = "TOUCHED_DOWN"
    elif pct >= cfg["near_limit_pct"]:
        s = "NEAR_UP"
    elif pct <= -cfg["near_limit_pct"]:
        s = "NEAR_DOWN"
    elif hi is not None and prev and hi / prev - 1 >= cfg["near_limit_pct"]:
        s = "INTRA_NEAR_UP"                  # 收盤回落，但盤中曾接近漲停
    elif lo is not None and prev and lo / prev - 1 <= -cfg["near_limit_pct"]:
        s = "INTRA_NEAR_DOWN"
    else:
        s = "NORMAL"
    return {**res, "status": s, "label": STATUS_LABEL[s], "prev_settle": prev,
            "limit_up": up, "limit_down": dn, "pct": round(pct * 100, 2)}


# ------------------------------------------------------------------ 選擇權
def is_weekly(expiry: str) -> bool:
    return bool(re.search(r"[WF]\d", str(expiry)))


def _quote(r) -> dict:
    q = {"bid": num(r.get("bid")), "ask": num(r.get("ask")), "last": num(r.get("close")),
         "settle": num(r.get("settle")), "vol": num(r.get("volume")), "oi": num(r.get("oi"))}
    if ok(q["bid"]) and ok(q["ask"]) and q["bid"] > q["ask"]:   # 交叉報價視為資料錯誤
        q["bid"] = q["ask"] = None
    return q


def build_chains(opt: pd.DataFrame, include_monthly: bool = True) -> dict:
    """回傳 {(到期別, 時段): {履約價: {"C": quote, "P": quote}}}。"""
    o = opt[opt["contract"] == "TXO"]
    exps = set(e for e in o["expiry"].unique() if is_weekly(e))
    if include_monthly:
        monthly = sorted(e for e in o["expiry"].unique() if re.fullmatch(r"\d{6}", e))
        exps |= set(monthly[:1])
    chains = {}
    for (exp, sess), g in o[o["expiry"].isin(exps)].groupby(["expiry", "session"]):
        ch: dict = {}
        for _, r in g.iterrows():
            k = num(r["strike"])
            if k is None:
                continue
            side = "C" if ("買" in r["cp"] or r["cp"].upper().startswith("C")) else "P"
            ch.setdefault(k, {})[side] = _quote(r)
        chains[(exp, sess)] = dict(sorted(ch.items()))
    return chains


def mid(q):
    if q and ok(q["bid"]) and ok(q["ask"]):
        return (q["bid"] + q["ask"]) / 2
    if q and ok(q.get("settle")):
        return q["settle"]
    return None


def _tight(q) -> bool:
    """買賣兩邊都有報價，且價差 ≤ MAX(10 點, 中價 × 30%)。"""
    if not q or not ok(q.get("bid")) or not ok(q.get("ask")):
        return False
    return q["ask"] - q["bid"] <= max(10.0, 0.3 * (q["ask"] + q["bid"]) / 2)


def implied_forward(chain: dict, F: float, n: int = 5, window: float = 0.05, min_pts: int = 3):
    """由買賣權平價 K + C − P 推算隱含期貨價：只用期貨價 ±5% 內、Call 與 Put 報價都夠緊的履約價，
    取最接近的 n 檔中位數；不足 min_pts 檔回傳 None（夜盤報價稀疏時常見，寧缺勿濫）。"""
    pts = []
    for k, q in chain.items():
        c, p = q.get("C"), q.get("P")
        if abs(k - F) <= F * window and _tight(c) and _tight(p):
            pts.append((abs(k - F), k + mid(c) - mid(p)))
    if len(pts) < min_pts:
        return None
    vals = sorted(v for _, v in sorted(pts)[:n])
    return vals[len(vals) // 2]
