"""無模型套利檢查。所有價格以「可成交價」計算：買用賣價(ask)、賣用買價(bid)，單位為點。

TXO 為歐式、現金結算，故 Box / 垂直 / 蝴蝶 的到期報酬是確定的；
期貨平價（Conversion / Reversal）用小台 MTX 對沖（每點 50 元，與 TXO 1:1）。
小台有週三到期的週契約，與週三週選同日、同價結算；週五週選則沒有同日結算的期貨。
每個訊號附上估算的所需資金（保證金＋支付的權利金）與每萬元報酬。
"""
from __future__ import annotations

from . import margin as mg
from .market import ok

# TX、MTX（含週契約）與 TXO（月選、週三週選、週五週選）最後結算價算法相同：
# 到期日證交所收盤前 30 分鐘（13:00–13:30）加權指數之簡單算術平均價
SETTLE_RULE = "到期日 13:00–13:30 加權指數簡單算術平均"
SAME_EXP_NOTE = f"與小台同日、同價結算（{SETTLE_RULE}），到期損益鎖定"


def basis_note(h: dict) -> str:
    return (f"選擇權 {h['opt_settle']:%m/%d} 結算、小台 {h['contract']} 於 {h['settle_date']:%m/%d} 結算，"
            f"選擇權以當日 30 分鐘均價結算後，期貨部位還有 {h['basis_days']} 天未對沖，"
            f"損益取決於屆時期貨與指數的價差，不是無風險套利")


def opt_cost(premium, cfg) -> float:
    return cfg["opt_fee"] / cfg["point_value"] + cfg["opt_tax"] * max(premium or 0, 0) + cfg["slippage_pts"]


def fut_cost(F, cfg) -> float:
    return cfg["fut_fee"] / cfg["point_value"] + cfg["fut_tax"] * F + cfg["slippage_pts"]


class Collector:
    def __init__(self, exp, sess, cfg, hold_days):
        self.exp, self.sess, self.cfg, self.hold = exp, sess, cfg, hold_days
        self.items: dict[str, list] = {}

    def add(self, kind, legs, edge, cost, *, capital=0.0, feasible=True, note="", deep=False, strikes=(),
            riskless=True):
        if edge < self.cfg["min_edge"]:
            return
        edge_ntd = round(edge * self.cfg["point_value"])
        self.items.setdefault(kind, []).append({
            "type": kind, "expiry": self.exp, "session": self.sess, "legs": legs,
            "edge_pts": round(edge, 2), "edge_ntd": edge_ntd,
            "cost_pts": round(cost, 2), "feasible": feasible, "note": note,
            "deep_otm": deep, "strikes": [float(k) for k in strikes], "riskless": riskless,
            "hold_days": self.hold, **mg.returns(edge_ntd, capital, self.hold),
        })

    def result(self):
        out = []
        for lst in self.items.values():
            out += sorted(lst, key=lambda s: -s["edge_pts"])[: self.cfg["top_n"]]
        return out


