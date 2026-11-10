"""選用：Telegram 通知。設定 repo secrets TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID 才會啟用。"""
from __future__ import annotations

import os

import requests


def notify(rep: dict, pages_url: str = "") -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    feas = [s for s in rep["signals"] if s["feasible"] and s["riskless"]]
    if not (token and chat) or not (rep["limit_event"] or feas):
        return False
    lines = [f"📊 TXO 週選套利掃描 {rep['date']}"]
    for sess, v in rep["sessions"].items():
        lines.append(f"{sess}盤 TX {v.get('expiry')}：{v['label']} {v.get('pct', '-')}%")
    lines.append(f"可成交無風險訊號 {len(feas)} 筆")
    for s in feas[:5]:
        lines.append(f"• {s['type']} {s['expiry']}：{s['legs']}（淨 {s['edge_pts']} 點）")
    if pages_url:
        lines.append(pages_url)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": "\n".join(lines)}, timeout=30)
    return r.ok
