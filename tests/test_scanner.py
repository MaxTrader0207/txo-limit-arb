import datetime as dt
from pathlib import Path

from scanner import taifex
from scanner.checks import scan_chain
from scanner.config import load_config
from scanner.market import limit_status
from scanner.pipeline import analyze

FX = Path(__file__).parent / "fixtures"


def load(name):
    ext = ".csv.gz" if name.startswith("taifex") else ".csv"
    fut = taifex.parse(taifex.read_text(FX / f"{name}_TX{ext}"))
    mtx = FX / f"{name}_MTX{ext}"
    if mtx.exists():
        import pandas as _pd
        fut = _pd.concat([fut, taifex.parse(taifex.read_text(mtx))], ignore_index=True)
    return fut, taifex.parse(taifex.read_text(FX / f"{name}_TXO{ext}"))


def test_limit_status_thresholds():
    cfg = load_config()
    base = {"expiry": "202611", "high": None, "low": None}
    assert limit_status({**base, "close": 25300, "chg": 2300}, cfg)["status"] == "LIMIT_UP"
    assert limit_status({**base, "close": 20700, "chg": -2300}, cfg)["status"] == "LIMIT_DOWN"
    assert limit_status({**base, "close": 23500, "chg": 500, "high": 25300}, cfg)["status"] == "TOUCHED_UP"
    assert limit_status({**base, "close": 24700, "chg": 1700}, cfg)["status"] == "NEAR_UP"
    assert limit_status({**base, "close": 23100, "chg": 100}, cfg)["status"] == "NORMAL"


def test_synthetic_limit_up_finds_planted_errors():
    fut, opt = load("synthetic_limitup")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    assert rep["limit_event"]
    types = {s["type"] for s in rep["signals"] if s["feasible"]}
    assert {"Put 單調性", "Call 單調性", "Put 蝴蝶凸性", "Call 蝴蝶凸性"} <= types
    # 期貨鎖漲停、選擇權隱含價更高 → Conversion 看起來有利，但必須標為不可成交
    conv = [s for s in rep["signals"] if s["type"].startswith("Conversion")]
    assert conv and not any(s["feasible"] for s in conv)


def test_taifex_20261110_basis_not_arbitrage():
    """期交所原始資料：近月期貨對週選有正價差，Reversal 看似有利，但到期日不同 → 只能算基差交易。"""
    fut, opt = load("taifex_20261110")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    assert not rep["limit_event"]
    assert {s["session"] for s in rep["series"]} == {"一般", "盤後"}
    assert any(not s["riskless"] for s in rep["signals"])
    assert not [s for s in rep["signals"] if s["riskless"] and s["feasible"]]
    assert all(s["type"].endswith("（基差）") for s in rep["signals"] if not s["riskless"])


def test_each_series_maps_to_the_tx_that_settles_with_it():
    """期交所 11/10 原始資料：每個選擇權序列都要對到「結算日 ≥ 自己」最近的 TX。"""
    fut, opt = load("taifex_20261110")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    m = {(s["expiry"], s["session"]): s for s in rep["series"]}
    expect = {  # 到期別: (選擇權結算日, 對應 TX, TX 結算日, 是否同日)
        "202611W2": ("2026-11-11", "202611", "2026-11-18", False),
        "202611F2": ("2026-11-13", "202611", "2026-11-18", False),
        "202611":   ("2026-11-18", "202611", "2026-11-18", True),
        "202611F3": ("2026-11-20", "202612", "2026-12-16", False),
    }
    for exp, (od, tx, td, same) in expect.items():
        s = m[(exp, "一般")]
        assert (s["opt_settle"], s["hedge"], s["hedge_settle"], s["matched"]) == (od, tx, td, same), exp
    # 只有同日結算的序列可以產生「無風險」期貨平價訊號
    for sig in rep["signals"]:
        if sig["type"].startswith(("Conversion", "Reversal")):
            assert sig["riskless"] == m[(sig["expiry"], sig["session"])]["matched"]


