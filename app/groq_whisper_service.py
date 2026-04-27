"""Cloud Whisper inference via Groq's free tier.

Replaces local Whisper for videos without YouTube captions. Audio is
recompressed via ffmpeg to mono 16kHz mp3 @ 24 kbps so that 3+ hour
recordings fit the 25 MB Groq free-tier upload cap. Transcription itself
runs at ~10–50× realtime on Groq's GPUs (a 1-hour video usually returns
in 30–90 seconds).

The class exposes a single async ``transcribe(audio_path, language=None)``
method that returns a list of ``TranscriptSegment`` matching what the local
WhisperService used to return — drop-in replacement for downstream code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from app.config import Settings
from app.models import TranscriptSegment


logger = logging.getLogger(__name__)


GROQ_TRANSCRIBE_TIMEOUT_SEC = 600   # ~10 min budget for big multi-hour audio
GROQ_AUDIO_MAX_BYTES = 25 * 1024 * 1024   # free-tier file-size cap


class GroqWhisperUnavailable(Exception):
    """Raised when Groq is not configured, rate-limited, or rejects the file."""


class GroqWhisperService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self._settings.groq_api_key)

    async def transcribe(
        self,
        audio_path: Path,
        language: str | None = None,
    ) -> list[TranscriptSegment]:
        if not self.enabled:
            raise GroqWhisperUnavailable(
                "GROQ_API_KEY не задан в .env — облачная транскрипция выключена."
            )
        compressed = await self._compress_audio(audio_path)
        try:
            return await self._call_groq(compressed, language=language)
        finally:
            try:
                compressed.unlink(missing_ok=True)
            except Exception:
                pass

    async def _compress_audio(self, input_path: Path) -> Path:
        """Re-encode audio to mono 16kHz mp3 @ 24kbps via ffmpeg.

        Speech-grade compression — at 24kbps mono we fit ~3 hours of audio
        into 25 MB while keeping Whisper accuracy nearly identical (anything
        above ~16kHz mono adds no useful information for ASR).
        """
        output_path = input_path.with_suffix(input_path.suffix + ".groq.mp3")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vn",                       # drop video stream if any
            "-ac", "1",                  # mono
            "-ar", "16000",              # 16kHz sample rate
            "-c:a", "libmp3lame",
            "-b:a", "24k",
            str(output_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace")[:500] if stderr else ""
            raise RuntimeError(
                f"ffmpeg.compress failed (rc={proc.returncode}): {err}"
            )
        size = output_path.stat().st_size
        logger.info(
            "groq.audio.compressed input=%s output=%s size_bytes=%s",
            input_path,
            output_path,
            size,
        )
        if size > GROQ_AUDIO_MAX_BYTES:
            output_path.unlink(missing_ok=True)
            raise GroqWhisperUnavailable(
                f"После компрессии аудио всё равно {size / 1024 / 1024:.1f} MB, "
                f"превышает Groq free-tier лимит "
                f"{GROQ_AUDIO_MAX_BYTES / 1024 / 1024:.0f} MB. Этот ролик "
                "слишком длинный, нужна нарезка по частям (пока не реализовано)."
            )
        return output_path

    async def _call_groq(
        self,
        audio_path: Path,
        language: str | None,
    ) -> list[TranscriptSegment]:
        api_key = self._settings.groq_api_key or ""
        model = self._settings.groq_whisper_model
        url = f"{self._settings.groq_base_url}/openai/v1/audio/transcriptions"

        started = time.monotonic()
        with audio_path.open("rb") as fh:
            files = {"file": (audio_path.name, fh, "audio/mpeg")}
            data: dict[str, str] = {
                "model": model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            }
            if language:
                data["language"] = language

            async with httpx.AsyncClient(timeout=GROQ_TRANSCRIBE_TIMEOUT_SEC) as client:
                try:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        files=files,
                        data=data,
                    )
                except httpx.ConnectError as exc:
                    raise GroqWhisperUnavailable(
                        f"Groq недоступен: {exc}"
                    ) from exc
                except httpx.ReadTimeout as exc:
                    raise GroqWhisperUnavailable(
                        f"Groq таймаут после {GROQ_TRANSCRIBE_TIMEOUT_SEC}s: {exc}"
                    ) from exc

        status = response.status_code
        if status == 429:
            raise GroqWhisperUnavailable(
                f"Groq rate-limited (HTTP 429): {response.text[:300]}"
            )
        if status >= 400:
            detail = response.text.strip().replace("\n", " ")[:400]
            raise RuntimeError(f"Groq HTTP {status}: {detail}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Groq вернул не-JSON ответ: {response.text[:300]}"
            ) from exc

        duration_sec = time.monotonic() - started
        segments = _segments_from_payload(payload)

        logger.info(
            "groq.transcribe.done model=%s segments=%s duration_sec=%.1f "
            "audio_size_bytes=%s detected_language=%r",
            model,
            len(segments),
            duration_sec,
            audio_path.stat().st_size,
            payload.get("language"),
        )
        return segments


def _segments_from_payload(payload: dict) -> list[TranscriptSegment]:
    """Convert Groq's verbose_json segments to our TranscriptSegment list.

    Groq returns ``segments: [{start, end, text, ...}]``. If the payload has
    no segment-level data (e.g. provider returned plain text), fall back to a
    single-segment representation so downstream code can still chunk + show
    a (timestampless) transcript.
    """
    segments_raw = payload.get("segments") or []
    parsed: list[TranscriptSegment] = []
    for item in segments_raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            continue
        parsed.append(TranscriptSegment(start=start, end=end, text=text))

    if parsed:
        return parsed

    fallback_text = str(payload.get("text") or "").strip()
    if fallback_text:
        return [TranscriptSegment(start=0.0, end=0.0, text=fallback_text)]
    return []
