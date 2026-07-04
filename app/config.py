from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _parse_user_ids(raw: str) -> set[int]:
    return {int(part.strip()) for part in raw.split(",") if part.strip()}


def _parse_user_id_list(raw: str) -> list[int]:
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: set[int]
    owner_user_id: int | None
    allowed_users_path: Path
    llm_provider: str
    llm_temperature: float
    llm_max_tokens: int
    llm_max_tokens_partial: int
    llm_max_tokens_final: int
    lmstudio_base_url: str
    lmstudio_model: str
    lmstudio_api_key: str | None
    lmstudio_auto_load: bool
    lmstudio_num_ctx: int
    openrouter_api_key: str | None
    openrouter_base_url: str
    openrouter_model_paid: str
    openrouter_model_free_chain: tuple[str, ...]
    openrouter_fallback_retry_passes: int
    openrouter_fallback_retry_delay_sec: int
    openrouter_runtime_state_path: Path
    openrouter_http_referer: str | None
    openrouter_x_title: str | None
    openrouter_daily_budget_usd: float
    openrouter_daily_request_limit: int
    openrouter_budget_state_path: Path
    groq_api_key: str | None
    groq_whisper_model: str
    groq_base_url: str
    telegraph_access_token: str | None
    telegraph_author_name: str
    ytdlp_cookies_path: Path | None
    bot_data_dir: Path
    database_path: Path
    transcript_chunk_max_chars: int
    openrouter_transcript_chunk_max_chars: int
    openrouter_chunk_size_by_model: tuple[tuple[str, int], ...]
    synthesis_hierarchy_threshold: int
    synthesis_group_size: int
    monitoring_enabled: bool
    monitoring_config_path: Path
    monitoring_state_path: Path
    monitoring_target_chat_id: int | None
    monitoring_llm_retry_interval_sec: int
    premiere_delay_hours: int
    summary_cache_path: Path
    summary_cache_ttl_days: int
    telegram_publish_channel_id: int | None
    channel_posts_path: Path
    tags_catalog_path: Path
    digests_path: Path
    digest_pins_path: Path
    system_prompt_path: Path

    def effective_chunk_max_chars(self, active_model: str | None = None) -> int:
        """Pick the chunk size that best fits the LLM that will actually run.

        For LM Studio: a single value (``transcript_chunk_max_chars``).
        For OpenRouter: try a per-model override first (``openrouter_chunk_size_by_model``)
        based on ``active_model``, fall back to the global
        ``openrouter_transcript_chunk_max_chars`` if no entry matches.

        Why per-model: free-chain mixes models with very different context
        windows (Qwen3-Next 256K, Llama 3.3 65K). We want to maximally fill
        the *current* model's context — chunks too small waste round-trips on
        the big-ctx primary, chunks too big overflow the smaller fallbacks.
        Whoever calls this should pass ``OpenRouterClient.active_model()``.
        """
        if self.llm_provider != "openrouter":
            return self.transcript_chunk_max_chars
        if active_model:
            for model_id, chunk_size in self.openrouter_chunk_size_by_model:
                if model_id == active_model:
                    return chunk_size
        return self.openrouter_transcript_chunk_max_chars


