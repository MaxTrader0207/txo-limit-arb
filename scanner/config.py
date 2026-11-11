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
    "opt_fee": 35.0,          # 選擇權每口單邊手續費（元）：開倉、平倉、價內結算各收一次
    "fut_fee": 35.0,          # 小台每口單邊手續費（元）：開倉、結算各收一次
    "opt_tax": 0.001,         # 選擇權交易稅：權利金 × 0.1%
    "settle_tax": 0.00002,    # 選擇權價內結算稅：結算價 × 十萬分之二
    "fee_floor": 10.0,        # 前端手續費最低可調到此值；後端保存「以此費率仍 ≥ 門檻」的訊號
    "fut_tax": 0.00002,       # 期貨交易稅：契約價值 × 0.002%
    "slippage_pts": 0.0,      # 每腳額外滑價（點）
    "min_edge": 1.0,          # 扣成本後至少幾點才列為訊號
    "top_n": 10,              # 每種訊號、每個序列最多保留幾筆
    "min_fut_volume": 50,     # 對沖期貨當日成交量低於此值時，訊號標為「存疑」
    # 報價可靠性：任一腳符合以下情況，訊號標為「存疑」（預設隱藏、不通知、不計入統計）
    "max_spread_pts": 10.0,   # 買賣價差 > MAX(10 點, 中價 × 30%) 視為過寬
    "max_spread_pct": 0.30,
    # 策略三：風險可控價差
    "spread_max_width": 500,  # 價差最大寬度（點）
    "spread_top": 5,          # 每個序列、每一邊保留前幾組
    "spread_min_move": 0.03,  # 有限風險：賣出點至少距指數收盤 3%，且指數要漲跌超過 3% 才會虧
    "spread_max_sessions": 2, # 價差賣方：距結算最多幾個交易時段（1＝結算前一日）
    "dir_max_sessions": 5,    # 方向性價差：距結算最多幾個交易時段
    "spread_min_hist": 120,
    "ma_periods": [30, 60, 100],   # 均線回檔多頭價差
    "ma_lookback": 10,
    "ma_strike_pct": 0.02,   # 至少要有幾筆歷史漲跌幅，才計算平均損益與虧損機率
    "cal_max_fwd_diff": 0.01, # 跨週別：相鄰兩結算日的隱含期貨價相差 > 1% 視為報價失真，不比較
    "include_monthly": True,  # 一併掃最近月選（唯一與 TX 同日結算的序列）
    # 策略二：結算日深價外賣方
    "index_limit_pct": 0.095, # 加權指數漲跌 ≥ 9.5% 視為漲跌停（成分股皆 ±10%，指數實際上限略低於 10%）
    "index_near_pct": 0.07,   # 加權指數「接近漲跌停」
    "hard_buffer_pct": 0.002, # 指數上下限再外推 0.2%，涵蓋無漲跌幅新股等例外
    # 保證金（原始保證金，元）：期交所股價指數類保證金表，2026/08/12 更新；調整時改這裡或用環境變數覆寫
    "margin_updated": "2026-08-12",
    "mtx_margin": 175250.0,   # 小型臺指（含週到期契約）
    "txo_a": 187000.0,        # 臺指選擇權風險保證金 A 值（週選相同）
    "txo_b": 94000.0,         # 臺指選擇權風險保證金 B 值
}


def load_config(**overrides) -> dict:
    cfg = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        env = os.environ.get(k.upper())
        if env not in (None, ""):
            if isinstance(v, bool):
                cfg[k] = env.lower() in ("1", "true", "yes")
            elif isinstance(v, list):
                cfg[k] = [type(v[0])(x) for x in env.split(",") if x.strip()]
            else:
                cfg[k] = type(v)(env)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg
