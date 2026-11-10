"""無模型套利檢查。所有價格以「可成交價」計算：買用賣價(ask)、賣用買價(bid)，單位為點。

TXO 為歐式、現金結算，故 Box / 垂直 / 蝴蝶 的到期報酬是確定的；
期貨平價（Conversion / Reversal）用小台 MTX 對沖，但週選與近月期貨到期日不同，含基差風險。
"""
from __future__ import annotations

from .market import ok

BASIS_NOTE = "週選與近月期貨到期日不同，含基差風險"


def opt_cost(premium, cfg) -> float:
    return cfg["opt_fee"] / cfg["point_value"] + cfg["opt_tax"] * max(premium or 0, 0) + cfg["slippage_pts"]


def fut_cost(F, cfg) -> float:
    return cfg["fut_fee"] / cfg["point_value"] + cfg["fut_tax"] * F + cfg["slippage_pts"]


class Collector:
    def __init__(self, exp, sess, cfg):
        self.exp, self.sess, self.cfg = exp, sess, cfg
        self.items: dict[str, list] = {}

    def add(self, kind, legs, edge, cost, *, feasible=True, note="", deep=False, strikes=()):
        if edge < self.cfg["min_edge"]:
            return
        self.items.setdefault(kind, []).append({
            "type": kind, "expiry": self.exp, "session": self.sess, "legs": legs,
            "edge_pts": round(edge, 2), "edge_ntd": round(edge * self.cfg["point_value"]),
            "cost_pts": round(cost, 2), "feasible": feasible, "note": note,
            "deep_otm": deep, "strikes": [float(k) for k in strikes],
        })

    def result(self):
        out = []
        for lst in self.items.values():
            out += sorted(lst, key=lambda s: -s["edge_pts"])[: self.cfg["top_n"]]
        return out


def scan_chain(chain: dict, exp: str, sess: str, F: float, status: str, cfg: dict,
               all_strikes: bool = False) -> list[dict]:
    p = cfg["deep_otm_pct"]
    call_deep = lambda k: k >= F * (1 + p)
    put_deep = lambda k: k <= F * (1 - p)
    oc = lambda x: opt_cost(x, cfg)
    col = Collector(exp, sess, cfg)
    ks = list(chain)

    # 1) 期貨平價 ---------------------------------------------------------------
    for k, q in chain.items():
        c, pq = q.get("C"), q.get("P")
        if not c or not pq:
            continue
        deep = call_deep(k) or put_deep(k)
        if ok(c["bid"]) and ok(pq["ask"]):          # Conversion：買期、買P、賣C
            cost = oc(c["bid"]) + oc(pq["ask"]) + fut_cost(F, cfg)
            feas = status != "LIMIT_UP"
            col.add("Conversion",
                    f"買小台@{F:g}、買P{k:g}@{pq['ask']:g}、賣C{k:g}@{c['bid']:g}",
                    c["bid"] - pq["ask"] - (F - k) - cost, cost, feasible=feas, deep=deep, strikes=(k,),
                    note=BASIS_NOTE if feas else "期貨漲停鎖死，買進期貨需排隊（多半成交不到）")
        if ok(c["ask"]) and ok(pq["bid"]):          # Reversal：賣期、賣P、買C
            cost = oc(c["ask"]) + oc(pq["bid"]) + fut_cost(F, cfg)
            feas = status != "LIMIT_DOWN"
            col.add("Reversal",
                    f"賣小台@{F:g}、賣P{k:g}@{pq['bid']:g}、買C{k:g}@{c['ask']:g}",
                    (F - k) - (c["ask"] - pq["bid"]) - cost, cost, feasible=feas, deep=deep, strikes=(k,),
                    note=BASIS_NOTE if feas else "期貨跌停鎖死，賣出期貨需排隊（多半成交不到）")

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
                        w - px - cost, cost, deep=deep, strikes=(k1, k2),
                        note="歐式現金結算；注意深價內腳流動性與保證金")
            legs = (c1["bid"], c2["ask"], p2["bid"], p1["ask"])
            if all(ok(x) for x in legs):
                px = c1["bid"] - c2["ask"] + p2["bid"] - p1["ask"]
                cost = sum(oc(x) for x in legs)
                col.add("Box 賣方", f"賣C{k1:g}、買C{k2:g}、賣P{k2:g}、買P{k1:g}，收 {px:.1f} 到期付 {w:g}",
                        px - w - cost, cost, deep=deep, strikes=(k1, k2), note="歐式現金結算；注意保證金")

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
                    if ok(a["ask"]) and ok(b["bid"]):     # C(k1) ≥ C(k2)
                        cost = oc(a["ask"]) + oc(b["bid"])
                        col.add("Call 單調性", f"買C{k1:g}@{a['ask']:g}、賣C{k2:g}@{b['bid']:g}",
                                b["bid"] - a["ask"] - cost, cost, deep=True, strikes=(k1, k2),
                                note="高履約價 Call 比低履約價貴")
                    if ok(a["bid"]) and ok(b["ask"]):     # C(k1) − C(k2) ≤ k2 − k1
                        cost = oc(a["bid"]) + oc(b["ask"])
                        col.add("Call 價差寬度", f"賣C{k1:g}@{a['bid']:g}、買C{k2:g}@{b['ask']:g}",
                                a["bid"] - b["ask"] - w - cost, cost, deep=True, strikes=(k1, k2),
                                note="價差權利金大於履約價間距")
                else:
                    if ok(b["ask"]) and ok(a["bid"]):     # P(k2) ≥ P(k1)
                        cost = oc(b["ask"]) + oc(a["bid"])
                        col.add("Put 單調性", f"買P{k2:g}@{b['ask']:g}、賣P{k1:g}@{a['bid']:g}",
                                a["bid"] - b["ask"] - cost, cost, deep=True, strikes=(k1, k2),
                                note="低履約價 Put 比高履約價貴")
                    if ok(b["bid"]) and ok(a["ask"]):     # P(k2) − P(k1) ≤ k2 − k1
                        cost = oc(b["bid"]) + oc(a["ask"])
                        col.add("Put 價差寬度", f"賣P{k2:g}@{b['bid']:g}、買P{k1:g}@{a['ask']:g}",
                                b["bid"] - a["ask"] - w - cost, cost, deep=True, strikes=(k1, k2),
                                note="價差權利金大於履約價間距")
        for k1, k2, k3 in zip(wing, wing[1:], wing[2:]):
            if abs((k2 - k1) - (k3 - k2)) > 1e-9:
                continue
            q1, q2, q3 = chain[k1][side], chain[k2][side], chain[k3][side]
            if ok(q1["ask"]) and ok(q2["bid"]) and ok(q3["ask"]):
                px = q1["ask"] - 2 * q2["bid"] + q3["ask"]
                cost = oc(q1["ask"]) + 2 * oc(q2["bid"]) + oc(q3["ask"])
                col.add(f"{name} 蝴蝶凸性", f"買1 {side}{k1:g}、賣2 {side}{k2:g}、買1 {side}{k3:g}，淨收 {-px:.1f}",
                        -px - cost, cost, deep=True, strikes=(k1, k2, k3),
                        note="蝴蝶到期報酬 ≥ 0 卻可淨收權利金")
    return col.result()
