from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from app.monitoring_config import MonitoredChannel, MonitoringConfig, MonitoringRules
from app.monitoring_rss import FeedEntry, fetch_channel_feed
from app.monitoring_state import MonitoringState
from app.models import TranscriptSegment, VideoChapter, VideoMetadata
from app.youtube_service import TranscriptUnavailable, YouTubeService


logger = logging.getLogger(__name__)


@dataclass
class ScheduledCandidate:
    """Result of the filter pipeline for one RSS entry that should be summarized."""

    feed_entry: FeedEntry
    metadata: VideoMetadata
    transcript_segments: list[TranscriptSegment]  # empty if captions unavailable
    transcript_source: str                         # "youtube" | "none"
    segment_spans: list[tuple[float, float]] = field(default_factory=list)
    expert_matches: list[str] = field(default_factory=list)
    show_matches: list[str] = field(default_factory=list)


EnqueueCallback = Callable[[ScheduledCandidate, MonitoredChannel], Awaitable[None]]


@dataclass
class ScanProgress:
    """Snapshot of run_scan() progress, emitted before each channel and once on completion."""

    channels_total: int
    channels_done: int                   # how many channels finished by the time of this snapshot
    current_channel: MonitoredChannel | None  # None on the final "done" snapshot
    enqueued_total: int


ProgressCallback = Callable[[ScanProgress], Awaitable[None]]


