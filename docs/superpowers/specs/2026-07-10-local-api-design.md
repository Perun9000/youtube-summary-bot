# Local API: тихая генерация из расширения — дизайн

**Дата:** 2026-07-10
**Статус:** утверждён владельцем (вариант A: локальный HTTP-endpoint + fallback на deep-link)

## Проблема

Кнопка расширения в UI YouTube открывает `https://t.me/<bot>?start=<video_id>`: браузер показывает страницу-прослойку t.me, затем открывается десктопный Telegram и забирает фокус. Владелец хочет: клик → ролик ушёл на генерацию, фокус остаётся на YouTube.

## Ограничения контекста

- Бот работает в Docker на том же маке, где стоит расширение (единственный пользователь фичи — владелец, подтверждено).
- Контейнер сейчас не публикует ни одного порта (aiogram polling).
- Постановка в очередь при deep-link идёт через `/start <video_id>` → `_enqueue_summary_job` (кэш-фастпас → квота-гейт → enqueue). У владельца квот нет (`quota_user_id=None`), язык — manual `ru` из `user_langs`.

## Решение

### 1. Серверная часть — `app/local_api.py`

- aiohttp-сервер (aiohttp уже в зависимостях aiogram) слушает `0.0.0.0:8799` внутри контейнера; в `docker-compose.yml` порт публикуется ТОЛЬКО на loopback: `"127.0.0.1:8799:8799"`. Снаружи мака порт недоступен.
- Новые поля `.env` / `Settings`: `LOCAL_API_TOKEN` (строка; пустая/отсутствует → сервер НЕ стартует, фича выключена), `LOCAL_API_PORT` (int, дефолт 8799).
- Один маршрут `POST /enqueue`:
  - Заголовок `X-Auth-Token` должен совпасть с `LOCAL_API_TOKEN`, иначе `401 {"error":"unauthorized"}`.
  - Тело: JSON `{"video_id": "<11 chars>"}`. Валидация `YOUTUBE_VIDEO_ID_RE.fullmatch`, иначе `400 {"error":"bad_video_id"}`.
  - Успех: постановка аналогично deep-link `/start`, но БЕЗ aiogram `Message` (его в HTTP-контексте нет) — по образцу восстановленных задач (`restore_pending_jobs`, `message=None`): `SummaryJob(message=None, chat_id=OWNER_USER_ID, lang=<резолв языка владельца через user_langs>, quota_user_id=None)` + персист через `job_store.add` + put в `summary_queue` под локом. Статусные сообщения pipeline умеет слать по голому `chat_id` (как для restored). Кэш-фастпас: если саммари в кэше — доставить из кэша тем же способом, что для restored/scheduled cache-hit, ответ `200 {"status":"cached"}`; иначе `200 {"status":"queued"}`.
  - Внутренняя ошибка → `500 {"error":"internal"}` (+ лог с полным traceback).
- CORS: preflight `OPTIONS` и `Access-Control-Allow-Origin` только для `https://www.youtube.com` (+ `Access-Control-Allow-Headers: Content-Type, X-Auth-Token`). Другие origins не получают CORS-заголовков.
- Жизненный цикл: старт/стоп рядом с polling в `app/main.py` (`aiohttp.web.AppRunner`), graceful shutdown. Лог `local_api.boot port=... enabled=...`.
- Deep-link-путь `/start` в боте НЕ меняется (используется всеми остальными и как fallback).

### 2. Расширение (browser-extension/)

- Options-страница: новое поле «Local API token» (пустое по умолчанию), хранится в `storage.sync` (dual-form, как остальные настройки). Пустой токен → расширение ведёт себя как раньше (только deep-link), никаких fetch.
- По клику кнопки (оба места: страница видео и превью-карточки):
  1. Если токен задан — fetch к `http://127.0.0.1:8799/enqueue` выполняет **background service worker** (content-script шлёт ему runtime-сообщение): Chrome блокирует запросы публичных страниц к loopback (Private Network Access), а background-контекст с `host_permissions` от этого свободен. Таймаут 1500 мс.
  2. `200` → кнопка на ~2 сек показывает «✅» (queued) и возвращается в исходный вид; фокус остаётся на YouTube.
  3. Не-200 / сетевые ошибки / таймаут → fallback: открыть старый `https://t.me/...?start=<id>` deep-link (текущее поведение).
- `manifest.json`: `host_permissions` += `"http://127.0.0.1:8799/*"`; bump версии (0.2.5). После выката владелец перезагружает расширение.

### 3. Безопасность

- Порт слушает только 127.0.0.1 хоста — сетевой доступ извне исключён.
- Токен обязателен; без него сервер не поднимается вовсе.
- CORS-ответы только для origin youtube.com — произвольный сайт в браузере не сможет прочитать ответ; сам запрос без валидного токена отклоняется.
- Токен в `.env` (не коммитится) и в `storage.sync` расширения; в репо не попадает.

### 4. Тестирование

- Юнит-тесты aiohttp-хендлера (aiohttp test utils): 401 без/с неверным токеном; 400 на битый video_id; 200 queued (enqueue-вызов с правильными chat_id/lang); 200 cached (кэш-ветка, enqueue не зовётся); выключенная фича (пустой токен → сервер не создаётся).
- Расширение — ручной чеклист: успех (✅, фокус на YouTube, саммари пришло в TG), бот остановлен → deep-link fallback, неверный токен в options → fallback.

### Вне скоупа

- Прогресс генерации на странице YouTube (владелец выбрал простую галочку).
- Публичный endpoint / другие устройства и пользователи.
- Изменение поведения для остальных пользователей расширения (без токена всё как раньше).
