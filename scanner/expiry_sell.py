"""策略二：週選結算日的深價外賣方。

原理
----
TXO 最後結算價 = 結算日 13:00–13:30 加權指數簡單算術平均。加權指數成分股單日漲跌幅 ±10%，
所以結算價必落在 [前日收盤 × 0.9, 前日收盤 × 1.1] 之內。

- 零風險區：Call 履約價 > 前日收盤 × 1.1、Put 履約價 < 前日收盤 × 0.9 → 到期價值必為 0。
  任何結算日都存在，賣到就是純收權利金（扣成本）。
- 低風險區：指數當日漲停（或接近）時，Put 履約價 ≤ 前日收盤（指數須把整天漲幅吐光才會價內）；
  跌停時反之為 Call 履約價 ≥ 前日收盤。風險不是零，但 30 分鐘均價結算會鈍化尾盤急殺。

殘餘風險：上市前 5 日無漲跌幅的新股、暫停交易後恢復等，會讓指數上下限不是絕對，所以加了 hard_buffer_pct。
本模組用收盤後的期交所日資料做「事後驗證」：該序列當天在這兩區實際成交的價格，以最後結算價算損益。
"""
from __future__ import annotations

import math

from .checks import opt_cost
from .market import num


def index_state(info: dict, cfg: dict) -> tuple[str, str]:
    """以加權指數收盤（或盤中高低）漲跌幅判斷漲跌停狀態。指數實際上限略低於 10%，門檻可調。"""
    lim, near = cfg["index_limit_pct"] * 100, cfg["index_near_pct"] * 100
    pct, hp, lp = info.get("pct") or 0, info.get("high_pct") or 0, info.get("low_pct") or 0
    if pct >= lim:
        return "LIMIT_UP", "指數漲停"
    if pct <= -lim:
        return "LIMIT_DOWN", "指數跌停"
    if hp >= lim:
        return "TOUCHED_UP", "盤中觸及漲停"
    if lp <= -lim:
        return "TOUCHED_DOWN", "盤中觸及跌停"
    if pct >= near:
        return "NEAR_UP", "接近漲停"
    if pct <= -near:
        return "NEAR_DOWN", "接近跌停"
    return "NORMAL", "正常"


def bounds(prev_close: float, cfg: dict) -> dict:
    b = cfg["hard_buffer_pct"]
    return {"ceiling": math.floor(prev_close * 1.10 * (1 + b)),     # Call 履約價 > 此值 → 零風險
            "floor": math.ceil(prev_close * 0.90 * (1 - b)),        # Put 履約價 < 此值 → 零風險
            "prev_close": prev_close}


