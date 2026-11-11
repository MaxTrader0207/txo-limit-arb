"""命令列入口。

  python -m scanner run [--date YYYY-MM-DD]          每日排程：下載＋分析（預設台北今天）
  python -m scanner backfill --start 2025-04-01 --end 2025-04-30   回測區間
  python -m scanner rebuild                          用封存資料重算全部（改參數後用）
  python -m scanner file --fut TX.csv --opt TXO.csv --date 2026-11-10   離線 CSV
  python -m scanner live                             結算日盤中：抓即時加權指數，算可賣界線
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

from . import taifex
from .config import TPE, load_config
from .expiry_sell import live_zones
from .notify import notify, notify_live
from .pipeline import analyze, run_date, write

ROOT = Path(__file__).resolve().parent.parent
DATA, ARCHIVE = ROOT / "docs" / "data", ROOT / "archive"


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s.replace("/", "-"), "%Y-%m-%d").date()


def print_report(rep):
    for k, v in rep["sessions"].items():
        print(f"  {k}盤 TX {v.get('expiry')}：{v['label']}  收 {v.get('close')} ({v.get('pct')}%)")
    sig = [s for s in rep["signals"] if s.get("above_min", True)]
    ok_ = lambda s: s["feasible"] and s["riskless"] and s.get("quality") == "可靠"
    print(f"  訊號 {len(sig)} 筆：可成交無風險且報價可靠 {sum(ok_(s) for s in sig)}、"
          f"報價存疑 {sum(s['feasible'] and s['riskless'] and s.get('quality') == '存疑' for s in sig)}、"
          f"跨週別 {sum(s.get('nature') == '跨週別' for s in sig)}、基差 {sum(s.get('nature') == '基差' for s in sig)}")


def expiring_today(d: dt.date) -> list[str]:
    """盤中還沒有當日 CSV：讀最近一份封存的 TXO CSV「契約到期日」（含假日順延）；沒有封存才用代碼推算。"""
    from .market import expiry_dates
    files = sorted(p for p in ARCHIVE.glob("*/*_TXO.csv.gz") if p.name[:8] < f"{d:%Y%m%d}")
    for p in reversed(files[-5:]):
        try:
            opt = taifex.parse(taifex.read_text(p))
        except Exception:
            continue
        if len(opt):
            return sorted(e for e, x in expiry_dates(opt).items() if x == d)
    return expiring_codes(d)


def expiring_codes(d: dt.date) -> list[str]:
    """依代碼規則推算今天結算的序列（不含假日順延）。"""
    n, ym = (d.day - 1) // 7 + 1, f"{d:%Y%m}"
    if d.weekday() == 2:
        return [ym] if n == 3 else [f"{ym}W{n}"]
    if d.weekday() == 4:
        return [f"{ym}F{n}"]
    return []


def run_live(cfg) -> int:
    import json
    from . import twse
    now = dt.datetime.now(TPE)
    exp = expiring_today(now.date())
    if not exp:
        print("今天不是結算日")
        return 0
    rt = twse.realtime()
    if not rt:
        print("取不到即時加權指數")
        return 0
    z = live_zones(rt, exp, cfg)
    z["fetched"] = now.isoformat(timespec="seconds")
    z["date"] = now.date().isoformat()
    p = DATA / "live.json"
    hist = []
    if p.exists():
        old = json.loads(p.read_text("utf-8"))
        hist = old.get("snapshots", []) if old.get("date") == z["date"] else []
    hist.append({k: z[k] for k in ("fetched", "last", "pct", "state")})
    z["snapshots"] = hist
    DATA.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(z, ensure_ascii=False), "utf-8")
    print(f"{z['fetched']} 加權 {rt['last']:,.2f}（{rt['pct']}%）{z['state_label']}；結算序列 {exp}")
    print(f"  零風險：Call K > {z['bounds']['ceiling']:,}、Put K < {z['bounds']['floor']:,}；低風險：{z['low_zone'] or '無'}")
    notify_live(z, os.environ.get("PAGES_URL", ""))
    return 0


def catch_up(today: dt.date, cfg, all_strikes=False, days: int = 7) -> None:
    """排程可能被 GitHub 丟棄：補跑最近 N 天內漏掉、或只有夜盤沒有日盤的平日。"""
    import json
    idx_p = DATA / "index.json"
    have = {}
    if idx_p.exists():
        have = {d["date"]: d for d in json.loads(idx_p.read_text("utf-8"))["days"]}
    for back in range(1, days + 1):
        d = today - dt.timedelta(days=back)
        s = have.get(d.isoformat())
        if d.weekday() >= 5 or (s and "一般" in s.get("status", {})):
            continue
        print(f"▶ 補跑 {d}")
        rep = run_date(d, cfg, DATA, ARCHIVE, "refresh", all_strikes)
        if rep:
            print_report(rep)
        time.sleep(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scanner")
    ap.add_argument("--min-edge", type=float)
    ap.add_argument("--all-strikes", action="store_true", help="垂直/蝴蝶不限深價外")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--date", default="")
    b = sub.add_parser("backfill"); b.add_argument("--start", required=True); b.add_argument("--end", required=True)
    b.add_argument("--expiry-only", action="store_true", help="只跑週三、週五（結算日）")
    sub.add_parser("live")
    sub.add_parser("rebuild")
    f = sub.add_parser("file"); f.add_argument("--fut", required=True); f.add_argument("--opt", required=True)
    f.add_argument("--date", required=True)
    a = ap.parse_args(argv)
    cfg = load_config(min_edge=a.min_edge)

    if a.cmd == "run":
        date = parse_date(a.date) if a.date.strip() else dt.datetime.now(TPE).date()
        print(f"▶ {date}")
        rep = run_date(date, cfg, DATA, ARCHIVE, "refresh", a.all_strikes)
        if rep:
            print_report(rep)
            notify(rep, os.environ.get("PAGES_URL", ""))
        catch_up(date, cfg, a.all_strikes)
        return 0

    if a.cmd == "backfill":
        d, end, n = parse_date(a.start), parse_date(a.end), 0
        while d <= end:
            if d.weekday() < 5 and (not a.expiry_only or d.weekday() in (2, 4)):
                print(f"▶ {d}")
                if run_date(d, cfg, DATA, ARCHIVE, "cache", a.all_strikes):
                    n += 1
                time.sleep(2)                      # 對期交所溫和一點
            d += dt.timedelta(days=1)
        print(f"完成 {n} 個交易日")
        return 0

    if a.cmd == "live":
        return run_live(cfg)

    if a.cmd == "rebuild":
        for p in sorted(ARCHIVE.glob("*/*_TXO.csv.gz")):
            d = dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
            run_date(d, cfg, DATA, ARCHIVE, "offline", a.all_strikes)
        return 0

    if a.cmd == "file":
        fut = taifex.parse(taifex.read_text(a.fut))
        opt = taifex.parse(taifex.read_text(a.opt))
        rep = analyze(parse_date(a.date), fut, opt, cfg, a.all_strikes)
        write(rep, DATA)
        print_report(rep)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