def test_hedge_picks_nearest_later_settlement():
    from scanner.market import hedge_for, third_wednesday
    futs = [{"contract": "202611", "settle_date": dt.date(2026, 11, 18)},
            {"contract": "202612", "settle_date": dt.date(2026, 12, 16)}]
    assert hedge_for(dt.date(2026, 11, 11), futs)["contract"] == "202611"
    assert hedge_for(dt.date(2026, 11, 18), futs)["contract"] == "202611"
    assert hedge_for(dt.date(2026, 11, 20), futs)["contract"] == "202612"
    assert hedge_for(dt.date(2027, 1, 20), futs) is None
    assert third_wednesday("202611") == dt.date(2026, 11, 18)


def test_code_fallback_matches_taifex_dates():
    from scanner.market import code_to_date
    # 與期交所 11/10 CSV 的「契約到期日」一致
    assert code_to_date("202611W2") == dt.date(2026, 11, 11)
    assert code_to_date("202611F2") == dt.date(2026, 11, 13)
    assert code_to_date("202611F3") == dt.date(2026, 11, 20)
    assert code_to_date("202612") == dt.date(2026, 12, 16)


def test_costs_are_charged():
    cfg = load_config(min_edge=-1e9)
    q = lambda b, a: {"bid": b, "ask": a, "last": None, "settle": None, "vol": None, "oi": None}
    chain = {44000.0: {"C": q(10, 11)}, 44100.0: {"C": q(10.5, 12)}}
    sig = scan_chain(chain, "W", "一般", 42000, "NORMAL", cfg)
    mono = [s for s in sig if s["type"] == "Call 單調性"][0]
    # 毛利 10.5 − 11 = −0.5，再扣兩腳手續費 0.8 與稅
    assert mono["edge_pts"] < -1.3


def test_series_settled_today_is_excluded():
    """W2 於 11/11 13:30 結算；以 11/11 為交易日分析時不得再出現在掃描序列中。"""
    fut, opt = load("taifex_20261110")
    rep = analyze(dt.date(2026, 11, 11), fut, opt, load_config())
    assert "202611W2" not in {s["expiry"] for s in rep["series"]}
    assert "202611W2" in {s["expiry"] for s in rep["expired_today"]}
    assert "202611W2" not in {s["expiry"] for s in rep["signals"]}


# ---------------------------------------------------------------- 策略二：結算日深價外賣方
import pandas as pd


def _opt_rows(exp, rows):
    """rows: (K, 'C'/'P', last, high, vol, settle)"""
    return pd.DataFrame([{"contract": "TXO", "session": "一般", "expiry": exp, "strike": k,
                          "cp": "買權" if s == "C" else "賣權", "close": last, "high": hi,
                          "volume": v, "settle": st} for k, s, last, hi, v, st in rows])


def test_twse_month_parser():
    from scanner.twse import parse_month
    js = {"stat": "OK", "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
          "data": [["115/11/09", "40,100.00", "41,500.00", "40,000.00", "41,400.00"],
                   ["115/11/10", "41,500.00", "42,700.00", "41,450.00", "42,600.00"]]}
    m = parse_month(js)
    assert m[dt.date(2026, 11, 10)]["close"] == 42600.0 and len(m) == 2


