from __future__ import annotations

import logging
import time
from pathlib import Path

from faster_whisper import WhisperModel

from app.config import Settings
from app.models import TranscriptSegment


logger = logging.getLogger(__name__)


class WhisperService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: WhisperModel | None = None

    def transcribe(self, audio_path: Path) -> list[TranscriptSegment]:
        started = time.monotonic()
        if self._model is None:
            logger.info(
                "whisper.model_load.start model=%s device=%s compute_type=%s",
                self._settings.whisper_model,
                self._settings.whisper_device,
                self._settings.whisper_compute_type,
            )
            model_started = time.monotonic()
            self._model = WhisperModel(
                self._settings.whisper_model,
                device=self._settings.whisper_device,
                compute_type=self._settings.whisper_compute_type,
            )
            logger.info("whisper.model_load.done duration_sec=%.1f", time.monotonic() - model_started)

        logger.info("whisper.transcribe.start audio=%s", audio_path)
        segments, _info = self._model.transcribe(
            str(audio_path),
            vad_filter=True,
            beam_size=5,
        )

        result: list[TranscriptSegment] = []
        for segment in segments:
            text = " ".join(segment.text.split())
            if text:
                result.append(
                    TranscriptSegment(
                        start=float(segment.start),
                        end=float(segment.end),
                        text=text,
                    )
                )
        logger.info(
            "whisper.transcribe.done duration_sec=%.1f segments=%s",
            time.monotonic() - started,
            len(result),
        )
        return result
