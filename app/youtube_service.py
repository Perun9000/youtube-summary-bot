from __future__ import annotations

import datetime
import json
import os
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


# Подстроки yt-dlp/YouTube-ошибок, сигнализирующие о бане/невалидности cookie
# именно этого аккаунта (а не сетевой сбой/приватное видео/и т.п.). Источники:
#  - "sign in to confirm" — yt-dlp сам детектит это по 'sign in' in reason.lower()
#    (yt_dlp/extractor/youtube/_video.py) при получении playability-статуса
#    с текстом вида "Sign in to confirm you're not a bot".
#  - "cookies are no longer valid" — явный warning yt-dlp, когда аккаунт
#    разлогинился/куки ротировались в браузере (extractor/youtube/_base.py).
#  - "http error 429" — YouTube режет по IP/аккаунту темп запросов.
_BAN_SIGNATURES = (
    "sign in to confirm",
    "cookies are no longer valid",
    "http error 429",
)


def _is_ban_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(signature in text for signature in _BAN_SIGNATURES)


class CookieAccount:
    """One cookie-account slot in the rotation pool: file + cooldown state."""

    __slots__ = ("name", "path", "cooldown_until", "last_failed_monotonic")

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        self.cooldown_until = 0.0  # monotonic timestamp; 0 == not cooling down
        self.last_failed_monotonic = 0.0  # 0 == never failed


class CookieRotator:
    """Round-robin pool of Netscape cookie files with ban cooldown.

    Discovers ``*.txt`` files under ``cookies_dir`` (sorted by filename) once,
    at construction time — accounts are added/removed by restarting the bot,
    not hot-reloaded. An empty or missing directory means ``enabled`` is
    False and callers must fall back to the legacy single-file path — this
    keeps the live bot's current behavior untouched when nobody has opted
    into rotation yet.

    Rotation index and cooldown state live in memory only (not persisted):
    losing them on restart is acceptable per spec, and keeping them out of
    the db avoids a second lock domain next to YtdlpUsage's.
    """

    COOLDOWN_SEC = 6 * 3600.0

    def __init__(self, cookies_dir: Path | None) -> None:
        self._lock = threading.Lock()
        self._accounts: list[CookieAccount] = []
        if cookies_dir and cookies_dir.is_dir():
            for path in sorted(cookies_dir.glob("*.txt")):
                self._accounts.append(CookieAccount(name=path.stem, path=path))
        self._next_index = 0

    def __len__(self) -> int:
        return len(self._accounts)

    @property
    def enabled(self) -> bool:
        return bool(self._accounts)

    def iter_pool(self):
        """Yield candidate accounts for one logical yt-dlp call.

        Advances the round-robin cursor by exactly one position per call
        (so consecutive fetch_metadata/download_audio/... calls fan out
        across accounts), then yields up to ``len(pool)`` accounts starting
        there — live (not cooling down) ones first, in round-robin order;
        if every account is cooling down, yields all of them ordered by
        least-recently-failed so a caller never gets refused outright.
        """
        with self._lock:
            if not self._accounts:
                return
            start = self._next_index % len(self._accounts)
            self._next_index = (self._next_index + 1) % len(self._accounts)
            now = time.monotonic()
            order = [self._accounts[(start + i) % len(self._accounts)] for i in range(len(self._accounts))]
            live = [a for a in order if a.cooldown_until <= now]
            if live:
                cooling = [a for a in order if a.cooldown_until > now]
                ordered = live + cooling
            else:
                ordered = sorted(order, key=lambda a: a.last_failed_monotonic)
        yield from ordered

    def mark_cooldown(self, name: str, reason: str) -> None:
        with self._lock:
            now = time.monotonic()
            for account in self._accounts:
                if account.name == name:
                    account.cooldown_until = now + self.COOLDOWN_SEC
                    account.last_failed_monotonic = now
                    break
        logger.warning("cookies.cooldown account=%s reason=%s", name, reason)

    def status(self) -> list[dict]:
        """Snapshot for /stats: name + whether it's currently cooling down."""
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "name": account.name,
                    "cooldown": account.cooldown_until > now,
                    "cooldown_remaining_sec": max(0.0, account.cooldown_until - now),
                }
                for account in self._accounts
            ]


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

    def before_call(self, account: str | None = None) -> None:
        # Весь метод под одним lock'ом: он сериализует и выдержку интервала,
        # и kv-инкремент. SELECT → INSERT OR REPLACE вне lock'а — lost update:
        # два to_thread-потока читают одинаковый count, и второй перезаписывает
        # первого (недосчёт); мутация _warned_day вне lock'а давала бы двойной
        # warning. db-запросы миллисекундные — удержание lock'а на них дёшево.
        #
        # ``account`` (если задан — multi-account режим CookieRotator) не
        # заменяет глобальный счётчик 'ytdlp_usage' — старый формат читается
        # как раньше (обратная совместимость), per-account разбивка живёт
        # рядом в отдельном ключе 'ytdlp_usage_accounts'.
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
                if account:
                    self._record_account_usage_locked(day, account)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ytdlp.usage_failed error=%s", exc)

    def _record_account_usage_locked(self, day: str, account: str) -> None:
        """Increment the per-account daily counter. Caller holds ``self._lock``."""
        row = self._db.query_one("SELECT value FROM kv WHERE key = 'ytdlp_usage_accounts'")
        state = json.loads(row["value"]) if row else {}
        counts = state.get("counts", {}) if state.get("day") == day else {}
        counts[account] = counts.get(account, 0) + 1
        self._db.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES ('ytdlp_usage_accounts', ?)",
            (json.dumps({"day": day, "counts": counts}),),
        )

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

    def account_counts_today(self) -> dict[str, int]:
        if self._db is None:
            return {}
        try:
            row = self._db.query_one("SELECT value FROM kv WHERE key = 'ytdlp_usage_accounts'")
            if not row:
                return {}
            state = json.loads(row["value"])
            if state.get("day") != self._today():
                return {}
            return {str(name): int(count) for name, count in state.get("counts", {}).items()}
        except Exception:  # noqa: BLE001
            return {}


