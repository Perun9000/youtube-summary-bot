from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class VideoChapter:
    start: float
    title: str


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    title: str
    channel_name: str
    channel_url: str
    duration_sec: float = 0.0
    description: str = ""
    chapters: tuple[VideoChapter, ...] = ()


@dataclass(frozen=True)
class ChannelInfo:
    channel_id: str
    channel_name: str
    channel_url: str


@dataclass(frozen=True)
class Chapter:
    start: str
    title: str
    notes: str


@dataclass(frozen=True)
class Summary:
    overview: str
    key_points: list[str]
    chapters: list[Chapter]
    raw_text: str


@dataclass(frozen=True)
class VideoContext:
    url: str
    video_id: str
    title: str
    transcript_text: str
    chunks: list[str]
    summary: Summary
    telegraph_url: str


@dataclass(frozen=True)
class VideoComment:
    text: str
    author: str
    like_count: int
    is_pinned: bool = False

