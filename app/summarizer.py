from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import logging
import re
import time

from app.models import Chapter, Summary, SummaryTags
from app.llm_client import GenerationUsage, LLMClient


logger = logging.getLogger(__name__)
SYNTHESIS_PARTIALS_MAX_CHARS = 8000
SYNTHESIS_RETRY_PARTIALS_MAX_CHARS = 4500
MID_SYNTHESIS_MAX_CHARS = 10000
DEFAULT_HIERARCHY_THRESHOLD = 6
DEFAULT_GROUP_SIZE = 5


SUMMARY_SYSTEM_PROMPT = """
КРИТИЧЕСКОЕ ПРАВИЛО ЯЗЫКА — соблюдай ВСЕГДА, во всех частях ответа:
- Весь текст ответа (overview, key_points, chapters.title, chapters.notes, любые
  промежуточные конспекты) пиши ТОЛЬКО на РУССКОМ ЯЗЫКЕ.
- Это правило применяется НЕЗАВИСИМО от языка транскрипции. Английский,
  немецкий, испанский, китайский транскрипт — всё равно ответ на РУССКОМ.
- Категорически запрещено: писать ответы на английском или смешанно. Если
  ловишь себя на формулировке вида "Key takeaway:", "The author argues...",
  "In summary,..." — НЕМЕДЛЕННО переформулируй на русском: "Главный тезис:",
  "Автор утверждает...", "Кратко:".
- Имена собственные, бренды, технические термины — отдельная история (см.
  правила ниже), но ОСНОВНОЙ текст всегда русский.

Ты аналитический ассистент, который делает структурированное резюме YouTube-ролика по транскрипции.
Не пересказывай транскрипт дословно: выделяй смысл, структуру, аргументы, примеры и выводы.
Не выдумывай факты. Если место неясное, формулируй осторожно.
Если есть практические шаги, выделяй их явно.
Если есть спорные утверждения, формулируй их как позицию автора.

Правила по названиям брендов, устройств, продуктов, компаний и технологий — соблюдай строго:
- ВСЕ иностранные названия брендов, моделей устройств, продуктов, сервисов, компаний, чипов, стандартов и технологий пиши ЛАТИНИЦЕЙ в их оригинальном написании.
- Если в транскрипции такое название записано кириллицей — это ошибка распознавания речи (Whisper, автосубтитры), её нужно исправить и восстановить корректное латинское написание.
- Примеры того, как делать НЕЛЬЗЯ → как НУЖНО: "Айфон" → "iPhone"; "Самсунг Гэлакси" → "Samsung Galaxy"; "Гугл Пиксел" → "Google Pixel"; "Оппо Файнд X9 Ультра" → "Oppo Find X9 Ultra"; "Снэпдрэгон" → "Snapdragon"; "Квалком" → "Qualcomm"; "Эпл" → "Apple"; "Вай-Фай" → "Wi-Fi"; "Блютус/Блютуз" → "Bluetooth"; "ОЛЕД/АМОЛЕД" → "OLED/AMOLED"; "ЮСБ-Си" → "USB-C"; "Фейс Айди" → "Face ID"; "А18 Про" → "A18 Pro"; "Тайп-Си" → "Type-C"; "ЭйАй" → "AI".
- Исключение — оригинально русскоязычные бренды (например, Яндекс, Сбер, ВКонтакте, Озон) пиши кириллицей, как принято в русском.
- Если точное латинское написание модели из контекста неясно, но очевидно, что речь о иностранном продукте — пиши бренд латиницей, а номер/суффикс модели так, как слышно в транскрипции, но без кириллической транслитерации.

Правила по остальной терминологии — соблюдай строго:
- Категорически запрещены транслитерации английских слов русскими буквами. Примеры того, как делать НЕЛЬЗЯ: "нумбность", "регреты", "митинг" (в смысле встреча), "флоу", "эдженда", "мув", "пойнт", "чарджить", "коммитить", "чарт", "хайлайт".
- Перед тем как записать слово русскими буквами — проверь, не является ли оно транслитерацией с английского. Если да, выбери один из двух вариантов ниже.
- Если у термина есть устоявшийся русский перевод — используй его. Обязательные соответствия: "regrets" → "сожаления" или "сомнения"; "numbness" → "онемение"; "meeting" → "встреча"; "agenda" → "повестка"; "move" → "ход, шаг"; "point" → "тезис, довод, мысль"; "chart" → "график"; "highlight" → "ключевой момент"; "commit" (to) → "брать на себя обязательство".
- Если устоявшегося русского аналога нет или перевод искажает смысл — оставляй оригинальный английский термин латиницей и в скобках давай короткое пояснение на русском. Примеры того, как делать НУЖНО: "burnout (профессиональное выгорание)", "flow (состояние потока, полной концентрации)", "product-market fit (соответствие продукта запросу рынка)".
- Никогда не изобретай новые русские слова по звучанию английского.
""".strip()