class MonitoringService:
    def __init__(
        self,
        *,
        config: MonitoringConfig,
        state: MonitoringState,
        youtube: YouTubeService,
        enqueue: EnqueueCallback,
    ) -> None:
        self._config = config
        self._state = state
        self._youtube = youtube
        self._enqueue = enqueue
        self._scan_lock = asyncio.Lock()

    @property
    def config(self) -> MonitoringConfig:
        return self._config

    @property
    def rules(self) -> MonitoringRules:
        return self._config.rules

    async def add_channel_by_url(self, url: str) -> tuple[MonitoredChannel, bool]:
        """Resolve a channel URL and add it to monitoring config.

        Returns (channel, added) — added=False if channel was already there.
        Also seeds the state with current RSS entries so the first scan doesn't
        drown the user in back-catalogue summaries.
        """
        channel_info = await asyncio.to_thread(self._youtube.resolve_channel, url)
        logger.info(
            "monitoring.channel.resolved channel_id=%s name=%r",
            channel_info.channel_id,
            channel_info.channel_name,
        )

        channel = MonitoredChannel(
            channel_id=channel_info.channel_id,
            channel_url=channel_info.channel_url or url,
            channel_name=channel_info.channel_name,
        )
        added = self._config.add_channel(channel)

        if added:
            try:
                async with httpx.AsyncClient() as client:
                    entries = await fetch_channel_feed(client, channel.channel_id)
            except Exception as exc:
                logger.warning(
                    "monitoring.channel.prime_failed channel_id=%s error=%s",
                    channel.channel_id,
                    exc,
                )
                entries = []
            if entries:
                self._state.prime_channel(
                    channel.channel_id, [entry.video_id for entry in entries]
                )
                self._state.save()

        return channel, added

    async def run_scan(self, progress: ProgressCallback | None = None) -> int:
        async with self._scan_lock:
            self._config.load()
            rules = self._config.rules
            total = len(rules.channels)
            if total == 0:
                logger.info("monitoring.scan.skip reason=no_channels")
                return 0

            logger.info("monitoring.scan.start channels=%s", total)
            candidates_enqueued = 0
            async with httpx.AsyncClient() as client:
                for index, channel in enumerate(rules.channels):
                    if progress is not None:
                        await _safe_progress(progress, ScanProgress(
                            channels_total=total,
                            channels_done=index,
                            current_channel=channel,
                            enqueued_total=candidates_enqueued,
                        ))
                    try:
                        candidates_enqueued += await self._scan_channel(client, channel, rules)
                    except Exception:
                        logger.exception(
                            "monitoring.scan.channel_failed channel_id=%s",
                            channel.channel_id,
                        )
            self._state.save()
            logger.info("monitoring.scan.done enqueued=%s", candidates_enqueued)

            if progress is not None:
                await _safe_progress(progress, ScanProgress(
                    channels_total=total,
                    channels_done=total,
                    current_channel=None,
                    enqueued_total=candidates_enqueued,
                ))
            return candidates_enqueued

    async def _scan_channel(
        self,
        client: httpx.AsyncClient,
        channel: MonitoredChannel,
        rules: MonitoringRules,
    ) -> int:
        try:
            entries = await fetch_channel_feed(client, channel.channel_id)
        except Exception as exc:
            logger.warning(
                "monitoring.scan.feed_failed channel_id=%s error=%s", channel.channel_id, exc
            )
            return 0

        enqueued = 0
        for entry in entries:
            if self._state.is_seen(channel.channel_id, entry.video_id):
                continue
            try:
                candidate, defer = await self._evaluate_entry(channel, entry, rules)
            except Exception:
                logger.exception(
                    "monitoring.entry.evaluate_failed channel_id=%s video_id=%s",
                    channel.channel_id,
                    entry.video_id,
                )
                # Treat unexpected failures as terminal — mark seen so we don't
                # retry the same broken entry forever.
                self._state.mark_seen(channel.channel_id, entry.video_id)
                continue

            if not defer:
                self._state.mark_seen(channel.channel_id, entry.video_id)

            if candidate is None:
                continue
            try:
                await self._enqueue(candidate, channel)
                enqueued += 1
            except Exception:
                logger.exception(
                    "monitoring.entry.enqueue_failed channel_id=%s video_id=%s",
                    channel.channel_id,
                    entry.video_id,
                )

        return enqueued

    async def _evaluate_entry(
        self,
        channel: MonitoredChannel,
        entry: FeedEntry,
        rules: MonitoringRules,
    ) -> tuple[ScheduledCandidate | None, bool]:
        """Run the filter pipeline for a single RSS entry.

        Returns ``(candidate, defer)`` where:
        - ``(candidate, False)`` — accepted, will be enqueued; caller marks seen.
        - ``(None, False)``     — rejected definitively; caller marks seen.
        - ``(None, True)``      — rejected for now (e.g. live stream with unknown
          duration), caller MUST NOT mark seen so the next scan re-evaluates.
        """
        # Cheap pre-filter on blacklists first — we may skip yt-dlp altogether.
        pre_text = f"{entry.title}\n{entry.description}"
        if rules.shows_blacklist and _matches_any(pre_text, rules.shows_blacklist):
            logger.info(
                "monitoring.entry.skip reason=shows_blacklist_pre video_id=%s title=%r",
                entry.video_id,
                entry.title,
            )
            return None, False
        if rules.experts_blacklist and _matches_any(pre_text, rules.experts_blacklist):
            logger.info(
                "monitoring.entry.skip reason=experts_blacklist_pre video_id=%s title=%r",
                entry.video_id,
                entry.title,
            )
            return None, False

        # Fetch full metadata for duration, chapters and canonical description.
        metadata = await asyncio.to_thread(self._youtube.fetch_metadata, entry.url)

        # Defer videos with unknown/zero duration: live streams in progress and
        # just-published VODs that YouTube hasn't tagged with a length yet.
        # Whisper-fallback on a multi-hour live stream would burn CPU for hours;
        # better to wait until tomorrow's scan when the duration is known.
        if not metadata.duration_sec or metadata.duration_sec <= 0:
            logger.info(
                "monitoring.entry.skip reason=duration_unknown video_id=%s defer=true",
                entry.video_id,
            )
            return None, True

        if metadata.duration_sec < rules.min_duration_sec:
            logger.info(
                "monitoring.entry.skip reason=too_short video_id=%s duration=%.0f",
                entry.video_id,
                metadata.duration_sec,
            )
            return None, False

        full_text = _compose_text_for_matching(metadata)
        chapter_titles = " \n".join(chapter.title for chapter in metadata.chapters)

        # Blacklists (second pass, full text).
        if rules.shows_blacklist and _matches_any(full_text + "\n" + chapter_titles, rules.shows_blacklist):
            logger.info("monitoring.entry.skip reason=shows_blacklist video_id=%s", entry.video_id)
            return None, False
        if rules.experts_blacklist and _matches_any(full_text + "\n" + chapter_titles, rules.experts_blacklist):
            logger.info("monitoring.entry.skip reason=experts_blacklist video_id=%s", entry.video_id)
            return None, False

        show_matches: list[str] = []
        if rules.shows_whitelist:
            show_matches = _matches_all(full_text, rules.shows_whitelist)
            if not show_matches:
                logger.info("monitoring.entry.skip reason=no_show_match video_id=%s", entry.video_id)
                return None, False

        # Experts whitelist — matches on title/description/chapter titles first,
        # then we may dive into the transcript for the final verdict.
        surface_text = f"{full_text}\n{chapter_titles}"
        expert_matches: list[str] = []
        if rules.experts_whitelist:
            expert_matches = _matches_all(surface_text, rules.experts_whitelist)

        # Try to grab captions — useful both for name matching and segment spans.
        segments: list[TranscriptSegment] = []
        transcript_source = "none"
        try:
            segments = await asyncio.to_thread(self._youtube.fetch_transcript, metadata.video_id)
            transcript_source = "youtube"
        except TranscriptUnavailable as exc:
            logger.info(
                "monitoring.entry.captions_unavailable video_id=%s reason=%s",
                entry.video_id,
                exc,
            )

        transcript_text = _segments_plain_text(segments)

        if rules.experts_whitelist and not expert_matches and transcript_text:
            expert_matches = _matches_all(transcript_text, rules.experts_whitelist)

        if rules.experts_whitelist and not expert_matches:
            logger.info(
                "monitoring.entry.skip reason=no_expert_match video_id=%s",
                entry.video_id,
            )
            return None, False

        # Segment-mode: only для "long" videos, когда есть whitelisted expert matches.
        segment_spans: list[tuple[float, float]] = []
        if (
            rules.experts_whitelist
            and expert_matches
            and metadata.duration_sec >= rules.expert_segment_threshold_sec
        ):
            segment_spans = _compute_expert_spans(
                segments=segments,
                chapters=metadata.chapters,
                expert_names=expert_matches,
                video_duration_sec=metadata.duration_sec,
                window_pre_sec=rules.expert_window_pre_sec,
                window_post_sec=rules.expert_window_post_sec,
                cluster_gap_sec=rules.expert_mention_cluster_gap_sec,
            )
            if not segment_spans:
                logger.info(
                    "monitoring.entry.skip reason=segment_unresolved video_id=%s experts=%s",
                    entry.video_id,
                    expert_matches,
                )
                return None, False

        logger.info(
            "monitoring.entry.accepted video_id=%s title=%r duration=%.0f "
            "show_matches=%s expert_matches=%s segment_spans=%s transcript_source=%s",
            entry.video_id,
            entry.title,
            metadata.duration_sec,
            show_matches,
            expert_matches,
            segment_spans,
            transcript_source,
        )

        return (
            ScheduledCandidate(
                feed_entry=entry,
                metadata=metadata,
                transcript_segments=segments,
                transcript_source=transcript_source,
                segment_spans=segment_spans,
                expert_matches=expert_matches,
                show_matches=show_matches,
            ),
            False,
        )


