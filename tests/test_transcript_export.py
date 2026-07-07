from app.models import TranscriptSegment
from app.transcript_export import save_transcript_markdown, transcript_markdown, transcript_path


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
