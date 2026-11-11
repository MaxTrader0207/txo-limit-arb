"""無模型套利檢查。所有價格以「可成交價」計算：買用賣價(ask)、賣用買價(bid)，單位為點。

TXO 為歐式、現金結算，故 Box / 垂直 / 蝴蝶 的到期報酬是確定的；
期貨平價（Conversion / Reversal）用小台 MTX 對沖（每點 50 元，與 TXO 1:1）。
小台有週三到期的週契約，與週三週選同日、同價結算；週五週選則沒有同日結算的期貨。
每個訊號附上估算的所需資金（保證金＋支付的權利金）與每萬元報酬。

成本模型（持有到結算，照最壞情況）：
- 開倉：每口選擇權、小台各收一次手續費；選擇權稅＝權利金 × 0.1%，期貨稅＝契約價值 × 十萬分之二
- 結算：價內的選擇權每口再收一次手續費＋稅（結算價 × 十萬分之二）；價外歸零不收。
  期貨持有到結算再收一次手續費＋稅。哪些腳會價內事前不知道，所以取該組合最多可能價內的口數。
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


def settle_cost(ref, cfg, n_opt: int = 1, n_fut: int = 0) -> float:
    """結算時的成本（點）：價內選擇權每口手續費＋結算稅、期貨每口手續費＋期貨稅。"""
    pv = cfg["point_value"]
    return (n_opt * (cfg["opt_fee"] / pv + cfg["settle_tax"] * ref)
            + n_fut * (cfg["fut_fee"] / pv + cfg["fut_tax"] * ref))


def quality_reasons(quotes, fut_vol, cfg) -> list[str]:
    """收盤最佳買賣價是收盤當下仍在簿上的委託，但可能只是單筆零星掛單。以下情況標為「存疑」：
    某腳當日無成交且無未平倉、某腳買賣價差過寬（多半不是造市報價）、期貨腳成交過少。"""
    r = []
    for q in quotes:
        if not q:
            continue
        if (q.get("vol") or 0) <= 0 and (q.get("oi") or 0) <= 0:
            r.append("有腳當日無成交也無未平倉")
        b, a = q.get("bid"), q.get("ask")
        if ok(b) and ok(a) and a - b > max(cfg["max_spread_pts"], cfg["max_spread_pct"] * (a + b) / 2):
            r.append("有腳買賣價差過寬")
    if fut_vol is not None and fut_vol < cfg["min_fut_volume"]:
        r.append(f"期貨腳當日僅成交 {fut_vol:.0f} 口")
    return list(dict.fromkeys(r))


class Collector:
    def __init__(self, exp, sess, cfg, hold_days, ref):
        self.exp, self.sess, self.cfg, self.hold, self.ref = exp, sess, cfg, hold_days, ref
        self.items: dict[str, list] = {}

    def add(self, kind, legs, edge, cost, *, capital=0.0, feasible=True, note="", deep=False, strikes=(),
            riskless=True, nature=None, quotes=(), fut_vol=None, n_opt=2, settle_opt=0, n_fut=0):
        """edge、cost：扣掉開倉成本後的值；這裡再扣結算成本。
        n_opt：開倉的選擇權口數；settle_opt：結算時最多可能價內的口數；n_fut：期貨口數（持有到結算）。
        也保存「手續費次數」，讓前端可依使用者輸入的手續費重算。"""
        cfg, pv = self.cfg, self.cfg["point_value"]
        sc = settle_cost(self.ref, cfg, settle_opt, n_fut)
        edge, cost = edge - sc, cost + sc
        opt_sides, fut_sides = n_opt + settle_opt, 2 * n_fut
        # 保存門檻用「最低手續費」計算，前端調低手續費時才看得到原本差一點的訊號
        floor_gain = (opt_sides * (cfg["opt_fee"] - cfg["fee_floor"]) + fut_sides * (cfg["fut_fee"] - cfg["fee_floor"])) / pv
        if edge + max(floor_gain, 0) < cfg["min_edge"]:
            return
        edge_ntd = round(edge * pv)
        reasons = quality_reasons(quotes, fut_vol, cfg)
        self.items.setdefault(kind, []).append({
            "type": kind, "expiry": self.exp, "session": self.sess, "legs": legs,
            "edge_pts": round(edge, 2), "edge_ntd": edge_ntd,
            "cost_pts": round(cost, 2), "opt_sides": opt_sides, "fut_sides": fut_sides, "capital": round(capital),
            "above_min": edge >= cfg["min_edge"], "feasible": feasible, "note": note,
            "deep_otm": deep, "strikes": [float(k) for k in strikes], "riskless": riskless,
            "nature": nature or ("套利" if riskless else "基差"),
            "quality": "存疑" if reasons else "可靠", "quality_reasons": reasons,
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
    col = Collector(exp, sess, cfg, hold_days, F)
    ks = list(chain)

    # 1) 期貨平價 ---------------------------------------------------------------
    riskless = bool(hedge and hedge["matched"])
    sfx = "" if riskless else "（基差）"
    note_ok = SAME_EXP_NOTE if riskless else (basis_note(hedge) if hedge else "")
    # 期貨腳可成交價：買進用賣價、賣出用買價；鎖漲停時沒有賣價，只能以漲停價排隊買（反之亦然）
    f_buy = f_sell = None
    if hedge:
        f_buy = hedge.get("ask") if ok(hedge.get("ask")) else (hedge.get("close") if status == "LIMIT_UP" else None)
        f_sell = hedge.get("bid") if ok(hedge.get("bid")) else (hedge.get("close") if status == "LIMIT_DOWN" else None)
        if hedge.get("ask") is None and hedge.get("bid") is None and "ask" not in hedge:   # 舊資料／測試：只有收盤價
            f_buy = f_sell = F
    for k, q in (chain.items() if hedge else []):
        c, pq = q.get("C"), q.get("P")
        if not c or not pq:
            continue
        deep = call_deep(k) or put_deep(k)
        if f_buy and ok(c["bid"]) and ok(pq["ask"]):          # Conversion：買期、買P、賣C
            cost = oc(c["bid"]) + oc(pq["ask"]) + fut_cost(f_buy, cfg)
            feas = status != "LIMIT_UP"
            col.add("Conversion" + sfx,
                    f"買小台 {hedge['contract']}@{f_buy:g}、買P{k:g}@{pq['ask']:g}、賣C{k:g}@{c['bid']:g}",
                    c["bid"] - pq["ask"] - (f_buy - k) - cost, cost,
                    capital=mg.futures(cfg) + mg.debit(pq["ask"], cfg) + mg.short_opt("C", k, F, cfg),
                    quotes=(c, pq), fut_vol=hedge.get("vol"), n_opt=2, settle_opt=1, n_fut=1,
                    feasible=feas, deep=deep, strikes=(k,), riskless=riskless,
                    note=note_ok if feas else "期貨漲停鎖死，買進期貨需排隊（多半成交不到）")
        if f_sell and ok(c["ask"]) and ok(pq["bid"]):          # Reversal：賣期、賣P、買C
            cost = oc(c["ask"]) + oc(pq["bid"]) + fut_cost(f_sell, cfg)
            feas = status != "LIMIT_DOWN"
            col.add("Reversal" + sfx,
                    f"賣小台 {hedge['contract']}@{f_sell:g}、賣P{k:g}@{pq['bid']:g}、買C{k:g}@{c['ask']:g}",
                    (f_sell - k) - (c["ask"] - pq["bid"]) - cost, cost,
                    capital=mg.futures(cfg) + mg.debit(c["ask"], cfg) + mg.short_opt("P", k, F, cfg),
                    quotes=(c, pq), fut_vol=hedge.get("vol"), n_opt=2, settle_opt=1, n_fut=1,
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
                        w - px - cost, cost, capital=mg.debit(px, cfg), deep=deep, strikes=(k1, k2), quotes=(c1, c2, p1, p2), n_opt=4, settle_opt=2,
                        note="兩組借方價差，只需付權利金；注意深價內腳流動性")
            legs = (c1["bid"], c2["ask"], p2["bid"], p1["ask"])
            if all(ok(x) for x in legs):
                px = c1["bid"] - c2["ask"] + p2["bid"] - p1["ask"]
                cost = sum(oc(x) for x in legs)
                cap = mg.credit_spread(w, c1["bid"] - c2["ask"], cfg) + mg.credit_spread(w, p2["bid"] - p1["ask"], cfg)
                col.add("Box 賣方", f"賣C{k1:g}、買C{k2:g}、賣P{k2:g}、買P{k1:g}，收 {px:.1f} 到期付 {w:g}",
                        px - w - cost, cost, capital=cap, deep=deep, strikes=(k1, k2), quotes=(c1, c2, p1, p2), n_opt=4, settle_opt=2,
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
                                b["bid"] - a["ask"] - cost, cost, capital=mg.debit(a["ask"] - b["bid"], cfg), quotes=(a, b), n_opt=2, settle_opt=2,
                                deep=True, strikes=(k1, k2), note="高履約價 Call 比低履約價貴")
                    if ok(a["bid"]) and ok(b["ask"]):     # C(k1) − C(k2) ≤ k2 − k1；貸方價差
                        cost = oc(a["bid"]) + oc(b["ask"])
                        col.add("Call 價差寬度", f"賣C{k1:g}@{a['bid']:g}、買C{k2:g}@{b['ask']:g}",
                                a["bid"] - b["ask"] - w - cost, cost,
                                capital=mg.credit_spread(w, a["bid"] - b["ask"], cfg), quotes=(a, b), n_opt=2, settle_opt=2,
                                deep=True, strikes=(k1, k2), note="價差權利金大於履約價間距")
                else:
                    if ok(b["ask"]) and ok(a["bid"]):     # P(k2) ≥ P(k1)；買高賣低＝借方價差
                        cost = oc(b["ask"]) + oc(a["bid"])
                        col.add("Put 單調性", f"買P{k2:g}@{b['ask']:g}、賣P{k1:g}@{a['bid']:g}",
                                a["bid"] - b["ask"] - cost, cost, capital=mg.debit(b["ask"] - a["bid"], cfg), quotes=(a, b), n_opt=2, settle_opt=2,
                                deep=True, strikes=(k1, k2), note="低履約價 Put 比高履約價貴")
                    if ok(b["bid"]) and ok(a["ask"]):     # P(k2) − P(k1) ≤ k2 − k1；貸方價差
                        cost = oc(b["bid"]) + oc(a["ask"])
                        col.add("Put 價差寬度", f"賣P{k2:g}@{b['bid']:g}、買P{k1:g}@{a['ask']:g}",
                                b["bid"] - a["ask"] - w - cost, cost,
                                capital=mg.credit_spread(w, b["bid"] - a["ask"], cfg), quotes=(a, b), n_opt=2, settle_opt=2,
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
                        -px - cost, cost, capital=cap, deep=True, strikes=(k1, k2, k3), quotes=(q1, q2, q3), n_opt=4, settle_opt=4,
                        note="蝴蝶到期報酬 ≥ 0 卻可淨收權利金")
    return col.result()


def scan_calendar(near: dict, far: dict, exp1: str, exp2: str, sess: str, F1: float, F2: float, cfg: dict,
                  hold_days: int, all_strikes: bool = False) -> list[dict]:
    """跨週別（同時段、相鄰兩個結算日）：近週選擇權不應比「價外程度相同」的遠週選擇權貴。

    以兩序列的隱含期貨價換算履約價（K2 = K1 × F2 / F1，已反映兩結算日之間的除權息）：
      Call：遠週履約價 K2 ≤ K1 × F2/F1 時，C_遠(K2) ≥ C_近(K1)
      Put ：遠週履約價 K2 ≥ K1 × F2/F1 時，P_遠(K2) ≥ P_近(K1)
    違反時「賣近週、買遠週」：近週結算時，遠週部位價值理論上 ≥ 近週要付的金額，所以結算時至少保住淨收的權利金。
    殘餘風險：兩序列隱含價比例變動（除權息預估改變）、近週結算後需賣出遠週部位的流動性，因此歸為「跨週別」而非無風險套利。
    """
    p = cfg["deep_otm_pct"]
    ratio = F2 / F1
    col = Collector(exp1, sess, cfg, hold_days, F1)
    oc = lambda x: opt_cost(x, cfg)
    note = (f"賣近週、買遠週；履約價依隱含期貨價 {F1:,.0f}→{F2:,.0f} 換算。近週 {hold_days} 天後結算，"
            f"之後需處理遠週部位；除權息旺季（7–8 月）隱含價比例較不穩")
    for side in "CP":
        name = "Call" if side == "C" else "Put"
        k_far = sorted(k for k, q in far.items() if q.get(side))
        for k1, q in near.items():
            q1 = q.get(side)
            if not q1 or not ok(q1["bid"]):
                continue
            deep = k1 >= F1 * (1 + p) if side == "C" else k1 <= F1 * (1 - p)
            if not (deep or all_strikes):
                continue
            target = k1 * ratio
            cands = [k for k in k_far if k <= target + 1e-9] if side == "C" else [k for k in k_far if k >= target - 1e-9]
            if not cands:
                continue
            k2 = max(cands) if side == "C" else min(cands)
            q2 = far[k2][side]
            if not ok(q2["ask"]):
                continue
            cost = oc(q1["bid"]) + oc(q2["ask"])
            col.add(f"{name} 跨週別", f"賣 {exp1} {side}{k1:g}@{q1['bid']:g}、買 {exp2} {side}{k2:g}@{q2['ask']:g}",
                    q1["bid"] - q2["ask"] - cost, cost,
                    capital=mg.short_opt(side, k1, F1, cfg) + mg.debit(q2["ask"], cfg),
                    deep=deep, strikes=(k1,), riskless=False, nature="跨週別", note=note, quotes=(q1, q2),
                    n_opt=2, settle_opt=2)   # 近週價內結算＋之後平倉遠週
    return col.result()
