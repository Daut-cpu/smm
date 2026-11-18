"""Фоновые задачи: ежедневная идея и регулярное обновление новостей."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .news import CACHE_SECONDS, NewsCollector

log = logging.getLogger(__name__)


def next_run(now: datetime, at: time) -> datetime:
    """Ближайший момент ``at`` после ``now`` (в часовом поясе ``now``)."""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = (candidate + timedelta(days=1)).replace(hour=at.hour, minute=at.minute)
    return candidate


async def daily_loop(at: time, tz: ZoneInfo, job: Callable[[], Awaitable[None]]) -> None:
    while True:
        now = datetime.now(tz)
        target = next_run(now, at)
        log.info("Следующая ежедневная идея: %s", target.strftime("%Y-%m-%d %H:%M %Z"))
        # спим кусками, чтобы не уплывать после засыпания VPS/перевода часов
        while (delay := (target - datetime.now(tz)).total_seconds()) > 0:
            await asyncio.sleep(min(delay, 300))
        try:
            await job()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка ежедневной идеи")
        await asyncio.sleep(1)


async def refresh_loop(collector: NewsCollector) -> None:
    """Держит кеш новостей тёплым, чтобы /idea отвечал быстро."""
    while True:
        try:
            await collector.get_news()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обновления новостей")
        await asyncio.sleep(CACHE_SECONDS + 5)
