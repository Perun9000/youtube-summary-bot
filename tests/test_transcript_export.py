from app.models import TranscriptSegment
from app.transcript_export import (
    pretty_transcript_filename,
    save_transcript_markdown,
    transcript_markdown,
    transcript_path,
)


def seg(start, text):
    return TranscriptSegment(start=start, end=start + 5, text=text)


def test_markdown_format():
    md = transcript_markdown(
        "Название ролика", "https://youtu.be/x",
        [seg(0, "первая  строка"), seg(65, "вторая"), seg(70, "   ")],
    )
    lines = md.splitlines()
    assert lines[0] == "# Название ролика"
    assert "[Ролик](https://youtu.be/x)" in md
    assert "**[00:00]** первая строка" in md
    assert "**[01:05]** вторая" in md
    assert md.count("**[") == 2  # пустой сегмент пропущен


def test_save_and_path(tmp_path):
    path = save_transcript_markdown(
        tmp_path, "dQw4w9WgXcQ", "T", "https://youtu.be/dQw4w9WgXcQ", [seg(0, "текст")]
    )
    assert path == transcript_path(tmp_path, "dQw4w9WgXcQ")
    assert path.read_text(encoding="utf-8").startswith("# T")


def test_pretty_filename_basic():
    assert pretty_transcript_filename("Канал", "Название ролика", "vid") == "Канал_Название ролика.md"


def test_pretty_filename_sanitizes_and_limits():
    name = pretty_transcript_filename("A/B:C", "x" * 100, "vid", max_len=20)
    assert "/" not in name and ":" not in name
    assert len(name) <= 20 + len(".md")


def test_pretty_filename_falls_back_to_video_id():
    assert pretty_transcript_filename("", "   ", "dQw4w9WgXcQ") == "dQw4w9WgXcQ.md"


def test_pretty_filename_does_not_cut_unicode_word_in_half():
    # Обрезка по границе целых слов — длинное кириллическое слово либо
    # входит в имя целиком, либо не входит вовсе, но не режется посередине.
    long_word = "ОченьДлинноеНеразрывноеСловоНаКириллице"
    title = f"Первое Второе Третье Четвертое {long_word} Финиш"
    name = pretty_transcript_filename("Канал", title, "vid", max_len=40)
    stem = name[: -len(".md")]
    allowed_tokens = {"Канал_Первое", "Второе", "Третье", "Четвертое", long_word, "Финиш"}
    for token in stem.split(" "):
        assert token in allowed_tokens
    assert len(stem) <= 40
