from app.models import Summary
from app.summarizer import _clean_json_text, _load_json, _summary_from_damaged_json, SummaryParseError

import pytest


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
