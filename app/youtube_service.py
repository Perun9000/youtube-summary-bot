from __future__ import annotations

import datetime
import json
import tempfile
import threading
import time
import logging
from pathlib import Path

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, VideoUnavailable

from app.config import Settings
from app.models import ChannelInfo, TranscriptSegment, VideoChapter, VideoComment, VideoMetadata
from app.utils import extract_video_id


logger = logging.getLogger(__name__)


class TranscriptUnavailable(Exception):
    pass


class YtdlpUsage:
    """Троттлинг и суточный счётчик yt-dlp-обращений.

    Анти-бот YouTube смотрит на IP и темп запросов: всплески и параллелизм
    триггерят «Sign in to confirm...». Минимальный интервал сглаживает темп,
    счётчик в kv даёт раннее предупреждение (warning в лог + строка в /stats)
    ДО того, как YouTube начнёт резать. Методы вызываются из sync-кода в
    to_thread — блокирующий sleep допустим и не трогает event loop.
    """

    def __init__(self, db, *, min_interval_sec: float, soft_daily_limit: int) -> None:
        self._db = db
        self._min_interval_sec = min_interval_sec
        self._soft_daily_limit = soft_daily_limit
        self._lock = threading.Lock()
        self._last_call_monotonic = 0.0
        self._warned_day = ""

    def _today(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def before_call(self) -> None:
        with self._lock:
            wait = self._min_interval_sec - (time.monotonic() - self._last_call_monotonic)
            if wait > 0:
                time.sleep(wait)
            self._last_call_monotonic = time.monotonic()
        if self._db is None:
            return
        try:
            day = self._today()
            row = self._db.query_one("SELECT value FROM kv WHERE key = 'ytdlp_usage'")
            state = json.loads(row["value"]) if row else {}
            count = (state.get("count", 0) if state.get("day") == day else 0) + 1
            self._db.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES ('ytdlp_usage', ?)",
                (json.dumps({"day": day, "count": count}),),
            )
            if count > self._soft_daily_limit and self._warned_day != day:
                self._warned_day = day
                logger.warning(
                    "ytdlp.soft_limit_exceeded count=%s limit=%s", count, self._soft_daily_limit
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ytdlp.usage_failed error=%s", exc)

    def today_count(self) -> int:
        if self._db is None:
            return 0
        try:
            row = self._db.query_one("SELECT value FROM kv WHERE key = 'ytdlp_usage'")
            if not row:
                return 0
            state = json.loads(row["value"])
            return int(state.get("count", 0)) if state.get("day") == self._today() else 0
        except Exception:  # noqa: BLE001
            return 0


class YouTubeService:
    def __init__(self, settings: Settings, db=None) -> None:
        self._settings = settings
        self._usage = YtdlpUsage(
            db,
            min_interval_sec=settings.ytdlp_min_interval_sec,
            soft_daily_limit=settings.ytdlp_soft_daily_limit,
        )

    def ytdlp_today_count(self) -> int:
        return self._usage.today_count()

    def fetch_metadata(self, url: str) -> VideoMetadata:
        self._usage.before_call()
        video_id = extract_video_id(url)
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            # Премьеры/запланированные стримы не имеют форматов, и без этой
            # опции yt-dlp кидает «Premieres in N hours» вместо метаданных.
            # С ней возвращается info с live_status/release_timestamp —
            # на них опирается детект премьер в pipeline (_is_upcoming).
            "ignore_no_formats_error": True,
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
                # Премьеры / запланированные стримы: live_status="is_upcoming",
                # release_timestamp — unix-время запланированного выхода.
                live_status = str(info.get("live_status") or "")
                release_raw = info.get("release_timestamp")
                try:
                    release_timestamp = float(release_raw) if release_raw else None
                except (TypeError, ValueError):
                    release_timestamp = None
                return VideoMetadata(
                    video_id=video_id,
                    title=title,
                    channel_name=str(channel_name).strip(),
                    channel_url=str(channel_url).strip(),
                    duration_sec=float(duration or 0),
                    description=str(description),
                    chapters=chapters,
                    live_status=live_status,
                    release_timestamp=release_timestamp,
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
        self._usage.before_call()
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
        self._usage.before_call()
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
                    # 64 kbps mono — баланс размера и качества для прослушивания
                    # человеком (не для ASR — Whisper всё равно ресамплит к 16 kHz):
                    #  - заметно чище, чем 32 kbps: динамика голоса, интонации,
                    #    смех и телефонные вставки звучат внятно;
                    #  - ~28 МБ/час: часовой ролик умещается в 50 МБ Telegram
                    #    без перекомпрессии, 90-минутный тоже (~42 МБ),
                    #    двухчасовой пойдёт через _ensure_audio_fits_telegram.
                    "preferredquality": "64",
                }
            ],
            # Принудительный mono на этапе перекодирования. YouTube часто
            # отдаёт стерео-дорожку, где обе колонки идентичны (записанная
            # речь), либо стереофонию, ничего не дающую разговорному ролику.
            # Mono = тот же битрейт расходуется на одну дорожку → лучше
            # звучит per канал, размер не растёт.
            "postprocessor_args": {
                "ffmpegextractaudio": ["-ac", "1"],
            },
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

    def fetch_top_comments(
        self,
        url: str,
        max_fetch: int = 30,
        top_n: int = 5,
    ) -> list[VideoComment]:
        """Fetch and rank top YouTube comments via yt-dlp's comment extractor.

        Strategy:
          - Ask yt-dlp for up to ``max_fetch`` top-sorted comments (limits the
            paginated walk yt-dlp normally does — we don't need thousands).
          - Drop replies (``parent != 'root'``) → только верхний уровень.
          - Drop pinned comments — закреплённые часто пишет сам автор канала
            или они куратятся им и не отражают реакцию аудитории.
          - Drop comments by the channel uploader (``author_is_uploader``) —
            тоже не «реальный пользователь».
          - Re-sort оставшиеся по ``like_count`` desc и берём топ ``top_n``.

        ``max_fetch`` намеренно > top_n*4: после фильтрации pinned/author может
        отсеяться 1–3 кандидата, нужен запас, чтобы на выходе всё ещё было top_n.

        Returns an empty list (without raising) if comments are disabled,
        the extractor fails, or the video has no comments yet — comments
        are an optional enrichment, not a hard dependency.
        """
        self._usage.before_call()
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "getcomments": True,
            "extractor_args": {
                "youtube": {
                    "max_comments": [str(max_fetch)],
                    "comment_sort": ["top"],
                }
            },
        }
        self._add_cookie_option(options)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            logger.warning("youtube.comments.fetch_failed url=%s error=%s", url, exc)
            return []

        raw = info.get("comments") if isinstance(info, dict) else None
        if not isinstance(raw, list):
            return []

        comments: list[VideoComment] = []
        skipped_pinned = 0
        skipped_uploader = 0
        for item in raw:
            if not isinstance(item, dict):
                continue
            parent = item.get("parent")
            # Replies have parent set to a parent-comment id (not 'root').
            if parent and parent != "root":
                continue
            # Закреплённые комментарии часто пишет сам автор канала или
            # пиарят что-то заранее — нам нужны реакции аудитории, не курирование.
            if item.get("is_pinned"):
                skipped_pinned += 1
                continue
            # Если комментарий написан владельцем канала — тоже не «реальный
            # пользователь», даже если не закреплён.
            if item.get("author_is_uploader"):
                skipped_uploader += 1
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            try:
                like_count = int(item.get("like_count") or 0)
            except (TypeError, ValueError):
                like_count = 0
            comments.append(
                VideoComment(
                    text=text,
                    author=str(item.get("author") or "").strip(),
                    like_count=like_count,
                    is_pinned=False,  # отфильтровали выше — здесь всегда False
                )
            )

        # YouTube's "top" sort is approximately by likes; we re-sort defensively.
        comments.sort(key=lambda c: c.like_count, reverse=True)
        result = comments[:top_n]
        logger.info(
            "youtube.comments.fetched url=%s fetched=%s top_level=%s "
            "skipped_pinned=%s skipped_uploader=%s returning=%s",
            url, len(raw), len(comments) + skipped_pinned + skipped_uploader,
            skipped_pinned, skipped_uploader, len(result),
        )
        return result

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
