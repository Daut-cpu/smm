from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from kino_bot.sources.rss import parse_feed
from kino_bot.images import accept_image, find_og_image


def make_feed(now):
    recent = format_datetime(now - timedelta(hours=2))
    old = format_datetime(now - timedelta(hours=50))
    return f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>Variety</title>
<item><title>Dune 3 trailer released</title><link>https://variety.com/2026/dune-3/</link>
<pubDate>{recent}</pubDate><description>&lt;p&gt;New trailer &lt;img src="https://img.example/desc.jpg"&gt;&lt;/p&gt;</description>
<media:content url="https://img.example/media.jpg" medium="image"/></item>
<item><title>Enclosure item</title><link>https://variety.com/2026/enc/</link><pubDate>{recent}</pubDate>
<enclosure url="https://img.example/enc.jpg" type="image/jpeg" length="1"/></item>
<item><title>No date</title><link>https://variety.com/x</link></item>
<item><title>Old</title><link>https://variety.com/old</link><pubDate>{old}</pubDate></item>
</channel></rss>""".encode()


def test_parse_feed_extracts_items_and_images():
    now = datetime.now(timezone.utc)
    items = parse_feed(make_feed(now), "https://variety.com/feed/")
    titles = [i.title for i in items]
    assert "No date" not in titles  # без даты возраст не проверить
    first = items[0]
    assert first.source == "Variety"
    assert first.summary == "New trailer"
    assert first.image_urls[0] == "https://img.example/media.jpg"
    assert "https://img.example/desc.jpg" in first.image_urls
    enc = next(i for i in items if i.title == "Enclosure item")
    assert enc.image_urls == ["https://img.example/enc.jpg"]


def test_og_image():
    page = '<html><head><meta content="/img/poster.jpg" property="og:image"><meta name="twitter:image" content="https://cdn.x/t.png"></head></html>'
    assert find_og_image(page, "https://site.ru/news/1") == ["https://site.ru/img/poster.jpg", "https://cdn.x/t.png"]


def test_accept_image_size_and_type():
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 6000
    assert accept_image(jpeg).filename == "image.jpg"
    assert accept_image(b"\xff\xd8\xff\xe0" + b"0" * 100) is None  # слишком маленькая
    assert accept_image(b"<html>" + b"0" * 6000) is None  # не картинка
    webp = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 6000
    assert accept_image(webp).filename == "image.webp"