def test_expiry_sell_zero_and_low_zones_on_limit_up_day():
    from scanner.expiry_sell import analyze_expiry
    cfg = load_config()
    prev = 40000.0
    taiex = {"prev_close": prev, "close": 43900.0, "high": 43950.0, "low": 40100.0,
             "pct": 9.75, "high_pct": 9.88, "low_pct": 0.25}
    opt = _opt_rows("202611W2", [
        (44200, "C", 0.8, 1.5, 120, 0.0),   # > 44000×1.002 → 零風險，賣 0.8 扣成本賺錢
        (44100, "C", 0.2, 0.3, 50, 0.0),    # 零風險但權利金太低，扣成本虧
        (44050, "C", 1.0, 1.0, 40, 0.0),    # 未超過含緩衝上限 44088 → 不列入
        (35800, "P", 0.5, 2.0, 80, 0.0),    # < 36000×0.998 → 零風險
        (39500, "P", 3.0, 25.0, 300, 0.0),  # 漲停日、K ≤ 前收 → 低風險
        (43000, "C", 900, 1500, 10, 870.0), # 價內 Call，用來反推最後結算價 43870
    ])
    es = analyze_expiry(dt.date(2026, 11, 11), opt, {"202611W2": dt.date(2026, 11, 11)}, taiex, cfg)
    assert es["state"] == "LIMIT_UP"
    assert es["bounds"]["ceiling"] == 44088 and es["bounds"]["floor"] == 35928
    s = es["series"][0]
    assert s["final_settle"] == 43870.0 and s["warnings"] == []
    zero = {(x["k"], x["side"]): x for x in s["zones"]["zero"]}
    assert set(zero) == {(44200, "C"), (44100, "C"), (35800, "P")}
    assert zero[(44200, "C")]["pnl_last"] > 0
    assert zero[(44100, "C")]["pnl_last"] < 0              # 0.2 點不夠付 0.4 點手續費
    low = s["zones"]["low"]
    assert [(x["k"], x["side"]) for x in low] == [(39500, "P")] and low[0]["payoff"] == 0


def test_no_low_zone_on_normal_day_and_live_codes():
    from scanner.expiry_sell import analyze_expiry
    from scanner.__main__ import expiring_codes
    taiex = {"prev_close": 40000.0, "close": 40400.0, "high": 40500.0, "low": 39900.0,
             "pct": 1.0, "high_pct": 1.25, "low_pct": -0.25}
    opt = _opt_rows("202611F2", [(39500, "P", 3.0, 5.0, 300, 0.0)])
    es = analyze_expiry(dt.date(2026, 11, 13), opt, {"202611F2": dt.date(2026, 11, 13)}, taiex, load_config())
    assert es["state"] == "NORMAL" and es["series"][0]["zones"]["low"] == []
    assert expiring_codes(dt.date(2026, 11, 11)) == ["202611W2"]
    assert expiring_codes(dt.date(2026, 11, 18)) == ["202611"]
    assert expiring_codes(dt.date(2026, 11, 13)) == ["202611F2"]
    assert expiring_codes(dt.date(2026, 11, 12)) == []


def test_expiry_sell_flags_settlement_outside_bounds():
    from scanner.expiry_sell import analyze_expiry
    taiex = {"prev_close": 38800.0, "close": 42620.0, "high": 42650.0, "low": 39400.0,
             "pct": 9.85, "high_pct": 9.92, "low_pct": 1.55}
    opt = _opt_rows("202611W2", [(43000, "C", 700, 800, 10, 680.0)])   # 反推 43680 > 38800×1.1
    es = analyze_expiry(dt.date(2026, 11, 11), opt, {"202611W2": dt.date(2026, 11, 11)}, taiex, load_config())
    assert any("超出理論區間" in w for w in es["series"][0]["warnings"])


def test_header_only_csv_is_holiday(tmp_path, monkeypatch):
    """休市日期交所回傳只有表頭的 CSV，必須視為非交易日、不得封存。"""
    import pytest
    header = "交易日期,契約,到期月份(週別)\n"
    arc = tmp_path / "2026"; arc.mkdir()
    import gzip
    (arc / "20261009_TX.csv.gz").write_bytes(gzip.compress(header.encode()))
    with pytest.raises(taifex.NoData):
        taifex.load(dt.date(2026, 10, 9), tmp_path, "cache")


# ---------------------------------------------------------------- 小台週契約、保證金、盤中極值
def _with_mtx(fut):
    """在 11/10 原始 TX 資料上加入小台月＋週契約（價格借用 TX 近月）。"""
    tx = fut[(fut["contract"] == "TX") & (fut["expiry"] == "202611")]
    rows = []
    for code in ("202611W2", "202611", "202611W4", "202612"):
        r = tx.copy()
        r["contract"], r["expiry"] = "MTX", code
        rows.append(r)
    return pd.concat([fut, *rows], ignore_index=True)


