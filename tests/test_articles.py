from kino_bot.articles import extract_article_text

PAGE = """<html><head><title>Сайт | Новость</title><meta property="og:image" content="/i.jpg"></head><body>
<nav><p>Меню сайта и много пунктов навигации, которые не относятся к статье вообще никак</p></nav>
<article><h1>Режиссёр «Дюны» снимет новый фильм</h1>
<p>Дени Вильнёв подтвердил, что следующим его проектом станет экранизация романа, о которой говорили давно.</p>
<p>Коротко</p>
<blockquote>«Это история, которую я хотел снять двадцать лет»</blockquote>
<p>Подписывайтесь на наш канал</p>
<script>var x = "<p>скрипт не должен попасть в текст никогда и ни при каких условиях</p>";</script>
<p>Съёмки начнутся весной, премьера ожидается не раньше 2028 года, пишет издание со ссылкой на студию.</p>
</article><footer><p>Все права защищены, перепечатка запрещена без разрешения редакции сайта</p></footer>
</body></html>"""


def test_extract_article_text():
    title, text = extract_article_text(PAGE)
    assert title == "Режиссёр «Дюны» снимет новый фильм"
    paragraphs = text.split("\n\n")
    assert paragraphs[0].startswith("Дени Вильнёв")
    assert "«Это история, которую я хотел снять двадцать лет»" in paragraphs
    assert paragraphs[-1].startswith("Съёмки начнутся весной")
    assert "Меню" not in text and "Подписывайтесь" not in text and "скрипт" not in text and "права" not in text
    assert "Коротко" not in text


def test_extract_without_article_tag():
    page = "<html><body><div><p>" + "Длинный абзац текста о кино. " * 5 + "</p></div></body></html>"
    title, text = extract_article_text(page)
    assert text.startswith("Длинный абзац")
