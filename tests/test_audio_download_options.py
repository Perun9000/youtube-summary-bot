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


def test_audio_options_pass_proxy_to_ytdlp(monkeypatch):
    """Env-прокси должен стать явным yt-dlp-параметром `proxy`.

    Для post-live HLS yt-dlp принудительно берёт ffmpeg (native не умеет),
    а FFmpegFD пробрасывает ffmpeg'у -http_proxy только из параметра
    `proxy` — env-переменные ffmpeg не видит (инцидент 2026-07-23,
    TzjbmraVPLE: 10 часов нулевого прогресса).
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://host.docker.internal:8118")
    assert _base_audio_options(Path("/tmp/x"))["proxy"] == "http://host.docker.internal:8118"


def test_audio_options_no_proxy_when_env_empty(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    assert "proxy" not in _base_audio_options(Path("/tmp/x"))
