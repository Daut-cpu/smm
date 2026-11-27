"""Запросы к Claude API: выбор повода, профиль стиля, написание и проверка поста."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic

from . import prompts
from .formatting import visible_len
from .models import NewsItem

log = logging.getLogger(__name__)

MAX_CANDIDATES = 40

# Модели, для которых доступен серверный фолбэк при отказе модели (refusal).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """Ошибка генерации, текст понятен администратору."""


# ---------------------------------------------------------------------------
# Материалы для поста
# ---------------------------------------------------------------------------

@dataclass
class SourceDoc:
    """Один материал-источник для поста."""

    kind: str  # "site", "tg", "author" (заметки автора), "forward" (пересланный пост)
    source: str
    text: str
    title: str = ""
    url: str = ""
    published: datetime | None = None

    def label(self, now: datetime | None = None) -> str:
        kinds = {"site": "сайт", "tg": "Telegram-канал", "author": "заметки автора", "forward": "пересланный пост"}
        parts = [self.source, kinds.get(self.kind, self.kind)]
        if self.published:
            hours = ((now or datetime.now(timezone.utc)) - self.published).total_seconds() / 3600
            parts.append(f"{max(0, int(hours))} ч назад" if hours >= 1 else "меньше часа назад")
        if self.url:
            parts.append(self.url)
        return " · ".join(p for p in parts if p)


def render_materials(docs: list[SourceDoc]) -> str:
    blocks = []
    for n, doc in enumerate(docs, start=1):
        head = f'<material n="{n}">\n[{doc.label()}]'
        if doc.title:
            head += f"\nЗаголовок: {doc.title}"
        blocks.append(f"{head}\n\n{doc.text.strip()}\n</material>")
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Выбор повода
# ---------------------------------------------------------------------------

@dataclass
class Selection:
    main: NewsItem
    related: list[NewsItem] = field(default_factory=list)
    format: str = "news"
    topic: str = ""


def _age(item: NewsItem, now: datetime) -> str:
    hours = item.age_hours(now)
    if hours < 1:
        return f"{max(1, int(hours * 60))} мин назад"
    return f"{int(hours)} ч назад"


def build_candidates_prompt(candidates: list[NewsItem], previous_topics: list[str], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["Свежие материалы:"]
    for n, item in enumerate(candidates, start=1):
        kind = "Telegram-канал" if item.kind == "tg" else "сайт"
        summary = item.summary.replace("\n", " ")
        if item.kind == "tg":
            header = f"[{n}] {item.source} ({kind}) · {_age(item, now)}"
            body = summary[:500]
        else:
            header = f"[{n}] {item.source} ({kind}) · {_age(item, now)} · {item.title}"
            body = summary[:350]
        lines.append(header)
        if body:
            lines.append(f"    {body}")
    lines.append("")
    if previous_topics:
        lines.append("Уже предлагали (не повторять эти сюжеты):")
        lines.extend(f"- {t}" for t in previous_topics[-60:])
    else:
        lines.append("Уже предлагали: ничего.")
    return "\n".join(lines)


def parse_selection(data: dict, candidates: list[NewsItem], digest: bool = False) -> Selection | None:
    main = data.get("main")
    if isinstance(main, str) and main.strip().isdigit():
        main = int(main)
    if not isinstance(main, int) or isinstance(main, bool):
        raise LLMError("В ответе модели нет номера материала")
    if main == 0:
        return None
    if not 1 <= main <= len(candidates):
        raise LLMError(f"Модель выбрала несуществующий материал №{main}")
    related: list[NewsItem] = []
    for n in data.get("related") or []:
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(candidates) and n != main:
            item = candidates[n - 1]
            if item not in related:
                related.append(item)
    fmt = data.get("format") if data.get("format") in prompts.FORMATS else "news"
    if digest:
        fmt = "digest"
        related = related[:5]
        if len(related) < 2:
            return None
    else:
        related = related[:3]
        if fmt == "digest":
            fmt = "news"  # подборку собираем только по команде /digest
    topic = data.get("topic") if isinstance(data.get("topic"), str) else ""
    return Selection(main=candidates[main - 1], related=related, format=fmt, topic=topic.strip())


# ---------------------------------------------------------------------------
# Написание и проверка
# ---------------------------------------------------------------------------

@dataclass
class Written:
    post: str
    facts: list[str]
    angle: str
    format: str


@dataclass
class Reviewed:
    post: str
    issues: list[str]


def find_cliches(text: str) -> list[str]:
    low = text.lower().replace("ё", "е")
    return [c for c in prompts.CLICHES if c.replace("ё", "е") in low]


def build_writer_system(profile: str, examples: list[str], gender: str | None) -> str:
    examples_block = ""
    if examples:
        items = "\n".join(f"<example>\n{e}\n</example>" for e in examples)
        examples_block = prompts.WRITER_EXAMPLES_BLOCK.format(items=items)
    profile_block = prompts.WRITER_PROFILE_BLOCK.format(profile=profile) if profile else ""
    return prompts.WRITER_SYSTEM.format(
        formats=prompts.formats_text(),
        author=prompts.WRITER_AUTHOR_GENDER.get(gender or "", ""),
        profile=profile_block,
        examples=examples_block,
    )


def build_writer_prompt(docs: list[SourceDoc], fmt: str, topic: str, max_len: int, recent_posts: list[str],
                        channel_style: str) -> str:
    parts = [
        "Материалы:",
        render_materials(docs),
        "",
        f"Повод: {topic}" if topic else "",
        (f"Рекомендуемый формат: {fmt} — {prompts.FORMATS.get(fmt, '')}. "
         "Если материалы явно просят другой формат, выбери его.") if fmt else
        "Формат выбери сам — тот, который лучше всего раскроет материал.",
        (f"Длина: строго не больше {max(300, max_len - 80)} знаков с пробелами. Пост публикуется подписью "
         "к фотографии, поэтому это жёсткий предел, даже если посты автора обычно длиннее. Сохраняй манеру, "
         "но рассказывай компактнее: меньше второстепенных деталей, а заход, мнение и финал оставь."),
    ]
    if channel_style:
        parts.append(f"Пожелания владельца канала: {channel_style}")
    if recent_posts:
        parts.append("")
        parts.append("Недавние посты канала — не повторяй их начала, финалы и характерные обороты:")
        parts.extend(f"- {p}" for p in recent_posts[-8:])
    parts.append("")
    parts.append("Разбери материалы и напиши пост.")
    return "\n".join(p for p in parts if p is not None)


def build_review_prompt(docs: list[SourceDoc], draft: str, facts: list[str], recent_posts: list[str],
                        max_len: int | None = None) -> str:
    parts = ["Материалы:", render_materials(docs), "", "Черновик поста:", "<draft>", draft, "</draft>"]
    if max_len:
        current = visible_len(draft)
        target = max(300, max_len - 80)
        parts += ["", f"Предел длины поста: {target} знаков с пробелами (пост идёт подписью к фото). "
                      f"Сейчас в черновике около {current} знаков."
                      + (" Сократи пост до предела." if current > target else " Длина в порядке.")]
    if facts:
        parts += ["", "Факты, на которые опирался автор черновика (их тоже сверь с материалами):"]
        parts += [f"- {f}" for f in facts]
    hits = find_cliches(draft)
    if hits:
        parts += ["", "В черновике найдены шаблонные обороты, их нужно убрать: " + ", ".join(f"«{h}»" for h in hits)]
    if recent_posts:
        parts += ["", "Недавние посты канала (начало … финал):"]
        parts += [f"- {p}" for p in recent_posts[-8:]]
    return "\n".join(parts)


def _text_of(response) -> str:
    return next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")


class PostWriter:
    def __init__(self, api_key: str, model: str, effort: str, channel_style: str = "", max_len: int = 1000,
                 gender: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0, max_retries=1)
        self.model = model
        self.effort = effort
        self.channel_style = channel_style
        self.max_len = max_len
        self.gender = gender

    async def _call(self, system: str, user: str, effort: str, schema: dict | None = None,
                    max_tokens: int = 16000) -> str:
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        params = dict(
            model=self.model,
            max_tokens=max_tokens,
            # Системные инструкции с профилем и образцами стабильны в течение дня — кешируем их
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        try:
            if self.model in FALLBACK_MODELS:
                response = await self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)
            else:
                response = await self.client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude API: неверный ключ ANTHROPIC_API_KEY") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"Claude API: модель {self.model} не найдена") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Claude API: превышен лимит запросов, попробуйте через минуту") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"Claude API отклонил запрос: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API вернул ошибку {exc.status_code}") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError("Claude API не ответил вовремя") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Нет соединения с Claude API") from exc

        usage = getattr(response, "usage", None)
        log.info(
            "Claude: stop_reason=%s, input=%s (из кеша %s), output=%s",
            response.stop_reason,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "cache_read_input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
        if response.stop_reason == "refusal":
            raise LLMError("Модель отказалась работать с этим материалом")
        if response.stop_reason == "max_tokens":
            raise LLMError("Ответ модели оборвался по лимиту длины")
        text = _text_of(response)
        if not text:
            raise LLMError("Модель вернула пустой ответ")
        return text

    async def _call_json(self, system: str, user: str, effort: str, schema: dict) -> dict:
        text = await self._call(system, user, effort, schema)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("Модель вернула некорректный JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("Модель вернула неожиданный формат ответа")
        return data

    # --- шаги ---------------------------------------------------------------

    async def select(self, candidates: list[NewsItem], previous_topics: list[str],
                     digest: bool = False) -> Selection | None:
        candidates = candidates[:MAX_CANDIDATES]
        system = (prompts.SELECT_DIGEST_SYSTEM if digest
                  else prompts.SELECT_SYSTEM.format(formats=prompts.formats_text()))
        data = await self._call_json(system, build_candidates_prompt(candidates, previous_topics), "low",
                                     prompts.SELECT_SCHEMA)
        return parse_selection(data, candidates, digest)

    async def build_profile(self, examples: list[str]) -> str:
        items = "\n".join(f"<post>\n{e}\n</post>" for e in examples)
        text = await self._call(prompts.PROFILE_SYSTEM, f"Посты автора:\n\n{items}", "medium")
        return text.strip()

    async def write(self, docs: list[SourceDoc], fmt: str, topic: str, profile: str, examples: list[str],
                    recent_posts: list[str], max_len: int | None = None) -> Written:
        system = build_writer_system(profile, examples, self.gender)
        user = build_writer_prompt(docs, fmt, topic, max_len or self.max_len, recent_posts, self.channel_style)
        data = await self._call_json(system, user, self.effort, prompts.WRITER_SCHEMA)
        post = data.get("post")
        if not isinstance(post, str) or not post.strip():
            raise LLMError("Модель вернула пустой текст поста")
        facts = [f for f in data.get("facts") or [] if isinstance(f, str)]
        fmt_out = data.get("format") if data.get("format") in prompts.FORMATS else fmt
        angle = data.get("angle") if isinstance(data.get("angle"), str) else ""
        log.info("Угол подачи (%s): %s", fmt_out, angle)
        return Written(post=post.strip(), facts=facts, angle=angle, format=fmt_out)

    async def review(self, docs: list[SourceDoc], written: Written, profile: str,
                     recent_posts: list[str], max_len: int | None = None) -> Reviewed:
        profile_block = prompts.WRITER_PROFILE_BLOCK.format(profile=profile) if profile else ""
        system = prompts.REVIEW_SYSTEM.format(
            author=prompts.WRITER_AUTHOR_GENDER.get(self.gender or "", ""), profile=profile_block
        )
        user = build_review_prompt(docs, written.post, written.facts, recent_posts, max_len or self.max_len)
        data = await self._call_json(system, user, self.effort, prompts.REVIEW_SCHEMA)
        post = data.get("post")
        if not isinstance(post, str) or not post.strip():
            raise LLMError("Редактор вернул пустой текст")
        issues = [i for i in data.get("issues") or [] if isinstance(i, str)]
        return Reviewed(post=post.strip(), issues=issues)
