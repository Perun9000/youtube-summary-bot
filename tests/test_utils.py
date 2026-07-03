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
