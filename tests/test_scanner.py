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


def test_catch_up_reruns_missing_and_night_only_days(tmp_path, monkeypatch):
    import json
    import scanner.__main__ as m
    (tmp_path / "index.json").write_text(json.dumps({"days": [
        {"date": "2026-11-09", "status": {"一般": "NORMAL", "盤後": "NORMAL"}},
        {"date": "2026-11-06", "status": {"盤後": "NORMAL"}},          # 只有夜盤，日盤排程被丟棄
    ]}), "utf-8")
    calls = []
    monkeypatch.setattr(m, "DATA", tmp_path)
    monkeypatch.setattr(m, "run_date", lambda d, *a, **k: calls.append(d))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m.catch_up(dt.date(2026, 11, 11), load_config())
    assert calls == [dt.date(2026, 11, 10), dt.date(2026, 11, 6), dt.date(2026, 11, 5), dt.date(2026, 11, 4)]


# ---------------------------------------------------------------- 報價可靠性、跨週別
def _q(b, a, vol=10, oi=10):
    return {"bid": b, "ask": a, "last": None, "settle": None, "vol": vol, "oi": oi}


def test_quality_reasons():
    from scanner.checks import quality_reasons
    cfg = load_config()
    assert quality_reasons([_q(0.1, 0.3), _q(200, 205)], 500, cfg) == []
    assert quality_reasons([_q(0.2, 20)], None, cfg) == ["有腳買賣價差過寬"]          # 20 − 0.2 > 10 點
    assert quality_reasons([_q(1000, 1200)], None, cfg) == []                       # 200 < 1100 × 30%
    assert quality_reasons([_q(3, 4, vol=0, oi=0)], None, cfg) == ["有腳當日無成交也無未平倉"]
    assert quality_reasons([], 9, cfg) == ["期貨腳當日僅成交 9 口"]


def test_doubtful_signals_not_counted():
    from scanner.pipeline import is_actionable, summary
    fut, opt = load("synthetic_limitup")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    for s in rep["signals"][:3]:
        s["quality"], s["quality_reasons"] = "存疑", ["有腳買賣價差過寬"]
    n_ok = sum(is_actionable(s) for s in rep["signals"])
    sm = summary(rep)
    assert sm["n_feasible"] == n_ok and sm["n_doubtful"] >= 1


def test_calendar_detects_near_dearer_than_far():
    from scanner.checks import scan_calendar
    cfg = load_config()
    near = {44000.0: {"C": _q(20, 21)}, 36000.0: {"P": _q(8, 9)}, 40000.0: {"C": _q(300, 305)}}
    far = {44400.0: {"C": _q(5, 6)}, 44500.0: {"C": _q(4, 5)}, 36300.0: {"P": _q(12, 13)}, 36400.0: {"P": _q(14, 15)}}
    sig = scan_calendar(near, far, "202611W2", "202611F2", "一般", 40000, 40400, cfg, hold_days=1)
    by = {s["type"]: s for s in sig}
    c = by["Call 跨週別"]                         # 44000 × 1.01 = 44440 → 遠週取 ≤ 44440 最高的 44400
    assert "C44400@6" in c["legs"] and c["edge_pts"] > 3 and c["nature"] == "跨週別" and not c["riskless"]
    assert "Put 跨週別" not in by                 # 36000 × 1.01 = 36360 → 遠週取 36400，賣 8 買 15 → 不成立
    assert all(s["strikes"] != [40000.0] for s in sig)   # 價平不在預設檢查範圍



