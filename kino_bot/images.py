"""Подбор и скачивание картинки к новости."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .models import NewsItem
from .sources.telegram import TelegramReader

log = logging.getLogger(__name__)

MIN_IMAGE_BYTES = 5 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # лимит Telegram на фото

_META_RE = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+))""")

SIGNATURES = {
    b"\xff\xd8\xff": ("image/jpeg", "jpg"),
    b"\x89PNG\r\n\x1a\n": ("image/png", "png"),
    b"GIF87a": ("image/gif", "gif"),
    b"GIF89a": ("image/gif", "gif"),
}


@dataclass
class Image:
    data: bytes
    filename: str


def detect_image(data: bytes) -> str | None:
    """Возвращает расширение файла, если байты похожи на изображение."""
    for sig, (_, ext) in SIGNATURES.items():
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def accept_image(data: bytes | None) -> Image | None:
    if not data or len(data) <= MIN_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
        return None
    ext = detect_image(bytes(data[:16]))
    if not ext or ext == "gif":  # gif Telegram отправит анимацией, а не фото
        return None
    return Image(data=bytes(data), filename=f"image.{ext}")


def find_og_image(page_html: str, base_url: str) -> list[str]:
    found: list[str] = []
    for tag in _META_RE.findall(page_html[:300_000]):
        attrs = {}
        for m in _ATTR_RE.finditer(tag):
            attrs[m.group(1).lower()] = m.group(3) or m.group(4) or m.group(5) or ""
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
            url = html.unescape(attrs.get("content", "")).strip()
            if url:
                url = urljoin(base_url, url)
                if url not in found:
                    found.append(url)
    return found


class ImageFinder:
    def __init__(self, http: httpx.AsyncClient, tg: TelegramReader | None):
        self.http = http
        self.tg = tg

    async def find(self, item: NewsItem) -> Image | None:
        try:
            if item.kind == "tg":
                if self.tg is None:
                    return None
                return accept_image(await self.tg.download_photo(item))
            for url in item.image_urls[:4]:
                image = await self._download(url, referer=item.link)
                if image:
                    return image
            if item.link:
                for url in await self._og_images(item.link):
                    image = await self._download(url, referer=item.link)
                    if image:
                        return image
        except Exception as exc:  # noqa: BLE001 — без картинки пост всё равно отправляется
            log.warning("Не удалось получить картинку для %s: %s", item.link, exc)
        return None

    async def _og_images(self, page_url: str) -> list[str]:
        try:
            resp = await self.http.get(page_url, timeout=10)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.info("Страница %s недоступна для поиска og:image: %s", page_url, exc)
            return []
        if "html" not in resp.headers.get("content-type", "html"):
            return []
        return find_og_image(resp.text, str(resp.url))

    async def _download(self, url: str, referer: str = "") -> Image | None:
        headers = {"Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"}
        if referer:
            headers["Referer"] = referer
        try:
            async with self.http.stream("GET", url, headers=headers, timeout=10) as resp:
                if resp.status_code != 200:
                    return None
                ctype = resp.headers.get("content-type", "").lower()
                if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
                    return None
                chunks = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        return None
                    chunks.append(chunk)
            return accept_image(b"".join(chunks))
        except Exception as exc:  # noqa: BLE001
            log.info("Картинка %s не скачалась: %s", url, exc)
            return None
