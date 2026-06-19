from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import get_settings
from ..database import SessionLocal
from .boost_stats import compute_boost_average
from .dashboard_settings import get_dashboard_min_daily_average
from .feishu_webhook import build_reminder_message, send_reminder
from .copy_sell_executor import run_due_copy_sell_tasks_once

logger = logging.getLogger(__name__)

UTC = timezone.utc
_scheduler: BackgroundScheduler | None = None


def _get_wallet_stats(min_daily_average: Decimal | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        configured_min = get_dashboard_min_daily_average(db)
        effective_min = configured_min if min_daily_average is None else min_daily_average
        return compute_boost_average(db, min_daily_average=effective_min)["wallets"]
    finally:
        db.close()


def _has_any_wallet_due(wallet_stats: list[dict]) -> bool:
    return any(w["daysRemaining"] is not None and w["daysRemaining"] <= 0 for w in wallet_stats)


def _has_traded_today(wallet_stats: list[dict]) -> bool:
    today = datetime.now(UTC).date()
    for w in wallet_stats:
        if w["daysRemaining"] is not None and w["daysRemaining"] <= 0:
            if w["lastTradeDate"] and w["lastTradeDate"] == today.isoformat():
                return True
    return False


def _check_and_remind() -> None:
    settings = get_settings()
    if not settings.pushplus_token and not settings.feishu_webhook_url:
        return

    try:
        wallet_stats = _get_wallet_stats()
        if not wallet_stats:
            return

        if not _has_any_wallet_due(wallet_stats):
            return

        if _has_traded_today(wallet_stats):
            logger.info("Due wallets already traded today, skipping reminder")
            return

        success = send_reminder(settings, wallet_stats)
        if success:
            logger.info("Reminder sent successfully")
        else:
            logger.warning("Failed to send reminder")
    except Exception as exc:
        logger.error(f"Scheduler reminder check failed: {exc}")


def _check_copy_sell_tasks() -> None:
    try:
        run_due_copy_sell_tasks_once()
    except Exception as exc:
        logger.error(f"Copy-sell scheduler check failed: {exc}")


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return

    _scheduler = BackgroundScheduler(timezone=UTC)

    # 3 reminder times: 09:00, 12:30, 19:30 UTC+8 = 01:00, 04:30, 11:30 UTC
    _scheduler.add_job(_check_and_remind, "cron", hour=1, minute=0, id="reminder_0900")
    _scheduler.add_job(_check_and_remind, "cron", hour=4, minute=30, id="reminder_1230")
    _scheduler.add_job(_check_and_remind, "cron", hour=11, minute=30, id="reminder_1930")
    _scheduler.add_job(
        _check_copy_sell_tasks,
        "interval",
        seconds=0.5,
        id="copy_sell_monitor",
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info("Scheduler started: reminders at 09:00/12:30/19:30 UTC+8, copy-sell monitor every 0.5s")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def trigger_reminder_now(min_daily_average: Decimal | None = None) -> str:
    settings = get_settings()
    if not settings.pushplus_token and not settings.feishu_webhook_url:
        return "No push channel configured"

    wallet_stats = _get_wallet_stats(min_daily_average=min_daily_average)
    if not wallet_stats:
        return "No wallets found"

    success = send_reminder(settings, wallet_stats)
    return "Reminder sent" if success else "Send failed"
