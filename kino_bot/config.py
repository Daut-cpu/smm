"""Настройки бота из файла .env / переменных окружения."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://www.kinonews.ru/rss/",
    "https://www.film.ru/rss.xml",
    "https://variety.com/feed/",
    "https://www.hollywoodreporter.com/feed/",
    "https://deadline.com/feed/",
    "https://collider.com/feed/",
]

DEFAULT_STYLE = (
    "Канал про кино и сериалы для широкой русскоязычной аудитории. "
    "Тон живой, дружелюбный, с лёгким юмором, без канцелярита и кликбейта."
)


class ConfigError(Exception):
    """Ошибка в настройках: не задан обязательный параметр или неверный формат."""


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,\n]", value) if part.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _normalize_channel(name: str) -> str:
    name = name.strip()
    name = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", name)
    name = name.lstrip("@").strip("/")
    return name


@dataclass
class Config:
    bot_token: str
    anthropic_api_key: str
    channel_id: str | int
    admin_ids: set[int] = field(default_factory=set)
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_session: str = "data/reader"
    tg_channels: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=lambda: list(DEFAULT_FEEDS))
    channel_style: str = DEFAULT_STYLE
    max_age_hours: float = 24
    daily_time: time | None = None
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Moscow"))
    show_source: bool = True
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    style_channel: str | None = None
    style_examples: int = 15
    style_file: str | None = "style_examples.txt"
    style_profile_file: str | None = None
    post_max_len: int = 1000
    author_gender: str | None = None
    review: bool = True

    @property
    def telegram_reader_enabled(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and (self.tg_channels or self.style_channel))

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = ".env") -> "Config":
        if env_file and Path(env_file).exists():
            load_dotenv(env_file)
        env = os.environ

        missing = [k for k in ("BOT_TOKEN", "ANTHROPIC_API_KEY", "CHANNEL_ID") if not env.get(k, "").strip()]
        if missing:
            raise ConfigError("Не заданы обязательные параметры: " + ", ".join(missing))

        channel_raw = env["CHANNEL_ID"].strip()
        channel_id: str | int
        if re.fullmatch(r"-?\d+", channel_raw):
            channel_id = int(channel_raw)
        else:
            channel_id = "@" + _normalize_channel(channel_raw)

        try:
            admin_ids = {int(x) for x in _split(env.get("ADMIN_IDS"))}
        except ValueError as exc:
            raise ConfigError("ADMIN_IDS должен содержать числовые id через запятую") from exc

        api_id_raw = env.get("TG_API_ID", "").strip()
        try:
            tg_api_id = int(api_id_raw) if api_id_raw else None
        except ValueError as exc:
            raise ConfigError("TG_API_ID должен быть числом") from exc

        daily_raw = env.get("DAILY_TIME", "").strip()
        daily_time = None
        if daily_raw:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", daily_raw)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ConfigError("DAILY_TIME должен быть в формате ЧЧ:ММ, например 10:00")
            daily_time = time(int(m.group(1)), int(m.group(2)))

        tz_name = env.get("TZ_NAME", "").strip() or "Europe/Moscow"
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"Неизвестный часовой пояс TZ_NAME={tz_name}") from exc

        try:
            max_age = float(env.get("MAX_AGE_HOURS", "").strip() or 24)
        except ValueError as exc:
            raise ConfigError("MAX_AGE_HOURS должен быть числом") from exc

        effort = env.get("EFFORT", "").strip().lower() or "medium"
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ConfigError("EFFORT должен быть одним из: low, medium, high, xhigh, max")

        try:
            style_examples = int(env.get("STYLE_EXAMPLES", "").strip() or 15)
            post_max_len = int(env.get("POST_MAX_LEN", "").strip() or 1000)
        except ValueError as exc:
            raise ConfigError("STYLE_EXAMPLES и POST_MAX_LEN должны быть числами") from exc
        if not 300 <= post_max_len <= 4000:
            raise ConfigError("POST_MAX_LEN должен быть от 300 до 4000")
        if post_max_len > 1024:
            log.warning("POST_MAX_LEN=%d больше подписи к фото (1024): длинные посты уйдут двумя сообщениями — "
                        "сначала фото, потом текст", post_max_len)
        style_channel_raw = env.get("STYLE_CHANNEL", "").strip()
        gender_raw = env.get("AUTHOR_GENDER", "").strip().lower()
        gender_map = {"f": "f", "ж": "f", "female": "f", "жен": "f", "m": "m", "м": "m", "male": "m", "муж": "m"}
        if gender_raw and gender_raw not in gender_map:
            raise ConfigError("AUTHOR_GENDER должен быть «ж» или «м» (или пусто — определить по образцам)")

        feeds_raw = env.get("FEEDS")
        feeds = _split(feeds_raw) if feeds_raw and feeds_raw.strip() else list(DEFAULT_FEEDS)

        return cls(
            bot_token=env["BOT_TOKEN"].strip(),
            anthropic_api_key=env["ANTHROPIC_API_KEY"].strip(),
            channel_id=channel_id,
            admin_ids=admin_ids,
            tg_api_id=tg_api_id,
            tg_api_hash=env.get("TG_API_HASH", "").strip() or None,
            tg_session=env.get("TG_SESSION", "").strip() or "data/reader",
            tg_channels=[_normalize_channel(c) for c in _split(env.get("TG_CHANNELS"))],
            feeds=feeds,
            channel_style=env.get("CHANNEL_STYLE", "").strip() or DEFAULT_STYLE,
            max_age_hours=max_age,
            daily_time=daily_time,
            tz=tz,
            show_source=_bool(env.get("SHOW_SOURCE"), True),
            model=env.get("MODEL", "").strip() or "claude-sonnet-5-5",
            effort=effort,
            style_channel=_normalize_channel(style_channel_raw) if style_channel_raw else None,
            style_examples=max(0, min(style_examples, 40)),
            style_file=env.get("STYLE_FILE", "").strip() or "style_examples.txt",
            post_max_len=post_max_len,
            style_profile_file=env.get("STYLE_PROFILE_FILE", "").strip() or None,
            author_gender=gender_map.get(gender_raw) if gender_raw else None,
            review=_bool(env.get("REVIEW"), True),
        )
