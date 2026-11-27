from types import SimpleNamespace as NS

from kino_bot.formatting import strip_hashtags
from kino_bot.llm import build_system_prompt
from kino_bot.sources.telegram import pick_style_posts
from kino_bot.style import clean_example, load_style_file


def test_strip_hashtags():
    text = "Текст про фильм #кино и <b>#Дюна</b>.\n\n#трейлер #премьера"
    assert strip_hashtags(text) == "Текст про фильм и ."
    assert strip_hashtags("Цена &#8381; 100") == "Цена &#8381; 100"  # html-сущность не трогаем


def msg(text, fwd=False, grouped=None):
    return NS(message=text, entities=None, fwd_from=object() if fwd else None, grouped_id=grouped)


def test_pick_style_posts_filters():
    long = "Длинный пост с мыслями автора. " * 5
    messages = [
        msg("коротко"),
        msg(long + " erid: 123"),
        msg(long, fwd=True),
        msg(long + "1", grouped=7),
        msg(long + "2", grouped=7),
        msg(long + "3"),
    ]
    assert pick_style_posts(messages, 10) == [long + "1", long + "3"]
    assert len(pick_style_posts(messages, 1)) == 1


def test_clean_example_removes_links_and_mentions():
    out = clean_example('Пост <a href="https://x.ru">тут</a> https://t.me/abc и @somechannel\n\n\n\nконец')
    assert "http" not in out and "@somechannel" not in out and "<a" not in out
    assert "тут" in out and out.endswith("конец")


def test_load_style_file(tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("Первый пример\n---\nВторой <b>пример</b>\n---\n\n", encoding="utf-8")
    assert load_style_file(f) == ["Первый пример", "Второй <b>пример</b>"]
    assert load_style_file(tmp_path / "missing.txt") == []


def test_system_prompt_with_and_without_examples():
    with_ex = build_system_prompt("стиль {не шаблон}", ["Пример {1}"], 800)
    assert "<example>\nПример {1}\n</example>" in with_ex
    assert "не больше 750 знаков" in with_ex and "стиль {не шаблон}" in with_ex
    without = build_system_prompt("стиль", [], 800)
    assert "<examples>" not in without
    assert "Без хэштегов" in without and "Без заголовка" in without
