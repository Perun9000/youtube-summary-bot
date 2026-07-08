"""Экспорт транскриптов в markdown для скачивания пользователем.

Файл пишется при каждой успешной генерации саммари (data/transcripts/{id}.md)
и отдаётся по кнопке «Транскрипт (md)» — доступ у allowlist и подписчиков.
Формат — под дальнейшую работу в заметках (Obsidian и т.п.).
"""
from __future__ import annotations

import re
from pathlib import Path

from app.models import TranscriptSegment
from app.utils import format_ts

TRANSCRIPTS_SUBDIR = "transcripts"

# Символы, запрещённые (или проблемные) в именах файлов на большинстве ФС —
# заменяются пробелом вместе с переводами строк.
_FORBIDDEN_FILENAME_CHARS_RE = re.compile(r'[/\\:*?"<>|\r\n]+')
_MULTI_SPACE_RE = re.compile(r" {2,}")
_MULTI_UNDERSCORE_RE = re.compile(r"_{2,}")
# Не обрезать по границе слова, если это оставит короче этого числа символов —
# тогда переходим к жёсткой обрезке (может разорвать слово посередине).
_MIN_WORD_BOUNDARY_LEN = 16


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


def _sanitize_filename_part(value: str) -> str:
    """Убрать запрещённые в именах файлов символы и схлопнуть пробелы."""
    cleaned = _FORBIDDEN_FILENAME_CHARS_RE.sub(" ", value)
    cleaned = _MULTI_UNDERSCORE_RE.sub("_", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip(" _")


def _truncate_by_words(stem: str, max_len: int) -> str:
    """Обрезать строку до ``max_len`` символов по границе целых слов.

    Если обрезка по границе слова оставила бы меньше
    ``_MIN_WORD_BOUNDARY_LEN`` символов (например, самое первое слово уже
    длиннее лимита), делаем жёсткую обрезку ровно до ``max_len`` — это
    единственный случай, когда слово может быть разорвано посередине.
    """
    if len(stem) <= max_len:
        return stem
    cut = stem[:max_len]
    boundary = cut.rfind(" ")
    if boundary >= _MIN_WORD_BOUNDARY_LEN:
        return cut[:boundary].rstrip()
    return cut.rstrip()


def pretty_transcript_filename(
    channel_name: str, title: str, video_id: str, max_len: int = 64
) -> str:
    """Человекочитаемое имя файла для отдачи транскрипта в Telegram.

    Формат ``{канал}_{название}.md``; дисковый путь (``transcript_path``)
    при этом не меняется — красивое имя используется только в
    ``FSInputFile(..., filename=...)``. Если после санитизации канал и
    название оба пусты — откатываемся на ``{video_id}.md``.
    """
    channel = _sanitize_filename_part(channel_name or "")
    title_s = _sanitize_filename_part(title or "")
    parts = [p for p in (channel, title_s) if p]
    if not parts:
        return f"{video_id}.md"
    stem = "_".join(parts)
    truncated = _truncate_by_words(stem, max_len)
    if not truncated:
        return f"{video_id}.md"
    return f"{truncated}.md"


def save_transcript_markdown(
    data_dir: Path, video_id: str, title: str, url: str, segments: list[TranscriptSegment]
) -> Path:
    path = transcript_path(data_dir, video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript_markdown(title, url, segments), encoding="utf-8")
    return path
