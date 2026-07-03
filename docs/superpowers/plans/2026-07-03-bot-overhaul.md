# Bot Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать неиспользуемые фичи (транскрипты, Q&A, локальный Whisper), мигрировать состояние на SQLite с персистентной очередью, распилить `bot_handlers.py`, добавить устойчивость внешних вызовов, тесты хрупкого ядра и утренний ранжированный дайджест мониторинга вместо потока отдельных саммари.

**Architecture:** Один файл `data/bot.db` (stdlib `sqlite3`, WAL, синхронные store-классы с сохранением текущих публичных API — call-sites не меняются). `bot_handlers.py` распиливается на `services_container / queue_service / pipeline / delivery / status_messages`. Scheduled-саммари перестают слать отдельные сообщения — накапливаются в таблице и уходят одним утренним дайджестом, ранжированным одним LLM-вызовом.

**Tech Stack:** Python 3.11, aiogram 3, httpx, sqlite3 (stdlib), pytest + pytest-asyncio (dev), yt-dlp, Groq Whisper API, LM Studio / OpenRouter.

## Global Constraints

- Публичные API store-классов сохраняются как есть (см. Interfaces в задачах) — сигнатуры вызовов в остальном коде не меняются, если задача явно не говорит иначе.
- Все пользовательские тексты — на русском, стиль существующих сообщений бота.
- Комментарии в коде — в стиле репозитория (русские, объясняют «почему»).
- Никаких новых runtime-зависимостей, кроме удаления `faster-whisper`. Dev-зависимости: `pytest>=8,<9`, `pytest-asyncio>=0.24,<1`.
- Тесты запускаются из корня репо: `./.venv/bin/pytest tests/ -q` (venv создаётся в Task 4).
- После каждой задачи: `./.venv/bin/python -m compileall app/ -q` проходит без ошибок (для задач до Task 4 — `python3 -m compileall app/ -q`).
- Коммит после каждой задачи. Сообщения коммитов — на английском, в стиле истории репо (`git log --oneline`).
- Каждый коммит заканчивается строкой `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- `docker compose build` должен проходить после задач 3, 9, 15 (там, где менялись зависимости/структура модулей).
- В этом файле "строка N" — номера строк на момент написания плана; после ранних задач они сдвигаются. Ищи по сигнатуре функции, номер — только ориентир.

---

### Task 1: Удалить Q&A (вопросы к саммари)

**Files:**
- Delete: `app/qa_service.py`
- Modify: `app/bot_handlers.py`, `app/main.py`, `app/models.py`, `README.md`

**Interfaces:**
- Consumes: —
- Produces: `Services` больше не имеет полей `qa` и `contexts`; `VideoContext` удалён из `app/models.py`. Все последующие задачи исходят из этого.

- [ ] **Step 1: Удалить qa_service.py**

```bash
git rm app/qa_service.py
```

- [ ] **Step 2: Вычистить bot_handlers.py**

Удалить:
1. Импорты: `from app.qa_service import QAService` (строка 43), `VideoContext` из импорта `app.models` (строка 35).
2. Поля `Services`: `qa: QAService` (строка 109), `contexts: dict[int, VideoContext]` (строка 111).
3. Команду `/reset` — хендлер `reset` (строки 435–441) и регистрацию `@router.message(Command("reset"))` над ним.
4. В `text_message` (строки 686–707) — весь Q&A-хвост начиная с `context = services.contexts.get(message.chat.id)` и до конца try/except. Заменить на:

```python
        await message.answer(
            "Я умею только саммари YouTube-роликов. Пришли ссылку на видео — "
            "или /help, чтобы посмотреть команды."
        )
```

5. Функцию `_restore_qa_context_from_cache` (строки 3174–3225) целиком и оба её вызова: в `_send_cached_summary_to_chat` (строка 3113) и в `_deliver_cached_summary_for_job` (строки 3153–3154, вместе с условием `if not job.scheduled and job.chat_id:`).
6. В `_process_youtube_job` — блок записи контекста (строки 2083–2094, `if not job.scheduled: services.contexts[chat_id] = VideoContext(...)`) вместе с комментарием над ним.
7. В `/help` и `/start` (хендлеры `start`, `help_command`) — убрать упоминания «вопросов по ролику»/Q&A, если есть (проверить текст глазами).

- [ ] **Step 3: Вычистить main.py и models.py**

`app/main.py`: убрать импорт `from app.qa_service import QAService`, аргументы `qa=QAService(llm)` и `contexts={}` из конструктора `Services`, `BotCommand(command="reset", ...)` из `OWNER_BOT_COMMANDS`.
`app/models.py`: удалить dataclass `VideoContext` (строки 64–73).

- [ ] **Step 4: Проверить компиляцию и упоминания**

```bash
python3 -m compileall app/ -q
grep -rn "qa_service\|QAService\|VideoContext\|contexts\[" app/ && echo "НАЙДЕНЫ ОСТАТКИ" || echo OK
```
Expected: компиляция без ошибок, `OK`.

- [ ] **Step 5: Обновить README**

Убрать из `README.md` упоминания Q&A (строки 9, 163 «контекст Q&A теряется») и команду `/reset` (строка 91).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Remove Q&A feature: no user demand"
```

---

### Task 2: Удалить сохранение транскриптов (файлы + страницы Telegra.ph)

**Files:**
- Modify: `app/bot_handlers.py`, `app/telegraph_service.py`, `app/summary_cache.py`, `README.md`
- Delete: `data/transcripts/` (содержимое)

**Interfaces:**
- Consumes: результат Task 1.
- Produces: `TelegraphService.publish(title, url, summary, top_comments=None) -> str` и `TelegraphService.edit(page_url_or_path, *, title, video_url, summary, top_comments=None) -> str` — параметр `transcript_url` удалён. `publish_transcript` удалён. `CachedSummary.transcript_url` остаётся в датаклассе (back-compat со старым JSON-кэшом), но всегда пишется `None`.

- [ ] **Step 1: telegraph_service.py**

Удалить: метод `publish_transcript` (строки 138–190), функцию `_transcript_to_nodes` (строки 370–438), константу `TRANSCRIPT_PAGE_JSON_BUDGET_BYTES` (строка 22), импорт `TranscriptSegment` и `format_ts`.
В `publish` и `edit`: убрать параметр `transcript_url` из сигнатур и из вызовов `_summary_to_nodes`. В `_summary_to_nodes` убрать параметр `transcript_url` и блок (строки 222–226), добавляющий ссылку «Полный транскрипт».

- [ ] **Step 2: bot_handlers.py**

Удалить:
1. `_save_transcript_to_file` (строки 72–77) и константу `TRANSCRIPTS_SUBDIR` (строка 64).
2. В `_process_youtube_job`: блок сохранения файла (строки 1973–1987), блок запуска `transcript_publish_task` (строки 1989–2002), блок ожидания `transcript_publish_task` (строки 2058–2066), переменные `transcript_url`/`transcript_publish_task` (строки 1813, 1989–1990), cancel-блоки в `except`-ветках (строки 2189–2190, 2200–2201), аргумент `transcript_url=transcript_url` в `services.telegraph.publish(...)` (строка 2076) и в `_save_summary_to_cache(...)` (строка 2164).
3. Функцию `_publish_transcript_background` (строки 2213–2238).
4. В `_save_summary_to_cache`: параметр `transcript_url` из сигнатуры, в конструкторе `CachedSummary` передавать `transcript_url=None`.
5. В `_refresh_cached_comments` (строки 2997–3076): найти вызов `services.telegraph.edit(...)` и убрать из него аргумент `transcript_url=...`.

- [ ] **Step 3: Проверка**

```bash
python3 -m compileall app/ -q
grep -rn "transcript_url\|publish_transcript\|_save_transcript_to_file\|TRANSCRIPTS_SUBDIR" app/ | grep -v "summary_cache.py" && echo "ОСТАТКИ" || echo OK
```
Expected: `OK` (единственные допустимые упоминания `transcript_url` — поле в `summary_cache.py`).

- [ ] **Step 4: Удалить сохранённые транскрипты**

```bash
rm -rf data/transcripts
```

- [ ] **Step 5: README**

Убрать упоминание сохранения транскриптов, если есть; проверить раздел «Что уже настроено».

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Drop transcript persistence: no file saving, no Telegraph transcript pages"
```

---

### Task 3: Удалить локальный Whisper (мёртвый код + тяжёлая зависимость)

**Files:**
- Delete: `app/whisper_service.py`
- Modify: `app/bot_handlers.py`, `app/main.py`, `requirements.txt`, `README.md`

Локальный Whisper закомментирован в pipeline с момента перехода на Groq (см. `_process_youtube_job`, комментарий «Локальный Whisper отключён»); `faster-whisper` — самая тяжёлая зависимость образа.

- [ ] **Step 1: Удалить файл и ссылки**

```bash
git rm app/whisper_service.py
```
`app/bot_handlers.py`: убрать импорт `from app.whisper_service import WhisperService`, поле `whisper: WhisperService` из `Services`, закомментированный блок локального Whisper в `_process_youtube_job` (строки 1884–1905, комментарии от `# === Локальный Whisper отключён` до `# transcript_source = "whisper"`).
`app/main.py`: убрать импорт `WhisperService` и аргумент `whisper=WhisperService(settings)`.
`app/config.py`: удалить поля `whisper_model`, `whisper_device`, `whisper_compute_type` из `Settings` и их чтение в `load_settings`.
`requirements.txt`: удалить строку `faster-whisper>=1.1,<2`.

- [ ] **Step 2: Проверка + docker build**

```bash
python3 -m compileall app/ -q
grep -rn "whisper_service\|WhisperService\|faster.whisper\|whisper_model\|whisper_device\|whisper_compute" app/ && echo "ОСТАТКИ" || echo OK
docker compose build
```
Expected: `OK`, образ собирается (и заметно быстрее/меньше).

- [ ] **Step 3: README**

В README убрать «Транскрибация … локально через faster-whisper» (строка 10), упоминание Whisper в «Ограничения MVP» (строка 164), переформулировать: транскрибация роликов без субтитров — через Groq Whisper API.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "Remove dead local Whisper path and faster-whisper dependency"
```

---

### Task 4: Тестовая инфраструктура + тесты хрупкого ядра

**Files:**
- Create: `requirements-dev.txt`, `tests/__init__.py`, `tests/test_summarizer_parsing.py`, `tests/test_chunker.py`, `tests/test_expert_spans.py`, `tests/test_utils.py`, `tests/test_digest_render.py`, `pytest.ini`
- Modify: `.gitignore` (добавить `.venv/`)

**Interfaces:**
- Consumes: чистые функции `app/summarizer.py` (`_load_json`, `_summary_from_damaged_json`, `_clean_json_text`), `app/transcript_chunker.py` (`chunk_transcript`, `segments_to_text`), `app/monitoring_service.py` (`_compute_expert_spans`, `_merge_spans`, `filter_segments_by_spans`), `app/utils.py`, `app/digest_service.py` (`render_digest_html`, `DigestEntry`).
- Produces: рабочий `pytest`-прогон — safety net для всех последующих рефакторингов.

- [ ] **Step 1: venv + зависимости**

`requirements-dev.txt`:
```
pytest>=8,<9
pytest-asyncio>=0.24,<1
```
```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
echo ".venv/" >> .gitignore
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 2: Тесты парсинга саммари (сначала — убедиться, что падают на пустом tests/)**