# Схема саммари: два блока — executive summary (overview) и подробный разбор
# тезисов (chapters). Коротких тезисов-буллетов больше нет, поэтому key_points
# в JSON не запрашиваем. Плейсхолдеры {topic_hint}/{speaker_hint}/{host_hint}
# убраны — .format(**kwargs) игнорирует лишние ключи, вызывающий код не трогаем.
SUMMARY_JSON_PROMPT = """
Составь саммари по транскрипции YouTube-ролика, следуя всем правилам из системного промпта
(язык, стиль, полнота, тон, фильтрация, обращение с фактами и т.д.).

Верни только JSON без markdown-обёртки и без комментариев. Все строковые значения — на русском.

Схема:
{{
  "overview": "Executive summary ролика. Начни с ГЛАВНОЙ МЫСЛИ или ключевого вывода одной чёткой формулировкой по сути. Затем — необходимый контекст: о чём ролик, какой фокус, кому и зачем это релевантно. Объём: 3–5 предложений, компактно и содержательно. Запрещено начинать с мета-фраз 'Ролик обсуждает…', 'Автор рассказывает о…', 'В видео говорится, что…', 'Видео посвящено…'. Сразу содержательный тезис.",
  "chapters": [
    {{
      "title": "Короткий содержательный заголовок тезиса (не таймкод, не 'Часть 1', не 'Введение')",
      "notes": "ПОДРОБНАЯ расшифровка тезиса без искусственных ограничений по длине. Включи саму идею, аргументацию автора, конкретные примеры, цифры, имена, цитаты, кейсы, нюансы, ограничения, контраргументы и практические выводы, если они есть. Пиши связным русским текстом; при необходимости — несколько абзацев, отделённых пустой строкой внутри одной строки JSON (\\n\\n). Приоритет — ПОЛНОТА, а не краткость: не выбрасывай важное ради экономии символов. Но и не растекайся: каждое предложение с нагрузкой, без воды и повторов."
    }}
  ],
  "tags": {{
    "topic": "Один тег — главная тема ролика. Одно слово или короткое словосочетание через _, без # и пробелов. Максимально точно отражает суть. Примеры: \\"экономика\\", \\"санкции\\", \\"выборы\\", \\"война\\", \\"медиа\\", \\"технологии\\", \\"международные_отношения\\", \\"внутренняя_политика\\".",
    "speakers": ["До 3 фамилий гостей. Кто ОТВЕЧАЕТ / приглашён как эксперт. НЕ включай ведущего/интервьюера. Если ролик — монолог автора без гостей, верни []. Только фамилия с заглавной, например \\"Гуриев\\", \\"Шульман\\"."],
    "hosts": ["До 5 фамилий ведущих. Кто ВЕДЁТ программу / задаёт вопросы / автор ролика. Один, несколько (соведущие) или 0 если неясно. НЕ путай с гостями. Только фамилия с заглавной."],
    "format": "Один из: интервью, расследование, разбор, новости, дискуссия."
  }}
}}

Количество тезисов в chapters определи сам по содержанию ролика: столько, сколько нужно для полного покрытия ключевых идей без повторов. Обычно 4–8, но может быть больше, если ролик длинный и насыщенный.

URL: {url}
Название: {title}

Транскрипция с таймкодами:
{transcript}
""".strip()


CHUNK_PROMPT = """
Сожми этот фрагмент транскрипции YouTube-ролика в структурированный конспект.

ЯЗЫК ОТВЕТА — РУССКИЙ. Это правило безусловное: даже если фрагмент полностью
на английском (или другом языке), пиши конспект только по-русски. Не оставляй
английских предложений, не пиши смешанно ("англо-русский гибрид"), не используй
конструкций вроде "The author argues...", "Key insight:", — переводи смысл и
формулируй на русском.

Сохрани важные факты, аргументы, примеры, цифры, имена, цитаты и выводы, которые потом можно объединить в ключевые тезисы всего ролика.
Не копируй текст дословно.
Категорически запрещено делить фрагмент поминутно, по таймкодам или пересказывать его по порядку transcript. Группируй по смысловым идеям.
Никаких транслитераций английских слов русскими буквами (не "нумбность", "флоу", "митинг"): либо нормальный русский термин, либо английский оригинал + пояснение в скобках.

URL: {url}
Название: {title}
Фрагмент {index} из {total}:
{chunk}
""".strip()


