import pytest
from app.utils import classify_youtube_url, extract_video_id, extract_youtube_url, format_ts


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])
def test_extract_video_id(url, vid):
    assert extract_video_id(url) == vid


def test_extract_video_id_raises_on_channel():
    with pytest.raises(ValueError):
        extract_video_id("https://www.youtube.com/@somechannel")


def test_classify():
    assert classify_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "video"
    assert classify_youtube_url("https://www.youtube.com/@somechannel") == "channel"
    assert classify_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ") == "unknown"


def test_extract_youtube_url_rejects_foreign():
    assert extract_youtube_url("глянь https://vimeo.com/123") is None
    assert extract_youtube_url("вот https://youtu.be/dQw4w9WgXcQ.") == "https://youtu.be/dQw4w9WgXcQ"


def test_format_ts():
    assert format_ts(65) == "01:05"
    assert format_ts(3665) == "01:01:05"


class TestSchemelessYoutubeUrls:
    """Telegram авто-линкует голые домены, и пользователи шлют ссылки без
    https:// (боевой кейс 2026-09-09: «youtube.com/shorts/wFOCW9JVe60» не
    распознался как URL вообще — бот ответил дежурной подсказкой).
    Безсхемное распознавание — ТОЛЬКО для YouTube-хостов, чтобы не ловить
    ложные срабатывания на произвольном тексте с точками."""

    def test_bare_shorts_link(self):
        url = extract_youtube_url("глянь youtube.com/shorts/wFOCW9JVe60 топ")
        assert url == "https://youtube.com/shorts/wFOCW9JVe60"

    def test_bare_watch_link_with_www(self):
        url = extract_youtube_url("www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_bare_youtu_be(self):
        url = extract_youtube_url("youtu.be/dQw4w9WgXcQ")
        assert url == "https://youtu.be/dQw4w9WgXcQ"

    def test_schemed_link_still_works(self):
        url = extract_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ")
        assert url == "https://m.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_bare_non_youtube_domain_ignored(self):
        assert extract_youtube_url("зацени vimeo.com/12345") is None

    def test_plain_text_with_dots_ignored(self):
        assert extract_youtube_url("посмотри позже. ок?") is None
