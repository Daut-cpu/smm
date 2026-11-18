"""Общие структуры данных."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_title(title: str) -> str:
    """Заголовок для сравнения дублей: нижний регистр, без пунктуации и лишних пробелов."""
    title = title.lower().replace("ё", "е")
    title = re.sub(r"[^\w\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def normalize_url(url: str) -> str:
    """Ссылка для сравнения дублей: без схемы, www, параметров, якоря и завершающего слэша."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    return urlunsplit(("", host, path, "", ""))


@dataclass
class NewsItem:
    source: str  # человекочитаемое имя: «Variety», «@kinopoisk»
    kind: str  # "rss" или "tg"
    title: str
    summary: str
    link: str
    published: datetime  # всегда timezone-aware (UTC)
    image_urls: list[str] = field(default_factory=list)
    # Для постов из Telegram: (username канала, id сообщения с фото)
    tg_photo_ref: tuple[str, int] | None = None
    # Сырые данные, нужные для скачивания фото (объект сообщения Telethon)
    tg_photo_message: Any = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return normalize_url(self.link) or normalize_title(self.title)

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.published).total_seconds() / 3600)
