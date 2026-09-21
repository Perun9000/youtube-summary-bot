"""Справочный блок из описания ролика для промпта суммаризатора.

Зачем: в авторском описании имена, фамилии и термины написаны правильно —
модель сверяет по ним искажения Whisper-транскрипта; официальные главы
помогают разбивке. Описание — недоверенный текст (пишет автор ролика),
поэтому блок идёт в ПОЛЬЗОВАТЕЛЬСКИЙ промпт с явной рамкой «только справка,
не пересказывать», а спонсорский мусор вырезается построчно.
"""
from __future__ import annotations

import re

from app.models import VideoMetadata

DESCRIPTION_MAX_CHARS = 800
CHAPTERS_MAX = 20

# Строка выбрасывается целиком, если содержит ссылку или промо-маркер.
_URL_RE = re.compile(r"https?://|www\.|t\.me/|youtu\.be|bit\.ly", re.IGNORECASE)
_PROMO_RE = re.compile(
    r"спонсор|реклам|промокод|промо-код|скидк|донат|boosty|patreon|"
    r"подпишись|подписывайтесь|наш магазин|erid|18\+",
    re.IGNORECASE,
)
_HASHTAGS_ONLY_RE = re.compile(r"^[#\w\s]*#\w+[\s#\w]*$")


def clean_description(raw: str, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    """Выкинуть рекламу/ссылки/хэштеги, ужать до max_chars по границе строки."""
    kept: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if _URL_RE.search(line) or _PROMO_RE.search(line):
            continue
        if line.startswith("#") and _HASHTAGS_ONLY_RE.match(line):
            continue
        kept.append(line)
    text = "\n".join(kept)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Обрезка по последней целой строке, чтобы не рвать имя посередине.
    newline = cut.rfind("\n")
    if newline > 100:
        return cut[:newline].rstrip()
    return cut.rstrip()


def _format_chapters(metadata: VideoMetadata) -> str:
    lines = []
    for chapter in metadata.chapters[:CHAPTERS_MAX]:
        total = int(chapter.start)
        hours, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        stamp = (
            f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
        )
        title = " ".join(str(chapter.title).split()).strip()
        if title:
            lines.append(f"{stamp} {title}")
    return "\n".join(lines)


def build_description_block(metadata: VideoMetadata) -> str:
    """Готовый блок для {description_block} в промпте; "" — если нечего дать."""
    description = clean_description(metadata.description)
    chapters = _format_chapters(metadata)
    if not description and not chapters:
        return ""
    parts = [
        "Справочный контекст от автора ролика (НЕ пересказывать и не цитировать "
        "как содержание; использовать только для сверки написания имён, фамилий "
        "и терминов из транскрипта и для границ глав; рекламу игнорировать):"
    ]
    if description:
        parts.append(f"Описание:\n{description}")
    if chapters:
        parts.append(f"Официальные главы:\n{chapters}")
    return "\n".join(parts)
