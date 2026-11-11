"""期交所資料下載與解析。"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import re
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
    ("date", "交易日期"), ("exp_date", "契約到期日"), ("contract", "契約"), ("expiry", "到期月份"),
    ("strike", "履約價"), ("cp", "買賣權"), ("open", "開盤價"), ("high", "最高價"),
    ("low", "最低價"), ("close", "收盤價"), ("chg_pct", "漲跌%"), ("chg", "漲跌價"),
    ("volume", "成交量"), ("settle", "結算價"), ("oi", "未沖銷"),
    ("bid", "最後最佳買價"), ("ask", "最後最佳賣價"), ("session", "交易時段"),
]
NUM_COLS = ["strike", "open", "high", "low", "close", "chg", "volume", "settle", "oi", "bid", "ask"]


class NoData(LookupError):
    """非交易日或期交所尚未更新。"""


def download(kind: str, date: dt.date, retries: int = 3, end: dt.date | None = None) -> str:
    """下載單日（或 date～end，期交所限制一次不超過一個月）的日行情 CSV 原文。"""
    url, cid = (OPT_URL, "TXO") if kind == "TXO" else (FUT_URL, kind)   # kind：TX、MTX、TXO
    d = date.strftime("%Y/%m/%d")
    form = {"down_type": "1", "commodity_id": cid, "commodity_id2": "",
            "queryStartDate": d, "queryEndDate": (end or date).strftime("%Y/%m/%d")}
    last = None
    for i in range(retries):
        try:
            r = requests.post(url, data=form, headers=HEADERS, timeout=60)
            r.raise_for_status()
            text = r.content.decode("cp950", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if "交易日期" not in text[:300] or len(lines) < 2:
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
    if "session" not in df.columns:            # 2017/5/15 之前沒有夜盤，舊檔可能沒有「交易時段」欄
        df["session"] = "一般"
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip()
    for c in NUM_COLS:
        if c in df.columns:
            s = df[c].str.replace(",", "", regex=False)
            df[c] = pd.to_numeric(s.where(~s.isin(["-", "", "nan", "None"])), errors="coerce")
    return df


def split_by_date(text: str) -> dict[dt.date, str]:
    """把多日的 CSV 原文依第一欄「交易日期」拆成每日一份（保留表頭與原始格式）。"""
    lines = text.splitlines()
    header, out = lines[0], {}
    for ln in lines[1:]:
        if not ln.strip():
            continue
        try:
            d = dt.datetime.strptime(ln.split(",", 1)[0].strip().strip('"'), "%Y/%m/%d").date()
        except ValueError:
            continue
        out.setdefault(d, [header]).append(ln)
    return {d: "\n".join(v) + "\n" for d, v in out.items()}


def bulk_archive(start: dt.date, end: dt.date, archive_dir: Path, kinds=("TX", "MTX", "TXO")) -> list[dt.date]:
    """回測用：每次下載一個月再依日期拆檔封存（比逐日快約 20 倍）；已封存的月份略過。回傳有資料的交易日。"""
    days: set[dt.date] = set()
    m = start.replace(day=1)
    while m <= end:
        nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        s, e = max(m, start), min(nxt - dt.timedelta(days=1), end)
        for kind in kinds:
            have = {dt.datetime.strptime(p.name[:8], "%Y%m%d").date()
                    for p in (archive_dir / f"{s:%Y}").glob(f"{s:%Y%m}*_{kind}.csv.gz")}
            have = {d for d in have if s <= d <= e}
            weekdays = {s + dt.timedelta(days=i) for i in range((e - s).days + 1)
                        if (s + dt.timedelta(days=i)).weekday() < 5}
            if have and len(have) >= len(weekdays) - 3:          # 已封存（容許約 3 天假日）
                if kind == "TXO":
                    days |= have
                continue
            try:
                parts = split_by_date(download(kind, s, end=e))
            except NoData:
                continue
            for d, txt in parts.items():
                archive(txt, archive_dir, d, kind)
            if kind == "TXO":
                days |= set(parts)
            print(f"  {s:%Y/%m} {kind}：{len(parts)} 個交易日")
            time.sleep(3)
        m = nxt
    return sorted(d for d in days if start <= d <= end)


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
    """回傳 (期貨, 選擇權)。期貨＝TX 加上 MTX（小台，含週到期契約；取不到時只有 TX）。
    mode：refresh＝一律重新下載並覆寫封存（每日排程用，夜盤後日盤資料會增加）
          cache  ＝有封存就用封存（回測用）
          offline＝只讀封存"""
    offline = mode == "offline"
    out = []
    for kind in ("TX", "TXO"):
        p = archive_dir / date.strftime("%Y") / f"{date:%Y%m%d}_{kind}.csv.gz"
        if p.exists() and mode != "refresh":
            text = read_text(p)
            if len([ln for ln in text.splitlines() if ln.strip()]) < 2:
                raise NoData(f"封存的 {p.name} 為空（非交易日）")
        elif offline:
            raise NoData(f"封存中沒有 {p.name}")
        else:
            text = download(kind, date)
            archive(text, archive_dir, date, kind)
        out.append(parse(text))
    fut, opt = out
    mtx = _load_optional("MTX", date, archive_dir, mode)
    if mtx is not None and len(mtx):
        fut = pd.concat([fut, mtx], ignore_index=True)
    return fut, opt


def _load_optional(kind: str, date: dt.date, archive_dir: Path, mode: str) -> pd.DataFrame | None:
    p = archive_dir / date.strftime("%Y") / f"{date:%Y%m%d}_{kind}.csv.gz"
    try:
        if p.exists() and mode != "refresh":
            return parse(read_text(p))
        if mode == "offline":
            return None
        text = download(kind, date)
        archive(text, archive_dir, date, kind)
        return parse(text)
    except (NoData, RuntimeError, ValueError) as e:
        print(f"  [警告] {kind} 取不到，改用 TX 對沖：{e}")
        return None


# ---------------------------------------------------------------- 最後結算價
# 日行情裡到期序列在結算當天的「結算價」欄為 0，真正的最後結算價（13:00–13:30 加權指數均價）
# 要從期交所「指數選擇權最後結算價」頁取得。結果快取在 archive/taifex/fsp.json：{到期別: {"date", "price"}}。
FSP_URL = "https://www.taifex.com.tw/cht/12/optIndxFSP"
_ROW = re.compile(r"(\d{4}/\d{2}/\d{2})\s*</t[dh]>\s*<t[dh][^>]*>\s*(\d{6}(?:[WF]\d)?)\s*</t[dh]>\s*<t[dh][^>]*>\s*([\d,.]+)",
                  re.S | re.I)


def parse_fsp(html: str) -> dict:
    out = {}
    for d, code, px in _ROW.findall(html):
        out[code] = {"date": d.replace("/", "-"), "price": float(px.replace(",", ""))}
    return out


def _form_fields(html: str, start: dt.date, end: dt.date) -> tuple[str, dict]:
    """解析查詢表單：年、月下拉選單依出現順序填起、迄；勾選 TXO；保留隱藏欄位。"""
    m = re.search(r"<form[^>]*action=\"([^\"]*)\"", html, re.S | re.I)
    from urllib.parse import urljoin
    action = urljoin(FSP_URL, m.group(1)) if m and m.group(1) else FSP_URL
    data, years, months = {}, [], []
    for name, body in re.findall(r"<select[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</select>", html, re.S | re.I):
        vals = re.findall(r"value=\"([^\"]*)\"", body)
        if any(v == "2025" for v in vals):
            years.append(name)
        elif any(v in ("01", "1") for v in vals) and any(v == "12" for v in vals):
            months.append(name)
    for name, val, typ in [(n, v, t) for t, n, v in re.findall(
            r"<input[^>]*type=\"(hidden|checkbox)\"[^>]*name=\"([^\"]+)\"[^>]*value=\"([^\"]*)\"", html, re.S | re.I)]:
        if typ.lower() == "hidden" or val.upper() == "TXO":
            data.setdefault(name, val)
    if len(years) >= 2 and len(months) >= 2:
        data[years[0]], data[years[1]] = str(start.year), str(end.year)
        data[months[0]], data[months[1]] = f"{start.month:02d}", f"{end.month:02d}"
    print(f"  [最後結算價] 表單 {action}；年 {years}、月 {months}；送出欄位 {sorted(data)}")
    return action, data


def fetch_fsp(archive_dir: Path, start: dt.date, end: dt.date) -> dict:
    """抓期交所指數選擇權最後結算價並更新快取；失敗時回傳既有快取。"""
    p = archive_dir / "taifex" / "fsp.json"
    cache = json.loads(p.read_text("utf-8")) if p.exists() else {}
    got, html = {}, ""
    try:
        r = requests.get(FSP_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        html = r.text
        got = parse_fsp(html)                         # 頁面預設顯示約最近一年
        if not got:
            i = html.find("202611")
            i = i if i >= 0 else html.find("最後結算日")
            print("  [最後結算價][除錯] 解析不到資料，表格片段：", repr(html[max(i - 400, 0):i + 600]))
    except (requests.RequestException, ValueError) as e:
        print(f"  [警告] 最後結算價頁面取得失敗：{e}")
    if html and start < dt.date.today() - dt.timedelta(days=300):
        try:
            action, data = _form_fields(html, start, end)
            if data:
                r2 = requests.post(action, data=data, headers=HEADERS, timeout=60)
                r2.raise_for_status()
                more = parse_fsp(r2.text)
                print(f"  [最後結算價] 查詢 {start:%Y/%m}～{end:%Y/%m} 取得 {len(more)} 筆")
                got.update(more)
        except (requests.RequestException, ValueError) as e:
            print(f"  [警告] 最後結算價歷史查詢失敗：{e}")
    if got:
        cache.update(got)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), "utf-8")
    print(f"  [最後結算價] 快取共 {len(cache)} 筆（本次 {len(got)} 筆）")
    return cache


def load_fsp(archive_dir: Path) -> dict:
    p = archive_dir / "taifex" / "fsp.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}
