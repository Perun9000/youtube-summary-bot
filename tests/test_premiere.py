import time

from app.models import VideoMetadata
from app.pipeline import _is_upcoming


def meta(**kw):
    return VideoMetadata(video_id="dQw4w9WgXcQ", title="t", channel_name="", channel_url="", **kw)


def test_upcoming_by_live_status():
    assert _is_upcoming(meta(live_status="is_upcoming"))


def test_upcoming_by_future_release_timestamp():
    assert _is_upcoming(meta(release_timestamp=time.time() + 3600))


def test_released_video_is_not_upcoming():
    assert not _is_upcoming(meta(release_timestamp=time.time() - 3600))


def test_regular_video_is_not_upcoming():
    assert not _is_upcoming(meta())
