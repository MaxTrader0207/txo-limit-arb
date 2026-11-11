"""策略三：風險可控的價差（週選為標的，結算前收盤進場、持有到結算）。

關鍵性質
--------
TXO 最後結算價＝結算日 13:00–13:30 加權指數均價；加權指數成分股單日漲跌幅 ±10%。
所以從今天（D）收盤到結算日（T）若還有 n 個交易時段，結算價必落在
    [D 日指數收盤 × 0.9ⁿ × (1 − 緩衝), D 日指數收盤 × 1.1ⁿ × (1 + 緩衝)]
n 取 D 之後到 T（含）的「平日數」；遇假日時實際交易時段更少，區間只會更窄，所以上下限一定成立。
在這個區間內，任何價差組合的最大虧損、最大獲利都能精確算出（分段線性，只要檢查端點與履約價）。

三種策略
--------
- 零風險價差賣方：賣出的履約價在區間外、再買更外一檔保護。到期必歸零，淨收的權利金就是獲利；
  買保護是為了把保證金從裸賣的 B 值（約 9.4 萬）降到「履約價差 × 50 − 權利金」。
- 有限風險價差賣方：賣出點在區間內、保護點在更外面。最大虧損＝區間內最壞情況，事前已知。
- 漲跌停日方向性價差：期貨鎖漲（跌）停時，順著方向買進多頭買權（空頭賣權）價差，最大虧損＝付出的權利金。
  屬方向性交易，不是套利。

報價用 D 日一般時段收盤最佳買賣價（賣用買價、買用賣價）。訊號在收盤後產生，可於當晚夜盤或結算日早上參考，
實際下單時報價會變動，但上下限（以 D 日收盤計）不變。結算後以最後結算價回填實際損益。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import margin as mg
from .checks import opt_cost, settle_cost
from .market import ok

UP_EVENTS = {"LIMIT_UP", "TOUCHED_UP"}
DOWN_EVENTS = {"LIMIT_DOWN", "TOUCHED_DOWN"}


# ---------------------------------------------------------------- 到期報酬
def intrinsic(side: str, k: float, S: float) -> float:
    return max(0.0, S - k) if side == "C" else max(0.0, k - S)


def payoff(legs, S: float) -> float:
    """legs：[(side, K, qty)]，qty > 0 買進、< 0 賣出。回傳到期報酬（點）。"""
    return sum(q * intrinsic(side, k, S) for side, k, q in legs)


def itm_count(legs, S: float) -> int:
    return sum(abs(q) for side, k, q in legs if intrinsic(side, k, S) > 0)


def sessions_until(d: dt.date, t: dt.date) -> int:
    """D 之後到 T（含）的平日數＝交易時段數的上限。"""
    n, x = 0, d
    while x < t:
        x += dt.timedelta(days=1)
        n += x.weekday() < 5
    return n


def bounds(prev: float, n: int, cfg: dict) -> tuple[float, float]:
    b = cfg["hard_buffer_pct"]
    return prev * 0.9 ** n * (1 - b), prev * 1.1 ** n * (1 + b)


def range_eval(legs, lo: float, hi: float):
    """區間 [lo, hi] 內的最壞／最好到期報酬，以及最多可能價內的口數（決定結算成本）。"""
    nodes = sorted({lo, hi, *(k for _, k, _ in legs if lo < k < hi)})
    probe = nodes + [x + 0.01 for x in nodes if x < hi]
    vals = [(payoff(legs, S), S) for S in nodes]
    worst = min(vals)
    best = max(vals)
    return worst, best, max(itm_count(legs, S) for S in probe)


def breakeven_move(legs, pnl_at, prev: float, lo: float, hi: float):
    """損益 = 0 的指數位置（最接近目前指數的那個），以漲跌 % 表示；區間內不會虧則回傳 None。"""
    nodes = sorted({lo, hi, *(k for _, k, _ in legs if lo < k < hi)})
    cands = []
    for a, b in zip(nodes, nodes[1:]):
        pa, pb = pnl_at(a), pnl_at(b)
        if pa == 0:
            cands.append(a)
        if (pa > 0) != (pb > 0) and pa != pb:
            cands.append(a + (b - a) * pa / (pa - pb))
    if pnl_at(nodes[-1]) == 0:
        cands.append(nodes[-1])
    if not cands:
        return None
    x = min(cands, key=lambda s: abs(s - prev))
    return round((x / prev - 1) * 100, 2)


# ---------------------------------------------------------------- 單一組合
def n_session_returns(hist: list[float] | None, n: int) -> list[float]:
    """由連續交易日的單日報酬，組出 n 個交易時段的累積報酬。"""
    if not hist:
        return []
    out = []
    for i in range(len(hist) - n + 1):
        g = 1.0
        for r in hist[i:i + n]:
            g *= 1 + r
        out.append(g - 1)
    return out


def build(kind: str, exp: str, settle: dt.date, n: int, legs, chain: dict, lo: float, hi: float,
          prev: float, cfg: dict, note: str, rets: list[float] | None = None):
    """依收盤報價建立一組價差的完整評估；報價不全則回傳 None。"""
    pv = cfg["point_value"]
    net, open_cost = 0.0, 0.0
    for side, k, q in legs:
        quote = (chain.get(k) or {}).get(side)
        px = quote and (quote["bid"] if q < 0 else quote["ask"])
        if not ok(px):
            return None
        net += -q * px                       # 收到為正、付出為負
        open_cost += abs(q) * opt_cost(px, cfg)
    open_sides = sum(abs(q) for _, _, q in legs)
    (w_pay, w_S), (b_pay, b_S), max_itm = range_eval(legs, lo, hi)
    worst = net + w_pay - open_cost - settle_cost(hi, cfg, max_itm)
    best_itm = itm_count(legs, b_S + 0.01) if b_pay > 0 else 0
    best = net + b_pay - open_cost - settle_cost(hi, cfg, best_itm)
    width = abs(legs[0][1] - legs[1][1])
    capital = mg.credit_spread(width, net, cfg) if net > 0 else mg.debit(-net, cfg)
    pnl_at = lambda S: net + payoff(legs, S) - open_cost
    item = {
        "kind": kind, "expiry": exp, "settle_date": settle.isoformat(), "sessions": n,
        "legs": [[s, k, q] for s, k, q in legs],
        "desc": "、".join(f"{'買' if q > 0 else '賣'}{s}{k:g}@{(chain[k][s]['ask'] if q > 0 else chain[k][s]['bid']):g}"
                         for s, k, q in legs),
        "net_pts": round(net, 2), "open_cost": round(open_cost, 3), "open_sides": open_sides,
        "worst_pts": round(worst, 2), "best_pts": round(best, 2),
        "worst_sides": open_sides + max_itm, "best_sides": open_sides + best_itm,
        "width": width, "capital": round(capital),
        "breakeven_move": breakeven_move(legs, pnl_at, prev, lo, hi),
        "note": note, "realized_pts": None,
    }
    item["worst_ntd"], item["best_ntd"] = round(worst * pv), round(best * pv)
    item["per_10k"] = round(best * pv / capital * 10000, 1) if capital >= 1000 else None
    item["reward_risk"] = round(best / -worst, 2) if worst < 0 else None
    # 歷史分布：以進場日之前的指數 n 時段漲跌幅，估平均損益與虧損機率
    item["ev_pts"] = item["loss_prob"] = item["ev_per_10k"] = None
    if rets and len(rets) >= cfg["spread_min_hist"]:
        pnls, itms = [], []
        for r in rets:
            S = min(max(prev * (1 + r), lo), hi)
            itm = itm_count(legs, S)
            pnls.append(pnl_at(S) - settle_cost(S, cfg, itm))
            itms.append(itm)
        ev = sum(pnls) / len(pnls)
        item["ev_pts"] = round(ev, 2)
        item["ev_sides"] = round(open_sides + sum(itms) / len(itms), 3)
        item["loss_prob"] = round(sum(p < 0 for p in pnls) / len(pnls) * 100, 1)
        item["hist_n"] = len(pnls)
        item["ev_per_10k"] = round(ev * pv / capital * 10000, 1) if capital >= 1000 else None
    return item


def fee_floor_gain(sides: int, cfg: dict) -> float:
    return sides * (cfg["opt_fee"] - cfg["fee_floor"]) / cfg["point_value"]


# ---------------------------------------------------------------- 策略掃描
def _otm_strikes(chain, side):
    return sorted(k for k, q in chain.items() if q.get(side))


def zero_risk(chain, exp, settle, n, lo, hi, prev, cfg, rets=None):
    out = []
    for side in "CP":
        ks = _otm_strikes(chain, side)
        shorts = [k for k in ks if (k > hi if side == "C" else k < lo)]
        for k1 in shorts:
            for k2 in ks:
                w = (k2 - k1) if side == "C" else (k1 - k2)
                if not (0 < w <= cfg["spread_max_width"]):
                    continue
                it = build("零風險價差賣方", exp, settle, n, [(side, k1, -1), (side, k2, 1)], chain, lo, hi, prev, cfg,
                           f"兩腳都在結算價區間 {lo:,.0f}～{hi:,.0f} 之外，到期必歸零", rets)
                if it and it["best_pts"] + fee_floor_gain(it["best_sides"], cfg) >= cfg["min_edge"]:
                    naked = (chain[k1][side]["bid"] - opt_cost(chain[k1][side]["bid"], cfg)) * cfg["point_value"]
                    it["naked_per_10k"] = round(naked / cfg["txo_b"] * 10000, 2)
                    out.append(it)
    return out


def limited_risk(chain, exp, settle, n, lo, hi, prev, cfg, rets=None):
    out = []
    mv = cfg["spread_min_move"]
    for side in "CP":
        ks = _otm_strikes(chain, side)
        if side == "C":
            shorts = [k for k in ks if prev * (1 + mv) <= k <= hi]
        else:
            shorts = [k for k in ks if lo <= k <= prev * (1 - mv)]
        for k1 in shorts:
            for k2 in ks:
                w = (k2 - k1) if side == "C" else (k1 - k2)
                if not (0 < w <= cfg["spread_max_width"]):
                    continue
                it = build("有限風險價差賣方", exp, settle, n, [(side, k1, -1), (side, k2, 1)], chain, lo, hi, prev, cfg,
                           f"賣出點在區間 {lo:,.0f}～{hi:,.0f} 內；最大虧損以區間內最壞情況計算", rets)
                if not it or it["worst_pts"] >= 0:
                    continue
                be = it["breakeven_move"]
                if be is None or abs(be) < mv * 100:
                    continue
                if it["best_pts"] + fee_floor_gain(it["best_sides"], cfg) >= cfg["min_edge"]:
                    out.append(it)
    return out


def directional(chain, exp, settle, n, lo, hi, prev, ref, up: bool, cfg):
    out = []
    side = "C" if up else "P"
    ks = _otm_strikes(chain, side)
    if up:
        firsts = [k for k in ks if ref * 0.99 <= k <= ref * 1.03]
    else:
        firsts = [k for k in ks if ref * 0.97 <= k <= ref * 1.01]
    for k1 in firsts:
        for k2 in ks:
            w = (k2 - k1) if up else (k1 - k2)
            if not (0 < w <= cfg["spread_max_width"]):
                continue
            it = build("漲跌停日方向性價差", exp, settle, n, [(side, k1, 1), (side, k2, -1)], chain, lo, hi, prev, cfg,
                       ("期貨鎖漲停，順勢買多頭買權價差" if up else "期貨鎖跌停，順勢買空頭賣權價差")
                       + "；最大虧損＝付出的權利金。方向性交易，不是套利")
            if it and it["best_pts"] > 0:
                out.append(it)
    return out


def _top(items, key, n):
    return sorted(items, key=key, reverse=True)[:n]


def scan_spreads(date: dt.date, chains: dict, exp_dates: dict, taiex: dict | None, sessions: dict,
                 fwd: dict, cfg: dict, hist: list[float] | None = None) -> dict | None:
    """chains：{(到期別, 時段): chain}；fwd：{到期別: 一般時段隱含期貨價}。只用一般時段。"""
    if not taiex or not taiex.get("close"):
        return {"error": "取不到加權指數收盤，無法計算結算價上下限"} if chains else None
    prev = taiex["close"]
    st = (sessions.get("一般") or {}).get("status", "NORMAL")
    up, down = st in UP_EVENTS, st in DOWN_EVENTS
    top = cfg["spread_top"]
    groups = {"zero": [], "limited": [], "directional": []}
    eligible = []
    for (exp, sess), ch in chains.items():
        t = exp_dates.get(exp)
        if sess != "一般" or not t or t <= date:
            continue
        n = sessions_until(date, t)
        if n < 1:
            continue
        lo, hi = bounds(prev, n, cfg)
        if n <= cfg["spread_max_sessions"]:
            eligible.append({"expiry": exp, "settle_date": t.isoformat(), "sessions": n, "lo": round(lo), "hi": round(hi)})
            rets = n_session_returns(hist, n)
            z = zero_risk(ch, exp, t, n, lo, hi, prev, cfg, rets)
            for side in "CP":
                groups["zero"] += _top([x for x in z if x["legs"][0][0] == side], lambda x: x["per_10k"] or 0, top)
            lim = limited_risk(ch, exp, t, n, lo, hi, prev, cfg, rets)
            # 有歷史分布時依「每萬元平均損益」排序，否則依報酬風險比
            rank = (lambda x: x["ev_per_10k"]) if rets and len(rets) >= cfg["spread_min_hist"] else \
                   (lambda x: x["reward_risk"] or 0)
            for side in "CP":
                groups["limited"] += _top([x for x in lim if x["legs"][0][0] == side], rank, top)
        if (up or down) and n <= cfg["dir_max_sessions"]:
            ref = fwd.get(exp) or prev
            d = directional(ch, exp, t, n, lo, hi, prev, ref, up, cfg)
            groups["directional"] += _top(d, lambda x: x["reward_risk"] or 0, top)
    for lst in groups.values():
        for it in lst:
            it["above_min"] = it["best_pts"] >= cfg["min_edge"] if it["kind"] != "漲跌停日方向性價差" else True
    return {"prev_close": prev, "taiex_source": taiex.get("source"), "market_status": st,
            "eligible": eligible, "hist_days": len(hist or []), **groups}


# ---------------------------------------------------------------- 結算後回填
def realize(item: dict, S: float, cfg: dict, settled_on: str | None = None) -> None:
    legs = [tuple(x) for x in item["legs"]]
    if settled_on and settled_on != item.get("settle_date"):
        item["postponed_to"] = settled_on      # 臨時休市順延：進場時算的上下限不再保證
    itm = itm_count(legs, S)
    pnl = item["net_pts"] + payoff(legs, S) - item["open_cost"] - settle_cost(S, cfg, itm)
    item["realized_pts"] = round(pnl, 2)
    item["realized_ntd"] = round(pnl * cfg["point_value"])
    item["realized_sides"] = item["open_sides"] + itm
    item["settled_at"] = S


def spread_summary(sp: dict | None) -> dict | None:
    """每種策略：達門檻筆數、首選（每個序列排名第一）的最好情況與實際損益合計。"""
    if not sp or "zero" not in sp:
        return None
    out = {}
    for g in ("zero", "limited", "directional"):
        lst = [x for x in sp[g] if x.get("above_min", True)]
        firsts, seen = [], set()
        for x in lst:
            key = (x["expiry"], x["legs"][0][0])
            if key not in seen:
                seen.add(key)
                firsts.append(x)
        real = [x["realized_ntd"] for x in firsts if x.get("realized_pts") is not None]
        out[g] = {"n": len(lst), "picks": len(firsts),
                  "best_ntd": sum(x["best_ntd"] for x in firsts),
                  "worst_ntd": sum(x["worst_ntd"] for x in firsts),
                  "realized_ntd": sum(real) if real else None, "settled": len(real),
                  "losses": sum(1 for r in real if r < 0)}
    return out


def settle_pending(data_dir: Path, settle_date: dt.date, finals: dict, cfg: dict, dump) -> int:
    """finals：{到期別: 最後結算價}。回填 settle_date 之前 10 天內、尚未結算的價差訊號，並更新 index 摘要。"""
    daily, n_done = data_dir / "daily", 0
    idx_p = data_dir / "index.json"
    idx = json.loads(idx_p.read_text("utf-8")) if idx_p.exists() else None
    for back in range(1, 11):
        d = settle_date - dt.timedelta(days=back)
        p = daily / f"{d.isoformat()}.json"
        if not p.exists():
            continue
        rep = json.loads(p.read_text("utf-8"))
        sp = rep.get("spreads")
        if not sp or "zero" not in sp:
            continue
        changed = False
        for g in ("zero", "limited", "directional"):
            for it in sp[g]:
                if it["expiry"] in finals and it.get("realized_pts") is None:
                    realize(it, finals[it["expiry"]], cfg, settle_date.isoformat())
                    changed, n_done = True, n_done + 1
        if changed:
            p.write_text(dump(rep), "utf-8")
            if idx:
                for day in idx["days"]:
                    if day["date"] == d.isoformat():
                        day["spreads"] = spread_summary(sp)
    if idx and n_done:
        idx_p.write_text(dump(idx), "utf-8")
    return n_done
