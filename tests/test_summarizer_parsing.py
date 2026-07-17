from app.models import Summary
from app.summarizer import (
    COMPACT_SUMMARY_PROMPT,
    SUMMARY_JSON_PROMPT,
    SYNTHESIS_PROMPT,
    Summarizer,
    SummaryParseError,
    SummaryUnusableError,
    _clean_json_text,
    _fallback_summary_from_raw,
    _load_json,
    _summary_from_damaged_json,
)

import pytest


# --- Q3, Изменение 1: overview-схема требует 2–4 абзаца через \n\n ---

@pytest.mark.parametrize(
    "prompt", [SUMMARY_JSON_PROMPT, SYNTHESIS_PROMPT, COMPACT_SUMMARY_PROMPT],
)
def test_overview_schema_requires_paragraph_breaks(prompt):
    # Схема поля "overview" должна требовать 2-4 абзаца, разделённых \n\n
    # (буквально — экранированная последовательность внутри строки промпта),
    # с главной мыслью в первом абзаце.
    assert '"overview"' in prompt
    overview_line = next(
        line for line in prompt.splitlines() if '"overview"' in line
    )
    assert "\\n\\n" in overview_line
    assert "абзац" in overview_line.lower()


VALID = '{"overview": "Кратко о ролике.", "chapters": [{"start": "00:01", "title": "Глава", "notes": "Текст."}], "tags": {"topic": "финансы", "speakers": ["Иванов"], "hosts": [], "format": "интервью"}}'


def test_load_json_plain():
    data = _load_json(VALID)
    assert data["overview"] == "Кратко о ролике."


def test_load_json_strips_markdown_fence():
    data = _load_json(f"```json\n{VALID}\n```")
    assert data["chapters"][0]["title"] == "Глава"


def test_load_json_extracts_object_from_prose():
    data = _load_json(f"Вот итог:\n{VALID}\nНадеюсь, помог!")
    assert "overview" in data


def test_load_json_raises_on_garbage():
    with pytest.raises(SummaryParseError):
        _load_json("никакого json здесь нет")


def test_damaged_json_truncated_chapters():
    # Модель оборвала ответ посреди третьей главы — типичный обрыв по max_tokens.
    raw = (
        '{"overview": "О чём видео.", "chapters": ['
        '{"start": "00:00", "title": "Первая", "notes": "Заметки один."},'
        '{"start": "10:00", "title": "Вторая", "notes": "Заметки два."},'
        '{"start": "20:00", "title": "Треть'
    )
    summary = _summary_from_damaged_json(raw)
    assert summary is not None
    assert summary.overview == "О чём видео."
    assert [c.title for c in summary.chapters] == ["Первая", "Вторая"]


def test_damaged_json_overview_only():
    summary = _summary_from_damaged_json('{"overview": "Только обзор", "chapters": [')
    assert summary is not None
    assert summary.overview == "Только обзор"
    assert summary.chapters == []


def test_damaged_json_no_structure_returns_none():
    assert _summary_from_damaged_json("просто текст без фигурных скобок") is None


def test_damaged_json_escaped_quotes_in_notes():
    raw = '{"overview": "X", "chapters": [{"start": "0", "title": "Т", "notes": "Он сказал: \\"да\\"."}], '
    summary = _summary_from_damaged_json(raw)
    assert summary is not None
    assert summary.chapters[0].notes == 'Он сказал: "да".'


# --- Guard: сырой текст модели не должен публиковаться как summary ---

REASONING_PROSE = (
    "We need to produce JSON with overview and chapters. The transcript is a bit "
    "messy, but we need to extract main ideas. Also mention that some experts note..."
)


def test_fallback_raises_on_reasoning_prose():
    # Ответ без JSON-структуры (chain-of-thought reasoning-модели) — брак,
    # его нельзя отдавать в канал как overview.
    with pytest.raises(SummaryUnusableError):
        _fallback_summary_from_raw(REASONING_PROSE)


def test_fallback_raises_on_unrecoverable_json():
    # JSON-объект без единого полезного поля тоже не публикуем.
    with pytest.raises(SummaryUnusableError):
        _fallback_summary_from_raw('{"foo": 1, "bar": [')


def test_fallback_still_recovers_damaged_json():
    summary = _fallback_summary_from_raw('{"overview": "Спасённый обзор", "chapters": [')
    assert summary.overview == "Спасённый обзор"


class _ReasoningOnlyLLM:
    """Фейковый LLM, который всегда отвечает reasoning-прозой вместо JSON."""

    @property
    def provider_name(self) -> str:
        return "fake"

    async def generate(self, prompt, system=None, usage=None, max_tokens=None, route="default"):
        return REASONING_PROSE


async def test_single_chunk_reasoning_output_fails_summarize():
    summarizer = Summarizer(_ReasoningOnlyLLM(), system_prompt_provider=lambda: "sys")
    with pytest.raises(SummaryUnusableError):
        await summarizer.summarize(url="https://youtu.be/x", title="t", chunks=["один чанк"])
