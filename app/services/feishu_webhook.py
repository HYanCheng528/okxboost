from __future__ import annotations

import requests

from ..config import Settings


def send_feishu_webhook(settings: Settings, content: str) -> bool:
    url = settings.feishu_webhook_url
    if not url:
        return False

    payload = {
        "msg_type": "text",
        "content": {"text": content},
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("code") == 0 or data.get("StatusCode") == 0
    except Exception:
        return False


def send_pushplus(settings: Settings, title: str, content: str) -> bool:
    token = settings.pushplus_token
    if not token:
        return False

    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "txt",
    }

    try:
        r = requests.post("http://www.pushplus.plus/send", json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("code") == 200
    except Exception:
        return False


def send_reminder(settings: Settings, wallet_stats: list[dict]) -> bool:
    msg = build_reminder_message(wallet_stats)
    if not msg:
        return False

    sent = False
    if settings.feishu_webhook_url:
        sent = send_feishu_webhook(settings, msg) or sent
    if settings.pushplus_token:
        sent = send_pushplus(settings, "刷量提醒", msg) or sent
    return sent


def build_reminder_message(wallet_stats: list[dict]) -> str:
    from datetime import datetime, timedelta, timezone

    utc8 = timezone(timedelta(hours=8))
    now_utc8 = datetime.now(utc8).strftime("%Y-%m-%d %H:%M")

    lines = [f"刷量提醒 ({now_utc8} UTC+8)\n"]

    for w in wallet_stats:
        label = w["label"] or "钱包"
        days = w["daysRemaining"]
        next_due = w.get("nextDueDate") or "未知"

        if days is None:
            lines.append(f"  {label}: 无数据")
        elif days <= 0:
            lines.append(f"⚠️ {label}: 今天该刷了！(截止 {next_due})")
        elif days <= 2:
            lines.append(f"📅 {label}: 还剩 {days} 天 (下次 {next_due})")
        else:
            lines.append(f"✅ {label}: 还剩 {days} 天 (下次 {next_due})")

    return "\n".join(lines)


def send_reward_notification(settings: Settings, period: int, token_name: str, chain: str, results) -> bool:
    from datetime import datetime, timedelta, timezone

    utc8 = timezone(timedelta(hours=8))
    now_utc8 = datetime.now(utc8).strftime("%Y-%m-%d %H:%M")

    lines = [f"Boost 空投检测 ({now_utc8} UTC+8)"]
    lines.append(f"第 {period} 期 - {token_name} [{chain.upper()}]\n")

    total_claimed = sum(r.claimed for r in results)
    total_sold = sum(r.sold_usdt for r in results)

    for r in results:
        label = r.label or r.wallet[:10]
        lines.append(f"  {label}: 领取 {r.claimed:.4f} 枚, 卖出 ${r.sold_usdt:.2f}")

    lines.append(f"\n合计: 领取 {total_claimed:.4f} 枚, 卖出 ${total_sold:.2f}")

    msg = "\n".join(lines)
    sent = False
    if settings.feishu_webhook_url:
        sent = send_feishu_webhook(settings, msg) or sent
    if settings.pushplus_token:
        sent = send_pushplus(settings, f"Boost空投 第{period}期", msg) or sent
    return sent
