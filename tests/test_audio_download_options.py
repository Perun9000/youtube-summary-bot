"""Опции yt-dlp для скачивания аудио.

HLS-потоки должен качать нативный загрузчик yt-dlp, а не ffmpeg: ffmpeg
игнорирует HTTP(S)_PROXY, и на хостинге за прокси (VPS в РФ + Paper VPN)
скачивание свежих роликов (только HLS-форматы) зависает намертво
(инцидент 2026-07-22, TzjbmraVPLE — 18 минут нулевого прогресса).
"""

from pathlib import Path

from app.youtube_service import _base_audio_options


def test_audio_options_prefer_native_hls():
    options = _base_audio_options(Path("/tmp/x"))
    assert options["hls_prefer_native"] is True


def test_audio_options_have_socket_timeout():
    # Без таймаута зависшее соединение висит вечно — «Скачиваю аудио...» без конца.
    assert _base_audio_options(Path("/tmp/x"))["socket_timeout"] > 0
