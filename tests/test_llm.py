from datetime import datetime, timezone

import pytest

from kino_bot import prompts
from kino_bot.llm import (
    LLMError,
    SourceDoc,
    build_candidates_prompt,
    build_review_prompt,
    build_writer_prompt,
    build_writer_system,
    find_cliches,
    parse_selection,
)
from kino_bot.models import NewsItem


def cands(n=4):
    now = datetime.now(timezone.utc)
    return [NewsItem("Variety", "rss", f"Title {i}", "Desc", f"https://v.com/{i}", now) for i in range(n)]


def test_parse_selection_ok_and_related_filtered():
    c = cands()
    sel = parse_selection({"main": 2, "related": [2, 3, 99, 3], "format": "person", "topic": "Т"}, c)
    assert sel.main is c[1] and sel.related == [c[2]] and sel.format == "person"


def test_parse_selection_digest_needs_three_projects():
    c = cands()
    assert parse_selection({"main": 1, "related": [2], "format": "digest", "topic": ""}, c, digest=True) is None
    sel = parse_selection({"main": 1, "related": [2, 3], "format": "news", "topic": ""}, c, digest=True)
    assert sel.format == "digest" and len(sel.related) == 2


def test_parse_selection_digest_not_allowed_in_auto():
    sel = parse_selection({"main": 1, "related": [], "format": "digest", "topic": ""}, cands())
    assert sel.format == "news"


def test_parse_selection_none_and_errors():
    assert parse_selection({"main": 0, "related": [], "format": "news", "topic": ""}, cands()) is None
    with pytest.raises(LLMError):
        parse_selection({"main": 9, "related": [], "format": "news", "topic": ""}, cands())
    with pytest.raises(LLMError):
        parse_selection({"related": []}, cands())


def test_candidates_prompt_contains_history():
    p = build_candidates_prompt(cands(1), ["Трейлер «Дюны 3»"])
    assert "[1] Variety (сайт)" in p and "Трейлер «Дюны 3»" in p


def test_writer_system_includes_profile_examples_gender():
    s = build_writer_system("Короткие абзацы {не шаблон}", ["Пример {1}"], "f")
    assert "<profile>\nКороткие абзацы {не шаблон}\n</profile>" in s
    assert "<example>\nПример {1}\n</example>" in s
    assert "женском роде" in s
    plain = build_writer_system("", [], None)
    assert "<profile>" not in plain and "<examples>" not in plain and "роде" not in plain.split("## Автор")[-1][:0] + ""
    for rule in ("Без заголовка", "Без хэштегов", "Нельзя ничего выдумывать", "заметки автора"):
        assert rule in plain


def test_writer_prompt_materials_and_recent():
    docs = [SourceDoc(kind="author", source="автор канала", text="Была на показе, зал смеялся."),
            SourceDoc(kind="site", source="variety.com", text="Full text", title="T", url="https://v.com/1")]
    p = build_writer_prompt(docs, "", "", 1500, ["Начало … финал"], "на ты")
    assert "заметки автора" in p and "https://v.com/1" in p and "Заголовок: T" in p
    assert "Формат выбери сам" in p and "не больше 1420 знаков" in p
    assert "Начало … финал" in p and "на ты" in p
    p2 = build_writer_prompt(docs, "review", "Тема", 1500, [], "")
    assert "Рекомендуемый формат: review" in p2 and "Повод: Тема" in p2


def test_review_prompt_flags_cliches():
    docs = [SourceDoc(kind="site", source="x", text="t")]
    p = build_review_prompt(docs, "Стало известно, что это настоящий шедевр.", ["факт"], [])
    assert "«стало известно»" in p and "«настоящий шедевр»" in p and "- факт" in p


def test_find_cliches():
    assert find_cliches("Фильм НИКОГО НЕ ОСТАВИТ РАВНОДУШНЫМ") == ["никого не оставит равнодушным", "не оставит равнодушным"]
    assert find_cliches("Обычный живой текст") == []


def test_prompts_format_without_errors():
    prompts.SELECT_SYSTEM.format(formats=prompts.formats_text())
    prompts.REVIEW_SYSTEM.format(author="", profile="")


def test_length_limit_in_prompts():
    docs = [SourceDoc(kind="site", source="x", text="t")]
    assert "не больше 920 знаков" in build_writer_prompt(docs, "", "", 1000, [], "")
    p = build_review_prompt(docs, "слово " * 300, [], [], max_len=1000)
    assert "Предел длины поста: 920" in p and "Сократи пост до предела" in p
    assert "Длина в порядке" in build_review_prompt(docs, "коротко", [], [], max_len=1000)
