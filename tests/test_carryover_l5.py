"""L5 carry-over: lang-branches introduced during i18n review (see task-l5-brief.md).

Покрывает две регрессии, которые ревью L5 попросило закрыть тестами:
1. ``_format_likes`` — ru-склонения не должны были измениться (byte-identical
   с до-i18n поведением), а non-ru должны брать сингуляр из
   ``summary.likes_one`` при count == 1.
2. ``_classify_youtube_download_error`` — подстрока yt-dlp-сообщения должна
   мапиться на правильный локализованный ключ, включая fallback-сниппет для
   нераспознанных ошибок.
"""

from app.delivery import _format_likes
from app.i18n import t
from app.pipeline import _classify_youtube_download_error


# --- _format_likes: ru path must stay byte-identical to pre-i18n behaviour ---


def test_format_likes_ru_declensions_unchanged():
    assert _format_likes(1, "ru") == "1 лайк"
    assert _format_likes(2, "ru") == "2 лайка"
    assert _format_likes(5, "ru") == "5 лайков"
    assert _format_likes(11, "ru") == "11 лайков"       # 11-14 -> лайков
    assert _format_likes(21, "ru") == "21 лайк"
    assert _format_likes(1200, "ru") == "1.2к лайков"


def test_format_likes_en_singular_plural_and_compact():
    assert _format_likes(1, "en") == "1 like"
    assert _format_likes(2, "en") == "2 likes"
    assert _format_likes(1200, "en") == "1.2K likes"


def test_format_likes_uses_likes_one_key_for_non_ru_singular():
    # count == 1 идёт через summary.likes_one, а не через summary.likes
    # (у которого {count} не обязан склоняться под единицу).
    for lang in ("en", "es", "pt", "fa", "ar", "id"):
        expected = t("summary.likes_one", lang, count=1)
        assert _format_likes(1, lang) == expected
        # count != 1 остаётся на summary.likes
        assert _format_likes(2, lang) == t("summary.likes", lang, count="2")


def test_format_likes_pt_singular_differs_from_plural():
    assert _format_likes(1, "pt") == "1 curtida"
    assert _format_likes(2, "pt") == "2 curtidas"


# --- _classify_youtube_download_error: substring -> localized key ---


def test_classify_private_video_en():
    exc = RuntimeError("ERROR: [youtube] abc123: Private video. Sign in if you've been invited.")
    reason = _classify_youtube_download_error(exc, "en")
    assert reason == t("ytdlp.private", "en")


def test_classify_private_video_ru():
    exc = RuntimeError("ERROR: [youtube] abc123: Private video. Sign in if you've been invited.")
    reason = _classify_youtube_download_error(exc, "ru")
    assert reason == t("ytdlp.private", "ru")


def test_classify_members_only_matches_correct_key_not_others():
    exc = RuntimeError("Join this channel to get access to members-only content")
    reason_en = _classify_youtube_download_error(exc, "en")
    reason_ru = _classify_youtube_download_error(exc, "ru")
    assert reason_en == t("ytdlp.members_only", "en")
    assert reason_ru == t("ytdlp.members_only", "ru")


def test_classify_unknown_error_falls_back_with_snippet():
    exc = RuntimeError("some completely unrecognized yt-dlp failure text")
    reason_en = _classify_youtube_download_error(exc, "en")
    reason_ru = _classify_youtube_download_error(exc, "ru")
    assert reason_en == t("ytdlp.download_failed", "en", snippet=str(exc))
    assert reason_ru == t("ytdlp.download_failed", "ru", snippet=str(exc))
    assert "some completely unrecognized" in reason_en
    assert "some completely unrecognized" in reason_ru


def test_classify_unknown_error_truncates_long_snippet():
    long_msg = "x" * 500
    exc = RuntimeError(long_msg)
    reason = _classify_youtube_download_error(exc, "en")
    # 197 chars + "..." = 200-char snippet, wrapped in the localized template
    assert "..." in reason
    assert len(reason) < len(long_msg)
