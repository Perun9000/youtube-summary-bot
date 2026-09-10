import datetime as _dt

import pytest

from app.models import Chapter, Summary, SummaryTags
from app.summary_verify import (
    find_unsupported_years,
    fix_unsupported_years,
    serialize_summary_for_fix,
)


def make_summary(overview="", chapters=None, tags=None):
    return Summary(
        overview=overview,
        key_points=[],
        chapters=chapters or [],
        raw_text="{}",
        tags=tags if tags is not None else SummaryTags(),
    )


# --- Слой 2: find_unsupported_years ---

def test_year_not_in_transcript_is_flagged():
    summary = make_summary(overview="Событие произошло в 2024 году.")
    transcript = "Ведущий рассказывает про экономику без упоминания годов."
    assert find_unsupported_years(summary, transcript) == ["2024"]


def test_year_present_in_transcript_is_clean():
    summary = make_summary(overview="Событие произошло в 2024 году.")
    transcript = "В 2024 году случилось важное событие в этой сфере."
    assert find_unsupported_years(summary, transcript) == []


def test_publish_year_is_legit_context():
    summary = make_summary(overview="На момент записи шёл 2023 год.")
    transcript = "Никаких годов тут не произносится вслух."
    assert find_unsupported_years(summary, transcript, publish_year="2023") == []


def test_today_year_is_legit_context_by_default():
    # Дефолт today_year — реальный текущий год (системные часы), без мока.
    current_year = str(_dt.date.today().year)
    summary = make_summary(overview=f"Сейчас {current_year} год.")
    transcript = "Транскрипт без годов."
    assert find_unsupported_years(summary, transcript) == []


def test_explicit_today_year_overrides_real_clock():
    summary = make_summary(overview="Идёт 2030 год.")
    transcript = "Транскрипт без годов."
    assert find_unsupported_years(summary, transcript, today_year="2030") == []


def test_years_in_chapter_title_and_notes_are_caught():
    summary = make_summary(
        overview="Без годов.",
        chapters=[Chapter(start="", title="Итоги 2019 года", notes="Подробности про 2021 год.")],
    )
    transcript = "Транскрипт без единого года."
    assert find_unsupported_years(summary, transcript) == ["2019", "2021"]


def test_no_years_anywhere_returns_empty():
    summary = make_summary(overview="Просто текст без чисел.")
    assert find_unsupported_years(summary, "и тут тоже без годов") == []


def test_multiple_unsupported_years_sorted():
    summary = make_summary(overview="Сравнение 2022 и 1998 годов.")
    transcript = "Ничего из этого не звучало."
    assert find_unsupported_years(summary, transcript) == ["1998", "2022"]


# --- serialize_summary_for_fix ---

def test_serialize_roundtrip_shape():
    summary = make_summary(
        overview="Обзор.",
        chapters=[Chapter(start="00:00", title="Глава", notes="Заметки.")],
    )
    import json

    data = json.loads(serialize_summary_for_fix(summary))
    assert data["overview"] == "Обзор."
    assert data["chapters"] == [{"title": "Глава", "notes": "Заметки."}]
    assert "tags" in data


# --- Слой 3: fix_unsupported_years (pipeline-фикс, best-effort) ---

FIXED_JSON = (
    '{"overview": "На момент записи шёл текущий год.", "chapters": [], '
    '"tags": {"topic": "", "speakers": [], "hosts": [], "format": ""}}'
)


async def _fake_generate_ok(prompt, system=None, usage=None, max_tokens=None, route="default"):
    return FIXED_JSON


def _fake_parse(raw: str) -> Summary:
    import json

    data = json.loads(raw)
    return make_summary(overview=data["overview"])