`tests/test_summarizer_parsing.py`:
```python
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
```

- [ ] **Step 3: Тесты чанкера**

`tests/test_chunker.py`:
```python
from app.models import TranscriptSegment
from app.transcript_chunker import chunk_transcript, segments_to_text


def test_chunks_respect_max_chars():
    text = "\n".join(f"строка номер {i}" for i in range(1000))
    chunks = chunk_transcript(text, max_chars=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


def test_single_long_line_is_kept_whole():
    text = "x" * 10_000
    chunks = chunk_transcript(text, max_chars=100)
    assert chunks == [text]  # строка не режется посреди — уходит целиком


def test_empty_text():
    assert chunk_transcript("", max_chars=100) == [""]


def test_segments_to_text_format():
    segs = [TranscriptSegment(start=0, end=2, text="привет  мир"), TranscriptSegment(start=65, end=70, text="дальше")]
    assert segments_to_text(segs) == "[00:00] привет мир\n[01:05] дальше"


def test_segments_to_text_skips_empty():
    segs = [TranscriptSegment(start=0, end=1, text="   ")]
    assert segments_to_text(segs) == ""
```

- [ ] **Step 4: Тесты expert-spans**

`tests/test_expert_spans.py`:
```python
from app.models import TranscriptSegment, VideoChapter
from app.monitoring_service import _compute_expert_spans, _merge_spans, filter_segments_by_spans


def seg(start, text):
    return TranscriptSegment(start=start, end=start + 5, text=text)


def test_merge_spans_merges_close_and_keeps_far():
    assert _merge_spans([(0, 100), (150, 200), (500, 600)], merge_gap_sec=60) == [(0, 200), (500, 600)]


def test_chapter_priority_over_transcript():
    spans = _compute_expert_spans(
        segments=[seg(3000, "тут Иванов говорит")],
        chapters=(VideoChapter(start=0, title="Интро"), VideoChapter(start=600, title="Иванов о рынке"), VideoChapter(start=1200, title="Финал")),
        expert_names=["Иванов"],
        video_duration_sec=3600,
        window_pre_sec=60,
        window_post_sec=180,
        cluster_gap_sec=300,
    )
    assert spans == [(600.0, 1200.0)]  # глава, а не окно вокруг упоминания


def test_transcript_clusters_with_window():
    spans = _compute_expert_spans(
        segments=[seg(1000, "слово Иванову"), seg(1100, "Иванов продолжает"), seg(3000, "снова Иванов")],
        chapters=(),
        expert_names=["Иванов"],
        video_duration_sec=3600,
        window_pre_sec=60,
        window_post_sec=180,
        cluster_gap_sec=300,
    )
    assert spans == [(940.0, 1280.0), (2940.0, 3180.0)]


def test_no_mentions_returns_empty():
    assert _compute_expert_spans(
        segments=[seg(10, "ни слова про эксперта")], chapters=(), expert_names=["Иванов"],
        video_duration_sec=100, window_pre_sec=60, window_post_sec=180, cluster_gap_sec=300,
    ) == []


def test_filter_segments_by_spans():
    segs = [seg(0, "a"), seg(500, "b"), seg(1000, "c")]
    assert filter_segments_by_spans(segs, [(400, 600)]) == [segs[1]]
    assert filter_segments_by_spans(segs, []) == segs
```

- [ ] **Step 5: Тесты utils и digest render**

`tests/test_utils.py`:
```python
import pytest
from app.utils import classify_youtube_url, extract_video_id, extract_youtube_url, format_ts


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
])
def test_extract_video_id(url, vid):
    assert extract_video_id(url) == vid


def test_extract_video_id_raises_on_channel():
    with pytest.raises(ValueError):
        extract_video_id("https://www.youtube.com/@somechannel")


def test_classify():
    assert classify_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "video"
    assert classify_youtube_url("https://www.youtube.com/@somechannel") == "channel"
    assert classify_youtube_url("https://example.com/watch?v=dQw4w9WgXcQ") == "unknown"


def test_extract_youtube_url_rejects_foreign():
    assert extract_youtube_url("глянь https://vimeo.com/123") is None
    assert extract_youtube_url("вот https://youtu.be/dQw4w9WgXcQ.") == "https://youtu.be/dQw4w9WgXcQ"


def test_format_ts():
    assert format_ts(65) == "01:05"
    assert format_ts(3665) == "01:01:05"
```

`tests/test_digest_render.py`:
```python
from app.digest_service import DigestEntry, render_digest_html, MAX_DIGEST_CHARS


def entry(i):
    return DigestEntry(video_id=f"video{i:06d}", title=f"Заголовок {i}", telegraph_url=f"https://telegra.ph/x-{i}", channel_name="Канал")


def test_empty_digest():
    assert "Пока пусто" in render_digest_html([])


def test_render_contains_links_and_fits_budget():
    html = render_digest_html([entry(i) for i in range(20)])
    assert len(html) <= MAX_DIGEST_CHARS
    assert 'href="https://telegra.ph/x-0"' in html  # newest (первый в списке) всегда внутри


def test_html_escaping():
    e = DigestEntry(video_id="v", title="A <b> & B", telegraph_url="https://telegra.ph/x", channel_name="")
    html = render_digest_html([e])
    assert "&lt;b&gt;" in html and "&amp;" in html
```

- [ ] **Step 6: Прогнать, зафиксировать зелёное состояние**

```bash
./.venv/bin/pytest tests/ -q
```
Expected: все тесты PASS. Если какой-то тест падает — сначала убедиться, что тест верно описывает фактическое поведение (поправить тест), поведение кода в этой задаче НЕ менять.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Add pytest infrastructure and tests for fragile core (JSON recovery, chunking, expert spans)"
```

---

### Task 5: Валидация конфига с внятными ошибками

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: —
- Produces: `load_settings()` — сигнатура и `Settings` без изменений, но все int/float парсятся через `_EnvReader`; при невалидных значениях — `RuntimeError` со списком ВСЕХ ошибок разом. Новое поле `Settings.database_path: Path` (default `data_dir / "bot.db"`) — используется в Task 6.

- [ ] **Step 1: Failing test**

`tests/test_config.py`:
```python
import pytest
from app.config import load_settings


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    # chdir в tmp — чтобы load_dotenv() не подцепил реальный .env репозитория.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")


def test_valid_env_loads(base_env):
    settings = load_settings()
    assert settings.database_path.name == "bot.db"


