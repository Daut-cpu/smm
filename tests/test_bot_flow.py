from datetime import datetime, timezone
from types import SimpleNamespace as NS

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from kino_bot.bot import KinoBot
from kino_bot.generator import Draft
from kino_bot.images import Image
from kino_bot.models import NewsItem

ADMIN = 100
CHANNEL = "@kino"


class FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, kind, text/caption, markup)
        self.deleted = []
        self.edited = []
        self.fail_channel = False
        self._id = 0

    def _msg(self, photo=False):
        self._id += 1
        return NS(message_id=self._id, photo=[NS(file_id=f"file{self._id}")] if photo else None,
                  delete=self._noop)

    async def _noop(self, *a, **k):
        return True

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if chat_id == CHANNEL and self.fail_channel:
            raise TelegramForbiddenError(method=SendMessage(chat_id=chat_id, text=text),
                                         message="Forbidden: bot is not a member of the channel chat")
        self.sent.append((chat_id, "text", text, reply_markup))
        return self._msg()

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        self.sent.append((chat_id, "photo", caption, reply_markup, photo))
        return self._msg(photo=True)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edited.append(message_id)

    async def send_chat_action(self, *a, **k):
        pass

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.sent.append((chat_id, "edit", text, None))


class FakeGenerator:
    def __init__(self):
        self.n = 0
        self.calls = []

    async def generate(self, chat_id, progress=None, mode="auto"):
        self.n += 1
        self.calls.append(mode)
        if progress:
            await progress("✍️ Пишу пост…")
        item = NewsItem("s", "rss", f"t{self.n}", "", f"https://x/{self.n}", datetime.now(timezone.utc))
        img = Image(b"\xff\xd8\xff" + b"0" * 6000, "image.jpg") if self.n % 2 else None
        return Draft(id=f"d{self.n}", chat_id=chat_id, text=f"<b>Пост {self.n}</b>", item=item,
                     topic=f"t{self.n}", image=img, mode=mode)

    async def generate_digest(self, chat_id, progress=None):
        return await self.generate(chat_id, progress, mode="digest")

    async def generate_from_material(self, chat_id, docs, image=None, source_url="", progress=None):
        d = await self.generate(chat_id, progress, mode="material")
        d.docs = docs
        return d


def query(data, user=ADMIN):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    return NS(data=data, from_user=NS(id=user), message=NS(chat=NS(id=user), message_id=999),
              answer=answer, answers=answers)


def message(user=ADMIN, text="/idea"):
    replies = []

    async def answer(t, **kw):
        replies.append(t)

    return NS(from_user=NS(id=user), chat=NS(id=user), text=text, answer=answer, replies=replies)


def make():
    bot = FakeBot()
    return KinoBot(bot, FakeGenerator(), CHANNEL, {ADMIN}), bot


async def test_idea_publish_flow():
    app, bot = make()
    await app.cmd_idea(message())
    draft_msgs = [s for s in bot.sent if s[0] == ADMIN and s[3] is not None]
    assert len(draft_msgs) == 1 and draft_msgs[0][1] == "photo"  # фото с подписью и кнопками
    q = query("pub:d1")
    await app.on_publish(q)
    channel = [s for s in bot.sent if s[0] == CHANNEL]
    assert channel[0][1] == "photo" and channel[0][2] == "<b>Пост 1</b>"
    assert channel[0][4] == "file2"  # публикуется тот же file_id, что и в черновике
    assert any("опубликован" in (s[2] or "") for s in bot.sent if s[0] == ADMIN)
    assert bot.edited  # кнопки убраны
    stale = query("pub:d1")
    await app.on_publish(stale)
    assert stale.answers == ["Черновик уже неактуален"]
    assert len([s for s in bot.sent if s[0] == CHANNEL]) == 1


async def test_more_and_cancel():
    app, bot = make()
    await app.cmd_idea(message())
    await app.on_more(query("more:d1"))
    assert app.drafts.get("d1") is None and app.drafts.get("d2") is not None
    await app.on_cancel(query("cancel:d2"))
    assert app.drafts.get("d2") is None
    assert bot.deleted  # черновик удалён из чата
    assert any((s[2] or "").startswith("Отменено") for s in bot.sent)


async def test_publish_failure_keeps_draft():
    app, bot = make()
    await app.cmd_idea(message())
    bot.fail_channel = True
    app.drafts.get("d1").photo_file_id = None
    app.drafts.get("d1").image = None
    await app.on_publish(query("pub:d1"))
    assert app.drafts.get("d1") is not None
    assert any("бот не администратор" in (s[2] or "") for s in bot.sent if s[0] == ADMIN)


async def test_non_admin_denied():
    app, bot = make()
    m = message(user=555)
    await app.cmd_idea(m)
    assert m.replies and m.replies[0].startswith("Нет доступа")
    await app.cmd_idea(message())
    q = query("pub:d1", user=555)
    await app.on_publish(q)
    assert q.answers == ["Нет доступа"]
    assert not [s for s in bot.sent if s[0] == CHANNEL]
    m2 = message(user=555, text="/myid")
    await app.cmd_myid(m2)
    assert "555" in m2.replies[0]


async def test_more_keeps_mode():
    from kino_bot.llm import SourceDoc
    from kino_bot.material import Material

    app, bot = make()
    await app.make_draft(ADMIN, mode="digest")
    await app.on_more(query("more:d1"))
    docs = [SourceDoc(kind="author", source="автор канала", text="заметки")]
    await app.make_draft(ADMIN, mode="material", material=Material(docs=docs))
    await app.on_more(query("more:d3"))
    assert app.generator.calls == ["digest", "digest", "material", "material"]
    assert app.drafts.get("d4").docs == docs  # новый вариант по тому же материалу
    assert any(s[1] == "edit" and "Пишу" in s[2] for s in bot.sent)  # статус показывает этап


async def test_short_text_gets_hint_not_material():
    app, bot = make()
    m = message(text="привет")
    m.media_group_id = None
    m.forward_origin = None
    m.photo = None
    m.caption = None
    m.entities = None
    m.caption_entities = None
    app.http = object()
    await app.any_text(m)
    assert "/idea" in m.replies[0]
    assert app.generator.calls == []


async def test_caption_sized_post_is_one_message():
    from kino_bot.bot import send_post

    bot = FakeBot()
    text = "а" * 1000
    msgs, file_id = await send_post(bot, ADMIN, text, "photo-id")
    assert len(msgs) == 1 and bot.sent[0][1] == "photo" and bot.sent[0][2] == text