def test_wednesday_weekly_hedged_by_same_day_mtx_weekly():
    fut, opt = load("taifex_20261110")
    rep = analyze(dt.date(2026, 11, 10), _with_mtx(fut), opt, load_config())
    m = {s["expiry"]: s for s in rep["series"] if s["session"] == "一般"}
    assert (m["202611W2"]["hedge"], m["202611W2"]["matched"]) == ("202611W2", True)    # 週三週選 ↔ 小台週契約
    assert (m["202611"]["hedge"], m["202611"]["matched"]) == ("202611", True)
    assert (m["202611F2"]["hedge"], m["202611F2"]["basis_days"]) == ("202611", 5)       # 週五週選仍無同日期貨
    assert (m["202611F3"]["hedge"], m["202611F3"]["basis_days"]) == ("202611W4", 5)     # 改對 11/25 小台週契約
    assert all(s["hedge_product"] == "MTX" for s in rep["series"])


def test_margin_formulas():
    from scanner import margin as mg
    cfg = load_config()
    assert mg.short_opt("C", 50000, 42000, cfg) == cfg["txo_b"]                  # 深價外 → B 值
    assert mg.short_opt("P", 42000, 42000, cfg) == cfg["txo_a"]                  # 價平 → A 值
    assert mg.credit_spread(100, 30, cfg) == 3500
    r = mg.returns(500, 100000, 5)
    assert r["per_10k"] == 50.0 and r["annual_pct"] == 36.5
    assert mg.returns(500, 0, 5)["per_10k"] is None


def test_intraday_extremes_trigger():
    from scanner.expiry_sell import extreme_sides, index_state
    from scanner.market import limit_status
    cfg = load_config()
    info = {"pct": -4.51, "high_pct": -2.74, "low_pct": -8.99}                  # 2026/11/6 實際走勢
    assert index_state(info, cfg)[0] == "INTRA_NEAR_DOWN"
    assert extreme_sides(info, cfg) == (False, True)
    row = {"expiry": "202611", "close": 40500, "chg": 500, "high": 43000, "low": 39900}
    assert limit_status(row, cfg)["status"] == "INTRA_NEAR_UP"                 # 前結算 40000，盤中 +7.5%


def test_signals_carry_capital():
    fut, opt = load("synthetic_limitup")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    fly = [s for s in rep["signals"] if "蝴蝶" in s["type"]]
    assert fly and all("capital_ntd" in s and s["hold_days"] >= 1 for s in fly)


# ---------------------------------------------------------------- 真實事件回歸測試
def test_20250407_limit_down_reversal_is_not_executable():
    """2025/4/7 台指期跌停鎖死：選擇權隱含價遠低於期貨，Reversal 看似暴利但賣不出期貨。"""
    fut, opt = load("taifex_20250407")
    rep = analyze(dt.date(2025, 4, 7), fut, opt, load_config())
    assert rep["sessions"]["一般"]["status"] == "LIMIT_DOWN"
    day = [s for s in rep["series"] if s["session"] == "一般" and s["matched"]]
    assert day and all(s["gap"] < -1000 for s in day)
    rev = [x for x in rep["signals"] if x["session"] == "一般" and x["type"] == "Reversal"]
    assert rev and not any(x["feasible"] for x in rev)


def test_20261103_stale_weekly_futures_last_price_ignored():
    """2026/11/3 小台 W2 僅成交 9 口、最後成交價 45080 為舊價；應改用買賣報價 43202/43223，不得產生大額假套利。"""
    fut, opt = load("taifex_20261103")
    rep = analyze(dt.date(2026, 11, 3), fut, opt, load_config())
    w2 = next(s for s in rep["series"] if s["expiry"] == "202611W2" and s["session"] == "一般")
    assert w2["fut_last"] == 45080 and abs(w2["futures"] - 43212.5) < 1
    big = [x for x in rep["signals"] if x["riskless"] and x["feasible"] and x["edge_pts"] > 50]
    assert big == []
