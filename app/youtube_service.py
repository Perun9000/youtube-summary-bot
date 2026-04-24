from __future__ import annotations

import tempfile
import logging
from pathlib import Path

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

from app.config import Settings
from app.models import ChannelInfo, TranscriptSegment, VideoChapter, VideoMetadata
from app.utils import extract_video_id


logger = logging.getLogger(__name__)


class TranscriptUnavailable(Exception):
    pass


class YouTubeService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def fetch_metadata(self, url: str) -> VideoMetadata:
        video_id = extract_video_id(url)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        self._add_cookie_option(options)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
                title = info.get("title") or f"YouTube video {video_id}"
                channel_name = (
                    info.get("channel")
                    or info.get("uploader")
                    or info.get("uploader_id")
                    or ""
                )
                channel_url = info.get("channel_url") or info.get("uploader_url") or ""
                duration = info.get("duration") or 0
                description = info.get("description") or ""
                chapters = _parse_chapters(info.get("chapters"))
                return VideoMetadata(
                    video_id=video_id,
                    title=title,
                    channel_name=str(channel_name).strip(),
                    channel_url=str(channel_url).strip(),
                    duration_sec=float(duration or 0),
                    description=str(description),
                    chapters=chapters,
                )
        except Exception:
            return VideoMetadata(
                video_id=video_id,
                title=f"YouTube video {video_id}",
                channel_name="",
                channel_url="",
            )

    def resolve_channel(self, url: str) -> ChannelInfo:
        """Resolve a channel handle / custom URL to a canonical channel_id.

        Uses yt-dlp in flat-extract mode so we don't fetch the whole video list.
        """
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": 1,
        }
        self._add_cookie_option(options)

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        candidate_ids = [
            info.get("channel_id"),
            info.get("uploader_id"),
            info.get("id"),
        ]
        channel_id = next(
            (
                str(candidate).strip()
                for candidate in candidate_ids
                if isinstance(candidate, str) and str(candidate).strip().startswith("UC")
            ),
            "",
        )
        if not channel_id:
            raise RuntimeError(f"Не удалось определить channel_id для {url}")

        channel_name = str(info.get("channel") or info.get("uploader") or info.get("title") or "").strip()
        channel_url = str(info.get("channel_url") or info.get("webpage_url") or url).strip()
        return ChannelInfo(
            channel_id=channel_id,
            channel_name=channel_name,
            channel_url=channel_url,
        )

    def fetch_transcript(self, video_id: str) -> list[TranscriptSegment]:
        try:
            transcript_api = YouTubeTranscriptApi()
            transcript_list = transcript_api.list(video_id)
            transcript = self._pick_transcript(transcript_list)
            raw_segments = transcript.fetch().to_raw_data()
        except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as exc:
            raise TranscriptUnavailable(str(exc)) from exc
        except Exception as exc:
            raise TranscriptUnavailable(str(exc)) from exc

        return [
            TranscriptSegment(
                start=float(item["start"]),
                end=float(item["start"]) + float(item.get("duration", 0)),
                text=str(item["text"]),
            )
            for item in raw_segments
            if str(item.get("text", "")).strip()
        ]

    def download_audio(self, url: str) -> Path:
        audio_dir = self._settings.bot_data_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        video_id = extract_video_id(url)

        cached_path = audio_dir / f"{video_id}.mp3"
        if cached_path.exists() and cached_path.stat().st_size > 0:
            logger.info("youtube.audio.cache_hit path=%s", cached_path)
            return cached_path

        for nested_cached_path in audio_dir.glob(f"**/{video_id}.mp3"):
            if nested_cached_path.is_file() and nested_cached_path.stat().st_size > 0:
                logger.info("youtube.audio.cache_hit path=%s", nested_cached_path)
                return nested_cached_path

        tmp_dir = Path(tempfile.mkdtemp(prefix="yt-", dir=audio_dir))

        options = {
            "format": "bestaudio/best",
            "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "64",
                }
            ],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
        }
        self._add_cookie_option(options)

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)

        audio_files = sorted(tmp_dir.glob("*.mp3"))
        if not audio_files:
            raise TranscriptUnavailable("Не удалось скачать аудио через yt-dlp")
        audio_files[0].replace(cached_path)
        return cached_path

    def _pick_transcript(self, transcript_list):
        try:
            return transcript_list.find_manually_created_transcript(["ru", "en"])
        except NoTranscriptFound:
            pass

        try:
            return transcript_list.find_generated_transcript(["ru", "en"])
        except NoTranscriptFound:
            pass

        for transcript in transcript_list:
            return transcript

        raise NoTranscriptFound("No transcript found", None, None)

    def _add_cookie_option(self, options: dict) -> None:
        cookies_path = self._settings.ytdlp_cookies_path
        if cookies_path and cookies_path.exists():
            options["cookiefile"] = str(cookies_path)


def _parse_chapters(raw: object) -> tuple[VideoChapter, ...]:
    if not isinstance(raw, list):
        return ()
    chapters: list[VideoChapter] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_time") or item.get("start") or 0)
        except (TypeError, ValueError):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        chapters.append(VideoChapter(start=start, title=title))
    chapters.sort(key=lambda chapter: chapter.start)
    return tuple(chapters)