# --------- pure helpers (testable without bot wiring) ---------


async def _safe_progress(callback: ProgressCallback, snapshot: ScanProgress) -> None:
    """Invoke a progress callback while shielding the scan loop from its failures."""
    try:
        await callback(snapshot)
    except Exception:
        logger.exception(
            "monitoring.progress.callback_failed channels_done=%s/%s",
            snapshot.channels_done,
            snapshot.channels_total,
        )


def _matches_any(text: str, patterns: list[str]) -> list[str]:
    text_lower = text.lower()
    return [pattern for pattern in patterns if pattern and pattern.lower() in text_lower]


def _matches_all(text: str, patterns: list[str]) -> list[str]:
    """Like _matches_any but deduplicated + preserves order of first match."""
    text_lower = text.lower()
    result: list[str] = []
    for pattern in patterns:
        if pattern and pattern.lower() in text_lower and pattern not in result:
            result.append(pattern)
    return result


def _compose_text_for_matching(metadata: VideoMetadata) -> str:
    return f"{metadata.title}\n{metadata.description}"


def _segments_plain_text(segments: list[TranscriptSegment]) -> str:
    return "\n".join(segment.text for segment in segments)


def _compute_expert_spans(
    *,
    segments: list[TranscriptSegment],
    chapters: tuple[VideoChapter, ...],
    expert_names: list[str],
    video_duration_sec: float,
    window_pre_sec: int,
    window_post_sec: int,
    cluster_gap_sec: int,
) -> list[tuple[float, float]]:
    video_end = float(video_duration_sec) if video_duration_sec > 0 else None

    # Priority 1: YouTube chapters mentioning the expert.
    chapter_spans: list[tuple[float, float]] = []
    if chapters:
        for index, chapter in enumerate(chapters):
            if not _matches_any(chapter.title, expert_names):
                continue
            start = max(0.0, chapter.start)
            end = chapters[index + 1].start if index + 1 < len(chapters) else (video_end or start + 600)
            if end > start:
                chapter_spans.append((start, end))
        if chapter_spans:
            return _merge_spans(chapter_spans, merge_gap_sec=60)

    # Priority 2: cluster expert name mentions in the transcript.
    if not segments:
        return []

    mention_times = sorted(
        segment.start
        for segment in segments
        if _matches_any(segment.text, expert_names)
    )
    if not mention_times:
        return []

    clusters: list[list[float]] = []
    current: list[float] = []
    for ts in mention_times:
        if not current or (ts - current[-1]) <= cluster_gap_sec:
            current.append(ts)
        else:
            clusters.append(current)
            current = [ts]
    if current:
        clusters.append(current)

    spans: list[tuple[float, float]] = []
    for cluster in clusters:
        start = max(0.0, cluster[0] - window_pre_sec)
        end = cluster[-1] + window_post_sec
        if video_end is not None:
            end = min(end, video_end)
        if end > start:
            spans.append((start, end))

    return _merge_spans(spans, merge_gap_sec=60)


def _merge_spans(
    spans: list[tuple[float, float]], merge_gap_sec: float = 60.0
) -> list[tuple[float, float]]:
    if not spans:
        return []
    spans_sorted = sorted(spans, key=lambda span: span[0])
    merged: list[tuple[float, float]] = [spans_sorted[0]]
    for start, end in spans_sorted[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + merge_gap_sec:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def filter_segments_by_spans(
    segments: list[TranscriptSegment], spans: list[tuple[float, float]]
) -> list[TranscriptSegment]:
    if not spans:
        return list(segments)
    result: list[TranscriptSegment] = []
    for segment in segments:
        seg_start = segment.start
        seg_end = segment.end if segment.end > segment.start else segment.start
        for span_start, span_end in spans:
            if seg_end >= span_start and seg_start <= span_end:
                result.append(segment)
                break
    return result


def format_spans_for_humans(spans: list[tuple[float, float]]) -> str:
    if not spans:
        return ""
    parts: list[str] = []
    for start, end in spans:
        parts.append(f"{_format_ts(start)}–{_format_ts(end)}")
    return ", ".join(parts)


def _format_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
