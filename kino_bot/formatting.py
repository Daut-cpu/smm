"""Очистка и проверка HTML-текста постов для Telegram."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = {"b", "i", "u", "s"}
TAG_ALIASES = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s"}
BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

MAX_POST_LEN = 800
CAPTION_LIMIT = 1024


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = TAG_ALIASES.get(tag, tag)
        if tag in ALLOWED_TAGS:
            self.out.append(f"<{tag}>")
            self.stack.append(tag)
        elif tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        tag = TAG_ALIASES.get(tag, tag)
        if tag in ALLOWED_TAGS and tag in self.stack:
            # закрываем всё, что открыто после этого тега, чтобы теги были вложены корректно
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break
        elif tag in {"p", "div", "li"} or tag.startswith("h") and tag[1:].isdigit():
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def _markdown_to_html(text: str) -> str:
    """Подстраховка на случай, если модель всё же использовала markdown."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)
    return text


def sanitize_html(text: str) -> str:
    """Оставляет только теги b, i, u, s; остальное — экранированный текст."""
    text = _markdown_to_html(text or "")
    parser = _Sanitizer()
    parser.feed(text)
    parser.close()
    result = parser.result()
    # убираем пустые теги и лишние пустые строки
    result = re.sub(r"<(b|i|u|s)>\s*</\1>", "", result)
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def visible_text(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def visible_len(text: str) -> int:
    return len(visible_text(text))


_TOKEN_RE = re.compile(r"(<[^>]+>|&[#\w]+;|[^<&]+|[<&])")


def truncate_html(text: str, limit: int) -> str:
    """Обрезает HTML до ``limit`` видимых символов по границе предложения, закрывая теги."""
    if visible_len(text) <= limit:
        return text
    out: list[str] = []
    stack: list[str] = []
    count = 0
    for token in _TOKEN_RE.findall(text):
        if token.startswith("<") and token.endswith(">") and len(token) > 1:
            m = re.match(r"</?\s*(\w+)", token)
            name = m.group(1) if m else ""
            if token.startswith("</"):
                if stack and stack[-1] == name:
                    stack.pop()
            else:
                stack.append(name)
            out.append(token)
            continue
        if token.startswith("&") and token.endswith(";"):
            if count + 1 > limit:
                break
            out.append(token)
            count += 1
            continue
        remaining = limit - count
        if len(token) <= remaining:
            out.append(token)
            count += len(token)
        else:
            out.append(token[:remaining])
            count = limit
            break
    cut = "".join(out)
    # откатываемся к концу последнего предложения, если оно не слишком далеко
    m = None
    for m in re.finditer(r"[.!?…](?:</\w+>)*(?=\s|$)", cut):
        pass
    if m and visible_len(cut[: m.end()]) >= limit * 0.6:
        cut = cut[: m.end()]
        # пересчитываем незакрытые теги
        stack = []
        for tag in re.finditer(r"<(/?)(\w+)[^>]*>", cut):
            if tag.group(1):
                if stack and stack[-1] == tag.group(2):
                    stack.pop()
            else:
                stack.append(tag.group(2))
    else:
        cut = cut.rstrip() + "…"
    for name in reversed(stack):
        cut += f"</{name}>"
    return cut


_HASHTAG_LINE_RE = re.compile(r"^\s*(#[\w]+[\s,]*)+$", re.U)


def fit_post(text: str, limit: int = MAX_POST_LEN) -> str:
    """Укладывает пост в лимит, сохраняя заголовок и строку с хэштегами."""
    if visible_len(text) <= limit:
        return text
    paragraphs = text.split("\n\n")
    tags = ""
    if paragraphs and _HASHTAG_LINE_RE.match(visible_text(paragraphs[-1])):
        tags = paragraphs.pop()
    reserve = visible_len(tags) + 2 if tags else 0
    body = "\n\n".join(paragraphs)
    body = truncate_html(body, max(100, limit - reserve))
    return f"{body}\n\n{tags}" if tags else body


_HASHTAG_RE = re.compile(r"(?<![\w&])#[\w]+")


def strip_hashtags(text: str) -> str:
    """Удаляет хэштеги, если модель всё же их добавила, и пустые строки после них."""
    text = _HASHTAG_RE.sub("", text)
    text = re.sub(r"<(b|i|u|s)>\s*</\1>", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def first_paragraph(text: str) -> str:
    plain = visible_text(text).strip()
    return plain.split("\n", 1)[0].strip()


SOURCE_LINK_TEXT = "Источник"


def source_link(url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{SOURCE_LINK_TEXT}</a>'
