"""Сбор свежих новостей из всех источников: фильтр по возрасту, дедупликация, кеш."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .models import NewsItem, normalize_title, normalize_url
from .sources.rss import fetch_feed
from .sources.telegram import TelegramReader

log = logging.getLogger(__name__)

CACHE_SECONDS = 30 * 60
MAX_ITEMS = 60


def dedupe_and_sort(items: list[NewsItem], max_age_hours: float, limit: int = MAX_ITEMS,
                    now: datetime | None = None) -> list[NewsItem]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    fresh = [i for i in items if cutoff <= i.published <= now + timedelta(minutes=10)]
    fresh.sort(key=lambda i: i.published, reverse=True)
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    result: list[NewsItem] = []
    for item in fresh:
        link = normalize_url(item.link)
        title = normalize_title(item.title)
        if (link and link in seen_links) or (title and title in seen_titles):
            continue
        if link:
            seen_links.add(link)
        if title:
            seen_titles.add(title)
        result.append(item)
        if len(result) >= limit:
            break
    return result


class NewsCollector:
    def __init__(self, feeds: list[str], http: httpx.AsyncClient, tg: TelegramReader | None,
                 max_age_hours: float):
        self.feeds = feeds
        self.http = http
        self.tg = tg
        self.max_age_hours = max_age_hours
        self._lock = asyncio.Lock()
        self._cache: list[NewsItem] = []
        self._cache_time = 0.0

    def invalidate(self) -> None:
        self._cache_time = 0.0

    async def get_news(self) -> list[NewsItem]:
        async with self._lock:
            if self._cache_time and time.monotonic() - self._cache_time < CACHE_SECONDS:
                return list(self._cache)
            items = await self._collect()
            self._cache = items
            self._cache_time = time.monotonic()
            return list(items)

    async def _collect(self) -> list[NewsItem]:
        counts: dict[str, int | str] = {}

        async def one_feed(url: str) -> list[NewsItem]:
            try:
                got = await fetch_feed(self.http, url)
                counts[url] = len(got)
                return got
            except Exception as exc:  # noqa: BLE001 — недоступный сайт пропускаем
                counts[url] = f"ошибка: {exc.__class__.__name__}"
                log.warning("Источник %s недоступен: %s", url, exc)
                return []

        async def tg_items() -> list[NewsItem]:
            if self.tg is None or not self.tg.ready:
                return []
            try:
                return await self.tg.fetch()
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось прочитать Telegram-каналы: %s", exc)
                return []

        results = await asyncio.gather(*(one_feed(u) for u in self.feeds), tg_items())
        all_items = [item for chunk in results for item in chunk]
        if self.tg is not None:
            counts.update(self.tg.last_counts)

        fresh = dedupe_and_sort(all_items, self.max_age_hours)
        per_source_fresh: dict[str, int] = {}
        for item in fresh:
            per_source_fresh[item.source] = per_source_fresh.get(item.source, 0) + 1
        log.info(
            "Обновление источников: всего записей %d, свежих после дедупликации %d. По источникам (всего): %s. "
            "Свежих по источникам: %s",
            len(all_items), len(fresh),
            ", ".join(f"{k}: {v}" for k, v in counts.items()) or "—",
            ", ".join(f"{k}: {v}" for k, v in per_source_fresh.items()) or "—",
        )
        return fresh
