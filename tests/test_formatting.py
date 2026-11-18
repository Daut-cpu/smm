from kino_bot.formatting import fit_post, sanitize_html, truncate_html, visible_len


def test_sanitize_keeps_only_allowed_tags():
    raw = '<p><strong>Заголовок</strong></p><p>Текст <a href="x">ссылка</a> <span>и</span> <em>курсив</em> <u>u</u> <s>s</s></p>'
    out = sanitize_html(raw)
    assert "<b>Заголовок</b>" in out
    assert "<i>курсив</i>" in out and "<u>u</u>" in out and "<s>s</s>" in out
    assert "<a" not in out and "<span" not in out and "<p>" not in out
    assert "ссылка" in out


def test_sanitize_escapes_and_closes_tags():
    out = sanitize_html("<b>Фильм 5 < 6 & ok")
    assert out == "<b>Фильм 5 &lt; 6 &amp; ok</b>"


def test_sanitize_markdown_fallback():
    assert sanitize_html("**Жирно** и *курсив*") == "<b>Жирно</b> и <i>курсив</i>"


def test_sanitize_removes_script_tags_but_keeps_text_escaped():
    out = sanitize_html("<script>alert(1)</script>Текст")
    assert "<script" not in out


def test_truncate_html_closes_tags_and_respects_limit():
    text = "<b>" + "Предложение номер один. " * 20 + "</b>"
    out = truncate_html(text, 100)
    assert visible_len(out) <= 101
    assert out.endswith("</b>")
    assert out.count("<b>") == out.count("</b>")


def test_fit_post_keeps_hashtags():
    body = "<b>Заголовок</b>\n\n" + ("Длинный текст про кино. " * 60)
    post = body + "\n\n#кино #трейлер"
    out = fit_post(post, 800)
    assert visible_len(out) <= 800
    assert out.endswith("#кино #трейлер")
    assert out.startswith("<b>Заголовок</b>")


def test_fit_post_noop_for_short():
    post = "<b>Х</b>\n\nтекст\n\n#кино"
    assert fit_post(post) == post
