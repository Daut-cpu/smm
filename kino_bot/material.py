"""Материал, который администратор прислал боту: ссылка, текст, заметки, пересланный пост, фото."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from aiogram import Bot
from aiogram.types import Message

from .articles import fetch_article
from .images import Image, accept_image
from .llm import SourceDoc

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")
MIN_NOTES_LEN = 60  # короче — скорее реплика боту, чем материал для поста
MAX_URLS = 3


@dataclass
class Material:
    docs: list[SourceDoc] = field(default_factory=list)
    image: Image | None = None
    source_url: str = ""
    failed_urls: list[str] = field(default_factory=list)


def message_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    urls: list[str] = []
    for ent in (message.entities or message.caption_entities or []):
        if ent.type == "text_link" and ent.url:
            urls.append(ent.url)
        elif ent.type == "url":
            urls.append(ent.extract_from(text))
    urls += URL_RE.findall(text)
    seen: list[str] = []
    for u in urls:
        u = u.rstrip(".,;:!?)»")
        if u not in seen:
            seen.append(u)
    return seen[:MAX_URLS]


def looks_like_material(message: Message) -> bool:
    text = (message.text or message.caption or "").strip()
    if message.forward_origin is not None:
        return bool(text)
    if message_urls(message):
        return True
    if message.photo and text:
        return True
    return len(text) >= MIN_NOTES_LEN


def _forward_source(message: Message) -> tuple[str, str]:
    """Имя и ссылка на исходный пост для пересланного сообщения."""
    origin = message.forward_origin
    chat = getattr(origin, "chat", None)
    if chat is not None:
        username = getattr(chat, "username", None)
        msg_id = getattr(origin, "message_id", None)
        url = f"https://t.me/{username}/{msg_id}" if username and msg_id else ""
        return (f"@{username}" if username else (chat.title or "канал")), url
    user = getattr(origin, "sender_user", None)
    if user is not None:
        return user.full_name, ""
    return getattr(origin, "sender_user_name", "") or "пересланное сообщение", ""


async def build_material(message: Message, bot: Bot, http: httpx.AsyncClient) -> Material:
    material = Material()
    text = (message.text or message.caption or "").strip()
    urls = message_urls(message)

    if message.forward_origin is not None:
        source, url = _forward_source(message)
        material.docs.append(SourceDoc(kind="forward", source=source, text=text, url=url))
        material.source_url = url
    else:
        notes = text
        for u in urls:
            notes = notes.replace(u, " ")
        notes = re.sub(r"\s+", " ", notes).strip()
        if notes:
            material.docs.append(SourceDoc(kind="author", source="автор канала", text=notes))

    for url in urls:
        article = await fetch_article(http, url)
        if article and article.text:
            host = urlsplit(article.url).netloc.removeprefix("www.")
            material.docs.append(SourceDoc(kind="site", source=host, title=article.title, text=article.text,
                                           url=article.url))
            material.source_url = material.source_url or article.url
        else:
            material.failed_urls.append(url)

    if message.photo:
        try:
            buf = io.BytesIO()
            await bot.download(message.photo[-1], destination=buf)
            material.image = accept_image(buf.getvalue())
        except Exception as exc:  # noqa: BLE001 — без фото пост всё равно пишем
            log.warning("Не удалось скачать фото из сообщения: %s", exc)
    return material
