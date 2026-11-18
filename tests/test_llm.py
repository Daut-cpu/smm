import json
from datetime import datetime, timezone

import pytest

from kino_bot.llm import LLMError, build_user_prompt, parse_response
from kino_bot.models import NewsItem


def cands():
    now = datetime.now(timezone.utc)
    return [
        NewsItem("Variety", "rss", "Dune 3 trailer", "Desc", "https://v.com/1", now),
        NewsItem("@kino", "tg", "Кастинг", "Текст поста из канала", "https://t.me/kino/5", now),
    ]


def test_parse_ok():
    c = cands()
    choice = parse_response(json.dumps({"index": 2, "topic": "Кастинг", "post": "<b>Х</b>"}), c)
    assert choice.item is c[1] and choice.post == "<b>Х</b>"


def test_parse_none():
    assert parse_response('{"index": 0, "topic": "", "post": ""}', cands()) is None


@pytest.mark.parametrize("text", ["не json", "[]", '{"index": 7, "topic": "", "post": "x"}',
                                  '{"index": 1, "topic": "", "post": "  "}', '{"post": "x"}'])
def test_parse_errors(text):
    with pytest.raises(LLMError):
        parse_response(text, cands())


def test_prompt_contains_candidates_and_history():
    p = build_user_prompt(cands(), ["Трейлер «Дюны 3»"])
    assert "[1] Variety (сайт)" in p and "[2] @kino (Telegram-канал)" in p
    assert "Трейлер «Дюны 3»" in p
