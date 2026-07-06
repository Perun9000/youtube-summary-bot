from app.delivery import _format_telegram_summary
from app.models import Summary, VideoComment
from app.services_container import MAX_TELEGRAM_MESSAGE_CHARS


def make_summary(overview="Обзор."):
    return Summary(overview=overview, key_points=[], chapters=[], raw_text="{}")


def fmt(**kw):
    return _format_telegram_summary(
        title="Заголовок", video_url="https://youtu.be/x", summary=make_summary(),
        telegraph_url="https://telegra.ph/x", channel_name="Канал",
        channel_url="https://youtube.com/@c", **kw,
    )


def test_signature_present():
    out = fmt(bot_username="Test_Bot")
    assert out.rstrip().endswith("<i>сделано @Test_Bot</i>")


def test_no_signature_without_username():
    out = fmt()
    assert "сделано @" not in out


def test_signature_survives_long_comment():
    # Гигантский топ-комментарий не должен вытеснять подпись за лимит.
    comment = VideoComment(text="х" * 5000, author="Автор", like_count=10)
    out = _format_telegram_summary(
        title="Заголовок", video_url="https://youtu.be/x",
        summary=make_summary("о" * 1500),
        telegraph_url="https://telegra.ph/x", channel_name="Канал",
        channel_url="https://youtube.com/@c", top_comment=comment,
        bot_username="Test_Bot",
    )
    assert len(out) <= MAX_TELEGRAM_MESSAGE_CHARS
    assert out.rstrip().endswith("<i>сделано @Test_Bot</i>")
