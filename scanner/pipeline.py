"""單日分析 → 輸出前端用的 JSON。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import taifex, twse
from .expiry_sell import analyze_expiry
from .checks import scan_chain
from .config import TPE
from .market import (LIMIT_EVENTS, num, ok, build_chains, expiry_dates, front_futures, fut_settle_date, hedge_contracts,
                     hedge_for, implied_forward, limit_status)

KEEP_CHAIN_DAYS = 30   # 只保留最近 N 個交易日的完整選擇權鏈；更舊的可用 rebuild 從封存重算


def analyze(date: dt.date, fut, opt, cfg: dict, all_strikes: bool = False, taiex: dict | None = None) -> dict:
    exp_dates = expiry_dates(opt)

    # 每個時段、每口對沖用期貨（小台月＋週契約）：狀態 + 實際結算日
    futs: dict[str, list[dict]] = {}
    hc = hedge_contracts(fut)
    product = hc["contract"].iloc[0] if len(hc) else "TX"
    for _, r in hc.iterrows():
        st = limit_status(r, cfg)
        if st.get("close") is None:
            continue
        futs.setdefault(r["session"], []).append(
            {**st, "contract": r["expiry"], "settle_date": fut_settle_date(r["expiry"], exp_dates),
             "bid": num(r.get("bid")), "ask": num(r.get("ask")), "vol": num(r.get("volume")) or 0})
    sessions = {s: limit_status(r, cfg) for s, r in front_futures(fut).items()}

    chains = build_chains(opt, cfg["include_monthly"])
    series, signals, expired = [], [], []
    for (exp, sess), ch in sorted(chains.items(), key=lambda x: (exp_dates.get(x[0][0], dt.date.max), x[0][1])):
        opt_settle = exp_dates.get(exp)
        if opt_settle and opt_settle <= date:
            # 到期契約最後交易日 13:30 收盤即以 30 分鐘均價結算、且無盤後時段；
            # 排程在收盤後才跑，此序列已無法交易，不產生訊號
            expired.append({"expiry": exp, "session": sess, "opt_settle": opt_settle.isoformat()})
            continue
        h = hedge_for(opt_settle, futs.get(sess, [])) if opt_settle else None
        if h is None:
            print(f"  [警告] {exp} {sess} 找不到結算日 ≥ {opt_settle} 的期貨，略過")
            continue
        # 期貨腳用可成交報價（買用賣價、賣用買價）；成交稀少的小台週契約「最後成交價」可能是數小時前的舊價
        bid, ask = h["bid"], h["ask"]
        F = (bid + ask) / 2 if ok(bid) and ok(ask) else h["close"]
        # 漲跌停是全市場事件；週契約收盤價可能失真，狀態改用同時段近月台指期
        mkt = sessions.get(sess, h)
        hedge = {"contract": h["contract"], "settle_date": h["settle_date"], "opt_settle": opt_settle,
                 "matched": h["settle_date"] == opt_settle,
                 "basis_days": (h["settle_date"] - opt_settle).days,
                 "bid": bid, "ask": ask, "close": h["close"], "vol": h["vol"]}
        fwd = implied_forward(ch, F)
        series.append({
            "expiry": exp, "session": sess, "opt_settle": opt_settle.isoformat(),
            "hedge": h["contract"], "hedge_product": product, "hedge_settle": h["settle_date"].isoformat(),
            "matched": hedge["matched"], "basis_days": hedge["basis_days"],
            "futures": F, "fut_bid": bid, "fut_ask": ask, "fut_last": h["close"], "fut_vol": h["vol"],
            "status": mkt["status"], "status_label": mkt["label"],
            "implied_fwd": round(fwd, 1) if fwd else None,
            "gap": round(fwd - F, 1) if fwd else None,
            "chain": [{"k": k, "C": q.get("C"), "P": q.get("P")} for k, q in ch.items()],
        })
        signals += scan_chain(ch, exp, sess, F, mkt["status"], cfg, all_strikes, hedge,
                              hold_days=max((opt_settle - date).days, 1))
    signals.sort(key=lambda s: (not (s["feasible"] and s["riskless"]), not s["feasible"], -s["edge_pts"]))
    used = {(s["hedge"], s["status"]) for s in series}
    return {
        "date": date.isoformat(),
        "generated": dt.datetime.now(TPE).isoformat(timespec="seconds"),
        "limit_event": any(s["status"] in LIMIT_EVENTS for s in sessions.values())
                       or any(st in LIMIT_EVENTS for _, st in used),
        "sessions": sessions, "series": series, "signals": signals, "expired_today": expired,
        "expiry_sell": analyze_expiry(date, opt, exp_dates, taiex, cfg),
        "settle_rule": "TX、小台（含週三週契約）與 TXO（月選、週三 W1/W2/W4/W5、週五 F）最後結算價皆為到期日 13:00–13:30 加權指數簡單算術平均",
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
        "expiry": expiry_summary(rep.get("expiry_sell")),
    }


def expiry_summary(es: dict | None) -> dict | None:
    if not es or "state" not in es:
        return None
    z = lambda key, f: sum(s["stats"][key][f] or 0 for s in es["series"])
    best = [s["stats"][k]["best_pnl"] for s in es["series"] for k in ("zero", "low")
            if s["stats"][k]["best_pnl"] is not None]
    return {"expiring": es["expiring"], "state": es["state"], "pct": es["taiex"]["pct"],
            "zero_volume": z("zero", "volume"), "zero_profitable": z("zero", "profitable"),
            "low_volume": z("low", "volume"), "low_losing": z("low", "losing"),
            "best_pnl": max(best, default=None)}


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
    prune_chains(daily, [d["date"] for d in days][:KEEP_CHAIN_DAYS], dump)


def prune_chains(daily: Path, keep: list[str], dump) -> None:
    """較舊交易日的 JSON 移除完整選擇權鏈，只留摘要與訊號，避免 repo 膨脹。"""
    keep = set(keep)
    for p in daily.glob("*.json"):
        if p.stem in keep:
            continue
        rep = json.loads(p.read_text("utf-8"))
        if any(s.get("chain") for s in rep.get("series", [])):
            for s in rep["series"]:
                s["chain"], s["chain_pruned"] = [], True
            p.write_text(dump(rep), "utf-8")


def run_date(date: dt.date, cfg: dict, data_dir: Path, archive_dir: Path,
             mode: str = "refresh", all_strikes: bool = False) -> dict | None:
    try:
        fut, opt = taifex.load(date, archive_dir, mode)
    except taifex.NoData as e:
        print(f"[略過] {e}")
        return None
    taiex = None
    if any(d == date for d in expiry_dates(opt).values()):
        taiex = twse.day_info(date, archive_dir, "refresh" if mode == "refresh" else
                              ("offline" if mode == "offline" else "cache"))
    rep = analyze(date, fut, opt, cfg, all_strikes, taiex)
    if not rep["series"] and not rep["expiry_sell"]:
        print(f"[略過] {date} 沒有可分析的週選序列")
        return None
    write(rep, data_dir)
    return rep
