"""Резолвит список YouTube-каналов в канонические channel_id и печатает YAML-блок.

Запуск из контейнера бота (yt-dlp уже установлен):

    docker compose exec bot python /app/scripts/resolve_channels.py

Либо локально, если yt-dlp установлен на хосте:

    python scripts/resolve_channels.py
"""
from __future__ import annotations

import sys
from typing import Any

try:
    from yt_dlp import YoutubeDL
except ImportError:  # pragma: no cover
    print("ERROR: yt-dlp не установлен. Запусти внутри контейнера бота.", file=sys.stderr)
    sys.exit(1)


CHANNEL_URLS: list[str] = [
    "https://www.youtube.com/@tvrain",
    "https://www.youtube.com/@StanislavKucher",
    "https://www.youtube.com/@feygin_zhivem",
    "https://www.youtube.com/@bbcrussian",
    "https://www.youtube.com/@TheBreakfastShowOfficial",
    "https://www.youtube.com/@PivovarovOfficial",
    "https://www.youtube.com/@BILD_RUSSIAN",
    "https://www.youtube.com/@HONEST_PERSON",
    "https://www.youtube.com/@BILDnaRusskom",
    "https://www.youtube.com/@nevzorovtv",
    "https://www.youtube.com/@itsgoodyou",
    "https://www.youtube.com/@kashin",
    "https://www.youtube.com/@PopularPolitics",
    "https://www.youtube.com/@latyninaTV",
    "https://www.youtube.com/@redaktsiya",
    "https://www.youtube.com/@MaximKats",
    "https://www.youtube.com/@FeyginLive",
    "https://www.youtube.com/@itpedia",
    "https://www.youtube.com/@varlamov",
    "https://www.youtube.com/@plushev",
    "https://www.youtube.com/@StanislavKrupin",
    "https://www.youtube.com/@vDud",
    "https://www.youtube.com/@HodorkovskyLive",
    "https://www.youtube.com/@ASolovyov",
    "https://www.youtube.com/@itonru",
    "https://www.youtube.com/@AlexNevzorov",
    "https://www.youtube.com/@MarkFeygin",
    "https://www.youtube.com/@Echo_Moscow",
    "https://www.youtube.com/@ekaterina_schulmann",
]


def resolve_one(ydl: YoutubeDL, url: str) -> dict[str, Any]:
    info = ydl.extract_info(url, download=False, process=False)
    channel_id = info.get("channel_id") or info.get("uploader_id") or info.get("id")
    title = info.get("channel") or info.get("uploader") or info.get("title") or ""
    return {"channel_id": channel_id, "title": title, "source_url": url}


def main() -> int:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": 1,
    }
    rows: list[dict[str, Any]] = []
    with YoutubeDL(opts) as ydl:
        for url in CHANNEL_URLS:
            try:
                rows.append(resolve_one(ydl, url))
            except Exception as exc:  # noqa: BLE001
                rows.append({"channel_id": None, "title": "", "source_url": url, "error": str(exc)[:200]})

    print("# === YAML-блок для data/monitoring.yaml ===")
    print("channels:")
    for row in rows:
        if row.get("channel_id") and row["channel_id"].startswith("UC"):
            print(f"  - id: {row['channel_id']}")
            print(f"    title: {row['title']!r}")
            print(f"    enabled: true")
            print(f"    # source: {row['source_url']}")
        else:
            print(f"  # FAILED: {row['source_url']} -> {row.get('error') or row.get('channel_id')!r}")

    print()
    print("# === Сырые результаты ===")
    for row in rows:
        print(row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
