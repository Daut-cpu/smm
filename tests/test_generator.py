from datetime import datetime, timezone

import pytest

from kino_bot.generator import CandidatesExhaustedError, Generator, NotEnoughForDigestError
from kino_bot.llm import LLMError, Reviewed, Selection, SourceDoc, Written
from kino_bot.models import NewsItem


class FakeCollector:
    def __init__(self, news):
        self.news = news

    async def get_news(self):
        return list(self.news)


class FakeWriter:
    def __init__(self, fail_write=False, fail_review=False):
        self.select_calls, self.write_calls, self.review_calls = [], [], []
        self.fail_write, self.fail_review = fail_write, fail_review
        self.limits = []
        self.long = False

    async def select(self, candidates, topics, digest=False):
        self.select_calls.append(([c.title for c in candidates], list(topics), digest))
        if digest:
            if len(candidates) < 3:
                return None
            return Selection(main=candidates[0], related=candidates[1:3], format="digest", topic="Подборка")
        return Selection(main=candidates[0], related=candidates[1:2], format="news", topic=f"Сюжет {candidates[0].title}")

    async def build_profile(self, examples):
        return "профиль"

    async def write(self, docs, fmt, topic, profile, examples, recent, max_len=None):
        self.write_calls.append((docs, fmt, topic, profile, examples, list(recent)))
        self.limits.append(max_len)
        if self.fail_write:
            raise LLMError("сбой")
        if self.long:
            return Written(post="Длинная фраза про кино и сериалы. " * 60, facts=[], angle="", format="news")
        return Written(post=f"Текст про {topic} <a href='x'>x</a> #кино", facts=["факт"], angle="угол", format=fmt or "news")

    async def review(self, docs, written, profile, recent, max_len=None):
        self.review_calls.append(written.post)
        if self.fail_review:
            raise LLMError("сбой проверки")
        return Reviewed(post=written.post.replace("Текст", "Проверенный текст"), issues=["шаблон"])


class FakeImages:
    async def find(self, item):
        return None


class FakeStyle:
    async def get(self):
        return ["Образец"]

    async def profile(self, writer, examples):
        return await writer.build_profile(examples)


def news(n):
    now = datetime.now(timezone.utc)
    return [NewsItem("s", "tg", f"t{i}", f"полный текст {i}", f"https://t.me/c/{i}", now) for i in range(n)]


def gen(n=4, **kw):
    writer = FakeWriter(**{k: v for k, v in kw.items() if k.startswith("fail")})
    g = Generator(FakeCollector(news(n)), writer, FakeImages(), show_source=True, style=FakeStyle(),
                  review=kw.get("review", True))
    return g, writer


async def test_pipeline_select_write_review_and_clean():
    g, w = gen()
    stages = []

    async def progress(s):
        stages.append(s)

    d = await g.generate(1, progress=progress)
    docs, fmt, topic, profile, examples, recent = w.write_calls[0]
    assert [doc.text for doc in docs] == ["полный текст 0", "полный текст 1"]  # главный материал + связанный
    assert profile == "профиль" and examples == ["Образец"] and fmt == "news"
    assert d.text.startswith("Проверенный текст")  # текст после проверки редактором
    assert "#кино" not in d.text and "<a href='x'>" not in d.text
    assert d.text.endswith('<a href="https://t.me/c/0">Источник</a>')
    assert any("Проверяю" in s for s in stages) and any("Пишу" in s for s in stages)


async def test_no_repeats_recent_posts_and_reset():
    g, w = gen(n=4)
    await g.generate(1)
    await g.generate(1)
    assert w.select_calls[1][0] == ["t2", "t3"]  # t0 и связанный t1 уже использованы
    assert "Сюжет t0" in w.select_calls[1][1]
    assert w.write_calls[1][5]  # второму посту переданы недавние посты
    with pytest.raises(CandidatesExhaustedError):
        await g.generate(1)
    d = await g.generate(1)  # история сброшена
    assert d.item.title == "t0"


async def test_failed_write_returns_items_to_pool():
    g, w = gen(fail_write=True)
    with pytest.raises(LLMError):
        await g.generate(1)
    w.fail_write = False
    d = await g.generate(1)
    assert d.item.title == "t0"


async def test_review_failure_falls_back_to_written():
    g, _ = gen(fail_review=True)
    d = await g.generate(1)
    assert d.text.startswith("Текст про")


async def test_review_can_be_disabled():
    g, w = gen(review=False)
    await g.generate(1)
    assert w.review_calls == []


async def test_digest():
    g, _ = gen(n=4)
    d = await g.generate_digest(1)
    assert d.mode == "digest" and "Источник" not in d.text
    with pytest.raises(NotEnoughForDigestError):
        await g.generate_digest(1)


async def test_from_material():
    g, w = gen()
    docs = [SourceDoc(kind="author", source="автор канала", text="Мои заметки с показа")]
    d = await g.generate_from_material(1, docs)
    assert d.mode == "material" and d.docs == docs
    assert w.write_calls[0][1] == ""  # формат выбирает модель
    assert "Источник" not in d.text  # у заметок автора нет ссылки


async def test_post_with_link_fits_photo_caption():
    from kino_bot.formatting import CAPTION_LIMIT, visible_len

    g, w = gen(n=6, review=False)
    w.long = True
    d = await g.generate(1)
    assert w.limits == [990]  # 1000 минус место под ссылку «Источник»
    assert visible_len(d.text) <= 1000 <= CAPTION_LIMIT
    assert d.text.endswith("Источник</a>")
    d2 = await g.generate_digest(1)
    assert w.limits[-1] == 1000  # у подборки ссылки нет
