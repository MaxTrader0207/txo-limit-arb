"""保證金與資金需求估算（依期交所規則簡化，屬估算值）。

- 選擇權賣方：權利金市值 + MAX(A 值 − 價外值, B 值)；收到的權利金已入帳，
  因此「需另外準備的資金」為 MAX(A − 價外值, B)。
- 選擇權買方：支付權利金。
- 同類型、同到期的價差：貸方價差（收權利金）保證金＝履約價差 × 50，扣掉收到的權利金；借方價差不收保證金。
- 期貨腳：小台原始保證金。
實際以期貨商計收為準（組合保證金、盤中調整、期貨商加收都可能不同）。
"""
from __future__ import annotations


def short_opt(side: str, k: float, ref: float, cfg: dict) -> float:
    pv = cfg["point_value"]
    otm = max(0.0, k - ref) if side == "C" else max(0.0, ref - k)
    return max(cfg["txo_a"] - otm * pv, cfg["txo_b"])


def credit_spread(width: float, credit_pts: float, cfg: dict) -> float:
    pv = cfg["point_value"]
    return max(width * pv - credit_pts * pv, 0.0)


def debit(pts: float, cfg: dict) -> float:
    return max(pts, 0.0) * cfg["point_value"]


def futures(cfg: dict) -> float:
    return cfg["mtx_margin"]


def returns(edge_ntd: float, capital: float, hold_days: int) -> dict:
    """每萬元報酬與年化（資金近乎 0 時不計算）。"""
    if capital < 1000:
        return {"capital_ntd": round(capital), "per_10k": None, "annual_pct": None}
    per = edge_ntd / capital * 10000
    return {"capital_ntd": round(capital), "per_10k": round(per, 1),
            "annual_pct": round(per / 100 * 365 / max(hold_days, 1), 1)}
