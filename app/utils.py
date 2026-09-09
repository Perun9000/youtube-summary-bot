from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def extract_first_url(text: str) -> str | None:
    """Return the first http(s) URL found in *text*, stripped of trailing
    punctuation. Domain/origin is **not** validated — caller decides what to
    do with it. Returns ``None`` if no URL is present.
    """
    match = re.search(r"https?://[^\s<>]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,;)")


# Безсхемные YouTube-ссылки: Telegram авто-линкует голые домены, и пользователи
# регулярно шлют «youtube.com/shorts/…» без https:// (кейс 2026-09-09) —
# extract_first_url их не видит (требует схему). Матчим ТОЛЬКО известные
# YouTube-хосты, чтобы произвольный текст с точками не превращался в URL.
_BARE_YOUTUBE_URL_RE = re.compile(
    r"(?<![\w/.])((?:www\.|m\.)?(?:youtube\.com|youtu\.be)/[^\s<>]+)",
    re.IGNORECASE,
)


def extract_youtube_url(text: str) -> str | None:
    url = extract_first_url(text)
    if url:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host in YOUTUBE_HOSTS or host.endswith(".youtube.com"):
            return url
        return None

    bare = _BARE_YOUTUBE_URL_RE.search(text)
    if bare:
        return "https://" + bare.group(1).rstrip(".,;)")
    return None


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host == "youtu.be":
        return parsed.path.strip("/").split("/")[0]

    if parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if video_id:
            return video_id

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1]

    raise ValueError("Не удалось определить video_id из ссылки")


def classify_youtube_url(url: str) -> str:
    """Returns 'video', 'channel', or 'unknown'."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "unknown"

    host = parsed.netloc.lower()
    if host not in YOUTUBE_HOSTS and not host.endswith(".youtube.com"):
        return "unknown"

    try:
        extract_video_id(url)
        return "video"
    except ValueError:
        pass

    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return "unknown"

    first = parts[0]
    if first.startswith("@"):
        return "channel"
    if first in {"channel", "c", "user"}:
        return "channel"
    return "unknown"


def format_ts(seconds: float) -> str:
    seconds_int = max(0, int(seconds))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def escape_html(value: str) -> str:
    return html.escape(value, quote=False)

