"""Посты-образцы, по которым модель копирует подачу текста.

Источники образцов:
- канал STYLE_CHANNEL: последние посты читаются через аккаунт Telethon и обновляются раз в сутки;
- файл STYLE_FILE: свои примеры, разделённые строкой из трёх дефисов ``---``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .formatting import sanitize_html
from .sources.telegram import TelegramReader

if TYPE_CHECKING:
    from .llm import PostWriter

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


def examples_hash(examples: list[str]) -> str:
    return hashlib.sha256("\n---\n".join(examples).encode("utf-8")).hexdigest()[:16]


_HASH_LINE_RE = re.compile(r"^<!-- examples-hash: (\w+) -->\n?")


class StyleProvider:
    def __init__(self, tg: TelegramReader | None, channel: str | None, limit: int, file: str | None,
                 profile_cache: str = "data/style_profile.md", profile_file: str | None = None):
        self.tg = tg
        self.channel = channel
        self.limit = limit
        self.file = file
        self.profile_cache = Path(profile_cache)
        self.profile_file = Path(profile_file) if profile_file else None
        self._profile = ""
        self._profile_hash = ""
        self._profile_lock = asyncio.Lock()
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

    async def profile(self, writer: "PostWriter", examples: list[str]) -> str:
        """Стилевой профиль автора. Свой файл STYLE_PROFILE_FILE важнее автоматического профиля.

        Автоматический профиль строится по образцам один раз и хранится в data/style_profile.md;
        пересобирается только когда меняются образцы.
        """
        if self.profile_file and self.profile_file.exists():
            return self.profile_file.read_text(encoding="utf-8").strip()
        if not examples:
            return ""
        digest = examples_hash(examples)
        async with self._profile_lock:
            if self._profile and self._profile_hash == digest:
                return self._profile
            cached = self._read_cache()
            if cached and cached[0] == digest:
                self._profile, self._profile_hash = cached[1], digest
                return self._profile
            try:
                profile = await writer.build_profile(examples)
            except Exception as exc:  # noqa: BLE001 — без профиля пишем по одним образцам
                log.warning("Не удалось построить стилевой профиль: %s", exc)
                return cached[1] if cached else self._profile
            self._profile, self._profile_hash = profile, digest
            try:
                self.profile_cache.parent.mkdir(parents=True, exist_ok=True)
                self.profile_cache.write_text(f"<!-- examples-hash: {digest} -->\n{profile}\n", encoding="utf-8")
                log.info("Стилевой профиль обновлён и сохранён в %s", self.profile_cache)
            except OSError as exc:
                log.warning("Не удалось сохранить профиль в %s: %s", self.profile_cache, exc)
            return profile

    def _read_cache(self) -> tuple[str, str] | None:
        try:
            raw = self.profile_cache.read_text(encoding="utf-8")
        except OSError:
            return None
        m = _HASH_LINE_RE.match(raw)
        if not m:
            return None
        return m.group(1), raw[m.end():].strip()
