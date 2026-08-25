"""Q10: защита саммари от временных галлюцинаций — слои 2 и 3.

Боевой баг: LLM расшифровывают "текущий год" из уст спикера как застрявшее
"настоящее" своих весов (например, 2024) — в саммари появляются годы, которых
нет в транскрипте. Слой 1 (грунтовка промпта датами) — см. app/summarizer.py
(build_date_grounding_block). Этот модуль — слои 2 (детерминированный
верификатор) и 3 (точечный llm-фикс, best-effort).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import dataclasses
import datetime as _dt
import json
import logging
import re

from app.models import Summary

logger = logging.getLogger(__name__)

# Годы 1950-2049. Только годы в v1 — числа и проценты не трогаем, слишком
# много false-positives (номера версий, статистика, таймкоды и т.п.).
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def _years_in(text: str) -> set[str]:
    return set(_YEAR_RE.findall(text or ""))


def find_unsupported_years(
    summary: Summary,
    transcript_text: str,
    *,
    publish_year: str | None = None,
    today_year: str | None = None,
) -> list[str]:
    """Годы, упомянутые в саммари, но не подтверждённые транскриптом.

    Год «поддержан», если:
    - его цифры встречаются в транскрипте (той же regex), ИЛИ
    - он равен году публикации ролика или сегодняшнему году — это легитимный
      контекст (ролик мог не произносить год явно, но говорить "в этом году").

    ``today_year`` по умолчанию — реальный текущий год (берётся с системных
    часов), передавать явно нужно только в тестах для детерминизма.
    """
    summary_years: set[str] = set()
    summary_years |= _years_in(summary.overview)
    for chapter in summary.chapters:
        summary_years |= _years_in(chapter.title)
        summary_years |= _years_in(chapter.notes)

    if not summary_years:
        return []

    legit = _years_in(transcript_text)
    if publish_year:
        legit.add(publish_year)
    legit.add(today_year if today_year is not None else str(_dt.date.today().year))

    return sorted(summary_years - legit)


FIX_PROMPT = """
В саммари ниже (JSON) есть годы, не подтверждённые транскрипцией ролика: {years}.

Дата публикации ролика: {publish_date}.

Исправь ТОЛЬКО утверждения с этими годами: замени год на формулировку из
транскрипта (если она там есть) или на относительную привязку ко времени
записи ("на момент записи", "к моменту выхода ролика" и т.п.), либо просто
убери конкретный год, если он не нужен по смыслу. Больше НИЧЕГО не меняй —
ни структуру, ни формулировки, ни порядок глав, ни теги. Пиши на том же
языке, что исходное саммари ниже — не переводи.

Верни ТОЛЬКО JSON той же схемы, без markdown-обёртки и без комментариев:
{{"overview": "...", "chapters": [{{"title": "...", "notes": "..."}}], "tags": {{"topic": "...", "speakers": [...], "hosts": [...], "format": "..."}}}}

Саммари для исправления:
{summary_json}
""".strip()


def serialize_summary_for_fix(summary: Summary) -> str:
    """Саммари → компактный JSON того же формата, что просит SUMMARY_JSON_PROMPT
    (overview/chapters[title,notes]/tags) — маленький контекст для фикс-вызова."""
    payload = {
        "overview": summary.overview,
        "chapters": [{"title": ch.title, "notes": ch.notes} for ch in summary.chapters],
        "tags": {
            "topic": summary.tags.topic,
            "speakers": list(summary.tags.speakers),
            "hosts": list(summary.tags.hosts),
            "format": summary.tags.format,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


async def fix_unsupported_years(
    *,
    summary: Summary,
    transcript_text: str,
    unsupported_years: list[str],
    publish_date: str,
    generate: Callable[..., Awaitable[str]],
    parse: Callable[[str], Summary],
    max_tokens: int | None,
    route: str,
    usage=None,
    job_id: str = "",
) -> Summary:
    """Q10 layer 3: один точечный llm-фикс + повторная проверка.

    Best-effort и никогда не рвёт доставку: любая проблема (исключение сети/
    бюджета, непарсибельный ответ, годы остались неподдержанными после
    фикса) — возвращаем ОРИГИНАЛЬНОЕ summary. Job не падает.

    Неудачный фикс-вызов всё же инкрементит общий circuit-breaker и тратит
    бюджет free/paid-цепочки (как любой обычный ``generate``) — осознанная
    цена за best-effort фикс, отдельно не изолируем.

    Теги в возвращаемом summary — ВСЕГДА от исходного (до-фиксного) summary,
    не от свежераспарсенного ``fixed``: ``_parse_tags_from_response`` не
    заполняет channel (он проставляется в pipeline ДО вызова этой функции,
    через ``_resolve_summary_tags``/``TagsCatalog``) и не канонизирует
    остальные теги. FIX_PROMPT и так запрещает модели трогать теги — здесь
    просто не даём случайному дрейфу модели их деградировать.
    """
    try:
        prompt = FIX_PROMPT.format(
            years=", ".join(unsupported_years),
            publish_date=publish_date or "неизвестна",
            summary_json=serialize_summary_for_fix(summary),
        )
        raw = await generate(prompt, system=None, usage=usage, max_tokens=max_tokens, route=route)
        fixed = parse(raw)
    except Exception as exc:  # noqa: BLE001 — best-effort фикс, не роняем job
        logger.warning("summary.verify.fixed=false job_id=%s reason=call_failed error=%s", job_id, exc)
        return summary

    remaining = find_unsupported_years(fixed, transcript_text, publish_year=_publish_year(publish_date))
    if remaining:
        logger.warning(
            "summary.verify.fixed=false job_id=%s reason=still_flagged remaining_years=%s",
            job_id,
            remaining,
        )
        return summary

    logger.info("summary.verify.fixed=true job_id=%s", job_id)
    return dataclasses.replace(fixed, tags=summary.tags)


def _publish_year(publish_date: str) -> str | None:
    """"ДД.MM.ГГГГ" (см. summarizer._format_upload_date_human) → "ГГГГ"."""
    if not publish_date:
        return None
    try:
        return _dt.datetime.strptime(publish_date, "%d.%m.%Y").date().strftime("%Y")
    except ValueError:
        return None