def test_invalid_int_collects_all_errors(base_env, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_CHUNK_MAX_CHARS", "abc")
    monkeypatch.setenv("LLM_MAX_TOKENS", "не число")
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    text = str(exc.value)
    assert "TRANSCRIPT_CHUNK_MAX_CHARS" in text
    assert "LLM_MAX_TOKENS" in text


def test_invalid_float(base_env, monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "tepло")
    with pytest.raises(RuntimeError, match="LLM_TEMPERATURE"):
        load_settings()
```

Run: `./.venv/bin/pytest tests/test_config.py -q` — Expected: FAIL (нет `database_path`, `int("abc")` даёт голый ValueError).

- [ ] **Step 2: Реализация `_EnvReader`**

В `app/config.py` добавить перед `load_settings`:

```python
class _EnvReader:
    """Читает переменные окружения с накоплением ошибок.

    Вместо падения на первом же int("abc") собираем все проблемы и
    показываем их одним RuntimeError — чтобы .env чинился за один заход,
    а не по одной переменной на рестарт.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def int(self, name: str, default: str) -> int:
        raw = os.getenv(name, default).strip() or default
        try:
            return int(raw)
        except ValueError:
            self.errors.append(f"{name}={raw!r} — ожидается целое число")
            return int(default)

    def float(self, name: str, default: str) -> float:
        raw = os.getenv(name, default).strip() or default
        try:
            return float(raw)
        except ValueError:
            self.errors.append(f"{name}={raw!r} — ожидается число")
            return float(default)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise RuntimeError(
                "Некорректные значения в .env:\n  - " + "\n  - ".join(self.errors)
            )
```

В `load_settings`: создать `env = _EnvReader()` сразу после `load_dotenv()`; заменить ВСЕ прямые `int(os.getenv(...))` / `float(os.getenv(...))` на `env.int(...)` / `env.float(...)` (сюда входят: `MONITORING_LLM_RETRY_INTERVAL_SEC`, `SUMMARY_CACHE_TTL_DAYS`, `OPENROUTER_FALLBACK_RETRY_PASSES`, `OPENROUTER_FALLBACK_RETRY_DELAY_SEC`, `OPENROUTER_DAILY_BUDGET_USD`, `OPENROUTER_DAILY_REQUEST_LIMIT`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_MAX_TOKENS_PARTIAL`, `LLM_MAX_TOKENS_FINAL`, `LMSTUDIO_NUM_CTX`, `TRANSCRIPT_CHUNK_MAX_CHARS`, `OPENROUTER_TRANSCRIPT_CHUNK_MAX_CHARS`, `SYNTHESIS_HIERARCHY_THRESHOLD`, `SYNTHESIS_GROUP_SIZE`). Для двухступенчатых fallback'ов (`LLM_MAX_TOKENS_PARTIAL` с default от `LLM_MAX_TOKENS`) — передавать default строкой: `env.int("LLM_MAX_TOKENS_PARTIAL", os.getenv("LLM_MAX_TOKENS", "1200"))`.
Перед `return Settings(...)` вызвать `env.raise_if_errors()`.
Добавить в `Settings` поле `database_path: Path` и в `load_settings`: `database_path=Path(os.getenv("DATABASE_PATH", str(data_dir / "bot.db"))).expanduser()`.

- [ ] **Step 3: Прогнать тесты**

```bash
./.venv/bin/pytest tests/ -q
```
Expected: PASS все.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "Validate .env values with aggregated error messages; add database_path setting"
```

---

### Task 6: SQLite-слой: `app/db.py` + миграция UserStore и SummaryCache

**Files:**
- Create: `app/db.py`
- Modify: `app/user_store.py`, `app/summary_cache.py`, `app/main.py`
- Test: `tests/test_db_stores.py`

**Interfaces:**
- Consumes: `Settings.database_path` (Task 5).
- Produces:
  - `Database(path: Path)` — методы `execute(sql, params=()) -> None`, `executemany(sql, seq) -> None`, `query(sql, params=()) -> list[sqlite3.Row]`, `query_one(sql, params=()) -> sqlite3.Row | None`, `close()`. Потокобезопасен (внутренний `threading.Lock`, `check_same_thread=False`, WAL).
  - `UserStore(db: Database, seed_user_ids: set[int], owner_user_id: int | None, legacy_json_path: Path | None = None)` — публичные методы прежние: `is_owner`, `is_allowed`, `list_users`, `add_user`, `remove_user`, property `owner_user_id`.
  - `SummaryCache(db: Database, ttl_days: int = 100, legacy_json_path: Path | None = None)` — прежние `get`, `put`, `delete`, `size`; `CachedSummary` без изменений.
  - Легаси-JSON при первом запуске импортируется и переименовывается в `<имя>.migrated`.

- [ ] **Step 1: Failing tests**

`tests/test_db_stores.py`:
```python
import json
import time

from app.db import Database
from app.summary_cache import CachedSummary, SummaryCache
from app.user_store import UserStore


def make_cached(video_id="dQw4w9WgXcQ", created_at_unix=None):
    now = created_at_unix if created_at_unix is not None else time.time()
    return CachedSummary(
        video_id=video_id, url=f"https://youtu.be/{video_id}", title="T", channel_name="C",
        channel_url="", summary_overview="O", summary_key_points=[], summary_chapters=[],
        summary_raw_text="{}", telegraph_url="https://telegra.ph/x", transcript_url=None,
        transcript_source="youtube", model="m", created_at_iso="", created_at_unix=now,
    )


def test_user_store_roundtrip(tmp_path):
    db = Database(tmp_path / "bot.db")
    store = UserStore(db, seed_user_ids={111}, owner_user_id=42)
    assert store.is_owner(42) and store.is_allowed(111)
    store.add_user(7, "Вася")
    # Новый инстанс поверх того же файла видит те же данные.
    store2 = UserStore(Database(tmp_path / "bot.db"), seed_user_ids=set(), owner_user_id=42)
    assert any(u.user_id == 7 and u.name == "Вася" for u in store2.list_users())
    assert store2.remove_user(7) is True
    assert store2.is_allowed(7) is False


def test_user_store_migrates_legacy_json(tmp_path):
    legacy = tmp_path / "users.json"
    legacy.write_text(json.dumps({"users": [{"id": 5, "name": "старый", "added_at": "2025-01-01"}]}), encoding="utf-8")
    db = Database(tmp_path / "bot.db")
    store = UserStore(db, seed_user_ids=set(), owner_user_id=None, legacy_json_path=legacy)
    assert store.is_allowed(5)
    assert not legacy.exists() and legacy.with_suffix(".json.migrated").exists()


def test_summary_cache_roundtrip_and_ttl(tmp_path):
    db = Database(tmp_path / "bot.db")
    cache = SummaryCache(db, ttl_days=100)
    cache.put(make_cached())
    assert cache.get("dQw4w9WgXcQ").title == "T"
    assert cache.size() == 1
    # Протухшая запись удаляется лениво.
    cache.put(make_cached(video_id="expiredvid1", created_at_unix=time.time() - 101 * 86400))
    assert cache.get("expiredvid1") is None
    assert cache.delete("dQw4w9WgXcQ") is True


def test_summary_cache_migrates_legacy_json(tmp_path):
    legacy = tmp_path / "summary_cache.json"
    import dataclasses
    legacy.write_text(json.dumps({"dQw4w9WgXcQ": dataclasses.asdict(make_cached())}), encoding="utf-8")
    cache = SummaryCache(Database(tmp_path / "bot.db"), ttl_days=100, legacy_json_path=legacy)
    assert cache.get("dQw4w9WgXcQ") is not None
    assert legacy.with_suffix(".json.migrated").exists()
```

Run: `./.venv/bin/pytest tests/test_db_stores.py -q` — Expected: FAIL (`No module named 'app.db'`).

- [ ] **Step 2: `app/db.py`**

```python
"""Единая SQLite-база бота.

Все store-классы (пользователи, кэш саммари, дайджесты, состояние мониторинга,
бюджет OpenRouter, персистентная очередь) живут в одном файле ``data/bot.db``.
Дизайн-решения:

- stdlib ``sqlite3``, синхронный API. Каждый запрос — миллисекунды, объёмы
  крошечные; тащить aiosqlite и делать все call-sites асинхронными незачем.
- Одно соединение на процесс, ``check_same_thread=False`` + process-local
  ``threading.Lock`` вокруг каждого запроса: и event loop, и to_thread-вызовы
  ходят через один сериализованный вход. Это убирает гонки, которые раньше
  были возможны между JSON-файлами.
- WAL — чтобы редкие конкурирующие чтения не ждали писателя.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS summary_cache (
    video_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at_unix REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    telegraph_url TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    created_at_unix REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, video_id)
);
CREATE TABLE IF NOT EXISTS digest_pins (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS monitoring_seen (
    channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    added_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (channel_id, video_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    scheduled INTEGER NOT NULL DEFAULT 0,
    disable_notification INTEGER NOT NULL DEFAULT 0,
    title_hint TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS morning_digest_items (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    channel_name TEXT NOT NULL DEFAULT '',
    telegraph_url TEXT NOT NULL DEFAULT '',
    overview TEXT NOT NULL DEFAULT '',
    tags_line TEXT NOT NULL DEFAULT '',
    duration_sec REAL NOT NULL DEFAULT 0,
    created_at_unix REAL NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("db.boot path=%s", path)

    @property
    def path(self) -> Path:
        return self._path

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def executemany(self, sql: str, seq) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def execute_returning_rowid(self, sql: str, params: tuple = ()) -> int:
        """INSERT с возвратом rowid атомарно под общим lock'ом.

        Отдельная пара execute + SELECT last_insert_rowid() между двумя
        захватами lock'а могла бы вернуть чужой id при конкурентной вставке.
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return int(cur.lastrowid)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def retire_legacy_json(path: Path) -> None:
    """Переименовать легаси-JSON после успешного импорта в SQLite.

    ``users.json`` → ``users.json.migrated``: файл остаётся на диске как бэкап,
    но повторная миграция при следующем старте не срабатывает.
    """
    try:
        path.rename(path.with_suffix(path.suffix + ".migrated"))
        logger.info("db.legacy_retired path=%s", path)
    except OSError as exc:
        logger.warning("db.legacy_retire_failed path=%s error=%s", path, exc)
```

- [ ] **Step 3: Переписать `app/user_store.py`**

Сохранить `AllowedUser`, `_parse_user`, `_now_iso`. Класс:

```python
class UserStore:
    """Persistent allow-list поверх SQLite (таблица ``users``).

    ``ALLOWED_USER_IDS`` — только seed при первом запуске (пустая таблица).
    ``legacy_json_path`` — путь к старому users.json: если таблица пуста,
    а файл есть — импортируем и переименовываем в .migrated.
    """

    def __init__(
        self,
        db: Database,
        seed_user_ids: set[int],
        owner_user_id: int | None,
        legacy_json_path: Path | None = None,
    ) -> None:
        self._db = db
        self._owner_user_id = owner_user_id
        if self._count() == 0:
            migrated = legacy_json_path is not None and self._migrate_legacy(legacy_json_path)
            if not migrated:
                self._seed(seed_user_ids)
        self._ensure_owner()

    def _count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def _migrate_legacy(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("users.migrate.load_failed path=%s", path)
            return False
        users = raw.get("users", []) if isinstance(raw, dict) else []
        rows = []
        for item in users:
            user = _parse_user(item)
            if user is not None:
                rows.append((user.user_id, user.name, user.added_at))
        if rows:
            self._db.executemany(
                "INSERT OR REPLACE INTO users(user_id, name, added_at) VALUES (?, ?, ?)", rows
            )
        retire_legacy_json(path)
        logger.info("users.migrated count=%s", len(rows))
        return True

    def _seed(self, seed_user_ids: set[int]) -> None:
        seed_ids = set(seed_user_ids)
        if self._owner_user_id is not None:
            seed_ids.add(self._owner_user_id)
        for user_id in seed_ids:
            self._db.execute(
                "INSERT OR IGNORE INTO users(user_id, name, added_at) VALUES (?, ?, ?)",
                (user_id, "owner" if user_id == self._owner_user_id else "", _now_iso()),
            )

    def _ensure_owner(self) -> None:
        if self._owner_user_id is None:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO users(user_id, name, added_at) VALUES (?, 'owner', ?)",
            (self._owner_user_id, _now_iso()),
        )

    @property
    def owner_user_id(self) -> int | None:
        return self._owner_user_id

    def is_owner(self, user_id: int | None) -> bool:
        return (
            user_id is not None
            and self._owner_user_id is not None
            and user_id == self._owner_user_id
        )

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        if self.is_owner(user_id):
            return True
        if self._owner_user_id is None and self._count() == 0:
            return True
        return self._db.query_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is not None

    def list_users(self) -> list[AllowedUser]:
        rows = self._db.query("SELECT user_id, name, added_at FROM users ORDER BY user_id")
        return [AllowedUser(user_id=r["user_id"], name=r["name"], added_at=r["added_at"]) for r in rows]

    def add_user(self, user_id: int, name: str = "") -> bool:
        name = name.strip()
        existing = self._db.query_one("SELECT name FROM users WHERE user_id = ?", (user_id,))
        if existing is not None and existing["name"] == name:
            return False
        if existing is None:
            self._db.execute(
                "INSERT INTO users(user_id, name, added_at) VALUES (?, ?, ?)",
                (user_id, name, _now_iso()),
            )
            return True
        self._db.execute("UPDATE users SET name = ? WHERE user_id = ?", (name, user_id))
        return False

    def remove_user(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            raise ValueError("Нельзя удалить владельца бота.")
        if self._db.query_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is None:
            return False
        self._db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return True
```

Импорты файла: `json`, `logging`, `Path`, `dataclass`, `datetime/timezone`, `Any`, плюс `from app.db import Database, retire_legacy_json`. Убрать `threading`.

- [ ] **Step 4: Переписать `app/summary_cache.py`**

`CachedSummary` не трогать. Класс:

```python
class SummaryCache:
    """Кэш готовых саммари поверх SQLite (таблица ``summary_cache``).

    Запись хранится как JSON-payload (asdict(CachedSummary)) — схема записи
    остаётся гибкой, отдельная колонка только у created_at_unix для TTL-чисток
    на SQL-уровне.
    """

    def __init__(self, db: Database, ttl_days: int = 100, legacy_json_path: Path | None = None) -> None:
        self._db = db
        self._ttl_seconds = _seconds_in_days(ttl_days)
        if legacy_json_path is not None:
            self._migrate_legacy(legacy_json_path)
        self._cleanup_expired_at_startup()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def _migrate_legacy(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM summary_cache")
        if row and int(row["n"]) > 0:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("summary_cache.migrate.load_failed path=%s", path)
            return
        if not isinstance(raw, dict):
            return
        imported = 0
        for vid, body in raw.items():
            if not isinstance(body, dict):
                continue
            try:
                entry = CachedSummary(**body)
            except (TypeError, KeyError) as exc:
                logger.warning("summary_cache.migrate.skip video_id=%s error=%s", vid, exc)
                continue
            self.put(entry)
            imported += 1
        retire_legacy_json(path)
        logger.info("summary_cache.migrated entries=%s", imported)

    def _cleanup_expired_at_startup(self) -> None:
        if self._ttl_seconds <= 0:
            return
        cutoff = time.time() - self._ttl_seconds
        self._db.execute("DELETE FROM summary_cache WHERE created_at_unix < ?", (cutoff,))

    def get(self, video_id: str) -> CachedSummary | None:
        row = self._db.query_one(
            "SELECT payload, created_at_unix FROM summary_cache WHERE video_id = ?", (video_id,)
        )
        if row is None:
            return None
        if self._ttl_seconds > 0 and (time.time() - row["created_at_unix"]) > self._ttl_seconds:
            self._db.execute("DELETE FROM summary_cache WHERE video_id = ?", (video_id,))
            logger.info("summary_cache.expired video_id=%s", video_id)
            return None
        try:
            return CachedSummary(**json.loads(row["payload"]))
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("summary_cache.corrupt_entry video_id=%s error=%s", video_id, exc)
            return None

    def put(self, entry: CachedSummary) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO summary_cache(video_id, payload, created_at_unix) VALUES (?, ?, ?)",
            (entry.video_id, json.dumps(asdict(entry), ensure_ascii=False), entry.created_at_unix),
        )
        logger.info("summary_cache.stored video_id=%s telegraph_url=%s", entry.video_id, entry.telegraph_url)

    def delete(self, video_id: str) -> bool:
        exists = self._db.query_one("SELECT 1 FROM summary_cache WHERE video_id = ?", (video_id,)) is not None
        if exists:
            self._db.execute("DELETE FROM summary_cache WHERE video_id = ?", (video_id,))
        return exists

    def size(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM summary_cache")
        return int(row["n"]) if row else 0
```

Убрать `threading`, `_seconds_in_days` оставить; добавить `from app.db import Database, retire_legacy_json`.

- [ ] **Step 5: Обновить `app/main.py`**

После `configure_logging(...)`:
```python
    from app.db import Database  # (импорт наверх файла)
    db = Database(settings.database_path)
```
Заменить конструкторы:
```python
    summary_cache = SummaryCache(
        db,
        ttl_days=settings.summary_cache_ttl_days,
        legacy_json_path=settings.summary_cache_path,
    )
    ...
    user_store = UserStore(
        db,
        seed_user_ids=settings.allowed_user_ids,
        owner_user_id=settings.owner_user_id,
        legacy_json_path=settings.allowed_users_path,
    )
```
В `Services` добавить поле `db: "Database | None" = None` (в `bot_handlers.py`) и передать `db=db` из main.

- [ ] **Step 6: Тесты**

```bash
./.venv/bin/pytest tests/ -q
```
Expected: PASS все, включая новые.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "SQLite storage layer: Database + migrate UserStore and SummaryCache from JSON"
```

---

### Task 7: SQLite: DigestStore, MonitoringState, бюджет/режим OpenRouter

**Files:**
- Modify: `app/digest_service.py`, `app/monitoring_state.py`, `app/llm_client.py`, `app/main.py`
- Test: `tests/test_db_stores2.py`

**Interfaces:**
- Consumes: `Database` (Task 6).
- Produces:
  - `DigestStore(db: Database, limit: int = DIGEST_LIMIT, legacy_digests_path: Path | None = None, legacy_pins_path: Path | None = None)` — прежние `add`, `list`, `get_pin`, `set_pin`, `clear_pin`, `_get_or_create_pin_lock`.
  - `MonitoringState(db: Database, legacy_json_path: Path | None = None)` — прежние `is_seen`, `mark_seen`, `prime_channel`; методы `load()`/`save()` УДАЛЕНЫ, их вызовы в `app/main.py` (строки 233–234) и `app/monitoring_service.py` (строки 108, 156) убрать.
  - `OpenRouterBudget` и `OpenRouterRuntimeState` в `llm_client.py` — конструкторы принимают `db: Database, legacy_json_path: Path | None = None` вместо `path`; публичные методы (`check`, `record`, `snapshot`; `is_paid_mode`, `set_paid_mode`) прежние. Состояние хранится в таблице `kv` (ключи `openrouter_budget`, `openrouter_paid_mode`) тем же JSON-payload'ом, что раньше лежал в файле.
  - `OpenRouterClient.__init__(self, settings, db)`; `create_llm_client(settings, db)` — вызов в `main.py` обновить. `LMStudioClient` игнорирует db (сигнатура фабрики единая).

- [ ] **Step 1: Failing tests**

`tests/test_db_stores2.py`:
```python
import json

from app.db import Database
from app.digest_service import DigestEntry, DigestStore
from app.monitoring_state import MonitoringState


def test_digest_add_dedup_and_limit(tmp_path):
    store = DigestStore(Database(tmp_path / "bot.db"), limit=3)
    for i in range(5):
        store.add(1, DigestEntry(video_id=f"v{i}", title=f"t{i}", telegraph_url="u", created_at_unix=i))
    entries = store.list(1)
    assert [e.video_id for e in entries] == ["v4", "v3", "v2"]  # newest-first, limit=3
    # Дедуп: повтор video_id переезжает наверх, не дублируется.
    store.add(1, DigestEntry(video_id="v3", title="new", telegraph_url="u", created_at_unix=99))
    entries = store.list(1)
    assert entries[0].video_id == "v3" and entries[0].title == "new"
    assert len(entries) == 3


def test_digest_pins(tmp_path):
    store = DigestStore(Database(tmp_path / "bot.db"))
    assert store.get_pin(1) is None
    store.set_pin(1, chat_id=10, message_id=20)
    assert store.get_pin(1) == (10, 20)
    store.clear_pin(1)
    assert store.get_pin(1) is None


def test_monitoring_state(tmp_path):
    state = MonitoringState(Database(tmp_path / "bot.db"))
    assert not state.is_seen("ch", "vid")
    state.mark_seen("ch", "vid")
    assert state.is_seen("ch", "vid")
    state.prime_channel("ch2", ["a", "b"])
    assert state.is_seen("ch2", "a") and state.is_seen("ch2", "b")


def test_monitoring_state_migrates_legacy(tmp_path):
    legacy = tmp_path / "monitoring_state.json"
    legacy.write_text(json.dumps({"channels": {"ch": {"seen_video_ids": ["x1"]}}}), encoding="utf-8")
    state = MonitoringState(Database(tmp_path / "bot.db"), legacy_json_path=legacy)
    assert state.is_seen("ch", "x1")
    assert legacy.with_suffix(".json.migrated").exists()
```

Run — Expected: FAIL (конструкторы ещё принимают Path).

- [ ] **Step 2: `digest_service.py`**

Убрать `threading`, `_load/_save/_atomic_write` и in-memory dict'ы. Новый конструктор и методы (рендер и pin-update-функции ниже по файлу НЕ трогать):

```python
class DigestStore:
    """Per-user digest list + pinned-message tracking поверх SQLite."""

    def __init__(
        self,
        db: Database,
        limit: int = DIGEST_LIMIT,
        legacy_digests_path: Path | None = None,
        legacy_pins_path: Path | None = None,
    ) -> None:
        self._db = db
        self._limit = limit
        self._pin_update_locks: dict[int, asyncio.Lock] = {}
        self._pin_locks_guard = threading.Lock()
        if legacy_digests_path is not None:
            self._migrate_digests(legacy_digests_path)
        if legacy_pins_path is not None:
            self._migrate_pins(legacy_pins_path)

    def _migrate_digests(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM digests")
        if row and int(row["n"]) > 0:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("digests.migrate.load_failed path=%s", path)
            return
        if isinstance(raw, dict):
            for raw_uid, raw_entries in raw.items():
                try:
                    user_id = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if not isinstance(raw_entries, list):
                    continue
                for body in raw_entries:
                    if isinstance(body, dict):
                        try:
                            self._insert(user_id, DigestEntry(**body))
                        except (TypeError, KeyError):
                            continue
        retire_legacy_json(path)

    def _migrate_pins(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("digests.migrate.pins_load_failed path=%s", path)
            return
        if isinstance(raw, dict):
            for raw_uid, body in raw.items():
                try:
                    self.set_pin(int(raw_uid), int(body["chat_id"]), int(body["message_id"]))
                except (TypeError, KeyError, ValueError):
                    continue
        retire_legacy_json(path)

    def _insert(self, user_id: int, entry: DigestEntry) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO digests(user_id, video_id, title, telegraph_url, channel_name, created_at_unix) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, entry.video_id, entry.title, entry.telegraph_url, entry.channel_name, entry.created_at_unix),
        )

    def add(self, user_id: int, entry: DigestEntry) -> list[DigestEntry]:
        self._insert(user_id, entry)
        # Подрезаем хвост за limit — старые записи наружу не отдаются, так что
        # можно чистить сразу на записи. rowid DESC — tie-breaker при равных
        # created_at_unix (порядок вставки).
        self._db.execute(
            "DELETE FROM digests WHERE user_id = ? AND video_id NOT IN ("
            "  SELECT video_id FROM digests WHERE user_id = ? "
            "  ORDER BY created_at_unix DESC, rowid DESC LIMIT ?)",
            (user_id, user_id, self._limit),
        )
        return self.list(user_id)

    def list(self, user_id: int) -> list[DigestEntry]:
        rows = self._db.query(
            "SELECT video_id, title, telegraph_url, channel_name, created_at_unix "
            "FROM digests WHERE user_id = ? ORDER BY created_at_unix DESC, rowid DESC LIMIT ?",
            (user_id, self._limit),
        )
        return [
            DigestEntry(
                video_id=r["video_id"], title=r["title"], telegraph_url=r["telegraph_url"],
                channel_name=r["channel_name"], created_at_unix=r["created_at_unix"],
            )
            for r in rows
        ]

    def get_pin(self, user_id: int) -> tuple[int, int] | None:
        row = self._db.query_one("SELECT chat_id, message_id FROM digest_pins WHERE user_id = ?", (user_id,))
        return (row["chat_id"], row["message_id"]) if row else None

    def set_pin(self, user_id: int, chat_id: int, message_id: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO digest_pins(user_id, chat_id, message_id) VALUES (?, ?, ?)",
            (user_id, chat_id, message_id),
        )

    def clear_pin(self, user_id: int) -> None:
        self._db.execute("DELETE FROM digest_pins WHERE user_id = ?", (user_id,))

    def _get_or_create_pin_lock(self, user_id: int) -> asyncio.Lock:
        with self._pin_locks_guard:
            lock = self._pin_update_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._pin_update_locks[user_id] = lock
            return lock
```

NB: dedup по `video_id` обеспечивается `INSERT OR REPLACE` по составному PK `(user_id, video_id)`; `rowid DESC` в обоих `ORDER BY` — tie-breaker при равных `created_at_unix`.

- [ ] **Step 3: `monitoring_state.py`**

```python
class MonitoringState:
    """Persistent 'what we've already seen' поверх SQLite (таблица monitoring_seen)."""

    def __init__(self, db: Database, legacy_json_path: Path | None = None) -> None:
        self._db = db
        if legacy_json_path is not None:
            self._migrate_legacy(legacy_json_path)

    def _migrate_legacy(self, path: Path) -> None:
        if not path.exists():
            return
        row = self._db.query_one("SELECT COUNT(*) AS n FROM monitoring_seen")
        if row and int(row["n"]) > 0:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("monitoring.state.migrate_failed path=%s", path)
            return
        channels_raw = (data or {}).get("channels") or {}
        now = time.time()
        rows = []
        for channel_id, entry in channels_raw.items():
            ids = entry.get("seen_video_ids") or [] if isinstance(entry, dict) else (entry if isinstance(entry, list) else [])
            for video_id in ids:
                if str(video_id).strip():
                    rows.append((str(channel_id), str(video_id), now))
        if rows:
            self._db.executemany(
                "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)", rows
            )
        retire_legacy_json(path)
        logger.info("monitoring.state.migrated rows=%s", len(rows))

    def is_seen(self, channel_id: str, video_id: str) -> bool:
        return self._db.query_one(
            "SELECT 1 FROM monitoring_seen WHERE channel_id = ? AND video_id = ?", (channel_id, video_id)
        ) is not None

    def mark_seen(self, channel_id: str, video_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)",
            (channel_id, video_id, time.time()),
        )

    def prime_channel(self, channel_id: str, video_ids: list[str]) -> None:
        now = time.time()
        self._db.executemany(
            "INSERT OR IGNORE INTO monitoring_seen(channel_id, video_id, added_at) VALUES (?, ?, ?)",
            [(channel_id, vid, now) for vid in video_ids if vid],
        )
```

Убрать вызовы `state.load()` (main.py:234), `self._state.save()` (monitoring_service.py:108, 156). `MAX_SEEN_PER_CHANNEL`/deque больше не нужны (полная история дешёвая в SQLite).

- [ ] **Step 4: `llm_client.py` — бюджет и runtime поверх kv**

`OpenRouterBudget.__init__(self, db: Database, daily_budget_usd: float, daily_request_limit: int, legacy_json_path: Path | None = None)`. Внутри: `_load()` читает `kv` ключ `openrouter_budget` (JSON `{"day": ..., "spent_usd": ..., "requests": ...}`); если ключа нет и `legacy_json_path` существует — прочитать старый файл того же формата, записать в kv, `retire_legacy_json`. `_save()` пишет JSON в kv через `INSERT OR REPLACE`. Логика `check/record/snapshot/_rollover_if_needed/_today` не меняется.

`OpenRouterRuntimeState.__init__(self, db: Database, legacy_json_path: Path | None = None)` — аналогично, ключ `openrouter_paid_mode`, значение `"true"/"false"`.

`OpenRouterClient.__init__(self, settings: Settings, db: Database)` — создать budget/runtime с `legacy_json_path=settings.openrouter_budget_state_path` / `settings.openrouter_runtime_state_path`. `create_llm_client(settings, db)` пробрасывает db в OpenRouter-ветку; LMStudio-ветка db не использует. В `main.py`: `llm = create_llm_client(settings, db)` (перенести создание db выше llm).

- [ ] **Step 5: `main.py` — конструкторы digest/monitoring**

```python
    digest_store = DigestStore(
        db,
        legacy_digests_path=settings.digests_path,
        legacy_pins_path=settings.digest_pins_path,
    )
    ...
    monitoring_state = MonitoringState(db, legacy_json_path=settings.monitoring_state_path)
```

- [ ] **Step 6: Тесты + smoke**

```bash
./.venv/bin/pytest tests/ -q
python3 -m compileall app/ -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Migrate digests, monitoring state and OpenRouter budget/runtime to SQLite"
```

---

### Task 8: Персистентная очередь задач

**Files:**
- Create: `app/job_store.py`
- Modify: `app/bot_handlers.py`, `app/main.py`
- Test: `tests/test_job_store.py`

**Interfaces:**
- Consumes: `Database`.
- Produces:
  - `JobStore(db: Database)`: `add(url, chat_id, *, scheduled: bool, disable_notification: bool, title_hint: str | None) -> int`; `set_status(job_id: int, status: str) -> None` (status ∈ queued/active/done/failed/cancelled); `pending() -> list[sqlite3.Row]` (queued+active, ORDER BY id); `counts_since(days: int) -> dict[str, int]`; `scheduled_pending_count() -> int`.
  - `SummaryJob.db_id: int | None = None` — новое поле.
  - `restore_pending_jobs(services) -> int` в `bot_handlers.py` — вызывается из `main()` после сборки Services, до `start_polling`.
  - `Services.job_store: "JobStore | None" = None`.

- [ ] **Step 1: Failing test**

`tests/test_job_store.py`:
```python
from app.db import Database
from app.job_store import JobStore


def test_job_lifecycle(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    job_id = store.add("https://youtu.be/x", 42, scheduled=False, disable_notification=False, title_hint=None)
    assert [r["id"] for r in store.pending()] == [job_id]
    store.set_status(job_id, "active")
    assert [r["status"] for r in store.pending()] == ["active"]
    store.set_status(job_id, "done")
    assert store.pending() == []
    assert store.counts_since(30)["done"] == 1


def test_scheduled_pending_count(tmp_path):
    store = JobStore(Database(tmp_path / "bot.db"))
    store.add("u1", 1, scheduled=True, disable_notification=True, title_hint="t")
    j2 = store.add("u2", 1, scheduled=False, disable_notification=False, title_hint=None)
    assert store.scheduled_pending_count() == 1
    store.set_status(j2, "done")
    assert store.scheduled_pending_count() == 1
```

- [ ] **Step 2: `app/job_store.py`**

```python
"""Персистентная очередь summary-задач (таблица ``jobs``).

asyncio.Queue остаётся рабочим механизмом «кто следующий» в памяти; таблица —
источник восстановления после рестарта. На enqueue пишем строку, worker двигает
status, при старте бота ``restore_pending_jobs`` перечитывает queued/active и
кладёт их обратно в очередь (message=None — доставка пойдёт через bot.send_message).
"""
from __future__ import annotations

import sqlite3
import time

from app.db import Database


class JobStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        url: str,
        chat_id: int,
        *,
        scheduled: bool,
        disable_notification: bool,
        title_hint: str | None,
    ) -> int:
        now = time.time()
        return self._db.execute_returning_rowid(
            "INSERT INTO jobs(url, chat_id, scheduled, disable_notification, title_hint, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
            (url, chat_id, int(scheduled), int(disable_notification), title_hint, now, now),
        )

    def set_status(self, job_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), job_id),
        )

    def pending(self) -> list[sqlite3.Row]:
        return self._db.query(
            "SELECT * FROM jobs WHERE status IN ('queued', 'active') ORDER BY id"
        )

    def scheduled_pending_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE scheduled = 1 AND status IN ('queued', 'active')"
        )
        return int(row["n"]) if row else 0

    def counts_since(self, days: int) -> dict[str, int]:
        cutoff = time.time() - days * 86400
        rows = self._db.query(
            "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= ? GROUP BY status", (cutoff,)
        )
        return {r["status"]: int(r["n"]) for r in rows}
