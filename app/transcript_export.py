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