def test_fee_model_35_per_side():
    """選擇權、小台每口單邊 35 元；持有到結算時價內腳再收手續費＋結算稅，價外歸零不收。"""
    from scanner.checks import opt_cost, settle_cost
    cfg = load_config()
    assert cfg["opt_fee"] == 35 and cfg["fut_fee"] == 35
    assert abs(opt_cost(10, cfg) - (0.7 + 0.01)) < 1e-9                  # 35/50 + 10 × 0.1%
    assert abs(settle_cost(42500, cfg, 1) - (0.7 + 0.85)) < 1e-9         # 手續費 0.7 點＋結算稅 42500 × 2e-5
    sig = scan_chain({44000.0: {"C": _q(10, 11)}, 44100.0: {"C": _q(15, 16)}}, "W", "一般", 40000, "NORMAL", cfg)
    mono = next(s for s in sig if s["type"] == "Call 單調性")
    assert (mono["opt_sides"], mono["fut_sides"]) == (4, 0)              # 開倉 2 口＋結算最多 2 口價內
    # 毛利 15 − 11 = 4；開倉 2 × 0.7 + 稅 0.026、結算 2 × (0.7 + 0.8) → 約 −0.43，35 元未達門檻；
    # 手續費降到 10 元（4 次 × 0.5 點 = +2）時為 1.57 ≥ 1，所以仍保存給前端
    assert -0.5 < mono["edge_pts"] < -0.4 and mono["above_min"] is False


def test_expiry_sell_charges_settlement_only_when_itm():
    from scanner.expiry_sell import analyze_expiry
    taiex = {"prev_close": 40000.0, "close": 43900.0, "high": 43950.0, "low": 40100.0,
             "pct": 9.75, "high_pct": 9.88, "low_pct": 0.25}
    opt = _opt_rows("202611W2", [(44200, "C", 0.8, 1.5, 120, 0.0), (43000, "C", 900, 1500, 10, 870.0),
                                 (39500, "P", 3.0, 25.0, 300, 0.0)])
    es = analyze_expiry(dt.date(2026, 11, 11), opt, {"202611W2": dt.date(2026, 11, 11)}, taiex, load_config())
    z = es["series"][0]["zones"]["zero"][0]
    assert z["opt_sides"] == 1 and abs(z["pnl_last"] - (0.8 - 0.7 - 0.0008)) < 0.01   # 價外歸零只付開倉


# ---------------------------------------------------------------- 策略三：風險可控價差
def test_spread_math_and_bounds():
    from scanner.spreads import bounds, range_eval, sessions_until, n_session_returns
    cfg = load_config()
    assert sessions_until(dt.date(2026, 11, 10), dt.date(2026, 11, 11)) == 1
    assert sessions_until(dt.date(2026, 11, 12), dt.date(2026, 11, 16)) == 2      # 週四 → 週一（跨週末）
    lo, hi = bounds(40000, 1, cfg)
    assert round(lo) == 35928 and round(hi) == 44088
    legs = [("C", 43000, -1), ("C", 43200, 1)]
    (w, _), (b, _), itm = range_eval(legs, lo, hi)
    assert (w, b, itm) == (-200, 0, 2)
    legs = [("C", 43500, -1), ("C", 44500, 1)]                    # 保護腳在上限之外 → 最大虧損只到上限
    (w, _), _, itm = range_eval(legs, lo, hi)
    assert round(w) == -(44088 - 43500) and itm == 1
    r = n_session_returns([0.01, -0.02, 0.03], 2)
    assert [round(x, 4) for x in r] == [round(1.01 * 0.98 - 1, 4), round(0.98 * 1.03 - 1, 4)]


def _chain(rows):
    return {float(k): {s: _q(b, a)} for k, s, b, a in rows}


def test_zero_risk_spread_and_naked_comparison():
    from scanner.spreads import zero_risk, bounds
    cfg = load_config()
    lo, hi = bounds(40000, 1, cfg)
    ch = _chain([(44200, "C", 3.0, 3.5), (44300, "C", 0.5, 1.0), (35800, "P", 0.2, 0.3)])
    ch[35700.0] = {"P": _q(0.1, 2.0)}
    z = zero_risk(ch, "W", dt.date(2026, 11, 11), 1, lo, hi, 40000, cfg)
    assert len(z) == 1                                             # Put 那組淨收 0.2 − 2.0 < 0，不列
    x = z[0]
    assert x["desc"] == "賣C44200@3、買C44300@1" and x["worst_pts"] == x["best_pts"] > 0
    assert x["capital"] == 100 * 50 - 2 * 50 and x["per_10k"] > 4 * x["naked_per_10k"]   # 保證金 4,900 vs 裸賣 94,000