async def test_fix_replaces_summary_when_years_resolved():
    original = make_summary(overview="Событие произошло в 2024 году.")
    fixed = await fix_unsupported_years(
        summary=original,
        transcript_text="Без годов в транскрипте.",
        unsupported_years=["2024"],
        publish_date="01.01.2023",
        generate=_fake_generate_ok,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert fixed.overview == "На момент записи шёл текущий год."
    assert fixed is not original


async def test_fix_preserves_original_canonical_tags():
    # Ревью: свежераспарсенный fixed.tags идёт из _parse_tags_from_response
    # (сырые LLM-теги, без channel и без канонизации через TagsCatalog).
    # Успешный фикс должен вернуть теги ИСХОДНОГО (до-фиксного) summary —
    # они уже прошли _resolve_summary_tags в pipeline до вызова этой функции.
    original_tags = SummaryTags(
        topic="экономика", speakers=("Иванов",), hosts=("Петров",),
        format="интервью", channel="Канал",
    )
    original = make_summary(overview="Событие произошло в 2024 году.", tags=original_tags)

    async def generate_with_drifted_tags(*a, **k):
        return (
            '{"overview": "На момент записи шёл текущий год.", "chapters": [], '
            '"tags": {"topic": "другое", "speakers": [], "hosts": [], "format": ""}}'
        )

    def parse_full(raw: str) -> Summary:
        import json

        from app.models import SummaryTags as _Tags

        data = json.loads(raw)
        raw_tags = data.get("tags") or {}
        return make_summary(
            overview=data["overview"],
            tags=_Tags(
                topic=raw_tags.get("topic", ""),
                speakers=tuple(raw_tags.get("speakers", [])),
                hosts=tuple(raw_tags.get("hosts", [])),
                format=raw_tags.get("format", ""),
            ),
        )

    fixed = await fix_unsupported_years(
        summary=original,
        transcript_text="Без годов в транскрипте.",
        unsupported_years=["2024"],
        publish_date="01.01.2023",
        generate=generate_with_drifted_tags,
        parse=parse_full,
        max_tokens=1000,
        route="default",
    )
    assert fixed.tags == original_tags
    assert fixed.tags.channel == "Канал"


async def test_fix_falls_back_when_prompt_building_raises():
    # Ревью: serialize_summary_for_fix/FIX_PROMPT.format должны быть внутри
    # try — иначе исключение там пролетает мимо best-effort фоллбэка и роняет
    # job вместо доставки оригинала.
    class _Explodes:
        def __getattr__(self, name):
            raise RuntimeError("boom during serialization")

    exploding_summary = _Explodes()

    async def unreachable_generate(*a, **k):
        raise AssertionError("не должен вызываться — сериализация промпта уже упала")

    result = await fix_unsupported_years(
        summary=exploding_summary,
        transcript_text="Без годов.",
        unsupported_years=["2024"],
        publish_date="",
        generate=unreachable_generate,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is exploding_summary


async def test_fix_falls_back_to_original_when_generate_raises():
    original = make_summary(overview="Событие произошло в 2024 году.")

    async def boom(*a, **k):
        raise RuntimeError("OPENROUTER_BUDGET_EXCEEDED: no budget")

    result = await fix_unsupported_years(
        summary=original,
        transcript_text="Без годов.",
        unsupported_years=["2024"],
        publish_date="",
        generate=boom,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is original


async def test_fix_falls_back_to_original_when_response_unparsable():
    original = make_summary(overview="Событие произошло в 2024 году.")

    async def bad_generate(*a, **k):
        return "это не json совсем"

    def raising_parse(raw: str) -> Summary:
        raise ValueError("bad json")

    result = await fix_unsupported_years(
        summary=original,
        transcript_text="Без годов.",
        unsupported_years=["2024"],
        publish_date="",
        generate=bad_generate,
        parse=raising_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is original


async def test_fix_falls_back_when_years_still_unsupported_after_fix():
    original = make_summary(overview="Событие произошло в 2024 году.")

    async def still_broken_generate(*a, **k):
        return '{"overview": "Всё ещё 2024 год.", "chapters": [], "tags": {}}'

    def parse_still_broken(raw: str) -> Summary:
        import json

        data = json.loads(raw)
        return make_summary(overview=data["overview"])

    result = await fix_unsupported_years(
        summary=original,
        transcript_text="Без годов в транскрипте.",
        unsupported_years=["2024"],
        publish_date="",
        generate=still_broken_generate,
        parse=parse_still_broken,
        max_tokens=1000,
        route="default",
    )
    assert result is original


# --- Англицизмы: find_untranslated_anglicisms (детектор, слой 2) ---

from app.summary_verify import find_untranslated_anglicisms, fix_untranslated_anglicisms  # noqa: E402


def test_lowercase_anglicisms_are_flagged_sorted():
    summary = make_summary(
        overview="Сомнения в компетентности leadership растут, идёт erosion аксиом."
    )
    assert find_untranslated_anglicisms(summary) == ["erosion", "leadership"]


def test_capitalized_brands_and_names_are_not_flagged():
    summary = make_summary(
        overview="Обзор iPhone 16 Pro и Samsung Galaxy: сравнение с Google Pixel на YouTube."
    )
    assert find_untranslated_anglicisms(summary) == []


def test_sanctioned_term_with_russian_gloss_is_not_flagged():
    # Формат «term (пояснение)» разрешён системным промптом — не флагаем.
    summary = make_summary(
        overview="Автор описывает burnout (профессиональное выгорание) у фаундеров."
    )
    assert find_untranslated_anglicisms(summary) == []


def test_sanctioned_multiword_term_with_gloss_is_not_flagged():
    summary = make_summary(
        overview="Ключ — product-market fit (соответствие продукта запросу рынка)."
    )
    assert find_untranslated_anglicisms(summary) == []


def test_run_with_capitalized_token_is_treated_as_name():
    # «beta» в связке с «macOS Tahoe» — часть названия, не англицизм.
    summary = make_summary(overview="Вышло обновление macOS Tahoe beta для всех.")
    assert find_untranslated_anglicisms(summary) == []


def test_short_latin_tokens_are_ignored():
    summary = make_summary(overview="Файл лежит в git и в zip, всё ок.")
    assert find_untranslated_anglicisms(summary) == []


def test_domain_like_tokens_are_ignored():
    summary = make_summary(overview="Сайт youtube.com упомянут вскользь.")
    assert find_untranslated_anglicisms(summary) == []


def test_anglicisms_in_chapters_are_caught_and_deduped():
    summary = make_summary(
        overview="Про leadership.",
        chapters=[
            Chapter(start="", title="Кризис leadership", notes="Наступает erosion доверия."),
        ],
    )
    assert find_untranslated_anglicisms(summary) == ["erosion", "leadership"]


def test_clean_russian_text_returns_empty():
    summary = make_summary(overview="Полностью русский текст без вкраплений.")
    assert find_untranslated_anglicisms(summary) == []


# --- Англицизмы: fix_untranslated_anglicisms (llm-фикс, слой 3) ---


async def test_anglicism_fix_replaces_summary_and_keeps_original_tags():
    original_tags = SummaryTags(
        topic="политика", speakers=("Иванов",), hosts=(), format="анализ", channel="Канал",
    )
    original = make_summary(
        overview="Сомнения в компетентности leadership растут.", tags=original_tags,
    )

    async def generate_fixed(*a, **k):
        return (
            '{"overview": "Сомнения в компетентности руководства растут.", "chapters": [], '
            '"tags": {"topic": "другое", "speakers": [], "hosts": [], "format": ""}}'
        )

    fixed = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["leadership"],
        generate=generate_fixed,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert fixed.overview == "Сомнения в компетентности руководства растут."
    assert fixed.tags == original_tags


async def test_anglicism_fix_accepts_partial_improvement():
    original = make_summary(overview="Тут erosion и leadership одновременно.")

    async def generate_partial(*a, **k):
        return '{"overview": "Тут размывание и leadership одновременно.", "chapters": [], "tags": {}}'

    fixed = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["erosion", "leadership"],
        generate=generate_partial,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert fixed.overview == "Тут размывание и leadership одновременно."


async def test_anglicism_fix_falls_back_when_nothing_improved():
    original = make_summary(overview="Опять erosion доверия.")

    async def generate_same(*a, **k):
        return '{"overview": "Опять erosion доверия!", "chapters": [], "tags": {}}'

    result = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["erosion"],
        generate=generate_same,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is original


async def test_anglicism_fix_falls_back_when_generate_raises():
    original = make_summary(overview="Опять erosion доверия.")

    async def boom(*a, **k):
        raise RuntimeError("OPENROUTER_BUDGET_EXCEEDED")

    result = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["erosion"],
        generate=boom,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is original


async def test_anglicism_fix_falls_back_when_response_unparsable():
    original = make_summary(overview="Опять erosion доверия.")

    async def bad_generate(*a, **k):
        return "не json"

    def raising_parse(raw: str) -> Summary:
        raise ValueError("bad json")

    result = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["erosion"],
        generate=bad_generate,
        parse=raising_parse,
        max_tokens=1000,
        route="default",
    )
    assert result is original


async def test_anglicism_fix_gloss_format_counts_as_resolved():
    # Модель оставила термин, но добавила пояснение в скобках — санкционированный
    # формат, детектор его больше не флагает → фикс принимается.
    original = make_summary(overview="Главное — качественный flow работы.")

    async def generate_glossed(*a, **k):
        return (
            '{"overview": "Главное — качественный flow (состояние потока) работы.", '
            '"chapters": [], "tags": {}}'
        )

    fixed = await fix_untranslated_anglicisms(
        summary=original,
        anglicisms=["flow"],
        generate=generate_glossed,
        parse=_fake_parse,
        max_tokens=1000,
        route="default",
    )
    assert "flow (состояние потока)" in fixed.overview
