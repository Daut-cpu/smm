"""Выбор новости и написание поста с помощью Claude API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from .models import NewsItem

log = logging.getLogger(__name__)

MAX_CANDIDATES = 40

# Модели, для которых доступен серверный фолбэк при отказе модели (refusal).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5-2"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {
            "type": "integer",
            "description": "Номер выбранной новости из списка; 0, если подходящих новостей нет",
        },
        "topic": {
            "type": "string",
            "description": "Короткое описание сюжета по-русски (5–10 слов), чтобы не повторяться в будущем",
        },
        "post": {"type": "string", "description": "Готовый текст поста в HTML"},
    },
    "required": ["index", "topic", "post"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты — редактор русскоязычного Telegram-канала про кино и сериалы. \
Из списка свежих новостей ты выбираешь одну, самую интересную для аудитории канала, и пишешь по ней готовый пост.

Как выбирать новость:
- Приоритет: премьеры и даты выхода, трейлеры и тизеры, кастинг, кассовые сборы, награды и номинации, громкие анонсы, продолжения известных франшиз.
- Если один и тот же сюжет есть в нескольких источниках, он важнее.
- Не выбирай сюжеты из списка «Уже предлагали», даже если их пишет другой источник или они сформулированы иначе.
- Пропускай рекламу, розыгрыши, подборки «что посмотреть», мелкие локальные новости и сюжеты, далёкие от кино и сериалов.
- Если ни одна новость не подходит (все повторяют уже предложенные сюжеты или не про кино), верни index = 0, пустые topic и post.

Как писать пост:
- Только на русском, даже если новость на английском.
- Названия фильмов и сериалов — как в российском прокате, в «ёлочках»; если русское название неизвестно, оставь оригинальное.
- Без заголовка: не выделяй первую строку как заголовок, сразу начинай с текста.
- Без хэштегов: ни одного символа # в посте.
- Длина поста — не больше {max_len} знаков.
- Только факты из исходной новости: не придумывай даты, цифры, имена и цитаты, которых в ней нет.
- Если новость взята из Telegram-канала, перескажи её своими словами, не копируй фразы. Удали чужие подписи, ссылки, призывы подписаться и упоминания @каналов.
- Форматирование — только HTML-теги <b> и <i>, и только если они есть в манере канала. Никакого markdown, ссылок и других тегов. Ссылку на источник бот добавит сам.

Стиль и аудитория канала:
{style}
{examples}
Ответ — строго JSON по схеме: index (номер новости из списка), topic (короткое описание сюжета), post (текст поста)."""

EXAMPLES_BLOCK = """
Ниже — посты канала, на которые нужно равняться. Пиши новый пост в точно такой же подаче: так же начинай пост, так же строй и разбивай на абзацы текст, держи ту же длину предложений и абзацев, то же обращение к читателю (на «ты» или на «вы»), ту же лексику, интонацию, юмор, то же количество и расположение эмодзи и то же форматирование. Если в образцах нет эмодзи или жирного шрифта, не используй их.
Перенимай только манеру. Не копируй из образцов предложения, факты, имена, подписи, ссылки и упоминания автора или канала. Правила «без заголовка» и «без хэштегов» важнее образцов.

<examples>
{items}
</examples>
"""


def build_system_prompt(style: str, examples: list[str], max_len: int) -> str:
    block = ""
    if examples:
        items = "\n".join(f"<example>\n{e}\n</example>" for e in examples)
        block = EXAMPLES_BLOCK.format(items=items)
    return SYSTEM_PROMPT.format(style=style, examples=block, max_len=max(200, max_len - 50))


class LLMError(Exception):
    """Ошибка генерации, текст понятен администратору."""


@dataclass
class LLMChoice:
    item: NewsItem
    topic: str
    post: str


def _age(item: NewsItem, now: datetime) -> str:
    hours = item.age_hours(now)
    if hours < 1:
        return f"{max(1, int(hours * 60))} мин назад"
    return f"{int(hours)} ч назад"


def build_user_prompt(candidates: list[NewsItem], previous_topics: list[str], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["Свежие новости:"]
    for n, item in enumerate(candidates, start=1):
        kind = "Telegram-канал" if item.kind == "tg" else "сайт"
        summary = item.summary.replace("\n", " ")
        if item.kind == "tg":
            header = f"[{n}] {item.source} ({kind}) · {_age(item, now)}"
            body = summary[:600]
        else:
            header = f"[{n}] {item.source} ({kind}) · {_age(item, now)} · {item.title}"
            body = summary[:400]
        lines.append(header)
        if body:
            lines.append(f"    {body}")
    lines.append("")
    if previous_topics:
        lines.append("Уже предлагали (не повторять эти сюжеты):")
        lines.extend(f"- {t}" for t in previous_topics[-60:])
    else:
        lines.append("Уже предлагали: ничего.")
    lines.append("")
    lines.append("Выбери одну новость и напиши пост.")
    return "\n".join(lines)


def parse_response(text: str, candidates: list[NewsItem]) -> LLMChoice | None:
    """Разбирает JSON-ответ модели. None — модель сообщила, что подходящих новостей нет."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("Модель вернула некорректный JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("Модель вернула неожиданный формат ответа")
    index = data.get("index")
    if isinstance(index, str) and index.strip().isdigit():
        index = int(index.strip())
    if not isinstance(index, int) or isinstance(index, bool):
        raise LLMError("В ответе модели нет номера новости")
    if index == 0:
        return None
    if not 1 <= index <= len(candidates):
        raise LLMError(f"Модель выбрала несуществующую новость №{index}")
    post = data.get("post")
    if not isinstance(post, str) or not post.strip():
        raise LLMError("Модель вернула пустой текст поста")
    topic = data.get("topic") if isinstance(data.get("topic"), str) else ""
    return LLMChoice(item=candidates[index - 1], topic=topic.strip(), post=post.strip())


class PostWriter:
    def __init__(self, api_key: str, model: str, effort: str, style: str, max_len: int = 800):
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0, max_retries=1)
        self.model = model
        self.effort = effort
        self.style = style
        self.max_len = max_len

    async def choose_and_write(self, candidates: list[NewsItem], previous_topics: list[str],
                               examples: list[str] | None = None) -> LLMChoice | None:
        candidates = candidates[:MAX_CANDIDATES]
        prompt = build_user_prompt(candidates, previous_topics)
        system = build_system_prompt(self.style, examples or [], self.max_len)
        params = dict(
            model=self.model,
            max_tokens=8000,
            # Образцы стиля меняются раз в сутки — кешируем системный промпт, чтобы не платить за них заново
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
            },
        )
        try:
            if self.model in FALLBACK_MODELS:
                response = await self.client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **params
                )
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
            "Claude: stop_reason=%s, input=%s, output=%s",
            response.stop_reason,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
        if response.stop_reason == "refusal":
            raise LLMError("Модель отказалась писать пост по этим новостям")
        if response.stop_reason == "max_tokens":
            raise LLMError("Ответ модели оборвался по лимиту длины")
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        if not text:
            raise LLMError("Модель вернула пустой ответ")
        return parse_response(text, candidates)
