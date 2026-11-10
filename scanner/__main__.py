"""命令列入口。

  python -m scanner run [--date YYYY-MM-DD]          每日排程：下載＋分析（預設台北今天）
  python -m scanner backfill --start 2025-04-01 --end 2025-04-30   回測區間
  python -m scanner rebuild                          用封存資料重算全部（改參數後用）
  python -m scanner file --fut TX.csv --opt TXO.csv --date 2026-11-10   離線 CSV
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
from .notify import notify
from .pipeline import analyze, run_date, write

ROOT = Path(__file__).resolve().parent.parent
DATA, ARCHIVE = ROOT / "docs" / "data", ROOT / "archive"


def parse_date(s: str) -> dt.date:
    return dt.datetime.strptime(s.replace("/", "-"), "%Y-%m-%d").date()


def print_report(rep):
    for k, v in rep["sessions"].items():
        print(f"  {k}盤 TX {v.get('expiry')}：{v['label']}  收 {v.get('close')} ({v.get('pct')}%)")
    print(f"  訊號 {len(rep['signals'])} 筆，可成交 {sum(s['feasible'] for s in rep['signals'])} 筆")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scanner")
    ap.add_argument("--min-edge", type=float)
    ap.add_argument("--all-strikes", action="store_true", help="垂直/蝴蝶不限深價外")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--date", default="")
    b = sub.add_parser("backfill"); b.add_argument("--start", required=True); b.add_argument("--end", required=True)
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
        return 0

    if a.cmd == "backfill":
        d, end, n = parse_date(a.start), parse_date(a.end), 0
        while d <= end:
            if d.weekday() < 5:
                print(f"▶ {d}")
                if run_date(d, cfg, DATA, ARCHIVE, "cache", a.all_strikes):
                    n += 1
                time.sleep(2)                      # 對期交所溫和一點
            d += dt.timedelta(days=1)
        print(f"完成 {n} 個交易日")
        return 0

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