SYNTHESIS_PROMPT = """
Ниже частичные конспекты длинного YouTube-ролика. Собери из них финальное саммари,
следуя всем правилам из системного промпта. Не пересказывай фрагменты по очереди —
собирай сквозные смысловые тезисы.

Верни только JSON без markdown-обёртки и без комментариев. JSON должен быть валидным:
закрывай все кавычки, не оставляй trailing comma, не обрывай строки. Все строковые
значения — на русском.

Схема:
{{
  "overview": "Executive summary ролика. Начни с ГЛАВНОЙ МЫСЛИ или ключевого вывода одной чёткой формулировкой по сути. Затем — необходимый контекст: о чём ролик, какой фокус, кому и зачем это релевантно. Объём: 3–5 предложений, компактно и содержательно. Запрещено начинать с мета-фраз 'Ролик обсуждает…', 'Автор рассказывает о…', 'В видео говорится, что…', 'Видео посвящено…'.",
  "chapters": [
    {{
      "title": "Короткий содержательный заголовок тезиса (не таймкод, не 'Фрагмент 1', не 'Введение')",
      "notes": "ПОДРОБНАЯ расшифровка тезиса без искусственных ограничений по длине. Включи саму идею, аргументацию, конкретные примеры, цифры, имена, цитаты, кейсы, нюансы, ограничения, контраргументы и практические выводы. Пиши связным русским текстом; при необходимости несколько абзацев внутри одной строки JSON (\\n\\n). Приоритет — ПОЛНОТА, а не краткость."
    }}
  ],
  "tags": {{
    "topic": "Один тег на русском — главная тема ролика.",
    "speakers": ["До 3 фамилий гостей. НЕ включай ведущего. Если гостей нет — []."],
    "hosts": ["До 5 фамилий ведущих. НЕ путай с гостями."],
    "format": "Один из: интервью, расследование, разбор, новости, дискуссия."
  }}
}}

Количество тезисов в chapters определи по содержанию ролика: столько, сколько нужно для полного покрытия ключевых идей без повторов.

URL: {url}
Название: {title}

Частичные конспекты:
{partials}
""".strip()


COMPACT_SUMMARY_PROMPT = """
Ниже материал по YouTube-ролику. Сделай компактное финальное саммари, следуя всем
правилам из системного промпта.

Цель: вернуть полностью валидный JSON и сохранить полноту саммари.
Верни только JSON без markdown-обёртки. Все строковые значения — на русском.

Схема:
{{
  "overview": "Executive summary ролика — главная мысль + короткий контекст, 3–5 предложений.",
  "chapters": [
    {{
      "title": "Содержательный заголовок тезиса",
      "notes": "Подробная расшифровка тезиса с аргументами, примерами, цифрами, нюансами и выводами. Пиши связным русским текстом. Если нужно экономить место — сокращай формулировки, а не выбрасывай важные идеи."
    }}
  ]
}}

URL: {url}
Название: {title}

Материал:
{source}
""".strip()


MID_SYNTHESIS_PROMPT = """
Ниже идут подряд несколько частичных конспектов одного длинного YouTube-ролика (группа {group_index} из {group_total}).
Твоя задача — сделать укрупнённый конспект этой группы. Это промежуточный шаг: поверх укрупнённых конспектов всех групп будет финальная сборка саммари, поэтому здесь важно не потерять факты.

ЯЗЫК ОТВЕТА — РУССКИЙ. Если в фрагментах есть английский текст, переведи смысл
на русский. Никаких смешанных формулировок.

Что делать:
- Объедини совпадающие и повторяющиеся идеи в один тезис.
- Сохрани ключевые аргументы, примеры, цифры, имена, цитаты, кейсы, практические выводы из всех фрагментов группы.
- Структурируй по смысловым идеям, а не по порядку фрагментов. Никакого поминутного деления и никаких таймкодов.
- Не делай финального summary в формате JSON — это нужно позже. Здесь верни связный русский текст конспекта.
- Никаких транслитераций английских слов русскими буквами: либо нормальный русский термин, либо английский оригинал + пояснение в скобках.

Формат ответа — связный русский текст на несколько абзацев. Можно выделять тезисы отдельными абзацами, но без markdown-заголовков и без JSON.

URL: {url}
Название: {title}

Частичные конспекты группы:
{partials}
""".strip()


