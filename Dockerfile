FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# JS-движок для yt-dlp: YouTube отдаёт подписные JS-челленджи, без их решения
# скачивание медиа получает HTTP 403 (yt-dlp EJS, деприкация extraction без
# JS runtime — инцидент 2026-08-19). deno — единственный runtime, включённый
# в yt-dlp по умолчанию.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Сами решатели JS-челленджей (пакет yt-dlp-ejs) ставятся ниже, вместе с
# requirements.txt, через extra "yt-dlp[default]" — обычная зависимость с
# PyPI, кэшируется как pip-слой. Никакого рантайм-фетча с github не
# требуется (--remote-components не нужен): yt-dlp сам находит установленный
# пакет yt-dlp-ejs. Слой инвалидируется, только когда меняется
# requirements.txt — решатель не перекачивается на каждый билд.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

CMD ["python", "-m", "app.main"]

