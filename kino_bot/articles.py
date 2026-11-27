"""Полный текст статьи со страницы сайта — материал для авторского поста."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx

from .images import find_og_image

log = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 9000
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "figure", "svg",
             "button", "iframe", "template"}
TEXT_TAGS = {"p", "h2", "h3", "blockquote", "li"}
# Типичный мусор в конце статей и в подписях
JUNK_RE = re.compile(
    r"(подпис(ывайтесь|аться)|читайте также|реклама|cookie|newsletter|sign up|subscribe|all rights reserved|"
    r"©|advertisement|related:|read more|getty images)",
    re.I,
)


@dataclass
class Article:
    url: str
    title: str
    text: str
    image_urls: list[str]


class _TextExtractor(HTMLParser):
    """Собирает абзацы текста; если на странице есть <article>, берёт только его."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.article_depth = 0
        self.saw_article = False
        self.current: list[str] | None = None
        self.current_tag = ""
        self.blocks: list[tuple[bool, str, str]] = []  # (внутри article, тег, текст)
        self.title = ""
        self._in_title = False
        self._in_h1 = False
        self.h1 = ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "article":
            self.article_depth += 1
            self.saw_article = True
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag in TEXT_TAGS and not self.skip_depth:
            self._flush()
            self.current = []
            self.current_tag = tag
        elif tag == "br" and self.current is not None:
            self.current.append(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "article" and self.article_depth:
            self._flush()
            self.article_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag in TEXT_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
        if self.current is not None and not self.skip_depth:
            self.current.append(data)

    def _flush(self):
        if self.current is not None:
            text = re.sub(r"\s+", " ", "".join(self.current)).strip()
            if text:
                self.blocks.append((self.article_depth > 0, self.current_tag, text))
        self.current = None


def extract_article_text(page_html: str) -> tuple[str, str]:
    """Возвращает (заголовок, текст статьи)."""
    parser = _TextExtractor()
    try:
        parser.feed(page_html[:2_000_000])
        parser.close()
    except Exception:  # noqa: BLE001 — кривой HTML: берём, что успели собрать
        pass
    parser._flush()
    blocks = parser.blocks
    if parser.saw_article and any(inside for inside, _, _ in blocks):
        blocks = [b for b in blocks if b[0]]
    paragraphs: list[str] = []
    seen: set[str] = set()
    for _, tag, text in blocks:
        min_len = 25 if tag in {"h2", "h3", "blockquote"} else 50
        if len(text) < min_len or text in seen:
            continue
        if len(text) < 200 and JUNK_RE.search(text):
            continue
        seen.add(text)
        paragraphs.append(text)
    body = "\n\n".join(paragraphs)
    if len(body) > MAX_ARTICLE_CHARS:
        body = body[:MAX_ARTICLE_CHARS].rsplit("\n\n", 1)[0]
    title = html.unescape((parser.h1 or parser.title).strip())
    return re.sub(r"\s+", " ", title), body


async def fetch_article(http: httpx.AsyncClient, url: str, timeout: float = 10) -> Article | None:
    try:
        resp = await http.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — недоступная статья не ломает генерацию
        log.info("Статья %s недоступна: %s", url, exc)
        return None
    if "html" not in resp.headers.get("content-type", "text/html"):
        return None
    page = resp.text
    title, text = extract_article_text(page)
    return Article(url=str(resp.url), title=title, text=text, image_urls=find_og_image(page, str(resp.url)))