@dataclass
class SummaryProgress:
    total_steps: int = 0
    completed_steps: int = 0
    current_step: str = "подготовка"
    _step_started: float | None = None
    _completed_durations: list[float] = field(default_factory=list)

    def configure(self, total_steps: int) -> None:
        self.total_steps = max(1, total_steps)
        self.completed_steps = 0
        self.current_step = "подготовка"
        self._step_started = None
        self._completed_durations.clear()

    def start_step(self, label: str) -> None:
        self.current_step = label
        self._step_started = time.monotonic()

    def complete_step(self) -> None:
        if self._step_started is not None:
            self._completed_durations.append(max(0.0, time.monotonic() - self._step_started))
        self.completed_steps = min(self.total_steps, self.completed_steps + 1)
        self._step_started = None

    def status_text(self) -> str:
        if self.total_steps <= 0:
            return ""
        return f"Этап summary: {self.current_step} ({self.completed_steps}/{self.total_steps})"

    def percent(self) -> int:
        if self.total_steps <= 0:
            return 0
        if self.completed_steps >= self.total_steps:
            return 100

        current_fraction = self._current_step_fraction()
        raw_percent = ((self.completed_steps + current_fraction) / self.total_steps) * 100
        return max(0, min(99, int(round(raw_percent))))

    def estimated_remaining_seconds(self) -> float | None:
        if self.total_steps <= 0 or not self._completed_durations:
            return None

        avg_step_sec = sum(self._completed_durations) / len(self._completed_durations)
        current_fraction = self._current_step_fraction()
        remaining_steps = max(0.0, self.total_steps - self.completed_steps - current_fraction)
        return remaining_steps * avg_step_sec

    def _current_step_fraction(self) -> float:
        if self._step_started is None:
            return 0.0

        elapsed = max(0.0, time.monotonic() - self._step_started)
        if self._completed_durations:
            expected_step_sec = max(1.0, sum(self._completed_durations) / len(self._completed_durations))
        else:
            expected_step_sec = 240.0
        return min(0.9, elapsed / expected_step_sec)


class SummaryParseError(ValueError):
    pass


