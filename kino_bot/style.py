"""Посты-образцы, по которым модель копирует подачу текста.

Источники образцов:
- канал STYLE_CHANNEL: последние посты читаются через аккаунт Telethon и обновляются раз в сутки;
- файл STYLE_FILE: свои примеры, разделённые строкой из трёх дефисов ``---``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from .formatting import sanitize_html
from .sources.telegram import TelegramReader

log = logging.getLogger(__name__)

REFRESH_SECONDS = 24 * 3600
RETRY_SECONDS = 30 * 60  # после ошибки чтения канала пробуем снова через полчаса
MAX_EXAMPLE_LEN = 1500


def clean_example(text: str) -> str:
    """Готовит образец для модели: только разрешённые теги, без ссылок и упоминаний каналов."""
    text = sanitize_html(text)
    text = re.sub(r"https?://\S+|t\.me/\S+", "", text)
    text = re.sub(r"(?<!\w)@\w{4,}", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_EXAMPLE_LEN]


def load_style_file(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8")
    parts = re.split(r"^\s*-{3,}\s*$", raw, flags=re.M)
    return [c for c in (clean_example(x) for x in parts) if c]


class StyleProvider:
    def __init__(self, tg: TelegramReader | None, channel: str | None, limit: int, file: str | None):
        self.tg = tg
        self.channel = channel
        self.limit = limit
        self.file = file
        self._examples: list[str] = []
        self._next_refresh = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> list[str]:
        async with self._lock:
            if self._next_refresh and time.monotonic() < self._next_refresh:
                return self._examples
            examples: list[str] = []
            failed = False
            if self.file:
                from_file = load_style_file(self.file)
                if from_file:
                    log.info("Образцы стиля из файла %s: %d", self.file, len(from_file))
                examples.extend(from_file)
            if self.channel and self.tg is not None and self.tg.ready:
                try:
                    posts = await self.tg.fetch_style_posts(self.channel, self.limit)
                    cleaned = [c for c in (clean_example(p) for p in posts) if c]
                    log.info("Образцы стиля из канала @%s: %d", self.channel, len(cleaned))
                    examples.extend(cleaned)
                except Exception as exc:  # noqa: BLE001 — без образцов бот пишет в базовом стиле
                    log.warning("Не удалось прочитать канал-образец @%s: %s", self.channel, exc)
                    failed = True
                    if self._examples:  # оставляем прошлые образцы, повторим позже
                        self._next_refresh = time.monotonic() + RETRY_SECONDS
                        return self._examples
            elif self.channel:
                log.warning("Канал-образец @%s задан, но аккаунт для чтения каналов не подключён", self.channel)
            self._examples = examples[: max(self.limit, 1) * 2]
            self._next_refresh = time.monotonic() + (RETRY_SECONDS if failed else REFRESH_SECONDS)
            return self._examples