def test_limited_risk_requires_move_and_reports_ev():
    from scanner.spreads import limited_risk, bounds
    cfg = load_config()
    lo, hi = bounds(40000, 1, cfg)
    ch = _chain([(41000, "C", 60, 61), (41200, "C", 40, 41), (42000, "C", 20, 21), (42200, "C", 10, 11)])
    rets = [0.0] * 150 + [0.08] * 2
    out = limited_risk(ch, "W", dt.date(2026, 11, 11), 1, lo, hi, 40000, cfg, rets)
    shorts = {x["legs"][0][1] for x in out}
    assert 41000 not in shorts and 42000 in shorts                 # 41000 只差 2.5%，未達 3% 門檻
    x = next(x for x in out if x["legs"][0][1] == 42000 and x["legs"][1][1] == 42200)
    assert x["worst_pts"] < 0 < x["best_pts"] and x["breakeven_move"] > 5
    assert x["loss_prob"] == round(2 / 152 * 100, 1) and x["ev_pts"] is not None


def test_directional_only_on_limit_days():
    from scanner.spreads import scan_spreads
    cfg = load_config()
    ch = {("W", "一般"): _chain([(40000, "C", 300, 310), (40500, "C", 120, 125), (41000, "C", 40, 42)])}
    exps = {"W": dt.date(2026, 11, 13)}
    tx = {"close": 40000.0}
    normal = scan_spreads(dt.date(2026, 11, 11), ch, exps, tx, {"一般": {"status": "NORMAL"}}, {"W": 40000}, cfg)
    assert normal["directional"] == []
    up = scan_spreads(dt.date(2026, 11, 11), ch, exps, tx, {"一般": {"status": "LIMIT_UP"}}, {"W": 40200}, cfg)
    assert up["directional"] and all(x["legs"][0][2] == 1 and x["legs"][0][0] == "C" for x in up["directional"])
    assert all(x["worst_pts"] < 0 and x["best_pts"] > 0 for x in up["directional"])


def test_settle_pending_backfills_realized(tmp_path):
    import json
    from scanner.spreads import settle_pending, spread_summary
    cfg = load_config()
    it = {"kind": "有限風險價差賣方", "expiry": "W", "legs": [["C", 42000, -1], ["C", 42200, 1]],
          "net_pts": 10.0, "open_cost": 1.42, "open_sides": 2, "best_ntd": 429, "worst_ntd": -9571,
          "best_pts": 8.58, "worst_pts": -191.4, "above_min": True, "realized_pts": None}
    rep = {"spreads": {"zero": [], "limited": [it], "directional": []}}
    (tmp_path / "daily").mkdir()
    (tmp_path / "daily" / "2026-11-10.json").write_text(json.dumps(rep), "utf-8")
    (tmp_path / "index.json").write_text(json.dumps({"days": [{"date": "2026-11-10"}]}), "utf-8")
    dump = lambda o: json.dumps(o, ensure_ascii=False)
    assert settle_pending(tmp_path, dt.date(2026, 11, 11), {"W": 42100.0}, cfg, dump) == 1
    got = json.loads((tmp_path / "daily" / "2026-11-10.json").read_text("utf-8"))["spreads"]["limited"][0]
    # 收 10 點、結算 42100 → 賣方賠 100 點；開倉成本 1.42、結算時 1 口價內：0.7 + 42100 × 2e-5
    assert got["realized_pts"] == round(10 - 100 - 1.42 - (0.7 + 42100 * 2e-5), 2) and got["realized_sides"] == 3
    idx = json.loads((tmp_path / "index.json").read_text("utf-8"))
    assert idx["days"][0]["spreads"]["limited"]["realized_ntd"] == got["realized_ntd"]


def test_parse_final_settlement_page():
    html = """<table><tr><th>最後結算日</th><th>契約月份</th><th>臺指選擇權（TXO）</th></tr>
    <tr><td align="center">2026/11/06</td><td align="center">202611F1</td><td align="right">39824</td><td>-</td></tr>
    <tr><td>2026/11/04</td>
        <td>202611W1</td>   <td>42,978</td></tr>
    <tr><td>2026/10/21</td><td>202610</td><td>46650</td><td>2911.85</td></tr></table>"""
    got = taifex.parse_fsp(html)
    assert got == {"202611F1": {"date": "2026-11-06", "price": 39824.0},
                   "202611W1": {"date": "2026-11-04", "price": 42978.0},
                   "202610": {"date": "2026-10-21", "price": 46650.0}}