class Summarizer:
    def __init__(
        self,
        llm: LLMClient,
        hierarchy_threshold: int = DEFAULT_HIERARCHY_THRESHOLD,
        group_size: int = DEFAULT_GROUP_SIZE,
        partial_max_tokens: int | None = None,
        final_max_tokens: int | None = None,
        system_prompt_provider: Callable[[], str] | None = None,
    ) -> None:
        self._llm = llm
        self._hierarchy_threshold = max(2, hierarchy_threshold)
        self._group_size = max(2, group_size)
        # When None, both stages use the LLMClient's default (settings.llm_max_tokens).
        # Set both to enable per-stage budgets (e.g. small for chunk partials,
        # large for final synthesis) — this prevents truncated overviews on long
        # videos without burning tokens on intermediate condensations.
        self._partial_max_tokens = partial_max_tokens
        self._final_max_tokens = final_max_tokens
        # Провайдер эффективного system prompt'а. Дёргается на каждом запуске
        # summarize() → owner-редактирование через /prompt_set подхватывается
        # без рестарта. По умолчанию — константа модуля.
        self._system_prompt_provider = system_prompt_provider or (
            lambda: SUMMARY_SYSTEM_PROMPT
        )

    async def summarize(
        self,
        url: str,
        title: str,
        chunks: list[str],
        progress: SummaryProgress | None = None,
        usage: GenerationUsage | None = None,
        context_hint: str | None = None,
        topic_hint: str = "",
        speaker_hint: str = "",
        host_hint: str = "",
    ) -> Summary:
        started = time.monotonic()
        logger.info(
            "summary.start title=%r chunks=%s context_hint=%s tags_hints=%s",
            title,
            len(chunks),
            bool(context_hint),
            bool(topic_hint or speaker_hint or host_hint),
        )
        system_prompt = self._system_prompt_with_hint(context_hint)
        prompt_kwargs = {
            "url": url,
            "title": title,
            "topic_hint": topic_hint,
            "speaker_hint": speaker_hint,
            "host_hint": host_hint,
        }
        if len(chunks) == 1:
            if progress:
                progress.configure(1)
                progress.start_step("финальное summary")
            logger.info("summary.single_chunk.start chars=%s", len(chunks[0]))
            raw = await self._llm.generate(
                SUMMARY_JSON_PROMPT.format(transcript=chunks[0], **prompt_kwargs),
                system=system_prompt,
                usage=usage,
                max_tokens=self._final_max_tokens,
            )
            try:
                summary = self._parse_summary(raw)
            except SummaryParseError as exc:
                logger.warning("summary.parse_json.failed mode=single raw_chars=%s error=%s", len(raw), exc)
                if progress:
                    progress.start_step("компактный повтор")
                retry_raw = await self._llm.generate(
                    COMPACT_SUMMARY_PROMPT.format(
                        url=url,
                        title=title,
                        source=_truncate_text(chunks[0], SYNTHESIS_RETRY_PARTIALS_MAX_CHARS),
                    ),
                    system=system_prompt,
                    usage=usage,
                    max_tokens=self._final_max_tokens,
                )
                if retry_raw.strip():
                    raw = retry_raw
                try:
                    summary = self._parse_summary(raw)
                except SummaryParseError as retry_exc:
                    logger.warning(
                        "summary.parse_json.fallback mode=single raw_chars=%s error=%s",
                        len(raw),
                        retry_exc,
                    )
                    summary = _fallback_summary_from_raw(raw)
            if progress:
                progress.complete_step()
            logger.info(
                "summary.done mode=single duration_sec=%.1f key_points=%s chapters=%s raw_chars=%s",
                time.monotonic() - started,
                len(summary.key_points),
                len(summary.chapters),
                len(raw),
            )
            return summary

        chunk_partials: list[str] = []
        num_chunks = len(chunks)

        use_hierarchy = num_chunks >= self._hierarchy_threshold
        num_groups = _num_groups(num_chunks, self._group_size) if use_hierarchy else 0
        if use_hierarchy and num_groups <= 1:
            use_hierarchy = False
            num_groups = 0

        if progress:
            total_steps = num_chunks + (num_groups if use_hierarchy else 0) + 1
            progress.configure(total_steps)

        for index, chunk in enumerate(chunks, start=1):
            if progress:
                progress.start_step(f"фрагмент {index}/{num_chunks}")
            chunk_started = time.monotonic()
            logger.info("summary.chunk.start index=%s total=%s chars=%s", index, num_chunks, len(chunk))
            partial = await self._llm.generate(
                CHUNK_PROMPT.format(url=url, title=title, index=index, total=num_chunks, chunk=chunk),
                system=system_prompt,
                usage=usage,
                max_tokens=self._partial_max_tokens,
            )
            logger.info(
                "summary.chunk.done index=%s total=%s duration_sec=%.1f response_chars=%s",
                index,
                num_chunks,
                time.monotonic() - chunk_started,
                len(partial),
            )
            chunk_partials.append(f"Фрагмент {index}:\n{partial}")
            if progress:
                progress.complete_step()

        if use_hierarchy:
            groups = _split_into_groups(chunk_partials, self._group_size)
            logger.info(
                "summary.hierarchy.start partials=%s groups=%s group_size=%s",
                len(chunk_partials),
                len(groups),
                self._group_size,
            )
            mid_partials: list[str] = []
            for group_index, group in enumerate(groups, start=1):
                if progress:
                    progress.start_step(f"группа {group_index}/{len(groups)}")
                group_started = time.monotonic()
                group_text = _compact_partials(group, MID_SYNTHESIS_MAX_CHARS)
                logger.info(
                    "summary.hierarchy.group.start index=%s total=%s members=%s compact_chars=%s",
                    group_index,
                    len(groups),
                    len(group),
                    len(group_text),
                )
                mid = await self._llm.generate(
                    MID_SYNTHESIS_PROMPT.format(
                        url=url,
                        title=title,
                        group_index=group_index,
                        group_total=len(groups),
                        partials=group_text,
                    ),
                    system=system_prompt,
                    usage=usage,
                    max_tokens=self._partial_max_tokens,
                )
                logger.info(
                    "summary.hierarchy.group.done index=%s total=%s duration_sec=%.1f response_chars=%s",
                    group_index,
                    len(groups),
                    time.monotonic() - group_started,
                    len(mid),
                )
                mid_partials.append(f"Группа {group_index}:\n{mid.strip()}")
                if progress:
                    progress.complete_step()
            synthesis_partials = mid_partials
            synthesis_source = "hierarchy"
        else:
            synthesis_partials = chunk_partials
            synthesis_source = "flat"

        partials_chars = sum(len(item) for item in synthesis_partials)
        partials_text = _compact_partials(synthesis_partials, SYNTHESIS_PARTIALS_MAX_CHARS)
        logger.info(
            "summary.synthesis.start source=%s partials=%s chars=%s compact_chars=%s",
            synthesis_source,
            len(synthesis_partials),
            partials_chars,
            len(partials_text),
        )
        if progress:
            progress.start_step("финальная сборка")
        raw = await self._llm.generate(
            SYNTHESIS_PROMPT.format(partials=partials_text, **prompt_kwargs),
            system=system_prompt,
            usage=usage,
            max_tokens=self._final_max_tokens,
        )

        if not raw.strip():
            if progress:
                progress.start_step("повтор финальной сборки")
            partials_text = _compact_partials(synthesis_partials, SYNTHESIS_RETRY_PARTIALS_MAX_CHARS)
            logger.warning(
                "summary.synthesis.empty_retry source=%s partials=%s compact_chars=%s",
                synthesis_source,
                len(synthesis_partials),
                len(partials_text),
            )
            raw = await self._llm.generate(
                COMPACT_SUMMARY_PROMPT.format(url=url, title=title, source=partials_text),
                system=system_prompt,
                usage=usage,
                max_tokens=self._final_max_tokens,
            )

        if raw.strip():
            try:
                summary = self._parse_summary(raw)
            except SummaryParseError as exc:
                logger.warning(
                    "summary.synthesis.parse_retry source=%s partials=%s raw_chars=%s error=%s",
                    synthesis_source,
                    len(synthesis_partials),
                    len(raw),
                    exc,
                )
                if progress:
                    progress.start_step("повтор финальной сборки")
                partials_text = _compact_partials(synthesis_partials, SYNTHESIS_RETRY_PARTIALS_MAX_CHARS)
                retry_raw = await self._llm.generate(
                    COMPACT_SUMMARY_PROMPT.format(url=url, title=title, source=partials_text),
                    system=system_prompt,
                    usage=usage,
                    max_tokens=self._final_max_tokens,
                )
                if retry_raw.strip():
                    raw = retry_raw
                try:
                    summary = self._parse_summary(raw)
                except SummaryParseError as retry_exc:
                    logger.warning(
                        "summary.synthesis.parse_fallback source=%s partials=%s raw_chars=%s error=%s",
                        synthesis_source,
                        len(synthesis_partials),
                        len(raw),
                        retry_exc,
                    )
                    summary = _fallback_summary_from_partials(synthesis_partials, raw_text=raw)
        else:
            logger.warning(
                "summary.synthesis.empty_fallback source=%s partials=%s",
                synthesis_source,
                len(synthesis_partials),
            )
            summary = _fallback_summary_from_partials(synthesis_partials)

        if progress:
            progress.complete_step()
        logger.info(
            "summary.done mode=chunked duration_sec=%.1f key_points=%s chapters=%s raw_chars=%s",
            time.monotonic() - started,
            len(summary.key_points),
            len(summary.chapters),
            len(raw),
        )
        return summary

    def _parse_summary(self, raw: str) -> Summary:
        try:
            data = _load_json(raw)
        except SummaryParseError:
            summary = _summary_from_damaged_json(raw)
            if summary is not None:
                logger.warning(
                    "summary.parse_json.recovered raw_chars=%s chapters=%s",
                    len(raw),
                    len(summary.chapters),
                )
                return summary
            raise
        chapters = [
            Chapter(
                start=str(item.get("start", "")).strip(),
                title=str(item.get("title", "")).strip(),
                notes=str(item.get("notes", "")).strip(),
            )
            for item in data.get("chapters", [])
            if isinstance(item, dict)
        ]
        tags = _parse_tags_from_response(data.get("tags"))

        # key_points больше не запрашиваем у модели и не рендерим отдельным
        # блоком — коротких тезисов-буллетов нет. Оставляем поле пустым, чтобы
        # не ломать Summary-контракт и downstream-код (кэш, Q&A, digest).
        return Summary(
            overview=str(data.get("overview", "")).strip() or raw.strip(),
            key_points=[],
            chapters=chapters,
            raw_text=raw,
            tags=tags,
        )

    def _system_prompt_with_hint(self, context_hint: str | None) -> str:
        base = self._system_prompt_provider() or SUMMARY_SYSTEM_PROMPT
        if not context_hint:
            return base
        hint = context_hint.strip()
        if not hint:
            return base
        return f"{base}\n\nДополнительный контекст:\n{hint}"