def scan_chain(chain: dict, exp: str, sess: str, F: float, status: str, cfg: dict,
               all_strikes: bool = False, hedge: dict | None = None, hold_days: int = 1) -> list[dict]:
    """F：對應期貨價（用於深價外分界、期貨平價、價外值）；status：對應期貨的漲跌停狀態；
    hedge：pipeline 配好的對應小台（None 表示沒有可對沖的期貨，跳過期貨平價）；hold_days：持有至選擇權結算的天數。"""
    p = cfg["deep_otm_pct"]
    call_deep = lambda k: k >= F * (1 + p)
    put_deep = lambda k: k <= F * (1 - p)
    oc = lambda x: opt_cost(x, cfg)
    col = Collector(exp, sess, cfg, hold_days)
    ks = list(chain)

    # 1) 期貨平價 ---------------------------------------------------------------
    riskless = bool(hedge and hedge["matched"])
    sfx = "" if riskless else "（基差）"
    note_ok = SAME_EXP_NOTE if riskless else (basis_note(hedge) if hedge else "")
    for k, q in (chain.items() if hedge else []):
        c, pq = q.get("C"), q.get("P")
        if not c or not pq:
            continue
        deep = call_deep(k) or put_deep(k)
        if ok(c["bid"]) and ok(pq["ask"]):          # Conversion：買期、買P、賣C
            cost = oc(c["bid"]) + oc(pq["ask"]) + fut_cost(F, cfg)
            feas = status != "LIMIT_UP"
            col.add("Conversion" + sfx,
                    f"買小台 {hedge['contract']}@{F:g}、買P{k:g}@{pq['ask']:g}、賣C{k:g}@{c['bid']:g}",
                    c["bid"] - pq["ask"] - (F - k) - cost, cost,
                    capital=mg.futures(cfg) + mg.debit(pq["ask"], cfg) + mg.short_opt("C", k, F, cfg),
                    feasible=feas, deep=deep, strikes=(k,), riskless=riskless,
                    note=note_ok if feas else "期貨漲停鎖死，買進期貨需排隊（多半成交不到）")
        if ok(c["ask"]) and ok(pq["bid"]):          # Reversal：賣期、賣P、買C
            cost = oc(c["ask"]) + oc(pq["bid"]) + fut_cost(F, cfg)
            feas = status != "LIMIT_DOWN"
            col.add("Reversal" + sfx,
                    f"賣小台 {hedge['contract']}@{F:g}、賣P{k:g}@{pq['bid']:g}、買C{k:g}@{c['ask']:g}",
                    (F - k) - (c["ask"] - pq["bid"]) - cost, cost,
                    capital=mg.futures(cfg) + mg.debit(c["ask"], cfg) + mg.short_opt("P", k, F, cfg),
                    feasible=feas, deep=deep, strikes=(k,), riskless=riskless,
                    note=note_ok if feas else "期貨跌停鎖死，賣出期貨需排隊（多半成交不到）")

    # 2) Box spread（純選擇權）---------------------------------------------------
    full = [k for k in ks if chain[k].get("C") and chain[k].get("P")]
    for i, k1 in enumerate(full):
        c1, p1 = chain[k1]["C"], chain[k1]["P"]
        for k2 in full[i + 1:]:
            c2, p2 = chain[k2]["C"], chain[k2]["P"]
            w, deep = k2 - k1, call_deep(k2) or put_deep(k1)
            legs = (c1["ask"], c2["bid"], p2["ask"], p1["bid"])
            if all(ok(x) for x in legs):
                px = c1["ask"] - c2["bid"] + p2["ask"] - p1["bid"]
                cost = sum(oc(x) for x in legs)
                col.add("Box 買方", f"買C{k1:g}、賣C{k2:g}、買P{k2:g}、賣P{k1:g}，付 {px:.1f} 到期收 {w:g}",
                        w - px - cost, cost, capital=mg.debit(px, cfg), deep=deep, strikes=(k1, k2),
                        note="兩組借方價差，只需付權利金；注意深價內腳流動性")
            legs = (c1["bid"], c2["ask"], p2["bid"], p1["ask"])
            if all(ok(x) for x in legs):
                px = c1["bid"] - c2["ask"] + p2["bid"] - p1["ask"]
                cost = sum(oc(x) for x in legs)
                cap = mg.credit_spread(w, c1["bid"] - c2["ask"], cfg) + mg.credit_spread(w, p2["bid"] - p1["ask"], cfg)
                col.add("Box 賣方", f"賣C{k1:g}、買C{k2:g}、賣P{k2:g}、買P{k1:g}，收 {px:.1f} 到期付 {w:g}",
                        px - w - cost, cost, capital=cap, deep=deep, strikes=(k1, k2),
                        note="兩組貸方價差，保證金各為履約價差 × 50")

    # 3) 垂直邊界 4) 蝴蝶 — 預設只看深價外翼 ----------------------------------------
    for side in "CP":
        name = "Call" if side == "C" else "Put"
        is_deep = call_deep if side == "C" else put_deep
        wing = [k for k in ks if chain[k].get(side) and (all_strikes or is_deep(k))]
        for i, k1 in enumerate(wing):
            a = chain[k1][side]
            for k2 in wing[i + 1:]:
                b, w = chain[k2][side], k2 - k1
                if side == "C":
                    if ok(a["ask"]) and ok(b["bid"]):     # C(k1) ≥ C(k2)；買低賣高＝借方價差
                        cost = oc(a["ask"]) + oc(b["bid"])
                        col.add("Call 單調性", f"買C{k1:g}@{a['ask']:g}、賣C{k2:g}@{b['bid']:g}",
                                b["bid"] - a["ask"] - cost, cost, capital=mg.debit(a["ask"] - b["bid"], cfg),
                                deep=True, strikes=(k1, k2), note="高履約價 Call 比低履約價貴")
                    if ok(a["bid"]) and ok(b["ask"]):     # C(k1) − C(k2) ≤ k2 − k1；貸方價差
                        cost = oc(a["bid"]) + oc(b["ask"])
                        col.add("Call 價差寬度", f"賣C{k1:g}@{a['bid']:g}、買C{k2:g}@{b['ask']:g}",
                                a["bid"] - b["ask"] - w - cost, cost,
                                capital=mg.credit_spread(w, a["bid"] - b["ask"], cfg),
                                deep=True, strikes=(k1, k2), note="價差權利金大於履約價間距")
                else:
                    if ok(b["ask"]) and ok(a["bid"]):     # P(k2) ≥ P(k1)；買高賣低＝借方價差
                        cost = oc(b["ask"]) + oc(a["bid"])
                        col.add("Put 單調性", f"買P{k2:g}@{b['ask']:g}、賣P{k1:g}@{a['bid']:g}",
                                a["bid"] - b["ask"] - cost, cost, capital=mg.debit(b["ask"] - a["bid"], cfg),
                                deep=True, strikes=(k1, k2), note="低履約價 Put 比高履約價貴")
                    if ok(b["bid"]) and ok(a["ask"]):     # P(k2) − P(k1) ≤ k2 − k1；貸方價差
                        cost = oc(b["bid"]) + oc(a["ask"])
                        col.add("Put 價差寬度", f"賣P{k2:g}@{b['bid']:g}、買P{k1:g}@{a['ask']:g}",
                                b["bid"] - a["ask"] - w - cost, cost,
                                capital=mg.credit_spread(w, b["bid"] - a["ask"], cfg),
                                deep=True, strikes=(k1, k2), note="價差權利金大於履約價間距")
        for k1, k2, k3 in zip(wing, wing[1:], wing[2:]):
            w = k2 - k1
            if abs(w - (k3 - k2)) > 1e-9:
                continue
            q1, q2, q3 = chain[k1][side], chain[k2][side], chain[k3][side]
            if ok(q1["ask"]) and ok(q2["bid"]) and ok(q3["ask"]):
                px = q1["ask"] - 2 * q2["bid"] + q3["ask"]
                cost = oc(q1["ask"]) + 2 * oc(q2["bid"]) + oc(q3["ask"])
                # 蝴蝶＝一組借方價差＋一組貸方價差
                if side == "C":
                    cap = mg.debit(q1["ask"] - q2["bid"], cfg) + mg.credit_spread(w, q2["bid"] - q3["ask"], cfg)
                else:
                    cap = mg.debit(q3["ask"] - q2["bid"], cfg) + mg.credit_spread(w, q2["bid"] - q1["ask"], cfg)
                col.add(f"{name} 蝴蝶凸性", f"買1 {side}{k1:g}、賣2 {side}{k2:g}、買1 {side}{k3:g}，淨收 {-px:.1f}",
                        -px - cost, cost, capital=cap, deep=True, strikes=(k1, k2, k3),
                        note="蝴蝶到期報酬 ≥ 0 卻可淨收權利金")
    return col.result()
