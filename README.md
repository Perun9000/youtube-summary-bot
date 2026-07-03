# YouTube Summary Telegram Bot

Локальный Telegram-бот: принимает ссылку на YouTube, получает transcript или делает локальную транскрибацию аудио, генерирует summary через локальную LLM в LM Studio, отправляет краткую версию в Telegram и публикует полный конспект в Telegra.ph.

## Что уже настроено

- Telegram long polling через `aiogram`.
- Доступ ограничен allow-list'ом: `OWNER_USER_ID` для владельца, `ALLOWED_USER_IDS` как seed при первом запуске, дальше `/data/users.json`.
- Summary только локально через LM Studio.
- Транскрибация роликов без субтитров локально через `faster-whisper`.
- `yt-dlp` используется как fallback для скачивания аудио и может использовать YouTube cookies.
- SQLite и OpenAI API не используются.

## Первый запуск с LM Studio на хосте

Это основной режим: бот работает в Docker, а LM Studio запускается обычным локальным приложением на macOS.

1. Открой LM Studio.

2. В LM Studio открой `Developer` и включи `Start server`.

3. Загрузи любую подходящую chat/instruct модель. Бот берёт первую loaded-модель, если в `.env` стоит `LMSTUDIO_MODEL=auto`.

4. Подними бота:

```bash
docker compose up --build
```

5. Отправь боту `/start`, затем YouTube-ссылку.

Проверить, какие модели видит бот:

```text
/models
```

Если хочешь закрепить конкретную модель, укажи её id в `.env`:

```dotenv
LMSTUDIO_MODEL=publisher/model-id
```

Если хочешь, чтобы бот сам пытался загрузить указанную модель через LM Studio REST API:

```dotenv
LMSTUDIO_AUTO_LOAD=true
```

По умолчанию `LMSTUDIO_AUTO_LOAD=false`, чтобы случайно не загрузить тяжёлую модель в память.

Для моделей, загруженных с маленьким контекстом, например `4096`, бот режет transcript на небольшие части:

```dotenv
TRANSCRIPT_CHUNK_MAX_CHARS=3000
LLM_MAX_TOKENS=1200
```

Для Qwen3 8B с context length `16384` текущий профиль такой:

```dotenv
LMSTUDIO_NUM_CTX=16384
TRANSCRIPT_CHUNK_MAX_CHARS=10000
LLM_MAX_TOKENS=4000
```

`LMSTUDIO_NUM_CTX` используется при автозагрузке модели через LM Studio API. Если `LMSTUDIO_AUTO_LOAD=false`, context length нужно выставить в LM Studio UI перед загрузкой модели. Размер чанка выбирается с запасом: transcript занимает только часть окна, потому что в контекст ещё попадают system prompt, инструкция и будущий ответ модели.

## Cookies для закрытых или age-restricted роликов

Если ролик требует авторизации, экспортируй cookies из браузера аккаунта, который имеет доступ к ролику, и положи файл сюда:

```text
data/youtube.cookies.txt
```

Файл не коммитится в git.

## Команды

Обычным пользователям доступны только:

- `/start` - краткая инструкция.
- `/help` - что умеет бот.

Владельцу дополнительно доступны:

- `/users` - список пользователей с доступом.
- `/user_add 123456789 Имя` - добавить пользователя.
- `/user_remove 123456789` - удалить пользователя.
- `/models` - показать модели, которые видит локальный LLM-сервер.
- `/model` - показать модель, которую бот использует для summary.
- `/queue` - показать текущую очередь генерации summary.
- `/stop` или `stop` - остановить текущую генерацию summary и очистить очередь.

Если прислать несколько YouTube-ссылок подряд, бот поставит их в общую очередь и будет генерировать summary по одному ролику за раз, в порядке получения ссылок. Служебное сообщение текущей генерации обновляется и при добавлении новых ссылок переносится в конец чата. Если в очереди несколько ссылок, в служебном сообщении появляется строка вида `очередь: 1/3`.

Если генерация конкретного ролика завершилась ошибкой, бот пришлёт отдельное сообщение с гиперссылкой на видео и причиной ошибки, а очередь перейдёт к следующей ссылке.

## Логи

Смотреть текущий прогон:

```bash
docker compose logs -f bot
```

Последние строки:

```bash
docker compose logs --tail=200 bot
```

Быстро посмотреть последние ошибки сразу из файла и из `docker logs`:

```bash
./scripts/log-errors.sh
```

Можно передать число строк, например:

```bash
./scripts/log-errors.sh 300
```

Файл-лог бота сохраняется в:

```text
data/logs/bot.log
```

Этот файл ротируется раз в 7 дней. Старые ротации тоже сохраняются, чтобы можно было посмотреть историю. Отдельно Docker-логи контейнера ограничены по размеру и количеству файлов через `docker-compose`, чтобы stdout/stderr тоже не разрастались бесконечно.

В логах есть `job_id`, по которому удобно отслеживать один ролик от старта до конца:

```text
job.start
queue.job.enqueued / queue.job.start / queue.job.done / queue.job.failed / queue.job.cancelled
job.metadata.done
job.transcript.done source=youtube
job.transcript.unavailable fallback=audio
job.audio_download.done
whisper.transcribe.done
job.chunking.done
summary.chunk.start / summary.chunk.done
summary.synthesis.start
summary.done
telegraph.publish.done
job.done
job.failed
```

Если LM Studio не вернула ответ на один LLM-вызов за 20 минут, бот делает один retry этого же вызова после короткой паузы. В логах это видно как:

```text
llm.generate.timeout_retry
llm.generate.retry_success
```

## Ограничения MVP

- Первый запуск Whisper и локальной LLM может быть медленным из-за загрузки моделей.
- Telegra.ph-токен можно оставить пустым: бот создаст временный аккаунт при запуске.