def _parse_tags_from_response(raw_tags) -> SummaryTags:
    """Best-effort parse of LLM-supplied ``tags`` block.

    LLM возвращает ``{"topic": "...", "speakers": [...], "hosts": [...], "format": "..."}``.
    Если поле потеряно или повреждено — возвращаем пустой ``SummaryTags``.
    Канал тут не извлекается: добавляется отдельно из metadata за пределами
    этой функции.
    """
    if not isinstance(raw_tags, dict):
        return SummaryTags()
    topic = str(raw_tags.get("topic") or "").strip()
    speakers = _parse_name_list(raw_tags.get("speakers"), limit=3)
    hosts = _parse_name_list(raw_tags.get("hosts"), limit=5)
    fmt = str(raw_tags.get("format") or "").strip()
    return SummaryTags(
        topic=topic,
        speakers=tuple(speakers),
        hosts=tuple(hosts),
        format=fmt,
    )


def _parse_name_list(raw, *, limit: int) -> list[str]:
    """Защитно парсим список фамилий: дропаем пустые, обрезаем до limit."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _load_json(raw: str) -> dict:
    cleaned = _clean_json_text(raw)
    last_error: json.JSONDecodeError | None = None

    for candidate in _json_candidates(cleaned):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            return data
        raise SummaryParseError("LLM returned JSON, but the root value is not an object")

    if last_error:
        raise SummaryParseError(str(last_error)) from last_error
    raise SummaryParseError("LLM did not return a JSON object")


def _clean_json_text(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return re.sub(r"```$", "", cleaned).strip()


def _json_candidates(cleaned: str) -> list[str]:
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        extracted = cleaned[start : end + 1]
        if extracted != cleaned:
            candidates.append(extracted)
    return candidates


def _summary_from_damaged_json(raw: str) -> Summary | None:
    cleaned = _clean_json_text(raw)
    if "{" not in cleaned:
        return None

    overview_value = _extract_json_value(cleaned, "overview")
    key_points_value = _extract_json_value(cleaned, "key_points")
    chapters_value = _extract_json_value(cleaned, "chapters")

    overview = overview_value.strip() if isinstance(overview_value, str) else ""
    key_points = _string_list(key_points_value)
    chapters = _chapters_from_value(chapters_value)
    if not chapters:
        chapters = _extract_complete_chapters(cleaned)

    if not overview and key_points:
        overview = _overview_from_points(key_points)
    if not key_points and chapters:
        key_points = _key_points_from_chapters(chapters)

    if not overview and not key_points and not chapters:
        return None

    return Summary(
        overview=overview or "Модель вернула повреждённый JSON, но часть структуры удалось восстановить.",
        key_points=key_points,
        chapters=chapters,
        raw_text=cleaned or raw,
    )


def _extract_json_value(text: str, key: str):
    pattern = rf'"{re.escape(key)}"\s*:'
    decoder = json.JSONDecoder()
    for match in re.finditer(pattern, text):
        candidate = text[match.end() :].lstrip()
        try:
            value, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        return value
    return None


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _chapters_from_value(value) -> list[Chapter]:
    if not isinstance(value, list):
        return []
    chapters: list[Chapter] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        chapter = _chapter_from_dict(item)
        if chapter.title or chapter.notes:
            chapters.append(chapter)
    return chapters


def _chapter_from_dict(item: dict) -> Chapter:
    return Chapter(
        start=str(item.get("start", "")).strip(),
        title=str(item.get("title", "")).strip(),
        notes=str(item.get("notes", "")).strip(),
    )


def _extract_complete_chapters(text: str) -> list[Chapter]:
    array_start = _find_array_start(text, "chapters")
    if array_start is None:
        return []

    chapters: list[Chapter] = []
    for object_text in _iter_complete_json_objects(text, array_start + 1):
        try:
            item = json.loads(object_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        chapter = _chapter_from_dict(item)
        if chapter.title or chapter.notes:
            chapters.append(chapter)
    return chapters


def _find_array_start(text: str, key: str) -> int | None:
    match = re.search(rf'"{re.escape(key)}"\s*:', text)
    if not match:
        return None
    array_start = text.find("[", match.end())
    return array_start if array_start >= 0 else None


def _iter_complete_json_objects(text: str, start: int):
    depth = 0
    object_start: int | None = None
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                object_start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and object_start is not None:
                yield text[object_start : index + 1]
                object_start = None
        elif char == "]" and depth == 0:
            break


def _overview_from_points(points: list[str]) -> str:
    first_points = [point.rstrip(".") for point in points[:2] if point.strip()]
    if not first_points:
        return ""
    overview = ". ".join(first_points)
    if not overview.endswith((".", "!", "?")):
        overview = f"{overview}."
    return overview


def _key_points_from_chapters(chapters: list[Chapter]) -> list[str]:
    points: list[str] = []
    for chapter in chapters:
        text = chapter.title or _first_sentence(chapter.notes)
        if text:
            points.append(text)
    return points


def _num_groups(n: int, max_size: int) -> int:
    if n <= 0 or max_size <= 0:
        return 0
    return max(1, (n + max_size - 1) // max_size)


def _split_into_groups(items: list[str], max_size: int) -> list[list[str]]:
    if not items:
        return []
    if max_size <= 0 or len(items) <= max_size:
        return [list(items)]
    num_groups = _num_groups(len(items), max_size)
    per_group = len(items) // num_groups
    extra = len(items) % num_groups
    groups: list[list[str]] = []
    idx = 0
    for group_idx in range(num_groups):
        size = per_group + (1 if group_idx < extra else 0)
        groups.append(items[idx : idx + size])
        idx += size
    return groups


def _compact_partials(partials: list[str], max_chars: int) -> str:
    if not partials:
        return ""

    separator_len = 2 * (len(partials) - 1)
    per_partial = max(250, (max_chars - separator_len) // len(partials))
    compacted = [_truncate_text(partial, per_partial) for partial in partials]
    return "\n\n".join(compacted)


def _truncate_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text

    truncated = text[: max(0, max_chars - 3)].rstrip()
    return f"{truncated}..."


def _fallback_summary_from_partials(partials: list[str], raw_text: str | None = None) -> Summary:
    if raw_text:
        recovered = _summary_from_damaged_json(raw_text)
        if recovered is not None and (recovered.key_points or recovered.chapters):
            return recovered

    chapters = [
        _chapter_from_partial(index, partial)
        for index, partial in enumerate(partials, start=1)
        if partial.strip()
    ]
    fallback_raw_text = raw_text or "\n\n".join(partials).strip()
    key_points = _key_points_from_chapters(chapters)
    return Summary(
        overview=_overview_from_points(key_points)
        or (
            "Финальная JSON-сборка не завершилась, поэтому сохранена структурная выжимка "
            "из промежуточных конспектов ролика."
        ),
        key_points=key_points,
        chapters=chapters,
        raw_text=fallback_raw_text,
    )


def _chapter_from_partial(index: int, partial: str) -> Chapter:
    cleaned = re.sub(r"^\s*(Фрагмент|Группа)\s+\d+\s*:\s*", "", partial.strip())
    first_line = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    title = _title_from_text(first_line) or f"Фрагмент {index}"
    notes = cleaned or partial.strip()
    return Chapter(start="", title=title, notes=notes)


def _title_from_text(text: str) -> str:
    text = re.sub(r"^[#*\-\d\.\s]+", "", text).strip()
    sentence = _first_sentence(text) or text
    return _truncate_text(sentence, 90)


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    match = re.search(r"^(.{20,220}?[.!?])(?:\s|$)", text)
    if match:
        return match.group(1).strip()
    return _truncate_text(text, 160)


def _fallback_summary_from_raw(raw: str) -> Summary:
    recovered = _summary_from_damaged_json(raw)
    if recovered is not None:
        return recovered

    cleaned = _clean_json_text(raw)
    if cleaned.startswith("{"):
        overview = (
            "Модель вернула summary в повреждённом JSON-формате. "
            "Полный ответ модели опубликован в Telegra.ph."
        )
    else:
        overview = _truncate_text(cleaned, 1200) or "Модель не вернула пригодный текст summary."

    return Summary(
        overview=overview,
        key_points=[],
        chapters=[],
        raw_text=cleaned or raw,
    )


def _format_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, seconds), 60)
    if minutes:
        return f"{minutes} мин {secs:02d} сек"
    return f"{secs} сек"
