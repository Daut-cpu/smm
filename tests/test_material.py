from datetime import datetime, timezone

from aiogram.types import Chat, Message, MessageEntity, MessageOriginChannel, PhotoSize

from kino_bot.material import _forward_source, looks_like_material, message_urls

NOW = datetime.now(timezone.utc)
CHAT = Chat(id=1, type="private")


def msg(**kw):
    return Message(message_id=1, date=NOW, chat=CHAT, **kw)


def test_urls_from_text_and_entities():
    text = "Смотрите https://variety.com/a, и вот ещё ссылка"
    m = msg(text=text, entities=[MessageEntity(type="text_link", offset=text.index("ссылка"), length=6,
                                               url="https://deadline.com/b")])
    assert message_urls(m) == ["https://deadline.com/b", "https://variety.com/a"]


def test_looks_like_material():
    assert not looks_like_material(msg(text="привет"))
    assert looks_like_material(msg(text="https://variety.com/a"))
    assert looks_like_material(msg(text="Была вчера на премьере, зал аплодировал стоя минут пять, " * 2))
    photo = [PhotoSize(file_id="f", file_unique_id="u", width=10, height=10)]
    assert looks_like_material(msg(photo=photo, caption="Мой кадр с фестиваля"))
    assert not looks_like_material(msg(photo=photo))


def test_forwarded_post():
    origin = MessageOriginChannel(type="channel", date=NOW, chat=Chat(id=-100, type="channel", username="kinonews",
                                                                     title="Кино"), message_id=42)
    m = msg(text="Короткий пересказ", forward_origin=origin)
    assert looks_like_material(m)
    assert _forward_source(m) == ("@kinonews", "https://t.me/kinonews/42")
