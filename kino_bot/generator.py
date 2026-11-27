"""Сборка черновика: повод → материалы → стиль → написание → проверка → текст и картинка."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from .articles import fetch_article
from .formatting import first_paragraph, fit_post, sanitize_html, source_link, strip_hashtags, visible_text
from .images import Image, ImageFinder
from .llm import LLMError, PostWriter, Selection, SourceDoc, find_cliches
from .models import NewsItem
from .news import NewsCollector
from .style import StyleProvider

log = logging.getLogger(__name__)

IMAGE_TIMEOUT = 12
ARTICLE_TIMEOUT = 12

Progress = Callable[[str], Awaitable[None]]


class NoNewsError(Exception):
    """Свежих новостей нет вообще (все источники пустые или недоступны)."""


class CandidatesExhaustedError(Exception):
    """Все свежие новости уже предлагались — история сброшена."""


class NotEnoughForDigestError(Exception):
    """Для подборки не нашлось хотя бы трёх подходящих проектов."""


@dataclass
class Draft:
    id: str
    chat_id: int
    text: str
    topic: str
    mode: str = "auto"  # auto, digest, material
    item: NewsItem | None = None
    docs: list[SourceDoc] = field(default_factory=list)  # для «Придумай ещё» по своему материалу
    source_url: str = ""
    image: Image | None = None
    photo_file_id: str | None = None
    message_ids: list[int] = field(default_factory=list)
    keyboard_message_id: int | None = None


class History:
    """Что уже предлагали: новости, сюжеты и недавние посты. Живёт только в памяти."""

    def __init__(self, max_topics: int = 200, max_posts: int = 10):
        self.used_keys: set[str] = set()
        self.topics: list[str] = []
        self.recent_posts: list[str] = []
        self.max_topics = max_topics
        self.max_posts = max_posts

    def add(self, items: list[NewsItem], topic: str) -> None:
        for item in items:
            self.used_keys.add(item.key)
        if topic:
            self.topics.append(topic)
            del self.topics[: -self.max_topics]

    def add_post(self, text: str) -> None:
        """Запоминает начало и финал поста, чтобы следующие не повторяли их."""
        plain = visible_text(text).strip()
        if not plain:
            return
        start = plain[:160].replace("\n", " ")
        end = plain[-160:].replace("\n", " ")
        self.recent_posts.append(f"{start} … {end}" if len(plain) > 320 else plain.replace("\n", " "))
        del self.recent_posts[: -self.max_posts]

    def is_used(self, item: NewsItem) -> bool:
        return item.key in self.used_keys

    def reset(self) -> None:
        self.used_keys.clear()
        self.topics.clear()


async def _noop(_: str) -> None:
    return None


class Generator:
    def __init__(self, collector: NewsCollector, writer: PostWriter, images: ImageFinder, show_source: bool,
                 style: StyleProvider | None = None, max_len: int = 2000, http: httpx.AsyncClient | None = None,
                 review: bool = True):
        self.collector = collector
        self.writer = writer
        self.images = images
        self.show_source = show_source
        self.style = style
        self.max_len = max_len
        self.http = http
        self.review = review
        self.history = History()
        self._lock = asyncio.Lock()  # выбор повода по очереди, чтобы два черновика не взяли одну новость

    # --- публичные режимы ------------------------------------------------------

    async def generate(self, chat_id: int, progress: Progress | None = None) -> Draft:
        """Пост по самому интересному свежему поводу."""
        return await self._from_news(chat_id, digest=False, progress=progress or _noop)

    async def generate_digest(self, chat_id: int, progress: Progress | None = None) -> Draft:
        """Подборка премьер из нескольких свежих материалов."""
        return await self._from_news(chat_id, digest=True, progress=progress or _noop)

    async def generate_from_material(self, chat_id: int, docs: list[SourceDoc], image: Image | None = None,
                                     source_url: str = "", progress: Progress | None = None) -> Draft:
        """Пост по материалу, который прислал администратор: ссылка, текст, заметки, пересланный пост."""
        progress = progress or _noop
        text, fmt = await self._write(docs, "", "", progress)
        if image is None and source_url:
            await progress("🖼 Подбираю картинку…")
            image = await self._image_for_url(source_url)
        topic = first_paragraph(text)[:150]
        if self.show_source and source_url:
            text = f"{text}\n\n{source_link(source_url)}"
        log.info("Черновик по материалу администратора (%s), картинка: %s", fmt, "есть" if image else "нет")
        return Draft(id=_new_id(), chat_id=chat_id, text=text, topic=topic, mode="material", docs=docs,
                     source_url=source_url, image=image)

    # --- конвейер --------------------------------------------------------------

    async def _from_news(self, chat_id: int, digest: bool, progress: Progress) -> Draft:
        await progress("🔎 Ищу свежие новости…")
        async with self._lock:
            news = await self.collector.get_news()
            if not news:
                raise NoNewsError()
            await progress("🧭 Выбираю повод для поста…")
            selection = await self._select(news, digest)
            # Сразу помечаем материалы как использованные, чтобы параллельный запрос их не взял
            self.history.add([selection.main, *selection.related], selection.topic)

        await progress("📰 Читаю источники…")
        items = [selection.main, *selection.related]
        try:
            docs = await self._collect_docs(items)
            text, fmt = await self._write(docs, selection.format, selection.topic, progress)
        except Exception:
            # пост не получился — возвращаем материалы в оборот, чтобы повтор мог их взять
            for item in items:
                self.history.used_keys.discard(item.key)
            raise

        if self.show_source and selection.main.link and not digest:
            text = f"{text}\n\n{source_link(selection.main.link)}"

        await progress("🖼 Подбираю картинку…")
        image = await self._image_for_item(selection.main)
        log.info("Черновик (%s): %s · %s · материалов %d, картинка: %s", fmt, selection.topic,
                 selection.main.link, len(docs), "есть" if image else "нет")
        return Draft(id=_new_id(), chat_id=chat_id, text=text, topic=selection.topic,
                     mode="digest" if digest else "auto", item=selection.main,
                     source_url=selection.main.link, image=image)

    async def _select(self, news: list[NewsItem], digest: bool) -> Selection:
        # Не больше двух запросов: второй — только если модель отвергла всю первую порцию.
        for _ in range(2):
            candidates = [n for n in news if not self.history.is_used(n)]
            if not candidates or (digest and len(candidates) < 3):
                if digest:
                    raise NotEnoughForDigestError()
                self.history.reset()
                raise CandidatesExhaustedError()
            batch = candidates[:40]
            selection = await self.writer.select(batch, self.history.topics, digest=digest)
            if selection is not None:
                return selection
            if digest:
                raise NotEnoughForDigestError()
            log.info("Модель не нашла подходящего повода среди %d материалов", len(batch))
            for n in batch:
                self.history.used_keys.add(n.key)
        self.history.reset()
        raise CandidatesExhaustedError()

    async def _collect_docs(self, items: list[NewsItem]) -> list[SourceDoc]:
        async def one(item: NewsItem) -> SourceDoc:
            text = item.summary
            title = item.title if item.kind == "rss" else ""
            if item.kind == "rss" and item.link and self.http is not None:
                try:
                    article = await asyncio.wait_for(fetch_article(self.http, item.link), ARTICLE_TIMEOUT)
                except asyncio.TimeoutError:
                    article = None
                if article and len(article.text) > len(text):
                    text = article.text
                    # картинки со страницы статьи пригодятся, если в RSS их не было
                    for url in article.image_urls:
                        if url not in item.image_urls:
                            item.image_urls.append(url)
            return SourceDoc(kind="tg" if item.kind == "tg" else "site", source=item.source, text=text,
                             title=title, url=item.link, published=item.published)

        return list(await asyncio.gather(*(one(i) for i in items)))

    async def _style(self) -> tuple[str, list[str]]:
        if self.style is None:
            return "", []
        examples = await self.style.get()
        profile = await self.style.profile(self.writer, examples)
        return profile, examples

    async def _write(self, docs: list[SourceDoc], fmt: str, topic: str, progress: Progress) -> tuple[str, str]:
        profile, examples = await self._style()
        await progress("✍️ Пишу пост…")
        recent = list(self.history.recent_posts)
        written = await self.writer.write(docs, fmt, topic, profile, examples, recent)
        post = written.post
        if self.review:
            await progress("🧐 Проверяю факты и стиль…")
            try:
                reviewed = await self.writer.review(docs, written, profile, recent)
                if reviewed.issues:
                    log.info("Редактор исправил: %s", "; ".join(reviewed.issues))
                post = reviewed.post
            except LLMError as exc:  # без проверки пост всё равно лучше, чем ничего
                log.warning("Проверка не удалась, отдаю черновик без неё: %s", exc)
        text = strip_hashtags(sanitize_html(post))
        if not text:
            raise LLMError("После очистки текст поста оказался пустым")
        leftovers = find_cliches(text)
        if leftovers:
            log.warning("В посте остались шаблонные обороты: %s", ", ".join(leftovers))
        text = fit_post(text, self.max_len)
        self.history.add_post(text)
        return text, written.format

    async def _image_for_item(self, item: NewsItem) -> Image | None:
        try:
            return await asyncio.wait_for(self.images.find(item), IMAGE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("Поиск картинки для %s занял слишком много времени", item.link)
            return None

    async def _image_for_url(self, url: str) -> Image | None:
        stub = NewsItem(source="", kind="rss", title="", summary="", link=url, published=datetime.now(timezone.utc))
        return await self._image_for_item(stub)


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


class DraftStore:
    """Черновики в памяти процесса; после перезапуска старые кнопки становятся неактуальными."""

    def __init__(self, max_per_chat: int = 30):
        self._drafts: dict[str, Draft] = {}
        self.max_per_chat = max_per_chat

    def add(self, draft: Draft) -> None:
        self._drafts[draft.id] = draft
        same_chat = [d for d in self._drafts.values() if d.chat_id == draft.chat_id]
        for old in same_chat[: max(0, len(same_chat) - self.max_per_chat)]:
            self._drafts.pop(old.id, None)

    def get(self, draft_id: str) -> Draft | None:
        return self._drafts.get(draft_id)

    def pop(self, draft_id: str) -> Draft | None:
        return self._drafts.pop(draft_id, None)

    def __len__(self) -> int:
        return len(self._drafts)
