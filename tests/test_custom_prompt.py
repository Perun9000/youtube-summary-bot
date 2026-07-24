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
