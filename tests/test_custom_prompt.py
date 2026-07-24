"""Одноразовый кастомный промпт: чистая логика (/myprompt).

Спека: docs/superpowers/specs/2026-07-23-custom-user-prompt-design.md
"""

from app.custom_prompt import (
    ARMED_TTL_SEC,
    AWAITING_INPUT_TTL_SEC,
    CUSTOM_PROMPT_MAX_CHARS,
    PendingCustomPrompt,
    parse_prompt_message,
    wrap_custom_prompt,
)

URL = "https://www.youtube.com/watch?v=abcABC12345"


def test_parse_url_plus_prompt():
    url, prompt = parse_prompt_message(f"Сделай упор на цифры и факты\n{URL}")
    assert url == URL
    assert prompt == "Сделай упор на цифры и факты"


def test_parse_prompt_only():
    url, prompt = parse_prompt_message("Пиши в стиле деловой газеты")
    assert url is None
    assert prompt == "Пиши в стиле деловой газеты"


def test_parse_url_only_gives_empty_prompt():
    url, prompt = parse_prompt_message(f"  {URL}  ")
    assert url == URL
    assert prompt == ""


def test_wrap_contains_guardrail_and_prompt():
    wrapped = wrap_custom_prompt("Только факты")
    assert "Только факты" in wrapped
    assert "не отменяют" in wrapped.lower()
    assert "json" in wrapped.lower()


def test_pending_expiry_by_stage():
    p = PendingCustomPrompt(stage="awaiting_input", started_at=1000.0)
    assert not p.expired(now=1000.0 + AWAITING_INPUT_TTL_SEC - 1)
    assert p.expired(now=1000.0 + AWAITING_INPUT_TTL_SEC + 1)
    a = PendingCustomPrompt(stage="armed", prompt="x", started_at=1000.0)
    assert not a.expired(now=1000.0 + ARMED_TTL_SEC - 1)
    assert a.expired(now=1000.0 + ARMED_TTL_SEC + 1)


def test_max_chars_constant():
    assert CUSTOM_PROMPT_MAX_CHARS == 500


# --- Прокидка через job: context_hint и кэш-байпас ---

from app.delivery import _is_job_cacheable  # noqa: E402
from app.pipeline import _build_context_hint  # noqa: E402
from app.services_container import SummaryJob  # noqa: E402


def _job(**kw):
    return SummaryJob(
        sequence=1, message=None, url=URL, enqueued_at=0.0, chat_id=1, **kw
    )


def test_context_hint_from_custom_prompt():
    hint = _build_context_hint(_job(custom_prompt="Только цифры"))
    assert "Только цифры" in hint
    assert "не отменяют" in hint.lower()


def test_context_hint_none_without_prompt_and_spans():
    assert _build_context_hint(_job()) is None


def test_context_hint_combines_segment_and_custom():
    job = _job(custom_prompt="Кратко", segment_spans=[(0.0, 60.0)])
    hint = _build_context_hint(job)
    assert "фрагмент" in hint.lower() and "Кратко" in hint


def test_custom_prompt_job_not_cacheable():
    assert _is_job_cacheable(_job(custom_prompt="x")) is False
    assert _is_job_cacheable(_job()) is True


# --- Доступ к фиче ---

from app.bot_handlers import _may_use_custom_prompt  # noqa: E402


class _U:
    def __init__(self, allowed=False, owner=False):
        self._a, self._o = allowed, owner

    def is_owner(self, uid):
        return self._o

    def is_allowed(self, uid):
        return self._a or self._o


class _B:
    def __init__(self, subs=()):
        self._s = set(subs)

    def is_subscriber(self, uid, now=None):
        return uid in self._s


class _S:
    def __init__(self, users, billing):
        self.users, self.billing = users, billing


def test_access_owner_allowlist_subscriber():
    assert _may_use_custom_prompt(1, _S(_U(owner=True), _B())) is True
    assert _may_use_custom_prompt(2, _S(_U(allowed=True), _B())) is True
    assert _may_use_custom_prompt(3, _S(_U(), _B(subs={3}))) is True
    assert _may_use_custom_prompt(4, _S(_U(), _B())) is False
    assert _may_use_custom_prompt(None, _S(_U(owner=True), _B())) is False