```

- [ ] **Step 3: Вплести в bot_handlers.py**

1. `SummaryJob`: добавить поле `db_id: int | None = None`.
2. `Services`: добавить `job_store: "JobStore | None" = None` (+ импорт).
3. В `_enqueue_summary_job` (внутри `async with services.summary_queue_lock:` перед `SummaryJob(...)`): `db_id = services.job_store.add(url, message.chat.id, scheduled=False, disable_notification=False, title_hint=None) if services.job_store else None`, передать `db_id=db_id` в `SummaryJob(...)`.
4. То же в `enqueue_scheduled_candidate` с `scheduled=True, disable_notification=True, title_hint=candidate.metadata.title or candidate.feed_entry.title`.
5. В `_summary_queue_worker`: перед запуском обработки job'а — `if services.job_store and job.db_id: services.job_store.set_status(job.db_id, "active")`; после успешного завершения — `"done"`; в except-ветках — `"failed"` / при CancelledError — `"cancelled"`. В `_drain_summary_queue` — каждому вычищенному job'у `"cancelled"`.
6. Важно: маршрут «нет субтитров → transcription_queue» НЕ финализирует статус (job вернётся в summary_queue после Groq) — статус остаётся `active`. В `_process_transcription_job`: при неудаче транскрипции — `"failed"`.
7. Новая функция:

```python
async def restore_pending_jobs(services: Services) -> int:
    """Восстановить незавершённые job'ы из БД после рестарта контейнера.

    message=None: доставка результата пойдёт через bot.send_message(chat_id) —
    тот же путь, что у scheduled-задач. pre_fetched-данные не персистились,
    metadata/субтитры будут получены заново (кэш саммари при этом продолжает
    отсекать полные повторы).
    """
    if services.job_store is None:
        return 0
    rows = services.job_store.pending()
    restored = 0
    async with services.summary_queue_lock:
        for row in rows:
            services.summary_next_sequence += 1
            job = SummaryJob(
                sequence=services.summary_next_sequence,
                message=None,
                url=row["url"],
                enqueued_at=time.monotonic(),
                chat_id=row["chat_id"],
                title_hint=row["title_hint"],
                scheduled=bool(row["scheduled"]),
                disable_notification=bool(row["disable_notification"]),
                db_id=row["id"],
            )
            services.job_store.set_status(row["id"], "queued")
            await services.summary_queue.put(job)
            restored += 1
        if restored and (services.summary_worker_task is None or services.summary_worker_task.done()):
            services.summary_worker_task = asyncio.create_task(_summary_queue_worker(services))
    if restored:
        logger.info("queue.restored jobs=%s", restored)
    return restored
