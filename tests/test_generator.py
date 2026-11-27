from datetime import datetime, timezone

import pytest

from kino_bot.generator import CandidatesExhaustedError, Generator
from kino_bot.llm import LLMChoice
from kino_bot.models import NewsItem


class FakeCollector:
    def __init__(self, news):
        self.news = news

    async def get_news(self):
        return list(self.news)


class FakeWriter:
    def __init__(self):
        self.calls = []

    async def choose_and_write(self, candidates, topics, examples=None):
        self.calls.append(([c.title for c in candidates], list(topics), list(examples or [])))
        c = candidates[0]
        return LLMChoice(item=c, topic=f"Сюжет {c.title}", post=f"<b>{c.title}</b>\n\nТекст <a href='x'>x</a>\n\n#кино")


class FakeImages:
    async def find(self, item):
        return None


def news(n):
    now = datetime.now(timezone.utc)
    return [NewsItem("s", "rss", f"t{i}", "", f"https://x.com/{i}", now) for i in range(n)]


async def test_generate_no_repeats_and_reset():
    writer = FakeWriter()
    gen = Generator(FakeCollector(news(2)), writer, FakeImages(), show_source=True)
    d1 = await gen.generate(1)
    d2 = await gen.generate(1)
    assert d1.item.title != d2.item.title
    assert "Сюжет t0" in writer.calls[1][1]  # история передана модели
    assert '<a href="https://x.com/1">Источник</a>' in d2.text
    assert "<a href='x'>" not in d2.text
    assert "#кино" not in d2.text  # хэштеги вырезаются
    with pytest.raises(CandidatesExhaustedError):
        await gen.generate(1)
    d3 = await gen.generate(1)  # история сброшена
    assert d3.item.title == "t0"


class FakeStyle:
    async def get(self):
        return ["Образец поста"]


async def test_style_examples_passed_to_writer():
    writer = FakeWriter()
    gen = Generator(FakeCollector(news(1)), writer, FakeImages(), show_source=False, style=FakeStyle())
    await gen.generate(1)
    assert writer.calls[0][2] == ["Образец поста"]