def _parse_optional_int(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


class _EnvReader:
    """Читает переменные окружения с накоплением ошибок.

    Вместо падения на первом же int("abc") собираем все проблемы и
    показываем их одним RuntimeError — чтобы .env чинился за один заход,
    а не по одной переменной на рестарт.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def int(self, name: str, default: str) -> int:
        raw = os.getenv(name, default).strip() or default
        try:
            return int(raw)
        except ValueError:
            self.errors.append(f"{name}={raw!r} — ожидается целое число")
            return int(default)

    def float(self, name: str, default: str) -> float:
        raw = os.getenv(name, default).strip() or default
        try:
            return float(raw)
        except ValueError:
            self.errors.append(f"{name}={raw!r} — ожидается число")
            return float(default)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise RuntimeError(
                "Некорректные значения в .env:\n  - " + "\n  - ".join(self.errors)
            )


def load_settings() -> Settings:
    load_dotenv()
    env = _EnvReader()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    data_dir = Path(os.getenv("BOT_DATA_DIR", "/data")).expanduser()
    allowed_user_ids_raw = os.getenv("ALLOWED_USER_IDS", "")
    allowed_user_ids = _parse_user_ids(allowed_user_ids_raw)
    owner_user_id = _parse_optional_int(os.getenv("OWNER_USER_ID", ""))
    if owner_user_id is None:
        owner_candidates = _parse_user_id_list(allowed_user_ids_raw)
        owner_user_id = owner_candidates[0] if owner_candidates else None
    allowed_users_path = Path(
        os.getenv("ALLOWED_USERS_PATH", str(data_dir / "users.json"))
    ).expanduser()
    cookies_raw = os.getenv("YTDLP_COOKIES_PATH", "").strip()
    cookies_path = Path(cookies_raw).expanduser() if cookies_raw else None

    monitoring_config_path = Path(
        os.getenv("MONITORING_CONFIG_PATH", str(data_dir / "monitoring.yaml"))
    ).expanduser()
    monitoring_state_path = Path(
        os.getenv("MONITORING_STATE_PATH", str(data_dir / "monitoring_state.json"))
    ).expanduser()
    monitoring_target_chat_id = _parse_optional_int(os.getenv("MONITORING_TARGET_CHAT_ID", ""))
    monitoring_enabled = os.getenv("MONITORING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    monitoring_llm_retry_interval_sec = env.int("MONITORING_LLM_RETRY_INTERVAL_SEC", "300")
    # Премьеры: через сколько часов после запланированного выхода ролика
    # возвращаться за саммари (чтобы успели появиться субтитры).
    premiere_delay_hours = env.int("PREMIERE_SUMMARY_DELAY_HOURS", "4")
    summary_cache_path = Path(
        os.getenv("SUMMARY_CACHE_PATH", str(data_dir / "summary_cache.json"))
    ).expanduser()
    summary_cache_ttl_days = env.int("SUMMARY_CACHE_TTL_DAYS", "100")
    telegram_publish_channel_id = _parse_optional_int(
        os.getenv("TELEGRAM_PUBLISH_CHANNEL_ID", "")
    )
    channel_posts_path = Path(
        os.getenv("CHANNEL_POSTS_PATH", str(data_dir / "channel_posts.json"))
    ).expanduser()
    tags_catalog_path = Path(
        os.getenv("TAGS_CATALOG_PATH", str(data_dir / "tags_catalog.json"))
    ).expanduser()
    system_prompt_path = Path(
        os.getenv("SYSTEM_PROMPT_PATH", str(data_dir / "system_prompt.txt"))
    ).expanduser()
    digests_path = Path(
        os.getenv("DIGESTS_PATH", str(data_dir / "digests.json"))
    ).expanduser()
    digest_pins_path = Path(
        os.getenv("DIGEST_PINS_PATH", str(data_dir / "digest_pins.json"))
    ).expanduser()

    llm_provider = os.getenv("LLM_PROVIDER", "lmstudio").strip().lower() or "lmstudio"
    if llm_provider not in {"lmstudio", "openrouter"}:
        raise RuntimeError(
            f"LLM_PROVIDER must be one of: lmstudio, openrouter (got {llm_provider!r})"
        )

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None
    openrouter_base_url = os.getenv(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    ).rstrip("/")

    # Платная модель: одиночная, без fallback (rate-limit'ов почти нет).
    openrouter_model_paid = os.getenv(
        "OPENROUTER_MODEL_PAID", "qwen/qwen3-next-80b-a3b-instruct"
    ).strip()

    # Free-цепочка: comma-separated, в порядке убывания качества русского.
    # На каждом 429/upstream-ошибке падаем в следующую модель.
    free_chain_raw = os.getenv("OPENROUTER_MODEL_FREE_CHAIN", "").strip()
    if free_chain_raw:
        openrouter_model_free_chain = tuple(
            m.strip() for m in free_chain_raw.split(",") if m.strip()
        )
    else:
        openrouter_model_free_chain = (
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "openai/gpt-oss-120b:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        )
    if not openrouter_model_free_chain:
        raise RuntimeError("OPENROUTER_MODEL_FREE_CHAIN cannot be empty")

    openrouter_fallback_retry_passes = env.int(
        "OPENROUTER_FALLBACK_RETRY_PASSES", "2"
    )
    openrouter_fallback_retry_delay_sec = env.int(
        "OPENROUTER_FALLBACK_RETRY_DELAY_SEC", "30"
    )
    openrouter_runtime_state_path = Path(
        os.getenv("OPENROUTER_RUNTIME_STATE_PATH", str(data_dir / "llm_runtime.json"))
    ).expanduser()

    # Per-model chunk sizes: comma-separated "model_id=chars" pairs.
    # Sized to fill ~25-50% of each model's context window after subtracting
    # output (LLM_MAX_TOKENS) and prompt overhead (~1.5K tokens), assuming
    # Russian Cyrillic ~2.5 chars/token for Qwen-family and ~2 chars/token
    # for Llama-family tokenizers.
    chunk_by_model_raw = os.getenv("OPENROUTER_CHUNK_SIZE_BY_MODEL", "").strip()
    if chunk_by_model_raw:
        parsed: list[tuple[str, int]] = []
        for entry in chunk_by_model_raw.split(","):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            model_id, raw_size = entry.split("=", 1)
            try:
                parsed.append((model_id.strip(), int(raw_size.strip())))
            except ValueError:
                continue
        openrouter_chunk_size_by_model = tuple(parsed)
    else:
        openrouter_chunk_size_by_model = (
            ("qwen/qwen3-next-80b-a3b-instruct", 120000),
            ("qwen/qwen3-next-80b-a3b-instruct:free", 120000),
            ("nvidia/nemotron-3-super-120b-a12b:free", 120000),
            ("openai/gpt-oss-120b:free", 80000),
            ("meta-llama/llama-3.3-70b-instruct:free", 50000),
        )

    openrouter_http_referer = os.getenv("OPENROUTER_HTTP_REFERER", "").strip() or None
    openrouter_x_title = os.getenv("OPENROUTER_X_TITLE", "").strip() or None
    openrouter_daily_budget_usd = env.float("OPENROUTER_DAILY_BUDGET_USD", "0.1")
    openrouter_daily_request_limit = env.int(
        "OPENROUTER_DAILY_REQUEST_LIMIT", "180"
    )
    openrouter_budget_state_path = Path(
        os.getenv("OPENROUTER_BUDGET_STATE_PATH", str(data_dir / "openrouter_budget.json"))
    ).expanduser()

    if llm_provider == "openrouter" and not openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter"
        )

    database_path = Path(
        os.getenv("DATABASE_PATH", str(data_dir / "bot.db"))
    ).expanduser()

    llm_temperature = env.float("LLM_TEMPERATURE", "0.2")
    llm_max_tokens = env.int("LLM_MAX_TOKENS", "1200")
    # Per-stage overrides for hierarchical summarization. If not set,
    # fall back to the single LLM_MAX_TOKENS (back-compat).
    llm_max_tokens_partial = env.int(
        "LLM_MAX_TOKENS_PARTIAL", os.getenv("LLM_MAX_TOKENS", "1200")
    )
    llm_max_tokens_final = env.int(
        "LLM_MAX_TOKENS_FINAL", os.getenv("LLM_MAX_TOKENS", "1200")
    )
    lmstudio_num_ctx = env.int("LMSTUDIO_NUM_CTX", "32768")
    transcript_chunk_max_chars = env.int("TRANSCRIPT_CHUNK_MAX_CHARS", "3000")
    openrouter_transcript_chunk_max_chars = env.int(
        "OPENROUTER_TRANSCRIPT_CHUNK_MAX_CHARS", "80000"
    )
    synthesis_hierarchy_threshold = env.int("SYNTHESIS_HIERARCHY_THRESHOLD", "6")
    synthesis_group_size = env.int("SYNTHESIS_GROUP_SIZE", "5")

    env.raise_if_errors()

    return Settings(
        telegram_bot_token=token,
        allowed_user_ids=allowed_user_ids,
        owner_user_id=owner_user_id,
        allowed_users_path=allowed_users_path,
        llm_provider=llm_provider,
        llm_temperature=llm_temperature,
        llm_max_tokens=llm_max_tokens,
        llm_max_tokens_partial=llm_max_tokens_partial,
        llm_max_tokens_final=llm_max_tokens_final,
        lmstudio_base_url=os.getenv("LMSTUDIO_BASE_URL", "http://host.docker.internal:1234").rstrip("/"),
        lmstudio_model=os.getenv("LMSTUDIO_MODEL", "auto").strip(),
        lmstudio_api_key=os.getenv("LMSTUDIO_API_KEY", "").strip() or None,
        lmstudio_auto_load=os.getenv("LMSTUDIO_AUTO_LOAD", "false").strip().lower() in {"1", "true", "yes", "on"},
        lmstudio_num_ctx=lmstudio_num_ctx,
        openrouter_api_key=openrouter_api_key,
        openrouter_base_url=openrouter_base_url,
        openrouter_model_paid=openrouter_model_paid,
        openrouter_model_free_chain=openrouter_model_free_chain,
        openrouter_fallback_retry_passes=openrouter_fallback_retry_passes,
        openrouter_fallback_retry_delay_sec=openrouter_fallback_retry_delay_sec,
        openrouter_runtime_state_path=openrouter_runtime_state_path,
        openrouter_http_referer=openrouter_http_referer,
        openrouter_x_title=openrouter_x_title,
        openrouter_daily_budget_usd=openrouter_daily_budget_usd,
        openrouter_daily_request_limit=openrouter_daily_request_limit,
        openrouter_budget_state_path=openrouter_budget_state_path,
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip() or None,
        groq_whisper_model=os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo").strip(),
        groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com").rstrip("/"),
        telegraph_access_token=os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip() or None,
        telegraph_author_name=os.getenv("TELEGRAPH_AUTHOR_NAME", "YouTube Summary Bot"),
        ytdlp_cookies_path=cookies_path,
        bot_data_dir=data_dir,
        database_path=database_path,
        transcript_chunk_max_chars=transcript_chunk_max_chars,
        openrouter_transcript_chunk_max_chars=openrouter_transcript_chunk_max_chars,
        openrouter_chunk_size_by_model=openrouter_chunk_size_by_model,
        synthesis_hierarchy_threshold=synthesis_hierarchy_threshold,
        synthesis_group_size=synthesis_group_size,
        monitoring_enabled=monitoring_enabled,
        monitoring_config_path=monitoring_config_path,
        monitoring_state_path=monitoring_state_path,
        monitoring_target_chat_id=monitoring_target_chat_id,
        monitoring_llm_retry_interval_sec=monitoring_llm_retry_interval_sec,
        premiere_delay_hours=premiere_delay_hours,
        summary_cache_path=summary_cache_path,
        summary_cache_ttl_days=summary_cache_ttl_days,
        telegram_publish_channel_id=telegram_publish_channel_id,
        channel_posts_path=channel_posts_path,
        tags_catalog_path=tags_catalog_path,
        digests_path=digests_path,
        digest_pins_path=digest_pins_path,
        system_prompt_path=system_prompt_path,
    )