def test_expiry_sell_prefers_official_final_settlement():
    from scanner.expiry_sell import analyze_expiry
    taiex = {"prev_close": 43307.54, "close": 42948.91, "high": 43741.54, "low": 41855.82,
             "pct": -0.83, "high_pct": 1.0, "low_pct": -3.35}
    opt = _opt_rows("202611W1", [(48000, "C", 0.3, 0.5, 10, 0.0), (42000, "C", 950, 1000, 5, 0.0)])
    es = analyze_expiry(dt.date(2026, 11, 4), opt, {"202611W1": dt.date(2026, 11, 4)}, taiex, load_config(),
                        {"202611W1": {"date": "2026-11-04", "price": 42978.0}})
    s = es["series"][0]
    assert s["final_settle"] == 42978.0 and not s["settle_approx"] and s["settle_source"] == "期交所最後結算價"


def test_parse_final_settlement_uppercase_tags():
    html = ('<TR bgcolor="#FFFFFF" class="12bk">\r\n\t\t<TD width="14%" align=middle>2026/11/06</TD>\r\n'
            '\t\t<TD width="10%" align=middle>202611F1</TD>\r\n\t<TD width="10%" align=right>39824</TD>\r\n'
            '\t<TD width="10%" align=right>-</TD></TR>')
    assert taifex.parse_fsp(html) == {"202611F1": {"date": "2026-11-06", "price": 39824.0}}


def test_postponed_settlement_uses_official_date_without_lookahead():
    """10/23 臨時休市、F4 順延到 10/27：10/27 當天要認得 F4 結算；10/22 回測時不可預知順延。"""
    from scanner.pipeline import analyze
    fut, opt = load("taifex_20261110")
    fsp = {"202611F2": {"date": "2026-11-16", "price": 40000.0}}       # 假設順延到下週一
    rep_before = analyze(dt.date(2026, 11, 10), fut, opt, load_config(), fsp=fsp)
    f2 = next(s for s in rep_before["series"] if s["expiry"] == "202611F2" and s["session"] == "一般")
    assert f2["opt_settle"] == "2026-11-13" and rep_before["final_settlements"] == {}
    rep_on = analyze(dt.date(2026, 11, 16), fut, opt, load_config(), fsp=fsp)
    assert rep_on["final_settlements"] == {"202611F2": 40000.0}


def test_split_by_date_and_bulk_archive(tmp_path, monkeypatch):
    text = ("交易日期,契約,到期月份(週別),收盤價\n2015/06/01,TX,201506,9700\n"
            "2015/06/02,TX,201506,9650\n2015/06/02,TX,201507,9640\n")
    parts = taifex.split_by_date(text)
    assert sorted(parts) == [dt.date(2015, 6, 1), dt.date(2015, 6, 2)]
    assert parts[dt.date(2015, 6, 2)].count("\n") == 3 and parts[dt.date(2015, 6, 2)].startswith("交易日期")
    calls = []

    def fake(kind, start, retries=3, end=None):
        calls.append((kind, start, end))
        return text if start.month == 6 else (_ for _ in ()).throw(taifex.NoData("x"))
    monkeypatch.setattr(taifex, "download", fake)
    monkeypatch.setattr(taifex.time, "sleep", lambda s: None)
    days = taifex.bulk_archive(dt.date(2015, 6, 1), dt.date(2015, 7, 3), tmp_path)
    assert days == [dt.date(2015, 6, 1), dt.date(2015, 6, 2)]
    assert ("TXO", dt.date(2015, 6, 1), dt.date(2015, 6, 30)) in calls and len(calls) == 6   # 2 個月 × 3 商品
    assert (tmp_path / "2015" / "20150602_TXO.csv.gz").exists()


def test_parse_without_session_column():
    df = taifex.parse("交易日期,契約,到期月份(週別),收盤價\n2015/06/01,TX,201506,9700\n")
    assert df["session"].tolist() == ["一般"]
