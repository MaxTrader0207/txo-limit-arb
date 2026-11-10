"""單日分析 → 輸出前端用的 JSON。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import taifex
from .checks import scan_chain
from .config import TPE
from .market import LIMIT_EVENTS, build_chains, front_futures, implied_forward, limit_status


def analyze(date: dt.date, fut, opt, cfg: dict, all_strikes: bool = False) -> dict:
    sessions = {s: limit_status(r, cfg) for s, r in front_futures(fut).items()}
    chains = build_chains(opt, cfg["include_monthly"])
    series, signals = [], []
    for (exp, sess), ch in sorted(chains.items()):
        st = sessions.get(sess)
        if not st or st.get("close") is None:
            continue
        F = st["close"]
        fwd = implied_forward(ch, F)
        series.append({
            "expiry": exp, "session": sess, "futures": F, "status": st["status"],
            "implied_fwd": round(fwd, 1) if fwd else None,
            "gap": round(fwd - F, 1) if fwd else None,
            "chain": [{"k": k, "C": q.get("C"), "P": q.get("P")} for k, q in ch.items()],
        })
        signals += scan_chain(ch, exp, sess, F, st["status"], cfg, all_strikes, st.get("expiry"))
    signals.sort(key=lambda s: (not (s["feasible"] and s["riskless"]), not s["feasible"], -s["edge_pts"]))
    return {
        "date": date.isoformat(),
        "generated": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "limit_event": any(s["status"] in LIMIT_EVENTS for s in sessions.values()),
        "sessions": sessions, "series": series, "signals": signals,
        "config": {k: v for k, v in cfg.items()},
    }


def summary(rep: dict) -> dict:
    feas = [s for s in rep["signals"] if s["feasible"] and s["riskless"]]
    best = feas[0] if feas else None
    return {
        "date": rep["date"], "limit_event": rep["limit_event"],
        "status": {k: v["status"] for k, v in rep["sessions"].items()},
        "pct": {k: v.get("pct") for k, v in rep["sessions"].items()},
        "n_signals": len(rep["signals"]), "n_feasible": len(feas),
        "n_basis": sum(1 for s in rep["signals"] if not s["riskless"]),
        "best": {k: best[k] for k in ("type", "expiry", "edge_pts", "legs")} if best else None,
    }


def write(rep: dict, data_dir: Path) -> None:
    daily = data_dir / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)
    (daily / f"{rep['date']}.json").write_text(dump(rep), "utf-8")

    idx_p = data_dir / "index.json"
    idx = json.loads(idx_p.read_text("utf-8")) if idx_p.exists() else {"days": []}
    days = [d for d in idx["days"] if d["date"] != rep["date"]] + [summary(rep)]
    days.sort(key=lambda d: d["date"], reverse=True)
    idx = {"updated": rep["generated"], "latest": days[0]["date"], "days": days}
    idx_p.write_text(dump(idx), "utf-8")


def run_date(date: dt.date, cfg: dict, data_dir: Path, archive_dir: Path,
             mode: str = "refresh", all_strikes: bool = False) -> dict | None:
    try:
        fut, opt = taifex.load(date, archive_dir, mode)
    except taifex.NoData as e:
        print(f"[略過] {e}")
        return None
    rep = analyze(date, fut, opt, cfg, all_strikes)
    if not rep["series"]:
        print(f"[略過] {date} 沒有可分析的週選序列")
        return None
    write(rep, data_dir)
    return rep
