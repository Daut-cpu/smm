from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from kino_bot.sources.telegram import build_items, is_ad, make_title


def msg(id, text="", minutes=10, grouped_id=None, photo=False, now=None):
    return NS(id=id, message=text, date=now - timedelta(minutes=minutes), grouped_id=grouped_id,
              photo=object() if photo else None)


def test_ads_detected():
    assert is_ad("Отличный сериал! #реклама")
    assert is_ad("Подробнее по ссылке. erid: 2Vtzqx")
    assert is_ad("На правах рекламы")
    assert not is_ad("Режиссёр рекламировал фильм на фестивале")


def test_build_items_albums_ads_and_textless():
    now = datetime.now(timezone.utc)
    messages = [
        msg(10, "Реклама: купите курс\nerid: abc", now=now),
        msg(9, "", photo=True, now=now),  # пост без текста
        msg(8, "", grouped_id=1, photo=True, now=now),
        msg(7, "**Новый трейлер «Дюны 3»**\nПодробности ниже", grouped_id=1, photo=True, now=now),
        msg(6, "Кастинг сиквела", minutes=60 * 30, now=now),  # старше 24 часов
    ]
    items = build_items("kinochannel", messages, 24, now=now)
    assert len(items) == 1
    item = items[0]
    assert item.title == "Новый трейлер «Дюны 3»"
    assert item.link == "https://t.me/kinochannel/7"
    assert item.tg_photo_ref == ("kinochannel", 7)  # первое фото альбома
    assert item.source == "@kinochannel"


def test_make_title_truncates():
    assert make_title("\n\n" + "а" * 300).endswith("…")
