"""Чтение новостей киносайтов через RSS."""

from __future__ import annotations

import calendar
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from ..models import NewsItem

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def feed_name(url: str, parsed_title: str | None = None) -> str:
    if parsed_title:
        title = strip_html(parsed_title)
        if 0 < len(title) <= 40:
            return title
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return host or url


def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def extract_images(entry, base_url: str = "") -> list[str]:
    """Картинки из записи RSS: media:content, media:thumbnail, enclosure, <img> в описании."""
    urls: list[str] = []

    def add(u: str | None) -> None:
        if u and u.startswith(("http://", "https://", "//")):
            u = "https:" + u if u.startswith("//") else u
            if u not in urls:
                urls.append(u)
        elif u and base_url:
            add(urljoin(base_url, u))

    for media in entry.get("media_content", []) or []:
        mtype = (media.get("type") or "").lower()
        medium = (media.get("medium") or "").lower()
        if mtype.startswith("image") or medium == "image" or (not mtype and not medium):
            add(media.get("url"))
    for thumb in entry.get("media_thumbnail", []) or []:
        add(thumb.get("url"))
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").lower().startswith("image"):
            add(enc.get("href") or enc.get("url"))
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and (link.get("type") or "").lower().startswith("image"):
            add(link.get("href"))
    html_parts = [entry.get("summary") or ""]
    for content in entry.get("content", []) or []:
        html_parts.append(content.get("value") or "")
    for part in html_parts:
        for m in _IMG_RE.finditer(part):
            add(html.unescape(m.group(1)))
    return urls


def parse_feed(content: bytes, url: str) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    name = feed_name(url, parsed.feed.get("title") if parsed.get("feed") else None)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        title = strip_html(entry.get("title") or "")
        link = (entry.get("link") or "").strip()
        published = _entry_date(entry)
        if not title or not published:
            continue
        summary = strip_html(entry.get("summary") or "")
        if not summary:
            for content in entry.get("content", []) or []:
                summary = strip_html(content.get("value") or "")
                if summary:
                    break
        items.append(
            NewsItem(
                source=name,
                kind="rss",
                title=title,
                summary=summary[:1500],
                link=link,
                published=published,
                image_urls=extract_images(entry, link or url),
            )
        )
    return items


async def fetch_feed(client: httpx.AsyncClient, url: str) -> list[NewsItem]:
    resp = await client.get(url)
    resp.raise_for_status()
    return parse_feed(resp.content, url)


def make_http_client(timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
    )
