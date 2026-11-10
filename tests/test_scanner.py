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
    return (taifex.parse(taifex.read_text(FX / f"{name}_TX{ext}")),
            taifex.parse(taifex.read_text(FX / f"{name}_TXO{ext}")))


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