```

8. `main.py`: `services.job_store = JobStore(db)` (через аргумент конструктора Services), и после `configure_bot_commands`: `await restore_pending_jobs(services)` (+ импорт из bot_handlers).
9. `/stats`: в `stats`-хендлере перед выводом лог-статистики добавить блок из БД:

```python
        job_counts = services.job_store.counts_since(30) if services.job_store else {}
        db_line = (
            f"Jobs за 30 дней (БД): ✅ {job_counts.get('done', 0)} · "
            f"❌ {job_counts.get('failed', 0)} · ⏹ {job_counts.get('cancelled', 0)}\n\n"
        )
```
и приклеить `db_line` к началу существующего текста статистики.

- [ ] **Step 4: Тесты + smoke**

```bash
./.venv/bin/pytest tests/ -q && python3 -m compileall app/ -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Persistent job queue in SQLite: restore pending jobs after restart, DB-backed /stats counts"
```

---

### Task 9: Распил bot_handlers.py (move-only, поведение не меняется)

**Files:**
- Create: `app/services_container.py`, `app/status_messages.py`, `app/delivery.py`, `app/pipeline.py`, `app/queue_service.py`
- Modify: `app/bot_handlers.py`, `app/main.py`

**Interfaces:**
- Consumes: всё из предыдущих задач.
- Produces: те же имена функций/классов, но в новых модулях. Внешние точки входа сохраняются реэкспортом: `app.bot_handlers` продолжает экспортировать `Services`, `build_router`, `enqueue_scheduled_candidate`, `restore_pending_jobs` (импортируя их из новых модулей), чтобы `main.py` менять минимально.

Правила: функции переносятся БЕЗ изменения тел; имена с `_` сохраняются (импорт `from app.delivery import _send_summary_delivery` — законен для внутренних модулей пакета). Циклы разрываются направлением зависимостей: `services_container` ← (все); `status_messages` ← delivery/pipeline/queue_service; `delivery` ← pipeline/queue_service/bot_handlers; `pipeline` ← queue_service; `queue_service` ← bot_handlers.

- [ ] **Step 1: `app/services_container.py`**

Перенести из bot_handlers.py: `SummaryJob`, `Services`, `PendingAdminInput`, `PENDING_ADMIN_TIMEOUT_SEC`, константы `MAX_TELEGRAM_MESSAGE_CHARS`, `TOP_COMMENT_MAX_CHARS`, `_YOUTUBE_VIDEO_ID_RE` (сделать публичной: `YOUTUBE_VIDEO_ID_RE`). Импорты — только типы (Settings, stores, LLMClient, Summarizer, TelegraphService и т.д.), БЕЗ импорта других новых модулей.

- [ ] **Step 2: `app/status_messages.py`**

Перенести: `_set_service_status`, `_bump_service_status`, `_forget_service_status`, `_delete_service_status`, `_render_service_status`, `_format_job_header`, `_queue_block`, `_job_label`, `_refresh_active_service_status`, `_run_with_telegram_status`, `_format_elapsed`, `_format_elapsed_minutes`, `_PROGRESS_BAR_CELLS`, `_PROGRESS_BAR_FULL`, `_PROGRESS_BAR_EMPTY`, `_format_job_progress`, `_estimate_job_total_seconds`, `_format_russian_hours`, `_format_russian_minutes`, `_delete_message_safely`, `_fit_telegram_message`, `_prefetch_job_title`.

- [ ] **Step 3: `app/delivery.py`**

Перенести: `_format_telegram_summary`, `_format_tags_line`, `_build_tags_hints`, `_resolve_summary_tags`, `_canonicalize_names`, `_normalize_channel_simple`, `_format_top_comment_line`, `_fit_escaped_text`, `_format_likes`, `_format_compact_count`, `_send_summary_delivery`, `_build_summary_keyboard`, `_is_job_cacheable`, `_lookup_cached_summary`, `_format_cached_summary_text`, `_refresh_cached_comments`, `_comments_equivalent`, `_send_cached_summary_to_chat`, `_deliver_cached_summary_for_job`, `_save_summary_to_cache`, `_format_generation_error`, `_estimate_reading_time_minutes`, `_resolve_digest_target`, `_update_user_digest_safely`, `_message_user_id`, `_job_is_owner`.

- [ ] **Step 4: `app/pipeline.py`**

Перенести: `_process_youtube_job`, `_fetch_top_comments_background`, `_cancel_task_safely`, `_build_context_hint`, `_process_transcription_job`, `_cleanup_audio_file`, `_send_transcription_failure`, `_classify_youtube_download_error`, `_download_audio_to_chat`, `_ensure_audio_fits_telegram`, `_publish_to_channel`, `_chat_id_to_link` (обе копии → оставить одну; вторая с `noqa: F811` — дубль, удалить первую), `_format_channel_post_caption`, `_channel_top_comment_line`, `_truncate_plain`, константы `TELEGRAM_AUDIO_MAX_BYTES`, `TELEGRAM_AUDIO_CAPTION_LIMIT`, `CHANNEL_POST_TEXT_BUDGET`.

- [ ] **Step 5: `app/queue_service.py`**

Перенести: `_summary_queue_worker`, `_transcription_queue_worker`, `_enqueue_summary_job`, `enqueue_scheduled_candidate`, `_enqueue_transcription_job`, `_stop_summary_queue`, `_drain_summary_queue`, `_format_queue_status`, `_is_llm_available`, `restore_pending_jobs`.

- [ ] **Step 6: `app/bot_handlers.py` — остаток**

Остаются: `build_router` со всеми командными хендлерами, `text_message`, `download_audio_callback`, `_compute_stats_for_telegram`, `_format_llm_mode_status`, `_format_scan_status`, `_run_manual_scan`, `_handle_channel_url`, `_answer_owner_only`, `_apply_user_add`, `_apply_user_remove`, `_apply_cache_drop`, `_apply_prompt_set`, `_is_allowed`, `_is_owner`, `SCAN_PROGRESS_THROTTLE_SEC`. В начало файла — реэкспорт:

```python
from app.services_container import PendingAdminInput, PENDING_ADMIN_TIMEOUT_SEC, Services, SummaryJob  # noqa: F401
from app.queue_service import enqueue_scheduled_candidate, restore_pending_jobs, _enqueue_summary_job, _stop_summary_queue  # noqa: F401
```
плюс точечные импорты из delivery/status_messages для функций, используемых хендлерами.

- [ ] **Step 7: Проверка**

```bash
python3 -m compileall app/ -q
./.venv/bin/pytest tests/ -q
./.venv/bin/python -c "import app.main"
grep -c "" app/bot_handlers.py
```
Expected: компиляция и тесты зелёные, `import app.main` без ошибок (упадёт только на отсутствии env — допустимо, если ошибка про TELEGRAM_BOT_TOKEN, а не ImportError), bot_handlers.py ужался примерно до ~1000 строк.

- [ ] **Step 8: Smoke в Docker**

```bash
docker compose build && docker compose up -d && sleep 10 && docker compose logs --tail=30 bot && docker compose down
```
Expected: в логах штатный boot (`db.boot`, `users.boot`, `monitoring.boot`), без Traceback.

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "Split bot_handlers into services_container/status_messages/delivery/pipeline/queue_service"
```

