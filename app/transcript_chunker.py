from __future__ import annotations

from app.models import TranscriptSegment
from app.utils import format_ts


def segments_to_text(segments: list[TranscriptSegment]) -> str:
    lines = []
    for segment in segments:
        text = " ".join(segment.text.split())
        if text:
            lines.append(f"[{format_ts(segment.start)}] {text}")
    return "\n".join(lines)


def chunk_transcript(transcript_text: str, max_chars: int = 16000) -> list[str]:
    lines = transcript_text.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks or [transcript_text]