def final_settlement(rows) -> tuple[float | None, float | None]:
    """由到期序列的結算價反推最後結算價：價內 Call 的 K + 結算價（取中位數）。
    結算日當天期交所公布的到期序列結算價即為履約價值，各檔反推應一致；回傳 (結算價, 各檔最大差距)。"""
    vals = sorted(r["k"] + r["settle"] for r in rows if r["side"] == "C" and (r["settle"] or 0) > 0)
    if not vals:
        return None, None
    return vals[len(vals) // 2], round(vals[-1] - vals[0], 2)


def analyze_expiry(date, opt, exp_dates: dict, taiex: dict | None, cfg: dict) -> dict | None:
    expiring = [e for e, d in exp_dates.items() if d == date]
    if not expiring:
        return None
    out = {"date": date.isoformat(), "expiring": expiring, "taiex": taiex, "series": []}
    if not taiex:
        out["error"] = "取不到加權指數資料，無法計算上下限"
        return out
    state, label = index_state(taiex, cfg)
    bd = bounds(taiex["prev_close"], cfg)
    out.update(state=state, state_label=label, bounds=bd)
    prev = taiex["prev_close"]
    up_day = state in ("LIMIT_UP", "TOUCHED_UP", "NEAR_UP")
    dn_day = state in ("LIMIT_DOWN", "TOUCHED_DOWN", "NEAR_DOWN")

    o = opt[(opt["contract"] == "TXO") & (opt["session"] == "一般")]
    for exp in expiring:
        g = o[o["expiry"] == exp]
        rows = []
        for _, r in g.iterrows():
            k = num(r["strike"])
            if k is None:
                continue
            side = "C" if ("買" in r["cp"] or r["cp"].upper().startswith("C")) else "P"
            rows.append({"k": k, "side": side, "last": num(r.get("close")), "high": num(r.get("high")),
                         "vol": num(r.get("volume")) or 0, "settle": num(r.get("settle"))})
        (S, spread), approx, warn = final_settlement(rows), False, []
        if S is not None and spread is not None and spread > 1:
            warn.append(f"各價內 Call 反推結算價相差 {spread} 點，結算價欄位可能不是最後結算價")
        if S is None and taiex.get("close"):
            S, approx = taiex["close"], True        # 後備：以收盤指數近似（非 30 分鐘均價）
        if S is not None and not (prev * 0.9 - 1 <= S <= prev * 1.1 + 1):
            warn.append(f"結算價 {S:,.2f} 超出理論區間 {prev * 0.9:,.0f}～{prev * 1.1:,.0f}，資料可能錯置，損益不可信")
        zones = {"zero": [], "low": []}
        for x in rows:
            zero = (x["side"] == "C" and x["k"] > bd["ceiling"]) or (x["side"] == "P" and x["k"] < bd["floor"])
            low = not zero and ((up_day and x["side"] == "P" and x["k"] <= prev) or
                                (dn_day and x["side"] == "C" and x["k"] >= prev))
            if not (zero or low) or x["vol"] <= 0:
                continue
            payoff = None
            if S is not None:
                payoff = max(0.0, S - x["k"]) if x["side"] == "C" else max(0.0, x["k"] - S)
            item = {**x, "payoff": payoff}
            for tag, px in (("pnl_last", x["last"]), ("pnl_high", x["high"])):
                item[tag] = (round(px - payoff - opt_cost(px, cfg), 2)
                             if px is not None and payoff is not None else None)
            zones["zero" if zero else "low"].append(item)
        stats = {}
        for z, lst in zones.items():
            pnl = [x["pnl_last"] for x in lst if x["pnl_last"] is not None]
            stats[z] = {
                "strikes": len(lst), "volume": sum(x["vol"] for x in lst),
                "premium_ntd": round(sum((x["last"] or 0) * x["vol"] for x in lst) * cfg["point_value"]),
                "max_last": max((x["last"] or 0 for x in lst), default=0),
                "profitable": sum(1 for p in pnl if p > 0), "losing": sum(1 for p in pnl if p < 0),
                "best_pnl": max(pnl, default=None), "worst_pnl": min(pnl, default=None),
            }
        for lst in zones.values():
            lst.sort(key=lambda x: -(x["last"] or 0))
        out["series"].append({"expiry": exp, "final_settle": S, "settle_approx": approx, "warnings": warn,
                              "zones": zones, "stats": stats})
    return out


def live_zones(rt: dict, expiring: list[str], cfg: dict) -> dict:
    """盤中：依即時加權指數給出零風險／低風險可賣的履約價界線。"""
    info = {"pct": rt["pct"], "high_pct": rt["pct"], "low_pct": rt["pct"]}
    state, label = index_state(info, cfg)
    bd = bounds(rt["prev_close"], cfg)
    return {**rt, "expiring": expiring, "state": state, "state_label": label, "bounds": bd,
            "low_zone": ("Put 履約價 ≤ " + f"{rt['prev_close']:,.0f}") if state.endswith("UP") and state != "NORMAL"
            else ("Call 履約價 ≥ " + f"{rt['prev_close']:,.0f}") if state.endswith("DOWN") else None}
