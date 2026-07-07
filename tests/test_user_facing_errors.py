from app.llm_client import FREE_CHAIN_EXHAUSTED_MARKER, OPENROUTER_BUDGET_EXCEEDED_MARKER
from app.pipeline import _user_facing_error_reason
from app.services_container import SummaryJob


class _FakeBilling:
    def __init__(self, subscriber_ids):
        self._ids = set(subscriber_ids)

    def is_subscriber(self, user_id, now=None):
        return user_id in self._ids


class _FakeServices:
    def __init__(self, subscriber_ids=()):
        self.billing = _FakeBilling(subscriber_ids)


def _job(quota_user_id=None):
    return SummaryJob(
        sequence=1, message=None, url="https://youtu.be/x",
        enqueued_at=0.0, chat_id=1, quota_user_id=quota_user_id,
    )


def test_owner_sees_technical_text():
    exc = RuntimeError(f"{FREE_CHAIN_EXHAUSTED_MARKER}: все модели отказали ... /llm_paid")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=None), _FakeServices())
    assert "/llm_paid" in reason  # полный технический текст без маскировки


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


def test_unknown_error_passes_through():
    exc = RuntimeError("что-то нейтральное сломалось")
    reason = _user_facing_error_reason(exc, _job(quota_user_id=99), _FakeServices())
    assert reason == "что-то нейтральное сломалось"
