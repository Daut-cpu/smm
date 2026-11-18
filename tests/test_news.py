from datetime import datetime, timedelta, timezone

from kino_bot.models import NewsItem
from kino_bot.news import dedupe_and_sort


def item(title, link, hours, source="s"):
    return NewsItem(source=source, kind="rss", title=title, summary="", link=link,
                    published=datetime.now(timezone.utc) - timedelta(hours=hours))


def test_dedupe_by_link_and_title_and_sort():
    items = [
        item("Old", "https://a.com/old", 30),
        item("Дюна 3: трейлер", "https://a.com/1", 5),
        item("Дюна 3 — трейлер!", "https://b.com/2", 3),  # тот же заголовок
        item("Другое", "https://www.a.com/1/?utm=x", 2),  # та же ссылка
        item("Свежее", "https://c.com/3", 1),
    ]
    out = dedupe_and_sort(items, 24)
    assert [i.title for i in out] == ["Свежее", "Другое", "Дюна 3 — трейлер!"]


def test_limit():
    items = [item(f"t{i}", f"https://x.com/{i}", i * 0.1) for i in range(100)]
    assert len(dedupe_and_sort(items, 24)) == 60
