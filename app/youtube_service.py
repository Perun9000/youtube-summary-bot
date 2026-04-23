from __future__ import annotations

import tempfile
import logging
from pathlib import Path

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

from app.config import Settings
from app.models import TranscriptSegment, VideoMetadata
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
                return VideoMetadata(
                    video_id=video_id,
                    title=title,
                    channel_name=str(channel_name).strip(),
                    channel_url=str(channel_url).strip(),
                )
        except Exception:
            return VideoMetadata(
                video_id=video_id,
                title=f"YouTube video {video_id}",
                channel_name="",
                channel_url="",
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
