"""Fallback-резолвер для каналов, которые не далась yt-dlp.

Делает HTTP GET на https://www.youtube.com/@handle, выдёргивает channelId
и og:title из HTML регулярками. Зависимостей нет — только stdlib.

Запуск:

    docker run --rm -v "$PWD/scripts:/scripts" python:3.11-slim \
        python /scripts/resolve_channels_fallback.py

Или прямо на хосте:

    python3 scripts/resolve_channels_fallback.py
"""
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


FAILED_URLS: list[str] = [
    "https://www.youtube.com/@StanislavKucher",
    "https://www.youtube.com/@feygin_zhivem",
    "https://www.youtube.com/@bbcrussian",
    "https://www.youtube.com/@TheBreakfastShowOfficial",
    "https://www.youtube.com/@PivovarovOfficial",
    "https://www.youtube.com/@BILD_RUSSIAN",
    "https://www.youtube.com/@BILDnaRusskom",
    "https://www.youtube.com/@itsgoodyou",
    "https://www.youtube.com/@redaktsiya",
    "https://www.youtube.com/@plushev",
    "https://www.youtube.com/@HodorkovskyLive",
    "https://www.youtube.com/@ASolovyov",
    "https://www.youtube.com/@itonru",
    "https://www.youtube.com/@MarkFeygin",
    "https://www.youtube.com/@Echo_Moscow",
]

# Каналы, у которых yt-dlp выдал кривой title — перепроверим заодно.
RECHECK_TITLES_URLS: list[str] = [
    "https://www.youtube.com/@kashin",
    "https://www.youtube.com/@MaximKats",
    "https://www.youtube.com/@itpedia",
]

CHANNEL_ID_RE = re.compile(r'"(?:browseId|externalChannelId|channelId)":"(UC[\w-]{22})"')
OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"')
HTML_TITLE_RE = re.compile(r"<title>([^<]+?)\s*-\s*YouTube</title>")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
    # bypass EU consent gate, без него на /@handle прилетает редирект на consent.youtube.com
    "Cookie": "CONSENT=YES+cb.20210720-07-p0.en+FX+999",
}


def fetch_html(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def resolve_one(url: str) -> dict:
    log(f"  → fetching {url}")
    try:
        html = fetch_html(url)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        log(f"      FAIL: {exc}")
        return {"channel_id": None, "title": "", "source_url": url, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        log(f"      FAIL ({type(exc).__name__}): {exc}")
        return {"channel_id": None, "title": "", "source_url": url, "error": str(exc)[:200]}
    cid_match = CHANNEL_ID_RE.search(html)
    title_match = OG_TITLE_RE.search(html) or HTML_TITLE_RE.search(html)
    cid = cid_match.group(1) if cid_match else None
    title = title_match.group(1).strip() if title_match else ""
    log(f"      ok: {cid} {title!r}")
    return {"channel_id": cid, "title": title, "source_url": url}


def main() -> int:
    rows: list[dict] = []
    total = len(FAILED_URLS) + len(RECHECK_TITLES_URLS)
    log(f"Resolving {total} channels via HTML scrape (this may take ~{total*5}–{total*15}s)...")

    log(f"\n[Phase 1/2] {len(FAILED_URLS)} failed-from-yt-dlp URLs:")
    for i, url in enumerate(FAILED_URLS, 1):
        log(f"[{i}/{len(FAILED_URLS)}]")
        rows.append(resolve_one(url))

    log(f"\n[Phase 2/2] {len(RECHECK_TITLES_URLS)} title rechecks:")
    for i, url in enumerate(RECHECK_TITLES_URLS, 1):
        log(f"[{i}/{len(RECHECK_TITLES_URLS)}]")
        rows.append(resolve_one(url))

    log("\nDone. Writing YAML block to stdout.\n")
    print("# === Фолбэк: ранее упавшие каналы + перепроверка title'ов ===", flush=True)
    print("# === YAML-блок (вставить в data/monitoring.yaml под channels:) ===", flush=True)
    for row in rows:
        if row.get("channel_id") and row["channel_id"].startswith("UC"):
            print(f"  - channel_id: {row['channel_id']}", flush=True)
            print(f"    channel_url: {row['source_url']}", flush=True)
            print(f"    channel_name: {row['title']!r}", flush=True)
        else:
            print(f"  # FAILED: {row['source_url']} -> {row.get('error') or 'no channelId in HTML'}", flush=True)
    print(flush=True)
    print("# === Сырые результаты ===", flush=True)
    for row in rows:
        print(row, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
