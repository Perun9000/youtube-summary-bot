"""Q3: overview-абзацы, компактная строка ⏱·🔮, топ-комментарий в blockquote."""
from app.delivery import _format_telegram_summary
from app.models import Summary, VideoComment


def build(overview: str = "Обзор.", **kw):
    summary = kw.pop("summary", None) or Summary(
        overview=overview, key_points=[], chapters=[], raw_text="{}",
    )
    args = dict(
        title="Заголовок", video_url="https://youtu.be/x", summary=summary,
        telegraph_url="https://telegra.ph/x", channel_name="Канал",
        channel_url="https://youtube.com/@c",
    )
    args.update(kw)
    return _format_telegram_summary(**args)


# --- Изменение 1: overview-абзацы доживают до текста сообщения ---

def test_overview_paragraphs_survive_to_message():
    overview = "Главная мысль первого абзаца.\n\nКонтекст второго абзаца."
    out = build(overview=overview)
    assert "Главная мысль первого абзаца.\n\nКонтекст второго абзаца." in out


def test_overview_paragraphs_not_collapsed_by_escaping():
    # escape_html не должен схлопывать \n\n в один перенос или пробел.
    overview = "Абзац раз.\n\nАбзац два.\n\nАбзац три."
    out = build(overview=overview)
    assert out.count("\n\n") >= 3  # межблочные разделители + внутри overview


# --- Изменение 3: топ-комментарий в expandable blockquote ---

def test_top_comment_blockquote_expandable():
    comment = VideoComment(text="Отличное видео, спасибо!", author="Автор", like_count=42)
    out = build(top_comment=comment)
    assert "<blockquote expandable>💬" in out
    assert "</blockquote>" in out
    assert "Топ-комментарий" in out
    assert "Отличное видео, спасибо!" in out
    # старой обёртки <i>...</i> вокруг топ-комментария быть не должно
    blockquote_start = out.index("<blockquote expandable>")
    blockquote_body = out[blockquote_start:]
    assert "<i>" not in blockquote_body


def test_top_comment_escapes_html_inside_blockquote():
    comment = VideoComment(text="<script>alert(1)</script> текст", author="A", like_count=1)
    out = build(top_comment=comment)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<blockquote expandable>" in out
    assert "</blockquote>" in out


def test_top_comment_quotes_preserved_ru():
    comment = VideoComment(text="Хороший разбор", author="A", like_count=3)
    out = build(top_comment=comment)
    assert "«Хороший разбор»" in out


def test_top_comment_fits_budget_and_no_dangling_blockquote():
    # Огромный комментарий не должен провоцировать обрезку "поперёк" блока —
    # _fit_telegram_message не должен резать message так, чтобы </blockquote>
    # потерялся, если общий текст уже уложен в бюджет заранее.
    comment = VideoComment(text="х" * 5000, author="Автор", like_count=10)
    out = build(overview="о" * 1500, top_comment=comment, bot_username="Test_Bot")
    assert "<blockquote expandable>" in out
    assert out.count("<blockquote expandable>") == out.count("</blockquote>")


# --- Изменение 4: компактная строка ⏱ время · ссылка 🔮 ---

def test_compact_line_both_parts():
    out = build()
    lines = out.split("\n\n")
    compact = next((b for b in lines if "⏱" in b), None)
    assert compact is not None, out
    assert "🔮" in compact
    assert "Время чтения" in compact
    assert " · " in compact
    # 🔮 стоит после ссылки, вне <a>...</a>
    assert compact.rstrip().endswith("🔮")
    assert "</a> 🔮" in compact


def test_compact_line_only_reading_time_without_telegraph():
    out = build(telegraph_url="")
    lines = out.split("\n\n")
    compact = next((b for b in lines if "⏱" in b), None)
    assert compact is not None, out
    assert "🔮" not in compact
    assert "·" not in compact


def test_no_separate_reading_time_and_link_lines():
    # Старый раздельный формат ("Время чтения: ..." отдельным блоком и
    # отдельная строка со ссылкой) больше не должен встречаться.
    out = build()
    blocks = out.split("\n\n")
    reading_blocks = [b for b in blocks if "Время чтения" in b]
    assert len(reading_blocks) == 1
    # тот же блок должен содержать и ссылку
    assert "🔮" in reading_blocks[0]
