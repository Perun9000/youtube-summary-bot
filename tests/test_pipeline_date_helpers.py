"""Q10: pipeline-хелперы даты публикации (used by summary_verify wiring)."""
import datetime as _dt

from app.models import VideoMetadata
from app.pipeline import _human_publish_date, _publish_year_from_metadata


def meta(**kw):
    return VideoMetadata(video_id="dQw4w9WgXcQ", title="t", channel_name="", channel_url="", **kw)


def test_publish_year_from_upload_date():
    assert _publish_year_from_metadata(meta(upload_date="20240115")) == "2024"


def test_publish_year_from_release_timestamp_when_no_upload_date():
    ts = _dt.datetime(2022, 6, 1, tzinfo=_dt.timezone.utc).timestamp()
    assert _publish_year_from_metadata(meta(release_timestamp=ts)) == "2022"


def test_publish_year_unknown_returns_none():
    assert _publish_year_from_metadata(meta()) is None


def test_human_publish_date_from_upload_date():
    assert _human_publish_date(meta(upload_date="20240115")) == "15.01.2024"


def test_human_publish_date_unknown_returns_empty():
    assert _human_publish_date(meta()) == ""


def test_human_publish_date_garbage_upload_date_falls_back_to_empty():
    assert _human_publish_date(meta(upload_date="garbage")) == ""
