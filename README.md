# TXO 週選 × 台指期 漲跌停套利掃描

每個交易日自動抓期交所台指期（TX）與台指選擇權（TXO）日行情，判斷近月台指期是否漲跌停，並檢查週選（偏重深價外）在扣除手續費與交易稅後，是否還有違反無套利邊界的組合。

## 架構

```
期交所 CSV ──► GitHub Actions（後端，排程）──► docs/data/*.json ──► GitHub Pages（前端，純查詢）
                     │
                     └─► archive/ 原始 CSV 封存（.gz，可重算、可回測）
```

| 位置 | 內容 |
|---|---|
| `scanner/taifex.py` | 下載（含重試）、解析、封存期交所 CSV |
| `scanner/market.py` | 近月期貨、漲跌停判斷、選擇權鏈整理、隱含期貨價 |
| `scanner/checks.py` | 套利檢查（以可成交買賣價、扣成本計算） |
| `scanner/pipeline.py` | 單日分析 → `docs/data/daily/<日期>.json` 與 `index.json` |
| `scanner/notify.py` | 選用 Telegram 通知（有漲跌停或可成交訊號才發） |
| `docs/` | 前端：交易日選單、期貨狀態、訊號表、選擇權鏈、歷史紀錄 |
| `.github/workflows/scan.yml` | 排程掃描＋提交資料＋部署 Pages |
| `.github/workflows/test.yml` | push 時跑測試 |

## 檢查項目

| 檢查 | 期貨腳 | 意義 |
|---|---|---|
| Conversion / Reversal | 小台（每點 50 元，與 TXO 1:1） | C − P 對 F − K 的偏離。漲停時只有「賣期貨」成交得到，跌停反之 |
| Box Spread | 不用 | 兩履約價合成多空，到期必收 K2 − K1 |
| 單調性 / 價差寬度 | 不用 | 高履約 Call 不該比低履約貴；價差權利金不該大於間距 |
| 蝴蝶凸性 | 不用 | 1-2-1 蝴蝶到期報酬 ≥ 0，若能淨收權利金即套利 |
| 隱含期貨價 | — | ATM 附近 K + C − P 的中位數；漲跌停時可看出期貨鎖住後的真實價位 |

## 排程（台北時間）

- 15:40 抓日盤，17:10 補抓（期交所延遲時）
- 隔日 06:30 抓夜盤（期交所把夜盤歸到下一個交易日）
- 每次都重新下載當日 CSV，所以日盤跑完會蓋掉只有夜盤的版本

## 第一次設定

1. 建 repo 並 push 這個資料夾
2. Settings → Pages → Source 選 **GitHub Actions**
3. Actions → 「掃描並部署」→ Run workflow，`mode=run`、`date=2026-11-10`，用期交所原始資料覆蓋種子資料
4. 想看歷史漲跌停日：`mode=backfill`、`start=2025-04-01`、`end=2025-04-30`
5. 選用：Settings → Secrets 加 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`

## 參數

在 `scanner/config.py`，或在 workflow 的 `env` 用大寫環境變數覆寫（如 `OPT_FEE: "15"`、`MIN_EDGE: "2"`）。改完參數跑 `mode=rebuild` 即可用封存資料重算全部日期。

## 本機

```bash
pip install -r requirements.txt pytest
pytest -q
python -m scanner run --date 2026-11-11
python -m scanner backfill --start 2025-04-01 --end 2025-04-30
python -m http.server -d docs 8000   # 開 http://localhost:8000
```

## 限制

- 報價是收盤「最後最佳買賣價」，屬事後偵測；要實際下單需接盤中即時報價
- 週選與近月期貨到期日不同，期貨平價含基差風險；Box、垂直、蝴蝶沒有這問題
- 選擇權本身也有漲跌幅，單邊鎖住時常只有一側報價
- 賣方腳需保證金，極端行情可能被追繳或砍倉
- GitHub 主機在海外：若期交所擋連線，workflow 會失敗並寄信通知；可改用 self-hosted runner 或在本機跑 `python -m scanner run` 後 push
- `tests/fixtures/real_20261110_*` 的期貨價是用選擇權平價推的代用值，不是期交所原始期貨收盤
