# Referral Share Step 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Кнопка «Поделиться» (только владельцу) формирует форвардабельное шер-сообщение с реф-ссылкой; `/start r<uid>_<vid>` привязывает нового пользователя к рефереру и сразу отдаёт саммари.

**Architecture:** Новая таблица `referrals` + тонкий store; шер-текст собирается в delivery.py из CachedSummary; ветка реф-payload в /start ПЕРЕД веткой голого video_id; выдача — существующими `_send_cached_summary_to_chat` / `_enqueue_summary_job`.

**Tech Stack:** Python 3.11, aiogram 3, SQLite (app/db.py), pytest asyncio_mode=auto.

**Спека:** `docs/superpowers/specs/2026-07-15-referral-share-step0-design.md`

## Global Constraints

- Реф-payload: `^r(\d{1,12})_([A-Za-z0-9_-]{11})$`; ссылка `https://t.me/{bot_username}?start=r{uid}_{video_id}`.
- Привязка: только когда `record_first_start(user_id, "referral")` вернул True и `referrer_id != user_id`; `INSERT OR IGNORE`.
- Кнопка «📤 Поделиться» и шер-сообщение — только владельцу (`services.users.is_owner`). Шер-текст — только ru.
- Тесты: `.venv/bin/python -m pytest`.

---

### Task 1: Таблица referrals + store

**Files:**
- Modify: `app/db.py` (в `_SCHEMA`)
- Create: `app/referrals_store.py`
- Modify: `app/services_container.py`, `app/main.py` (wiring)
- Test: `tests/test_referrals_store.py`

**Interfaces:**
- Produces: `ReferralsStore(db)`: `bind(user_id, referrer_id, video_id="") -> bool` (True = записали; self-ref и повторы → False), `referrer_of(user_id) -> int | None`. `Services.referrals: ReferralsStore | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
from app.db import Database
from app.referrals_store import ReferralsStore


def _store(tmp_path):
    return ReferralsStore(Database(tmp_path / "bot.db"))


def test_bind_first_touch_wins(tmp_path):
    store = _store(tmp_path)
    assert store.bind(user_id=10, referrer_id=1, video_id="abcABC12345") is True
    assert store.bind(user_id=10, referrer_id=2, video_id="otherVID123") is False
    assert store.referrer_of(10) == 1


def test_bind_rejects_self_referral(tmp_path):
    store = _store(tmp_path)
    assert store.bind(user_id=5, referrer_id=5) is False
    assert store.referrer_of(5) is None


def test_referrer_of_unknown_user(tmp_path):
    assert _store(tmp_path).referrer_of(404) is None
```

- [ ] **Step 2: Run to verify fail** — `.venv/bin/python -m pytest tests/test_referrals_store.py -q` → ImportError.

- [ ] **Step 3: Implement**

`app/db.py`, в `_SCHEMA` добавить:

```sql
CREATE TABLE IF NOT EXISTS referrals (
    user_id INTEGER PRIMARY KEY,
    referrer_id INTEGER NOT NULL,
    video_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
```

`app/referrals_store.py`:

```python
"""Привязки рефералов (MGM, ступень 0 — только учёт, без наград).

Модель атрибуции — first-touch навсегда: строка пишется один раз при первом
контакте пользователя с ботом, повторные переходы её не меняют
(см. спеку 2026-07-15-referral-share-step0-design.md).
"""
from __future__ import annotations

import time

from app.db import Database


class ReferralsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def bind(self, user_id: int, referrer_id: int, video_id: str = "") -> bool:
        """Привязать user_id к referrer_id. True — записали именно сейчас."""
        if user_id == referrer_id:
            return False
        cursor_rowcount = self._db.execute(
            "INSERT OR IGNORE INTO referrals(user_id, referrer_id, video_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, referrer_id, video_id, time.time()),
        )
        return self.referrer_of(user_id) == referrer_id and cursor_rowcount != 0

    def referrer_of(self, user_id: int) -> int | None:
        row = self._db.query_one(
            "SELECT referrer_id FROM referrals WHERE user_id = ?", (user_id,)
        )
        return int(row["referrer_id"]) if row else None
```

