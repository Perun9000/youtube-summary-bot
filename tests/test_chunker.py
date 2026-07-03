from app.models import TranscriptSegment
from app.transcript_chunker import chunk_transcript, segments_to_text


def test_chunks_respect_max_chars():
    text = "\n".join(f"строка номер {i}" for i in range(1000))
    chunks = chunk_transcript(text, max_chars=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_single_long_line_is_kept_whole():
    text = "x" * 10_000
    chunks = chunk_transcript(text, max_chars=100)
    assert chunks == [text]  # строка не режется посреди — уходит целиком


def test_empty_text():
    assert chunk_transcript("", max_chars=100) == [""]


def test_segments_to_text_format():
    segs = [TranscriptSegment(start=0, end=2, text="привет  мир"), TranscriptSegment(start=65, end=70, text="дальше")]
    assert segments_to_text(segs) == "[00:00] привет мир\n[01:05] дальше"


def test_segments_to_text_skips_empty():
    segs = [TranscriptSegment(start=0, end=1, text="   ")]
    assert segments_to_text(segs) == ""
