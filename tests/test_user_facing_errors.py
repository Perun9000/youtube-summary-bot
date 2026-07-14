from app.i18n import UserFacingError, t
from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER, OPENROUTER_BUDGET_EXCEEDED_MARKER
from app.pipeline import _user_facing_error_reason
from app.services_container import SummaryJob


OWNER_ID = 555


class _FakeBilling:
    def __init__(self, subscriber_ids):
        self._ids = set(subscriber_ids)

    def is_subscriber(self, user_id, now=None):
        return user_id in self._ids


class _FakeSettings:
    owner_user_id = OWNER_ID


class _FakeServices:
    def __init__(self, subscriber_ids=()):
        self.billing = _FakeBilling(subscriber_ids)
        self.settings = _FakeSettings()


def _job(quota_user_id=None, lang="ru", chat_id=1):
    return SummaryJob(
        sequence=1, message=None, url="https://youtu.be/x",
        enqueued_at=0.0, chat_id=chat_id, quota_user_id=quota_user_id, lang=lang,
    )


def test_owner_sees_technical_text():
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: все модели отказали ... /llm_paid")
    reason = _user_facing_error_reason(exc, _job(chat_id=OWNER_ID), _FakeServices())
    assert "/llm_paid" in reason  # полный технический текст без маскировки


def test_allowlist_user_gets_friendly_text_not_technical():
    # quota_user_id=None (без квот), но чат НЕ владельца — allowlist-пользователь.
    # Технические подробности видит только владелец.
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: все модели отказали ... /llm_paid")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=None, chat_id=42), _FakeServices())
    assert "/llm_paid" not in reason and "OpenRouter" not in reason
    assert reason == t("error.temporary_overload", "ru")


def test_allowlist_user_gets_internal_error_for_generic_exception():
    exc = RuntimeError('OpenRouter вернул HTTP 404: {"error": ...}')
    reason = _user_facing_error_reason(exc, _job(quota_user_id=None, chat_id=42), _FakeServices())
    assert reason == t("error.internal", "ru")
    assert "404" not in reason


def test_free_external_gets_friendly_daily_limit_text():
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: все модели отказали 429")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99), _FakeServices())
    assert "03:00" in reason and "/subscribe" in reason
    assert "OpenRouter" not in reason and "429" not in reason


def test_subscriber_gets_friendly_text_without_subscribe_pitch():
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: всё лежит")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=7), _FakeServices({7}))
    assert "/subscribe" not in reason and "OpenRouter" not in reason


def test_budget_marker_masked_for_external():
    exc = RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: daily cap")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99), _FakeServices())
    assert "бюджет" in reason and "OPENROUTER" not in reason


def test_groq_not_configured_gets_localized_text_for_external():
    exc = RuntimeError(
        "Субтитры YouTube недоступны для этого ролика, "
        "а GROQ_API_KEY не настроен — облачное распознавание "
        "выключено. Добавь ключ Groq в .env и перезапусти бот."
    )
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99, lang="es"), _FakeServices())
    assert reason == t("error.groq_unavailable", "es", error="GROQ_API_KEY not configured")
    assert "GROQ_API_KEY не настроен" not in reason


def test_generic_exception_gets_internal_error_for_external():
    exc = Exception("boom")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99, lang="es"), _FakeServices())
    assert reason == t("error.internal", "es")
    assert "boom" not in reason


def test_user_facing_error_passes_through_unchanged_for_external():
    already_localized = t("error.heavy_quota", "es", remaining=3)
    exc = UserFacingError(already_localized)
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99, lang="es"), _FakeServices())
    assert reason == already_localized


def test_owner_sees_raw_generic_exception_unchanged():
    exc = Exception("boom")
    reason = _user_facing_error_reason(exc, _job(chat_id=OWNER_ID), _FakeServices())
    assert reason == "boom"
