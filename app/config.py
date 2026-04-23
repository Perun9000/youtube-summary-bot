from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_user_ids(raw: str) -> set[int]:
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: set[int]
    llm_temperature: float
    llm_max_tokens: int
    lmstudio_base_url: str
    lmstudio_model: str
    lmstudio_api_key: str | None
    lmstudio_auto_load: bool
    lmstudio_num_ctx: int
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    telegraph_access_token: str | None
    telegraph_author_name: str
    ytdlp_cookies_path: Path | None
    bot_data_dir: Path
    transcript_chunk_max_chars: int
    synthesis_hierarchy_threshold: int
    synthesis_group_size: int


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    data_dir = Path(os.getenv("BOT_DATA_DIR", "/data")).expanduser()
    cookies_raw = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    cookies_path = Path(cookies_raw).expanduser() if cookies_raw else None

    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS", "")),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1200")),
        lmstudio_base_url=os.getenv("LMSTUDIO_BASE_URL", "http://host.docker.internal:1234").rstrip("/"),
        lmstudio_model=os.getenv("LMSTUDIO_MODEL", "auto").strip(),
        lmstudio_api_key=os.getenv("LMSTUDIO_API_KEY", "").strip() or None,
        lmstudio_auto_load=os.getenv("LMSTUDIO_AUTO_LOAD", "false").strip().lower() in {"1", "true", "yes", "on"},
        lmstudio_num_ctx=int(os.getenv("LMSTUDIO_NUM_CTX", "32768")),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        whisper_device=os.getenv("WHISPER_DEVICE", "cpu"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8"),
        telegraph_access_token=os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip() or None,
        telegraph_author_name=os.getenv("TELEGRAPH_AUTHOR_NAME", "YouTube Summary Bot"),
        ytdlp_cookies_path=cookies_path,
        bot_data_dir=data_dir,
        transcript_chunk_max_chars=int(os.getenv("TRANSCRIPT_CHUNK_MAX_CHARS", "3000")),
        synthesis_hierarchy_threshold=int(os.getenv("SYNTHESIS_HIERARCHY_THRESHOLD", "6")),
        synthesis_group_size=int(os.getenv("SYNTHESIS_GROUP_SIZE", "5")),
    )
