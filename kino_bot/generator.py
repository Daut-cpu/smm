"""Сборка черновика: свежие новости → Claude → текст и картинка. История предложенного."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from .formatting import first_paragraph, fit_post, sanitize_html, source_link
from .images import Image, ImageFinder
from .llm import LLMError, PostWriter
from .models import NewsItem
from .news import NewsCollector

log = logging.getLogger(__name__)

IMAGE_TIMEOUT = 12  # секунд на поиск картинки, чтобы уложиться в 30 секунд на черновик


class NoNewsError(Exception):
    """Свежих новостей нет вообще (все источники пустые или недоступны)."""


class CandidatesExhaustedError(Exception):
    """Все свежие новости уже предлагались — история сброшена."""


@dataclass
class Draft:
    id: str
    chat_id: int
    text: str
    item: NewsItem
    topic: str
    image: Image | None = None
    photo_file_id: str | None = None
    message_ids: list[int] = field(default_factory=list)
    keyboard_message_id: int | None = None


class History:
    """Что уже предлагали: ключи новостей и краткие сюжеты. Живёт только в памяти."""

    def __init__(self, max_topics: int = 200):
        self.used_keys: set[str] = set()
        self.topics: list[str] = []
        self.max_topics = max_topics

    def add(self, item: NewsItem, topic: str) -> None:
        self.used_keys.add(item.key)
        if topic:
            self.topics.append(topic)
            del self.topics[: -self.max_topics]

    def is_used(self, item: NewsItem) -> bool:
        return item.key in self.used_keys

    def reset(self) -> None:
        self.used_keys.clear()
        self.topics.clear()


class Generator:
    def __init__(self, collector: NewsCollector, writer: PostWriter, images: ImageFinder, show_source: bool):
        self.collector = collector
        self.writer = writer
        self.images = images
        self.show_source = show_source
        self.history = History()
        self._lock = asyncio.Lock()  # один запрос к истории за раз, чтобы два черновика не взяли одну новость

    async def generate(self, chat_id: int) -> Draft:
        async with self._lock:
            news = await self.collector.get_news()
            if not news:
                raise NoNewsError()
            # Не больше двух запросов: второй — только если модель отвергла всю первую порцию.
            for _ in range(2):
                candidates = [n for n in news if not self.history.is_used(n)]
                if not candidates:
                    self.history.reset()
                    raise CandidatesExhaustedError()
                batch = candidates[:40]
                choice = await self.writer.choose_and_write(batch, self.history.topics)
                if choice is None:
                    log.info("Модель не нашла подходящих новостей среди %d кандидатов", len(batch))
                    for n in batch:
                        self.history.used_keys.add(n.key)
                    continue
                text = sanitize_html(choice.post)
                if not text:
                    raise LLMError("После очистки текст поста оказался пустым")
                text = fit_post(text)
                topic = choice.topic or first_paragraph(text)
                self.history.add(choice.item, topic)
                if first_paragraph(text) and first_paragraph(text) != topic:
                    self.history.topics.append(first_paragraph(text))
                break
            else:
                self.history.reset()
                raise CandidatesExhaustedError()

        if self.show_source and choice.item.link:
            text = f"{text}\n\n{source_link(choice.item.link)}"

        try:
            image = await asyncio.wait_for(self.images.find(choice.item), IMAGE_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("Поиск картинки для %s занял слишком много времени", choice.item.link)
            image = None

        log.info("Черновик: %s (%s, %s), картинка: %s", topic, choice.item.source, choice.item.link,
                 "есть" if image else "нет")
        return Draft(
            id=uuid.uuid4().hex[:10],
            chat_id=chat_id,
            text=text,
            item=choice.item,
            topic=topic,
            image=image,
        )


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
