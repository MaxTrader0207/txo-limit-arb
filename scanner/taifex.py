"""期交所資料下載與解析。"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import time
from pathlib import Path

import pandas as pd
import requests

FUT_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
OPT_URL = "https://www.taifex.com.tw/cht/3/optDataDown"
HEADERS = {"User-Agent": "Mozilla/5.0 (txo-limit-arb)", "Referer": "https://www.taifex.com.tw/"}

# (標準欄名, 期交所欄名關鍵字)；漲跌% 必須排在 漲跌價 之前
COLS = [
    ("date", "交易日期"), ("contract", "契約"), ("expiry", "到期月份"),
    ("strike", "履約價"), ("cp", "買賣權"), ("open", "開盤價"), ("high", "最高價"),
    ("low", "最低價"), ("close", "收盤價"), ("chg_pct", "漲跌%"), ("chg", "漲跌價"),
    ("volume", "成交量"), ("settle", "結算價"), ("oi", "未沖銷"),
    ("bid", "最後最佳買價"), ("ask", "最後最佳賣價"), ("session", "交易時段"),
]
NUM_COLS = ["strike", "open", "high", "low", "close", "chg", "volume", "settle", "oi", "bid", "ask"]


class NoData(LookupError):
    """非交易日或期交所尚未更新。"""


def download(kind: str, date: dt.date, retries: int = 3) -> str:
    url, cid = (FUT_URL, "TX") if kind == "TX" else (OPT_URL, "TXO")
    d = date.strftime("%Y/%m/%d")
    form = {"down_type": "1", "commodity_id": cid, "commodity_id2": "",
            "queryStartDate": d, "queryEndDate": d}
    last = None
    for i in range(retries):
        try:
            r = requests.post(url, data=form, headers=HEADERS, timeout=60)
            r.raise_for_status()
            text = r.content.decode("cp950", errors="replace")
            if "交易日期" not in text[:300]:
                raise NoData(f"{cid} {d} 查無資料（非交易日或尚未更新）")
            return text
        except NoData:
            raise
        except requests.RequestException as e:
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"下載 {cid} {d} 失敗：{last}")


def parse(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), dtype=str, index_col=False)
    rename = {}
    for col in df.columns:
        c = str(col).strip()
        for key, label in COLS:
            if label in c and key not in rename.values():
                rename[col] = key
                break
    df = df.rename(columns=rename)[[v for v in rename.values()]]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    for c in NUM_COLS:
        if c in df.columns:
            s = df[c].str.replace(",", "", regex=False)
            df[c] = pd.to_numeric(s.where(~s.isin(["-", "", "nan", "None"])), errors="coerce")
    return df


def read_text(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    if str(path).endswith(".gz"):
        raw = gzip.decompress(raw)
    for enc in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"無法解碼 {path}")


def archive(text: str, archive_dir: Path, date: dt.date, kind: str) -> Path:
    p = archive_dir / date.strftime("%Y") / f"{date:%Y%m%d}_{kind}.csv.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(text.encode("utf-8")))
    return p


def load(date: dt.date, archive_dir: Path, mode: str = "refresh") -> tuple[pd.DataFrame, pd.DataFrame]:
    """mode：refresh＝一律重新下載並覆寫封存（每日排程用，夜盤後日盤資料會增加）
             cache  ＝有封存就用封存（回測用）
             offline＝只讀封存"""
    offline = mode == "offline"
    out = []
    for kind in ("TX", "TXO"):
        p = archive_dir / date.strftime("%Y") / f"{date:%Y%m%d}_{kind}.csv.gz"
        if p.exists() and mode != "refresh":
            text = read_text(p)
        elif offline:
            raise NoData(f"封存中沒有 {p.name}")
        else:
            text = download(kind, date)
            archive(text, archive_dir, date, kind)
        out.append(parse(text))
    return out[0], out[1]