NB: проверить сигнатуру `Database.execute` — если она не возвращает rowcount,
возвращать `True`, когда до вставки `referrer_of` был `None`, а после — равен
`referrer_id` (сравнение до/после).

`app/services_container.py`: поле `referrals: ReferralsStore | None = None`
рядом с другими store'ами. `app/main.py`: создать и передать
(`referrals=ReferralsStore(db)` — по образцу соседних store'ов).

- [ ] **Step 4: Run** — тесты Task 1 зелёные.
- [ ] **Step 5: Commit** — `git commit -m "Add referrals store (first-touch binding)"`.

---

### Task 2: Шер-сообщение из CachedSummary

**Files:**
- Modify: `app/delivery.py`
- Test: `tests/test_referral_share.py` (новый)

**Interfaces:**
- Consumes: `CachedSummary` (title, channel_name, summary_overview, summary_chapters, summary_raw_text), `_estimate_reading_time_minutes`, `html.escape`-эквивалент, используемый в delivery.
- Produces: `build_share_message(cached: CachedSummary, bot_username: str, referrer_id: int) -> str` (HTML).

- [ ] **Step 1: Write the failing tests**

```python
from app.delivery import build_share_message
from app.summary_cache import CachedSummary


def _cached(**overrides):
    base = dict(
        video_id="abcABC12345",
        url="https://www.youtube.com/watch?v=abcABC12345",
        title="Заголовок <ролика>",
        channel_name="Канал & Ко",
        channel_url="",
        summary_overview=(
            "Первое предложение о главном. Второе предложение с деталями. "
            "Третье предложение. Четвёртое лишнее предложение."
        ),
        summary_key_points=[],
        summary_chapters=[{"start": "00:00", "title": "Глава", "notes": "Текст " * 200}],
        summary_raw_text="",
        telegraph_url="https://telegra.ph/x",
    )
    base.update(overrides)
    return CachedSummary(**base)


def test_share_message_has_ref_link_and_escaping():
    text = build_share_message(_cached(), bot_username="TestBot", referrer_id=42)
    assert "https://t.me/TestBot?start=r42_abcABC12345" in text
    assert "Заголовок &lt;ролика&gt;" in text
    assert "Канал &amp; Ко" in text


def test_share_message_trims_overview_to_sentences():
    text = build_share_message(_cached(), bot_username="TestBot", referrer_id=42)
    assert "Первое предложение о главном." in text
    assert "Четвёртое лишнее предложение." not in text
```

- [ ] **Step 2: Run to verify fail** — ImportError `build_share_message`.

- [ ] **Step 3: Implement**

В `app/delivery.py` (публичная функция, рядом с форматтерами; для reading
time переиспользовать `_estimate_reading_time_minutes` через восстановление
Summary из cached — в delivery уже есть аналогичный код, см.
`_format_cached_summary_text`; если он строит Summary — переиспользовать тот
же путь):

```python
_SHARE_OVERVIEW_MAX_CHARS = 300


def _first_sentences(text: str, max_chars: int) -> str:
    """Первые предложения текста до max_chars, обрезка по границе предложения."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for stop in (". ", "! ", "? "):
        idx = cut.rfind(stop)
        if idx > 40:
            return cut[: idx + 1].strip()
    return cut.rstrip() + "…"


def build_share_message(cached: CachedSummary, bot_username: str, referrer_id: int) -> str:
    """Форвардабельное шер-сообщение: польза чату + реф-ссылка текстом.

    Без inline-кнопок: Telegram срезает их при форварде, а ссылка текстом
    переживает и форвард, и копипаст (спека, ступень 0).
    """
    ref_link = f"https://t.me/{bot_username}?start=r{referrer_id}_{cached.video_id}"
    title = html_escape(cached.title)
    channel = html_escape(cached.channel_name or "")
    teaser = html_escape(_first_sentences(cached.summary_overview, _SHARE_OVERVIEW_MAX_CHARS))
    minutes = _estimate_reading_time_minutes(_summary_from_cached(cached))
    lines = [f"🎬 <b>{title}</b>"]
    if channel:
        lines.append(channel)
    lines.append("")
    lines.append(teaser)
    lines.append("")
    lines.append(f"⏱ Полное саммари — {minutes} мин чтения:")
    lines.append(f'🔮 <a href="{ref_link}">получить у бота</a>')
    return "\n".join(lines)
```

NB: `html_escape` / `_summary_from_cached` — использовать те же хелперы, что
`_format_cached_summary_text` (найти фактические имена в delivery.py; если
восстановления Summary нет — посчитать минуты по len(summary_raw_text или
overview+chapters notes) тем же правилом, что в `_estimate_reading_time_minutes`).

- [ ] **Step 4: Run** — тесты Task 2 зелёные.
- [ ] **Step 5: Commit** — `git commit -m "Build forwardable share message with referral link"`.

---

### Task 3: Кнопка «Поделиться» (owner-only) + callback

**Files:**
- Modify: `app/delivery.py` (`_build_summary_keyboard`)
- Modify: `app/bot_handlers.py` (callback `share:<video_id>`)
- Test: `tests/test_referral_share.py`

**Interfaces:**
- Consumes: `build_share_message` (Task 2), `services.summary_cache.get_any`, `services.users.is_owner`, `services.analytics.record`.
- Produces: ряд «📤 Поделиться» (`callback_data=f"share:{video_id}"`) в клавиатуре при `is_owner=True`.

- [ ] **Step 1: Write the failing test**

```python
from app.delivery import _build_summary_keyboard


def test_share_button_only_for_owner():
    owner_kb = _build_summary_keyboard(
        telegraph_url="https://telegra.ph/x", video_id="abcABC12345",
        is_owner=True, lang="ru",
    )
    texts = [b.callback_data or b.url for row in owner_kb.inline_keyboard for b in row]
    assert "share:abcABC12345" in texts

    user_kb = _build_summary_keyboard(
        telegraph_url="https://telegra.ph/x", video_id="abcABC12345",
        is_owner=False, lang="ru",
    )
    texts = [b.callback_data or b.url for row in user_kb.inline_keyboard for b in row]
    assert "share:abcABC12345" not in texts
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement**

В `_build_summary_keyboard`, после ряда с транскриптом (внутри `if video_id:`):

```python
        if is_owner:
            rows.append([
                InlineKeyboardButton(
                    text="📤 Поделиться",
                    callback_data=f"share:{video_id}",
                )
            ])
```

В `app/bot_handlers.py`, рядом с обработчиком `transcript:` (скопировать его
структуру доступа/ответов):

```python
    @router.callback_query(F.data.startswith("share:"))
    async def share_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else None
        if user_id is None or not services.users.is_owner(user_id):
            await callback.answer()
            return
        video_id = (callback.data or "").split(":", 1)[1]
        cached = services.summary_cache.get_any(video_id) if services.summary_cache else None
        if cached is None:
            await callback.answer(
                "Саммари уже не в кэше — перегенерируй ролик.", show_alert=True
            )
            return
        text = build_share_message(
            cached, bot_username=services.bot_username or "", referrer_id=user_id
        )
        if callback.message is not None:
            await callback.message.answer(
                text, parse_mode="HTML", disable_web_page_preview=True
            )
        if services.analytics is not None:
            services.analytics.record(user_id, "share_button", video_id)
        await callback.answer("Готово — форвардни сообщение в чат")
```

(`build_share_message` импортировать из `app.delivery` там же, где другие
импорты delivery в bot_handlers.)

- [ ] **Step 4: Run** — тесты зелёные; полный suite тоже.
- [ ] **Step 5: Commit** — `git commit -m "Owner-only share button producing forwardable message"`.

---

### Task 4: Ветка реф-ссылки в /start + питч

**Files:**
- Modify: `app/bot_handlers.py` (модульный regex + ветка в `start()`)
- Modify: `app/locales/ru.json` (ключ `ref.pitch`; en-аналог в en.json, если файл есть — t() падает в en-фолбэк)
- Test: `tests/test_referral_share.py`

**Interfaces:**
- Consumes: `ReferralsStore.bind`, `record_first_start`, `_send_cached_summary_to_chat`, `_enqueue_summary_job`.
- Produces: `_parse_ref_payload(payload: str) -> tuple[int, str] | None` (модульная, чистая).

- [ ] **Step 1: Write the failing tests**

```python
from app.bot_handlers import _parse_ref_payload


def test_parse_ref_payload_valid():
    assert _parse_ref_payload("r42_abcABC12345") == (42, "abcABC12345")


def test_parse_ref_payload_rejects_garbage():
    assert _parse_ref_payload("abcABC12345") is None          # голый video_id
    assert _parse_ref_payload("r42_short") is None            # короткий id
    assert _parse_ref_payload("rX_abcABC12345") is None       # uid не число
    assert _parse_ref_payload("r42-abcABC12345") is None      # нет разделителя _
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement**

`app/bot_handlers.py`, на уровне модуля рядом с `_YOUTUBE_VIDEO_ID_RE`:

```python
_REF_PAYLOAD_RE = re.compile(r"^r(\d{1,12})_([A-Za-z0-9_-]{11})$")


def _parse_ref_payload(payload: str) -> tuple[int, str] | None:
    match = _REF_PAYLOAD_RE.fullmatch(payload)
    if not match:
        return None
    return int(match.group(1)), match.group(2)
```

В `start()`, СРАЗУ ПОСЛЕ извлечения `payload` и ДО ветки
`_YOUTUBE_VIDEO_ID_RE.fullmatch(payload)`:

```python
        ref = _parse_ref_payload(payload) if payload else None
        if ref is not None:
            referrer_id, video_id = ref
            user_id = _message_user_id(message)
            logger.info(
                "ref.start chat_id=%s user_id=%s referrer_id=%s video_id=%s",
                message.chat.id, user_id, referrer_id, video_id,
            )
            if services.analytics is not None and user_id is not None:
                is_first = services.analytics.record_first_start(user_id, "referral")
                services.analytics.record(
                    user_id, "ref_start", f"{referrer_id}:{video_id}"
                )
                if is_first and services.referrals is not None:
                    services.referrals.bind(user_id, referrer_id, video_id)
            cached = (
                services.summary_cache.get(video_id, lang)
                or services.summary_cache.get_any(video_id)
            ) if services.summary_cache else None
            if cached is not None:
                await _send_cached_summary_to_chat(message, cached, services)
            else:
                url = f"https://www.youtube.com/watch?v={video_id}"
                await _enqueue_summary_job(message, url, services)
            await message.answer(
                t("ref.pitch", lang, weekly=services.settings.quota_free_weekly)
            )
            return
```

`app/locales/ru.json`:

```json
"ref.pitch": "Пришли мне ссылку на любое YouTube-видео — сделаю такое же саммари. Бесплатно: {weekly} в неделю."
```

(en.json — английский аналог: "Send me any YouTube link and I'll summarize it the same way. Free: {weekly}/week.")

NB: `_send_cached_summary_to_chat` и `_enqueue_summary_job` — проверить
фактические импорты/имена в bot_handlers (enqueue уже используется в этой же
функции; cached-отправка импортируется из delivery, где она определена).
Гейт `_has_access` в начале `start()` уже отсекает приватный режим — реф-ветка
его не обходит.

- [ ] **Step 4: Run** — тесты Task 4 и полный suite зелёные.
- [ ] **Step 5: Commit** — `git commit -m "Handle referral deep links in /start"`.

---

### Task 5: Выкат и живой smoke

- [ ] Проверить очередь: `sqlite3 data/bot.db "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','active');"` → 0.
- [ ] `docker compose up -d --build` → Started; в логах `billing.boot`, без Traceback.
- [ ] Живой smoke владельцем: открыть любое саммари в боте → «📤 Поделиться» → пришло шер-сообщение с корректной ссылкой; клик по ссылке самому себе → саммари приходит, `referrals` пуст (self-ref), в `analytics_events` есть `share_button` и `ref_start`.
- [ ] SQL-проверка: `sqlite3 data/bot.db "SELECT event, detail FROM analytics_events ORDER BY rowid DESC LIMIT 5"`.