---

### Task 10: Telegraph: ретраи + деградация без падения job'а

**Files:**
- Modify: `app/telegraph_service.py`, `app/pipeline.py`
- Test: `tests/test_telegraph_retry.py`

**Interfaces:**
- Consumes: `TelegraphService` после Task 2.
- Produces: `TelegraphService._post_with_retries(endpoint: str, data: dict) -> dict` — внутренний helper c 3 попытками (пауза 2s, 8s) на `httpx.HTTPError` и 5xx. `publish`/`edit`/`_create_account` ходят через него. `_process_youtube_job` при финальном отказе publish продолжает доставку без кнопки и без кэширования.

- [ ] **Step 1: Failing test**

`tests/test_telegraph_retry.py`:
```python
import httpx
import pytest

from app.config import Settings
from app.telegraph_service import TelegraphService


def make_service(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    from app.config import load_settings
    svc = TelegraphService(load_settings())
    svc._access_token = "token"
    return svc


async def test_retries_then_succeeds(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_post(self, url, data=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://telegra.ph/ok"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.telegraph_service.RETRY_DELAYS_SEC", (0, 0))
    result = await svc._post_with_retries("createPage", {"title": "t"})
    assert result["result"]["url"] == "https://telegra.ph/ok"
    assert calls["n"] == 3


async def test_gives_up_after_attempts(tmp_path, monkeypatch):
    svc = make_service(tmp_path, monkeypatch)

    async def fake_post(self, url, data=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.telegraph_service.RETRY_DELAYS_SEC", (0, 0))
    with pytest.raises(httpx.ConnectError):
        await svc._post_with_retries("createPage", {"title": "t"})
```

- [ ] **Step 2: Реализация**

В `telegraph_service.py`:

```python
RETRY_DELAYS_SEC: tuple[float, ...] = (2.0, 8.0)


class TelegraphService:
    ...
    async def _post_with_retries(self, endpoint: str, data: dict) -> dict:
        """POST к api.telegra.ph с ретраями на сетевые ошибки и 5xx.

        Часовая генерация саммари не должна пропадать из-за секундного
        сбоя HTTP — три попытки с паузами 2s/8s. 4xx (наши ошибки данных)
        не ретраим.
        """
        last_exc: Exception | None = None
        for attempt, delay in enumerate((*RETRY_DELAYS_SEC, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(f"https://api.telegra.ph/{endpoint}", data=data)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                last_exc = exc
                if delay is None:
                    break
                logger.warning(
                    "telegraph.retry endpoint=%s attempt=%s error=%s", endpoint, attempt, exc
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc
```
(+ `import asyncio`). Переписать `publish`, `edit`, `_create_account`: заменить блок `async with httpx.AsyncClient... response.json()` на `data = await self._post_with_retries("createPage", {...})` (соответственно `editPage`, `createAccount`), дальнейшая обработка `data.get("ok")` без изменений.

- [ ] **Step 3: Деградация в pipeline**

В `_process_youtube_job` обернуть публикацию:

```python
        await _set_service_status(services, message, "Публикую полный конспект в Telegra.ph...", job=job)
        try:
            telegraph_url = await _run_with_telegram_status(
                services=services,
                source_message=message,
                operation=services.telegraph.publish(
                    title=title, url=url, summary=summary, top_comments=top_comments,
                ),
                base_text="Публикую полный конспект в Telegra.ph...",
                job=job,
            )
        except Exception:
            # Telegra.ph лежит — не роняем job: пользователь получит краткое
            # саммари в чат, просто без кнопки на полный конспект. Кэш не
            # пишем (без URL запись бесполезна), дайджест сам пропустит
            # запись без telegraph_url.
            logger.exception("job.telegraph.publish_failed job_id=%s — деградируем без страницы", job_id)
            telegraph_url = ""
```
Ниже по функции: `_format_telegram_summary(..., telegraph_url=telegraph_url, ...)` — проверить, что функция терпит пустую строку (если она вставляет ссылку безусловно — обернуть блок ссылки условием `if telegraph_url`); в `_send_summary_delivery(... telegraph_url=telegraph_url or None)`; блок кэширования дополнить условием `and telegraph_url`; digest-блок уже защищён (`if not telegraph_url: return` внутри `_update_user_digest_safely`).

- [ ] **Step 4: Тесты**

```bash
./.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Telegraph resilience: retry with backoff, degrade to no-page delivery instead of failing the job"
```

---

### Task 11: Circuit breaker для OpenRouter

**Files:**
- Modify: `app/llm_client.py`
- Test: `tests/test_circuit_breaker.py`

**Interfaces:**
- Consumes: —
- Produces: класс `CircuitBreaker(threshold: int = 2, cooldown_sec: float = 600)` в `llm_client.py`: `is_open() -> bool`, `record_failure() -> None`, `record_success() -> None`, `remaining_sec() -> float`. `OpenRouterClient` получает `self._breaker = CircuitBreaker()`; `generate()` при открытом breaker'e мгновенно кидает `RuntimeError` с остатком кулдауна.

- [ ] **Step 1: Failing test**

`tests/test_circuit_breaker.py`:
```python
from app.llm_client import CircuitBreaker


def test_opens_after_threshold(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.llm_client.time.monotonic", lambda: clock["t"])
    b = CircuitBreaker(threshold=2, cooldown_sec=600)
    assert not b.is_open()
    b.record_failure()
    assert not b.is_open()          # одна неудача — ещё не паттерн
    b.record_failure()
    assert b.is_open()
    clock["t"] += 601
    assert not b.is_open()          # кулдаун истёк — пробуем снова


def test_success_resets(monkeypatch):
    monkeypatch.setattr("app.llm_client.time.monotonic", lambda: 0.0)
    b = CircuitBreaker(threshold=2, cooldown_sec=600)
    b.record_failure()
    b.record_success()
    b.record_failure()
    assert not b.is_open()
```

- [ ] **Step 2: Реализация**

В `llm_client.py` (рядом с константами, `import time` уже есть/добавить):

```python
class CircuitBreaker:
    """Предохранитель от бесполезных полных проходов fallback-цепочки.

    Если OpenRouter лёг целиком (N подряд задач исчерпали все модели цепочки),
    каждая следующая задача без предохранителя ждала бы 4–6 минут таймаутов.
    После ``threshold`` подряд неудач открываемся на ``cooldown_sec`` — задачи
    в этот период падают мгновенно с понятной причиной.
    """

    def __init__(self, threshold: int = 2, cooldown_sec: float = 600.0) -> None:
        self._threshold = threshold
        self._cooldown_sec = cooldown_sec
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown_sec:
            # Кулдаун истёк — half-open: даём следующей задаче попробовать.
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def remaining_sec(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._cooldown_sec - (time.monotonic() - self._opened_at))

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
```

В `OpenRouterClient.__init__`: `self._breaker = CircuitBreaker()`. В начале `generate()`:

```python
        if self._breaker.is_open():
            raise RuntimeError(
                "OpenRouter временно недоступен (circuit breaker), "
                f"следующая попытка через ~{int(self._breaker.remaining_sec() / 60) + 1} мин."
            )
```
В `generate()` найти точку успешного возврата результата → перед `return` вызвать `self._breaker.record_success()`; в точке, где цепочка исчерпана и кидается финальная ошибка (после всех passes в `_generate_with_retries`/`generate`) → `self._breaker.record_failure()`. ВАЖНО: `record_failure` только при исчерпании ВСЕЙ цепочки, не на промежуточных 429.

- [ ] **Step 3: Тесты**

```bash
./.venv/bin/pytest tests/ -q
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "Circuit breaker for OpenRouter: fail fast while the provider is down"
```

---

### Task 12: Лимит /prompt_set + уведомление о молча пропущенных видео

**Files:**
- Modify: `app/bot_handlers.py` (`_apply_prompt_set`), `app/monitoring_service.py`, `app/main.py`

**Interfaces:**
- Consumes: —
- Produces: `MonitoringService.__init__(..., on_skip: Callable[[str, str], Awaitable[None]] | None = None)` — вызывается как `await on_skip(video_title, reason_text)` для reason `segment_unresolved`.

- [ ] **Step 1: Лимит prompt_set**

В `_apply_prompt_set` после проверки `prompt_text.startswith("/")` добавить:

```python
    MAX_SYSTEM_PROMPT_CHARS = 8000
    if len(prompt_text) > MAX_SYSTEM_PROMPT_CHARS:
        await message.answer(
            f"Промпт слишком длинный: {len(prompt_text)} символов при лимите "
            f"{MAX_SYSTEM_PROMPT_CHARS}. Такой промпт съест контекст модели и "
            "испортит все саммари. Сократи и пришли ещё раз."
        )
        return
```
(константу вынести на уровень модуля рядом с `PENDING_ADMIN_TIMEOUT_SEC`).

- [ ] **Step 2: Уведомление о segment_unresolved**

`monitoring_service.py`: конструктор — добавить параметр `on_skip: Callable[[str, str], Awaitable[None]] | None = None`, сохранить в `self._on_skip`. В `_evaluate_entry`, в ветке `if not segment_spans:` (строки 336–341) перед `return None, False` добавить:

```python
                if self._on_skip is not None:
                    try:
                        await self._on_skip(
                            entry.title,
                            "нашёл эксперта ({}), но не смог выделить фрагмент с ним".format(
                                ", ".join(expert_matches)
                            ),
                        )
                    except Exception:
                        logger.exception("monitoring.on_skip_failed video_id=%s", entry.video_id)
```

`main.py`: рядом с `_enqueue`:

```python
        async def _notify_skip(title: str, reason: str) -> None:
            owner = settings.owner_user_id
            if owner is None:
                return
            await bot.send_message(
                chat_id=owner,
                text=f"Мониторинг пропустил видео «{title}»: {reason}.",
                disable_notification=True,
            )
```
и передать `on_skip=_notify_skip` в `MonitoringService(...)`.

- [ ] **Step 3: Проверка + Commit**

```bash
python3 -m compileall app/ -q && ./.venv/bin/pytest tests/ -q
git add -A && git commit -m "Limit /prompt_set length; notify owner about silently skipped segment-mode videos"
```

---

### Task 13: Browser extension: настраиваемый хэндл бота

**Files:**
- Create: `browser-extension/options.html`, `browser-extension/options.js`
- Modify: `browser-extension/manifest.json`, `browser-extension/content.js`, `browser-extension/README.md`

- [ ] **Step 1: manifest.json**

Добавить в корень манифеста:
```json
  "permissions": ["storage"],
  "options_ui": { "page": "options.html", "open_in_tab": false }
```

- [ ] **Step 2: options.html + options.js**

`options.html`:
```html
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>YouTube Summary — настройки</title>
<style>body{font:14px system-ui;padding:16px;min-width:320px}label{display:block;margin-bottom:6px}input{width:100%;padding:6px;box-sizing:border-box}button{margin-top:10px;padding:6px 14px}#status{color:green;margin-left:8px}</style>
</head>
<body>
  <label for="handle">Telegram-хэндл бота (без @):</label>
  <input id="handle" placeholder="YouTube_Sum_mary_bot">
  <button id="save">Сохранить</button><span id="status"></span>
  <script src="options.js"></script>
</body>
</html>
```

`options.js`:
```javascript
const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_HANDLE = "YouTube_Sum_mary_bot";

function load() {
  api.storage.sync.get({ botHandle: DEFAULT_HANDLE }, (items) => {
    document.getElementById("handle").value = items.botHandle;
  });
}

document.getElementById("save").addEventListener("click", () => {
  const value = document.getElementById("handle").value.trim().replace(/^@/, "") || DEFAULT_HANDLE;
  api.storage.sync.set({ botHandle: value }, () => {
    const status = document.getElementById("status");
    status.textContent = "Сохранено";
    setTimeout(() => (status.textContent = ""), 1500);
  });
});

load();
```

- [ ] **Step 3: content.js**

Найти константу `BOT_HANDLE` и точку сборки deep-link URL. Заменить на асинхронное чтение:

```javascript
const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_BOT_HANDLE = "YouTube_Sum_mary_bot";

function getBotHandle() {
  return new Promise((resolve) => {
    try {
      api.storage.sync.get({ botHandle: DEFAULT_BOT_HANDLE }, (items) =>
        resolve(items.botHandle || DEFAULT_BOT_HANDLE)
      );
    } catch {
      resolve(DEFAULT_BOT_HANDLE);
    }
  });
}
```
В click-обработчике кнопки: `const handle = await getBotHandle();` и `https://t.me/${handle}?start=${videoId}` (обработчик сделать `async`).

- [ ] **Step 4: Проверка + README + Commit**

Загрузить unpacked в Chrome (`chrome://extensions`), открыть любой `/watch`-URL, убедиться, что кнопка работает с дефолтом, поменять хэндл в options — кнопка ведёт на новый. В README расширения — абзац про options.
```bash
git add -A && git commit -m "Browser extension: configurable bot handle via options page"
```

---

### Task 14: Утренний ранжированный дайджест мониторинга

**Files:**
- Create: `app/morning_digest.py`
- Modify: `app/pipeline.py`, `app/queue_service.py`, `app/delivery.py`, `app/monitoring_config.py`, `app/main.py`, `app/services_container.py`
- Test: `tests/test_morning_digest.py`

**Interfaces:**
- Consumes: `Database` (таблица `morning_digest_items` уже в схеме Task 6), `JobStore.scheduled_pending_count()`, `Services.llm`, `Services.monitoring`, `Settings.monitoring_target_chat_id`.
- Produces:
  - `MorningDigestItem` (dataclass): `video_id, title, channel_name, telegraph_url, overview, tags_line, duration_sec, created_at_unix`.
  - `MorningDigestStore(db)`: `add(item) -> None`, `unsent() -> list[MorningDigestItem]`, `mark_sent(video_ids: list[str]) -> None`.
  - `build_rank_prompt(items, interests) -> str`; `parse_rank_response(raw: str, valid_ids: set[str]) -> dict[str, tuple[int, str]]`; `render_morning_digest(items: list[MorningDigestItem], ranks: dict[str, tuple[int, str]]) -> str` (HTML ≤ 4000).
  - `async maybe_send_morning_digest(services) -> bool` — отправляет, если есть неотправленные items и нет pending scheduled-задач.
  - `MonitoringRules.interests: list[str]` — новый опциональный список тем в `monitoring.yaml`.
  - Поведенческое изменение: scheduled-job'ы больше НЕ шлют отдельное сообщение с саммари; успешный результат уходит в `morning_digest_items`, а в чат — один дайджест после разбора всей пачки. Ошибки scheduled-job'ов по-прежнему шлются отдельными тихими сообщениями. Pinned-дайджест owner'а обновляется как раньше.

- [ ] **Step 1: Failing tests**

`tests/test_morning_digest.py`:
```python
from app.db import Database
from app.morning_digest import (
    MorningDigestItem,
    MorningDigestStore,
    build_rank_prompt,
    parse_rank_response,
    render_morning_digest,
)


def item(i, vid=None):
    return MorningDigestItem(
        video_id=vid or f"vid{i:08d}", title=f"Видео {i}", channel_name="Канал",
        telegraph_url=f"https://telegra.ph/v{i}", overview=f"Обзор {i}",
        tags_line="#тема", duration_sec=600, created_at_unix=1000 + i,
    )


def test_store_roundtrip(tmp_path):
    store = MorningDigestStore(Database(tmp_path / "bot.db"))
    store.add(item(1))
    store.add(item(2))
    assert [i.title for i in store.unsent()] == ["Видео 1", "Видео 2"]
    store.mark_sent([i.video_id for i in store.unsent()])
    assert store.unsent() == []


def test_parse_rank_response_valid():
    raw = '[{"video_id": "vid00000001", "score": 8, "reason": "по вашей теме"}, {"video_id": "unknown", "score": 5, "reason": "x"}]'
    ranks = parse_rank_response(raw, valid_ids={"vid00000001"})
    assert ranks == {"vid00000001": (8, "по вашей теме")}


def test_parse_rank_response_garbage_returns_empty():
    assert parse_rank_response("не json", valid_ids={"a"}) == {}
    assert parse_rank_response('{"not": "a list"}', valid_ids={"a"}) == {}


def test_parse_rank_response_clamps_score():
    raw = '[{"video_id": "a", "score": 99, "reason": "r"}, {"video_id": "b", "score": -3, "reason": "r"}]'
    ranks = parse_rank_response(raw, valid_ids={"a", "b"})
    assert ranks["a"][0] == 10 and ranks["b"][0] == 0


def test_render_sorted_and_fits():
    items = [item(1), item(2), item(3)]
    ranks = {items[0].video_id: (3, "так себе"), items[2].video_id: (9, "огонь")}
    html = render_morning_digest(items, ranks)
    assert len(html) <= 4000
    # Ранжированные выше, внутри — по убыванию score; без оценки — в конце.
    assert html.index("Видео 3") < html.index("Видео 1") < html.index("Видео 2")
    assert "огонь" in html and 'href="https://telegra.ph/v3"' in html


def test_build_rank_prompt_mentions_interests_and_items():
    prompt = build_rank_prompt([item(1)], interests=["инвестиции", "AI"])
    assert "инвестиции" in prompt and "vid00000001" in prompt and "Обзор 1" in prompt
```

Run — Expected: FAIL (`No module named 'app.morning_digest'`).

- [ ] **Step 2: `app/morning_digest.py`**

