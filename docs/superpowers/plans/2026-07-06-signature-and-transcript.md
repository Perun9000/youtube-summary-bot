# Signature & Transcript Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ненавязчивая подпись «сделано @бот» в конце каждого саммари (переживает копирование текста в комментарии) + кнопка «Транскрипт (md)» под саммари, скачивание — подписчикам и allowlist.

**Architecture:** Подпись — последний блок в `_format_telegram_summary` (username бота получается через `bot.get_me()` при старте и живёт в `Services.bot_username`); бюджет топ-комментария учитывает длину подписи. Транскрипты снова сохраняются на диск — теперь в markdown (`data/transcripts/{video_id}.md`, новый модуль `app/transcript_export.py`); кнопка в `_build_summary_keyboard` с callback `transcript:{video_id}`; хендлер гейтит по allowlist/подписке и шлёт файл документом.

**Tech Stack:** Python 3.11, aiogram 3 (FSInputFile, callback_query), pytest.

## Global Constraints

- Подпись: `<i>сделано @{username}</i>` — @-упоминание видимым текстом (авто-линкуется Telegram'ом и переживает копирование), НЕ `<a href>`-ссылка на слове. Если username недоступен (`bot_username is None`) — подписи нет, сообщение как раньше.
- Подпись присутствует во всех доставках саммари пользователю (свежая генерация и cache-hit); в утреннем дайджесте мониторинга — НЕ добавляется (owner-контекст).
- Скачивание транскрипта: доступно allowlist ИЛИ активному подписчику; бесплатным внешним — callback-alert с предложением /subscribe. Кнопка видна всем (это витрина фичи подписки).
- Транскрипт сохраняется при каждой успешной генерации (segment-mode сохраняет свой отфильтрованный фрагмент); ошибка сохранения не ломает job (warning). Для роликов, обработанных до фичи, файла нет — alert «транскрипт не сохранён».
- `/cache_drop` удаляет и md-файл транскрипта.
- Тексты русские; suite сейчас 86/86, после плана 91/91; вывод чистый.
- Текст кнопки — «📄 Транскрипт (md)»: решение владельца — 📄 здесь иконка типа файла; прежнее требование «убрать эмодзи из кнопок» относилось к двум существующим кнопкам и не распространяется на эту.
- Коммиты английские, в конце `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Подпись «сделано @бот»

**Files:**
- Modify: `app/services_container.py`, `app/main.py`, `app/delivery.py`, `app/pipeline.py`
- Test: `tests/test_signature.py`

**Interfaces:**
- Consumes: `_format_telegram_summary(title, video_url, summary, telegraph_url, channel_name, channel_url, scheduled=False, segment_spans=None, expert_matches=None, top_comment=None)` (app/delivery.py:35); `_format_cached_summary_text(cached, override_top_comments=...)` (вызывает первую); их call-sites: pipeline (свежая генерация), `_send_cached_summary_to_chat`, `_deliver_cached_summary_for_job` (все имеют `services`).
- Produces:
  - `Services.bot_username: str | None = None`.
  - `_format_telegram_summary(..., bot_username: str | None = None)` — при непустом username добавляет последний блок `<i>сделано @{username}</i>`; длина подписи вычитается из бюджета топ-комментария (подпись не должна обрезаться).
  - `_format_cached_summary_text(..., bot_username: str | None = None)` — прокидывает дальше.

- [ ] **Step 1: Failing tests**

`tests/test_signature.py`:

```python
from app.delivery import _format_telegram_summary
from app.models import Summary, VideoComment
from app.services_container import MAX_TELEGRAM_MESSAGE_CHARS


def make_summary(overview="Обзор."):
    return Summary(overview=overview, key_points=[], chapters=[], raw_text="{}")


def fmt(**kw):
    return _format_telegram_summary(
        title="Заголовок", video_url="https://youtu.be/x", summary=make_summary(),
        telegraph_url="https://telegra.ph/x", channel_name="Канал",
        channel_url="https://youtube.com/@c", **kw,
    )


def test_signature_present():
    out = fmt(bot_username="Test_Bot")
    assert out.rstrip().endswith("<i>сделано @Test_Bot</i>")


def test_no_signature_without_username():
    out = fmt()
    assert "сделано @" not in out


def test_signature_survives_long_comment():
    # Гигантский топ-комментарий не должен вытеснять подпись за лимит.
    comment = VideoComment(text="х" * 5000, author="Автор", like_count=10)
    out = _format_telegram_summary(
        title="Заголовок", video_url="https://youtu.be/x",
        summary=make_summary("о" * 1500),
        telegraph_url="https://telegra.ph/x", channel_name="Канал",
        channel_url="https://youtube.com/@c", top_comment=comment,
        bot_username="Test_Bot",
    )
    assert len(out) <= MAX_TELEGRAM_MESSAGE_CHARS
    assert out.rstrip().endswith("<i>сделано @Test_Bot</i>")
```

Run: `./.venv/bin/pytest tests/test_signature.py -q` — FAIL (`unexpected keyword argument 'bot_username'`).

- [ ] **Step 2: delivery.py**

В `_format_telegram_summary`: параметр `bot_username: str | None = None`. Перед блоком `if top_comment is not None:`:

```python
    # Подпись со ссылкой на бота. Именно @-упоминание видимым текстом (а не
    # <a href> на слове): при копировании текста сообщения в комментарии
    # Telegram href теряется, а @mention остаётся и авто-линкуется.
    signature_line = f"<i>сделано @{bot_username}</i>" if bot_username else ""
```

В блоке top_comment учесть подпись в бюджете (заменить вычисление `available_chars`):

```python
    if top_comment is not None:
        base_text = "\n\n".join(blocks)
        separator_len = 2 if base_text else 0
        signature_cost = (len(signature_line) + 2) if signature_line else 0
        available_chars = (
            MAX_TELEGRAM_MESSAGE_CHARS - len(base_text) - separator_len - signature_cost
        )
        top_comment_line = _format_top_comment_line(top_comment, available_chars)
        if top_comment_line:
            blocks.append(top_comment_line)

    if signature_line:
        blocks.append(signature_line)
```

В `_format_cached_summary_text`: параметр `bot_username: str | None = None`, прокинуть в `_format_telegram_summary(...)`. Оба вызова `_format_cached_summary_text(...)` (в `_send_cached_summary_to_chat` и `_deliver_cached_summary_for_job`) — добавить `bot_username=services.bot_username`.

- [ ] **Step 3: pipeline.py + wiring**

`app/pipeline.py`: в вызов `_format_telegram_summary(...)` (успешный путь `_process_youtube_job`, ветка manual/else) добавить `bot_username=services.bot_username`.
`app/services_container.py`: `Services` — поле `bot_username: str | None = None` (рядом с `bot`).
`app/main.py`: после `bot = Bot(token=...)`:

```python
    try:
        bot_username = (await bot.get_me()).username
        logger.info("bot.boot username=@%s", bot_username)
    except Exception:
        logger.exception("bot.get_me_failed — подпись в саммари будет отключена")
        bot_username = None
```
и `bot_username=bot_username` в `Services(...)`.

- [ ] **Step 4: Прогнать всё**

`./.venv/bin/pytest tests/ -q` — 89 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Bot signature line in summary messages: copy-surviving @mention"
```

---

### Task 2: Кнопка «Транскрипт (md)» для подписчиков и allowlist

**Files:**
- Create: `app/transcript_export.py`
- Modify: `app/pipeline.py` (сохранение), `app/delivery.py` (кнопка), `app/bot_handlers.py` (callback-хендлер, `_apply_cache_drop`), `README.md`
- Test: `tests/test_transcript_export.py`

**Interfaces:**
- Consumes: `segments_to_text`/`TranscriptSegment`, `format_ts` (app/utils.py), `services.users.is_allowed`, `services.billing.is_subscriber`, `_build_summary_keyboard(telegraph_url, video_id, is_owner)` (app/delivery.py:308).
- Produces:
  - `transcript_markdown(title: str, url: str, segments: list[TranscriptSegment]) -> str` — md-документ: `# {title}`, ссылка на ролик, затем строки `**[MM:SS]** текст` (пустые сегменты пропускаются).
  - `transcript_path(data_dir: Path, video_id: str) -> Path` → `data_dir / "transcripts" / f"{video_id}.md"`.
  - `save_transcript_markdown(data_dir: Path, video_id: str, title: str, url: str, segments) -> Path` — mkdir + запись.
  - Кнопка `«Транскрипт (md)»` (callback `transcript:{video_id}`) отдельным рядом в `_build_summary_keyboard`.

- [ ] **Step 1: Failing tests**

`tests/test_transcript_export.py`:

```python
from app.models import TranscriptSegment
from app.transcript_export import save_transcript_markdown, transcript_markdown, transcript_path


def seg(start, text):
    return TranscriptSegment(start=start, end=start + 5, text=text)


def test_markdown_format():
    md = transcript_markdown(
        "Название ролика", "https://youtu.be/x",
        [seg(0, "первая  строка"), seg(65, "вторая"), seg(70, "   ")],
    )
    lines = md.splitlines()
    assert lines[0] == "# Название ролика"
    assert "[Ролик](https://youtu.be/x)" in md
    assert "**[00:00]** первая строка" in md
    assert "**[01:05]** вторая" in md
    assert md.count("**[") == 2  # пустой сегмент пропущен


def test_save_and_path(tmp_path):
    path = save_transcript_markdown(
        tmp_path, "dQw4w9WgXcQ", "T", "https://youtu.be/dQw4w9WgXcQ", [seg(0, "текст")]
    )
    assert path == transcript_path(tmp_path, "dQw4w9WgXcQ")
    assert path.read_text(encoding="utf-8").startswith("# T")
```

Run — FAIL (`No module named 'app.transcript_export'`).

- [ ] **Step 2: app/transcript_export.py**

```python
"""Экспорт транскриптов в markdown для скачивания пользователем.

Файл пишется при каждой успешной генерации саммари (data/transcripts/{id}.md)
и отдаётся по кнопке «Транскрипт (md)» — доступ у allowlist и подписчиков.
Формат — под дальнейшую работу в заметках (Obsidian и т.п.).
"""
from __future__ import annotations

from pathlib import Path

from app.models import TranscriptSegment
from app.utils import format_ts

TRANSCRIPTS_SUBDIR = "transcripts"


def transcript_path(data_dir: Path, video_id: str) -> Path:
    return data_dir / TRANSCRIPTS_SUBDIR / f"{video_id}.md"


def transcript_markdown(title: str, url: str, segments: list[TranscriptSegment]) -> str:
    lines = [f"# {title}", "", f"[Ролик]({url})", ""]
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        lines.append(f"**[{format_ts(segment.start)}]** {text}")
    return "\n".join(lines) + "\n"


def save_transcript_markdown(
    data_dir: Path, video_id: str, title: str, url: str, segments: list[TranscriptSegment]
) -> Path:
    path = transcript_path(data_dir, video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript_markdown(title, url, segments), encoding="utf-8")
    return path
```

- [ ] **Step 3: Сохранение в pipeline**

В `_process_youtube_job`, сразу после строки `transcript_text = segments_to_text(segments)`:

```python
        # Сохраняем транскрипт в markdown — его можно скачать кнопкой под
        # саммари (allowlist и подписчики). Ошибка записи не ломает job.
        try:
            saved = await asyncio.to_thread(
                save_transcript_markdown,
                services.settings.bot_data_dir, video_id, title, url, segments,
            )
            logger.info("job.transcript_md.saved job_id=%s path=%s", job_id, saved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job.transcript_md.save_failed job_id=%s error=%s", job_id, exc)
```
(+ импорт `from app.transcript_export import save_transcript_markdown`).

- [ ] **Step 4: Кнопка в клавиатуре**

`app/delivery.py`, `_build_summary_keyboard` — после сборки `row` заменить финал:

```python
    rows: list[list[InlineKeyboardButton]] = []
    if row:
        rows.append(row)
    if video_id:
        rows.append([
            InlineKeyboardButton(
                text="📄 Транскрипт (md)",
                callback_data=f"transcript:{video_id}",
            )
        ])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)
```
(докстроку функции дополнить третьей кнопкой; про эмодзи 📄 см. Global Constraints).

- [ ] **Step 5: Callback-хендлер**

`app/bot_handlers.py`, внутри `build_router` (рядом с `download_audio_callback`):

```python
    @router.callback_query(F.data.startswith("transcript:"))
    async def transcript_callback(callback: CallbackQuery) -> None:
        video_id = (callback.data or "").split(":", 1)[1]
        user_id = callback.from_user.id if callback.from_user else None
        allowed = user_id is not None and (
            services.users.is_allowed(user_id)
            or (services.billing is not None and services.billing.is_subscriber(user_id))
        )
        if not allowed:
            await callback.answer(
                "Скачивание транскрипта доступно по подписке — /subscribe",
                show_alert=True,
            )
            return
        path = transcript_path(services.settings.bot_data_dir, video_id)
        if not path.exists():
            await callback.answer(
                "Транскрипт не сохранён для этого ролика (обработан до появления функции).",
                show_alert=True,
            )
            return
        await callback.answer()
        if callback.message is not None:
            await services.bot.send_document(
                chat_id=callback.message.chat.id,
                document=FSInputFile(path, filename=f"{video_id}.md"),
                disable_notification=True,
            )
```
Импорты: `from app.transcript_export import transcript_path`, `FSInputFile` из aiogram.types (проверить наличие).

- [ ] **Step 6: cache_drop чистит md**

В `_apply_cache_drop` после успешного `services.summary_cache.delete(video_id)`:

```python
        transcript_path(services.settings.bot_data_dir, video_id).unlink(missing_ok=True)
```

- [ ] **Step 7: README + прогнать всё**

README, раздел «Монетизация»: пункт про кнопку «Транскрипт (md)» (allowlist+подписчики; файлы в `data/transcripts/*.md`; ролики до фичи — без файла).

`./.venv/bin/pytest tests/ -q` — 91 passed. `python3 -m compileall app/ -q`.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "Transcript (md) download button for subscribers and allowlist"
```
