"""選用：Telegram 通知。設定 repo secrets TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID 才會啟用。"""
from __future__ import annotations

import os

import requests


def _send(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=30)
    return r.ok


def notify_live(z: dict, pages_url: str = "") -> bool:
    """結算日盤中：指數接近或達漲跌停才通知。"""
    if z["state"] == "NORMAL":
        return False
    b = z["bounds"]
    lines = [f"⚡ 結算日 {z['date']} {z['time']}：加權 {z['last']:,.2f}（{z['pct']}%）{z['state_label']}",
             f"今日結算：{', '.join(z['expiring'])}（13:00–13:30 加權指數均價）",
             f"零風險賣方：Call K > {b['ceiling']:,}、Put K < {b['floor']:,}",
             f"低風險賣方：{z['low_zone'] or '無'}",
             "請以即時報價確認買價是否高於手續費＋稅（約 0.4 點）"]
    if pages_url:
        lines.append(pages_url)
    return _send("\n".join(lines))


def notify(rep: dict, pages_url: str = "") -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    from .pipeline import is_actionable
    feas = [s for s in rep["signals"] if is_actionable(s)]
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
    return _send("\n".join(lines))
