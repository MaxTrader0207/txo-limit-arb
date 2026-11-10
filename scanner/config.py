"""全域參數。手續費請依自己券商費率調整；也可用環境變數覆寫（例如 OPT_FEE=15）。"""
from __future__ import annotations

import datetime as dt
import os

TPE = dt.timezone(dt.timedelta(hours=8))

DEFAULTS = {
    "limit_pct": 0.10,        # 台指期每日漲跌幅（以前一日結算價計）
    "limit_tol_pts": 1.0,     # 收盤距漲跌停價幾點內視為鎖住
    "near_limit_pct": 0.07,   # 「接近漲跌停」門檻
    "deep_otm_pct": 0.03,     # 深價外：Call K ≥ F×(1+3%)、Put K ≤ F×(1−3%)
    "point_value": 50,        # TXO 與小台 MTX 每點 50 元 → 1:1 對沖
    "opt_fee": 20.0,          # 選擇權每口手續費（元）
    "fut_fee": 25.0,          # 小台每口手續費（元）
    "opt_tax": 0.001,         # 選擇權交易稅：權利金 × 0.1%
    "fut_tax": 0.00002,       # 期貨交易稅：契約價值 × 0.002%
    "slippage_pts": 0.0,      # 每腳額外滑價（點）
    "min_edge": 1.0,          # 扣成本後至少幾點才列為訊號
    "top_n": 10,              # 每種訊號、每個序列最多保留幾筆
    "include_monthly": True,  # 一併掃最近月選（唯一與 TX 同日結算的序列）
}


def load_config(**overrides) -> dict:
    cfg = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        env = os.environ.get(k.upper())
        if env not in (None, ""):
            cfg[k] = type(v)(env) if not isinstance(v, bool) else env.lower() in ("1", "true", "yes")
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg
