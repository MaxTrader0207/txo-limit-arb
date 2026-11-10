import datetime as dt
from pathlib import Path

from scanner import taifex
from scanner.checks import scan_chain
from scanner.config import load_config
from scanner.market import limit_status
from scanner.pipeline import analyze

FX = Path(__file__).parent / "fixtures"


def load(name):
    return (taifex.parse(taifex.read_text(FX / f"{name}_TX.csv")),
            taifex.parse(taifex.read_text(FX / f"{name}_TXO.csv")))


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
    conv = [s for s in rep["signals"] if s["type"] == "Conversion"]
    assert conv and not any(s["feasible"] for s in conv)


def test_real_20261110_is_clean():
    fut, opt = load("real_20261110")
    rep = analyze(dt.date(2026, 11, 10), fut, opt, load_config())
    assert not rep["limit_event"]
    assert rep["signals"] == []
    s = rep["series"][0]
    assert abs(s["gap"]) < 50 and len(s["chain"]) > 200


def test_costs_are_charged():
    cfg = load_config(min_edge=-1e9)
    q = lambda b, a: {"bid": b, "ask": a, "last": None, "settle": None, "vol": None, "oi": None}
    chain = {44000.0: {"C": q(10, 11)}, 44100.0: {"C": q(10.5, 12)}}
    sig = scan_chain(chain, "W", "一般", 42000, "NORMAL", cfg)
    mono = [s for s in sig if s["type"] == "Call 單調性"][0]
    # 毛利 10.5 − 11 = −0.5，再扣兩腳手續費 0.8 與稅
    assert mono["edge_pts"] < -1.3
