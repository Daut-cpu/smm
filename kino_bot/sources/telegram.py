"""Чтение публичных Telegram-каналов через пользовательский аккаунт (Telethon).

Bot API не умеет читать чужие каналы, поэтому используется отдельный аккаунт.
Сессия аккаунта создаётся один раз командой ``python -m kino_bot.login``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..models import NewsItem

log = logging.getLogger(__name__)

AD_RE = re.compile(r"(#?\bреклама\b|\bна правах рекламы\b|\berid\b)", re.I)

# Telegram ограничивает частые запросы: читаем каналы не чаще, чем раз в 30 минут.
MIN_REFRESH_SECONDS = 30 * 60


def is_ad(text: str) -> bool:
    return bool(AD_RE.search(text or ""))


def make_title(text: str, limit: int = 150) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", line).strip(" *_-—•")
        if len(line) >= 3:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def build_items(channel: str, messages: list, max_age_hours: float, now: datetime | None = None) -> list[NewsItem]:
    """Превращает сообщения канала (новые → старые) в новости, склеивая альбомы."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    groups: dict[object, list] = {}
    order: list[object] = []
    for msg in messages:
        if msg.date is None or msg.date < cutoff:
            continue
        key = ("album", msg.grouped_id) if getattr(msg, "grouped_id", None) else ("single", msg.id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(msg)

    items: list[NewsItem] = []
    for key in order:
        group = sorted(groups[key], key=lambda m: m.id)
        text_msg = next((m for m in group if (m.message or "").strip()), None)
        if text_msg is None:
            continue  # пост без текста
        text = text_msg.message.strip()
        if is_ad(text):
            continue
        photo_msg = next((m for m in group if getattr(m, "photo", None)), None)
        items.append(
            NewsItem(
                source=f"@{channel}",
                kind="tg",
                title=make_title(text),
                summary=text[:1500],
                link=f"https://t.me/{channel}/{group[0].id}",
                published=text_msg.date,
                tg_photo_ref=(channel, photo_msg.id) if photo_msg else None,
                tg_photo_message=photo_msg,
            )
        )
    return items


class TelegramReader:
    def __init__(self, api_id: int, api_hash: str, session: str, channels: list[str], max_age_hours: float):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.channels = channels
        self.max_age_hours = max_age_hours
        self._client = None
        self._authorized = False
        self._lock = asyncio.Lock()
        self._last_fetch = 0.0
        self._cached: list[NewsItem] = []
        self.last_counts: dict[str, int | str] = {}

    async def start(self) -> bool:
        from telethon import TelegramClient

        Path(self.session).parent.mkdir(parents=True, exist_ok=True)
        self._client = TelegramClient(self.session, self.api_id, self.api_hash)
        await self._client.connect()
        self._authorized = await self._client.is_user_authorized()
        if not self._authorized:
            log.warning(
                "Аккаунт для чтения Telegram-каналов не авторизован. "
                "Выполните `python -m kino_bot.login`. Пока каналы читаться не будут."
            )
        else:
            me = await self._client.get_me()
            log.info("Аккаунт для чтения каналов: id=%s", getattr(me, "id", "?"))
        return self._authorized

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    @property
    def ready(self) -> bool:
        return self._client is not None and self._authorized

    async def fetch(self) -> list[NewsItem]:
        if not self.ready:
            return []
        async with self._lock:
            if self._last_fetch and time.monotonic() - self._last_fetch < MIN_REFRESH_SECONDS:
                return list(self._cached)
            counts: dict[str, int | str] = {}
            sem = asyncio.Semaphore(4)

            async def one(channel: str) -> list[NewsItem]:
                async with sem:
                    try:
                        got = await self._fetch_channel(channel)
                        counts[f"@{channel}"] = len(got)
                        return got
                    except Exception as exc:  # noqa: BLE001 — один канал не должен ронять сбор
                        counts[f"@{channel}"] = f"ошибка: {exc.__class__.__name__}"
                        log.warning("Канал @%s недоступен: %s", channel, exc)
                        return []

            chunks = await asyncio.gather(*(one(c) for c in self.channels))
            items = [item for chunk in chunks for item in chunk]
            self._cached = items
            self._last_fetch = time.monotonic()
            self.last_counts = counts
            return list(items)

    async def _fetch_channel(self, channel: str) -> list[NewsItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)
        messages = []
        async for msg in self._client.iter_messages(channel, limit=100):
            if msg.date and msg.date < cutoff:
                break
            messages.append(msg)
        return build_items(channel, messages, self.max_age_hours)

    async def fetch_style_posts(self, channel: str, limit: int) -> list[str]:
        """Последние посты канала-образца для подражания стилю."""
        if not self.ready:
            return []
        messages = [m async for m in self._client.iter_messages(channel, limit=max(60, limit * 4))]
        return pick_style_posts(messages, limit)

    async def download_photo(self, item: NewsItem) -> bytes | None:
        if not self.ready or item.tg_photo_message is None:
            return None
        data = await self._client.download_media(item.tg_photo_message, file=bytes)
        return data if isinstance(data, (bytes, bytearray)) else None


def message_html(msg) -> str:
    """Текст сообщения с форматированием (жирный, курсив и т. п.) в виде HTML."""
    text = msg.message or ""
    entities = getattr(msg, "entities", None)
    if not entities:
        return text
    try:
        from telethon.extensions import html as tl_html

        return tl_html.unparse(text, entities)
    except Exception:  # noqa: BLE001 — без форматирования тоже годится
        return text


def pick_style_posts(messages: list, limit: int, min_len: int = 80) -> list[str]:
    """Отбирает посты-образцы стиля: с текстом, без рекламы и пересылок, не слишком короткие."""
    posts: list[str] = []
    seen_albums: set = set()
    for msg in messages:
        text = (msg.message or "").strip()
        if len(text) < min_len or is_ad(text) or getattr(msg, "fwd_from", None):
            continue
        album = getattr(msg, "grouped_id", None)
        if album:
            if album in seen_albums:
                continue
            seen_albums.add(album)
        posts.append(message_html(msg))
        if len(posts) >= limit:
            break
    return posts