def _base_audio_options(tmp_dir: Path) -> dict:
    """Опции yt-dlp для скачивания аудио (без cookie — их добавляет вызывающий).

    hls_prefer_native — критично для работы за HTTP-прокси: свежие ролики
    YouTube отдаёт только HLS-потоками, а ffmpeg-загрузчик игнорирует
    HTTP(S)_PROXY и виснет там, где прямой доступ к googlevideo зарезан
    (VPS в РФ). Нативный загрузчик yt-dlp ходит через прокси как все
    python-запросы. socket_timeout страхует от вечного «Скачиваю аудио...».

    Для post-live HLS (свежезавершённые эфиры) yt-dlp игнорирует
    hls_prefer_native и принудительно берёт ffmpeg — поэтому env-прокси
    дополнительно передаётся явным параметром `proxy`: только его FFmpegFD
    пробрасывает в ffmpeg флагом -http_proxy.
    """
    options: dict = {
        "format": "bestaudio/best",
        "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
        "hls_prefer_native": True,
        "socket_timeout": 30,
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
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        options["proxy"] = proxy
    return options


class YouTubeService:
    def __init__(self, settings: Settings, db=None) -> None:
        self._settings = settings
        self._usage = YtdlpUsage(
            db,
            min_interval_sec=settings.ytdlp_min_interval_sec,
            soft_daily_limit=settings.ytdlp_soft_daily_limit,
        )
        self._cookie_rotator = CookieRotator(settings.ytdlp_cookies_dir)

    def ytdlp_today_count(self) -> int:
        return self._usage.today_count()

    def cookie_rotation_enabled(self) -> bool:
        return self._cookie_rotator.enabled

    def cookie_account_status(self) -> list[dict]:
        """Per-account snapshot (name/cooldown/today's count) for /stats."""
        counts = self._usage.account_counts_today()
        status = self._cookie_rotator.status()
        for entry in status:
            entry["count_today"] = counts.get(entry["name"], 0)
        return status

    def _run_with_cookie_rotation(self, build_options, perform):
        """Run one logical yt-dlp call, rotating cookie accounts on ban errors.

        ``build_options(cookie_path)`` returns the yt-dlp options dict for a
        given cookie file (or ``None`` in legacy single/no-cookie mode).
        ``perform(options)`` executes the actual yt-dlp call and returns the
        result. With an empty rotation pool (no ``data/cookies/*.txt``) this
        degenerates to exactly the old single-attempt behavior — the live
        bot's current single-cookie setup is untouched.

        On a ban-signature failure (see ``_is_ban_error``) the account is put
        on a 6h cooldown and the same call is retried immediately on the next
        account, for at most one full lap of the pool. Non-ban exceptions
        propagate immediately without rotating — rotation only helps if the
        cause is the account's cookies, not e.g. a network error or a video
        that's genuinely unavailable.
        """
        if not self._cookie_rotator.enabled:
            self._usage.before_call()
            options = build_options(None)
            return perform(options)

        last_exc: Exception | None = None
        for account in self._cookie_rotator.iter_pool():
            self._usage.before_call(account.name)
            options = build_options(account.path)
            try:
                return perform(options)
            except Exception as exc:  # noqa: BLE001
                if not _is_ban_error(exc):
                    raise
                logger.warning("cookies.rotate account=%s reason=%s", account.name, exc)
                self._cookie_rotator.mark_cooldown(account.name, str(exc))
                last_exc = exc
        assert last_exc is not None  # pool is non-empty when enabled
        raise last_exc

    def fetch_metadata(self, url: str) -> VideoMetadata:
        video_id = extract_video_id(url)

        def build_options(cookie_path: Path | None) -> dict:
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
            self._add_cookie_option(options, cookie_path)
            return options

        def perform(options: dict) -> VideoMetadata:
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

        try:
            return self._run_with_cookie_rotation(build_options, perform)
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
        def build_options(cookie_path: Path | None) -> dict:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "playlistend": 1,
            }
            self._add_cookie_option(options, cookie_path)
            return options

        def perform(options: dict) -> ChannelInfo:
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

        return self._run_with_cookie_rotation(build_options, perform)

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
        """Скачать аудиодорожку ролика в mp3 (для Groq Whisper и кнопки owner'а)."""
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

        def build_options(cookie_path: Path | None) -> dict:
            options = _base_audio_options(tmp_dir)
            self._add_cookie_option(options, cookie_path)
            return options

        def perform(options: dict) -> None:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)

        self._run_with_cookie_rotation(build_options, perform)

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
        def build_options(cookie_path: Path | None) -> dict:
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
            self._add_cookie_option(options, cookie_path)
            return options

        def perform(options: dict) -> object:
            with yt_dlp.YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = self._run_with_cookie_rotation(build_options, perform)
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

    def _add_cookie_option(self, options: dict, cookie_path: Path | None = None) -> None:
        # cookie_path set => multi-account rotation is enabled and picked
        # this account's file; it always exists (CookieRotator only lists
        # files it found on disk). None => legacy single-file mode, same
        # existence check as before rotation existed.
        if cookie_path is not None:
            options["cookiefile"] = str(cookie_path)
            return
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