```python
"""Утренний дайджест мониторинга.

Scheduled-саммари не шлются отдельными сообщениями — складываются в таблицу
``morning_digest_items``. Когда пачка суточного скана дообработана (в очереди
не осталось scheduled-задач), бот один раз зовёт LLM отранжировать видео по
интересам пользователя (interests + whitelists из monitoring.yaml) и шлёт одно
сообщение со списком: score, ссылка на конспект, «почему стоит внимания».
Если LLM недоступна — фолбэк: неранжированный список, дайджест всё равно уходит.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from app.db import Database
from app.utils import escape_html


logger = logging.getLogger(__name__)

MAX_DIGEST_MESSAGE_CHARS = 4000

RANK_SYSTEM_PROMPT = (
    "Ты помогаешь отбирать YouTube-видео по интересам пользователя. "
    "Отвечай строго JSON-массивом без пояснений."
)

RANK_PROMPT_TEMPLATE = """
Интересы пользователя: {interests}

Ниже — новые видео за сутки (id, название, канал, краткий обзор, теги).
Оцени каждое по релевантности интересам от 0 до 10 и одним коротким
предложением объясни, почему видео стоит (или не стоит) внимания.

Видео:
{items_block}

Ответ — строго JSON-массив вида:
[{{"video_id": "...", "score": 0, "reason": "..."}}]
""".strip()


@dataclass(frozen=True)
class MorningDigestItem:
    video_id: str
    title: str
    channel_name: str
    telegraph_url: str
    overview: str
    tags_line: str
    duration_sec: float
    created_at_unix: float


class MorningDigestStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, item: MorningDigestItem) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO morning_digest_items"
            "(video_id, title, channel_name, telegraph_url, overview, tags_line, duration_sec, created_at_unix, sent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (item.video_id, item.title, item.channel_name, item.telegraph_url,
             item.overview, item.tags_line, item.duration_sec, item.created_at_unix),
        )

    def unsent(self) -> list[MorningDigestItem]:
        rows = self._db.query(
            "SELECT * FROM morning_digest_items WHERE sent = 0 ORDER BY created_at_unix"
        )
        return [
            MorningDigestItem(
                video_id=r["video_id"], title=r["title"], channel_name=r["channel_name"],
                telegraph_url=r["telegraph_url"], overview=r["overview"], tags_line=r["tags_line"],
                duration_sec=r["duration_sec"], created_at_unix=r["created_at_unix"],
            )
            for r in rows
        ]

    def mark_sent(self, video_ids: list[str]) -> None:
        self._db.executemany(
            "UPDATE morning_digest_items SET sent = 1 WHERE video_id = ?",
            [(vid,) for vid in video_ids],
        )


def build_rank_prompt(items: list[MorningDigestItem], interests: list[str]) -> str:
    interests_text = ", ".join(interests) if interests else "не заданы (оценивай общую содержательность)"
    lines = []
    for it in items:
        overview = it.overview[:600]
        lines.append(
            f"- id: {it.video_id}\n  название: {it.title}\n  канал: {it.channel_name}\n"
            f"  обзор: {overview}\n  теги: {it.tags_line or '—'}"
        )
    return RANK_PROMPT_TEMPLATE.format(interests=interests_text, items_block="\n".join(lines))


def parse_rank_response(raw: str, valid_ids: set[str]) -> dict[str, tuple[int, str]]:
    """Разобрать JSON-ответ ранжирования. Мусор → пустой dict (fallback-режим)."""
    cleaned = raw.strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    ranks: dict[str, tuple[int, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("video_id") or "")
        if vid not in valid_ids:
            continue
        try:
            score = int(entry.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))
        reason = str(entry.get("reason") or "").strip()
        ranks[vid] = (score, reason)
    return ranks


def render_morning_digest(
    items: list[MorningDigestItem], ranks: dict[str, tuple[int, str]]
) -> str:
    """HTML-сообщение дайджеста, ≤4000 символов.

    Порядок: сначала видео с оценкой (по убыванию score), затем без оценки
    (fallback, если LLM не отранжировала). Не влезающие в бюджет строки
    молча отбрасываются с конца — самое релевантное всегда наверху и внутри.
    """
    ranked = sorted(
        (it for it in items if it.video_id in ranks),
        key=lambda it: ranks[it.video_id][0],
        reverse=True,
    )
    unranked = [it for it in items if it.video_id not in ranks]
    ordered = [*ranked, *unranked]

    head = f"📬 <b>Дайджест мониторинга</b> — новых видео: {len(items)}"
    parts: list[str] = [head]
    used = len(head)
    for it in ordered:
        title = escape_html(it.title or it.video_id)
        url = escape_html(it.telegraph_url)
        channel = escape_html(it.channel_name or "")
        rank = ranks.get(it.video_id)
        if rank is not None:
            score, reason = rank
            line = f"\n\n<b>{score}/10</b> · <a href=\"{url}\">{title}</a>"
            if channel:
                line += f" · {channel}"
            if reason:
                line += f"\n<i>{escape_html(reason)}</i>"
        else:
            line = f"\n\n• <a href=\"{url}\">{title}</a>" + (f" · {channel}" if channel else "")
        if used + len(line) > MAX_DIGEST_MESSAGE_CHARS:
            break
        parts.append(line)
        used += len(line)
    return "".join(parts)


async def maybe_send_morning_digest(services) -> bool:
    """Отправить дайджест, если пачка scheduled-задач дообработана.

    Вызывается из queue-worker'а после каждой завершённой задачи и один раз
    на старте бота (на случай рестарта между «всё сгенерили» и «отправили»).
    """
    store = services.morning_digest
    if store is None or services.job_store is None:
        return False
    if services.job_store.scheduled_pending_count() > 0:
        return False
    items = store.unsent()
    if not items:
        return False
    target_chat_id = services.settings.monitoring_target_chat_id
    if target_chat_id is None or services.bot is None:
        return False

    interests: list[str] = []
    if services.monitoring is not None:
        rules = services.monitoring.rules
        interests = [*rules.interests, *rules.shows_whitelist, *rules.experts_whitelist]

    ranks: dict[str, tuple[int, str]] = {}
    try:
        raw = await services.llm.generate(
            build_rank_prompt(items, interests), system=RANK_SYSTEM_PROMPT
        )
        ranks = parse_rank_response(raw or "", {it.video_id for it in items})
        logger.info("morning_digest.ranked items=%s ranked=%s", len(items), len(ranks))
    except Exception:
        logger.exception("morning_digest.rank_failed — шлём без ранжирования")

    text = render_morning_digest(items, ranks)
    try:
        await services.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=True,
        )
    except Exception:
        logger.exception("morning_digest.send_failed items=%s", len(items))
        return False
    store.mark_sent([it.video_id for it in items])
    logger.info("morning_digest.sent items=%s chat_id=%s", len(items), target_chat_id)
    return True
```

- [ ] **Step 3: interests в monitoring_config.py**

`MonitoringRules`: добавить поле `interests: list[str] = field(default_factory=list)`. В `to_dict()`: `"interests": list(self.interests)`. В `_parse_rules`: `interests=_string_list(data.get("interests"))`. В `SEED_YAML` после блока experts добавить:
```yaml
# Темы, которые вам интересны — используются для ранжирования утреннего дайджеста.
interests: []
```

- [ ] **Step 4: Подавить per-video доставку для scheduled + запись items**

`services_container.py`: `Services` — добавить поле `morning_digest: "MorningDigestStore | None" = None`.

`pipeline.py`, `_process_youtube_job`, успешный путь: заменить безусловный вызов `_send_summary_delivery(...)` на:

```python
        if job.scheduled and services.morning_digest is not None:
            # Scheduled-саммари не шлём отдельным сообщением — оно уйдёт
            # одной строкой утреннего дайджеста после разбора всей пачки.
            services.morning_digest.add(MorningDigestItem(
                video_id=video_id,
                title=title,
                channel_name=getattr(metadata, "channel_name", "") or "",
                telegraph_url=telegraph_url or "",
                overview=summary.overview,
                tags_line=_format_tags_line(summary.tags),
                duration_sec=metadata.duration_sec or 0.0,
                created_at_unix=time.time(),
            ))
        else:
            summary_text = _format_telegram_summary(...)  # существующий код
            await _send_summary_delivery(...)             # существующий код
```
(`_format_telegram_summary`/`_send_summary_delivery` — существующие вызовы переносятся в else-ветку без изменений; импорт `MorningDigestItem` из `app.morning_digest`, `_format_tags_line` уже в delivery.)
Блоки «удалить сервисный статус», «pinned digest», «кэширование» — остаются ОБЩИМИ для обеих веток (после if/else).

Аналогично в `delivery.py`, `_deliver_cached_summary_for_job`: если `job.scheduled` и store доступен — `services.morning_digest.add(...)` из полей `cached` (`overview=cached.summary_overview`, `tags_line=_format_tags_line(cached.tags_obj())`, `duration_sec=0.0`) вместо `_send_summary_delivery`; pinned-digest блок остаётся.

Ошибочный путь (`except` в `_process_youtube_job`) НЕ трогаем — ошибки по-прежнему шлются отдельным сообщением.

- [ ] **Step 5: Триггер отправки в queue_service.py**

В `_summary_queue_worker`, после завершения обработки каждого job'а (в той же точке, где ставится финальный db-статус, после `"done"`/`"failed"`):

```python
            if job.scheduled:
                try:
                    await maybe_send_morning_digest(services)
                except Exception:
                    logger.exception("morning_digest.trigger_failed")
```

`main.py`: `services.morning_digest = MorningDigestStore(db)` (через конструктор Services); после `restore_pending_jobs(services)` добавить:

```python
    try:
        await maybe_send_morning_digest(services)
    except Exception:
        logger.exception("morning_digest.startup_check_failed")
```

- [ ] **Step 6: Тесты + smoke**

```bash
./.venv/bin/pytest tests/ -q && python3 -m compileall app/ -q
```
Expected: PASS. Ручной smoke: `docker compose up -d`, `/scan_now` от owner'а — по завершении скана в целевой чат приходит один дайджест вместо потока сообщений (при условии, что скан что-то нашёл).

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Morning ranked monitoring digest: batch scheduled summaries into one LLM-ranked message"
```

---

### Task 15: Документация, .gitignore, финальная верификация

**Files:**
- Modify: `README.md`, `.gitignore`, `.dockerignore`

- [ ] **Step 1: README**

Обновить: хранение — SQLite `data/bot.db` (JSON-файлы мигрируются автоматически, остаются как `*.migrated`-бэкапы); очередь переживает рестарт; мониторинг шлёт один утренний дайджест (описать `interests:` в monitoring.yaml); секция логов — добавить события `morning_digest.*`, `db.boot`, `queue.restored`; секция тестов:

````markdown
## Тесты

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
./.venv/bin/pytest tests/ -q
```
````

- [ ] **Step 2: .gitignore / .dockerignore**

Убедиться, что `data/` игнорируется (если нет — добавить `data/bot.db*`, `data/*.migrated`). В `.dockerignore` добавить `.venv/`, `tests/`, `docs/`.

- [ ] **Step 3: Финальная верификация**

```bash
./.venv/bin/pytest tests/ -q
docker compose build
docker compose up -d && sleep 15 && docker compose logs --tail=50 bot | grep -E "boot|migrated|restored|Traceback" ; docker compose down
```
Expected: тесты зелёные; в логах — `db.boot`, строки миграции (`users.migrated`, `summary_cache.migrated`, ...) при первом запуске, без Traceback.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "Update README for SQLite storage, persistent queue, morning digest and tests"
```
