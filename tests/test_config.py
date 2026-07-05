import pytest
from app.config import load_settings


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    # Полная изоляция от реального .env репозитория. ВАЖНО: chdir недостаточно —
    # load_dotenv() по умолчанию ищет .env от файла config.py вверх по дереву
    # (usecwd=False), а не от текущей директории, поэтому глушим сам вызов.
    monkeypatch.setattr("app.config.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")


def test_valid_env_loads(base_env):
    settings = load_settings()
    assert settings.database_path.name == "bot.db"


def test_invalid_int_collects_all_errors(base_env, monkeypatch):
    monkeypatch.setenv("TRANSCRIPT_CHUNK_MAX_CHARS", "abc")
    monkeypatch.setenv("LLM_MAX_TOKENS", "не число")
    with pytest.raises(RuntimeError) as exc:
        load_settings()
    text = str(exc.value)
    assert "TRANSCRIPT_CHUNK_MAX_CHARS" in text
    assert "LLM_MAX_TOKENS" in text


def test_invalid_float(base_env, monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "tepло")
    with pytest.raises(RuntimeError, match="LLM_TEMPERATURE"):
        load_settings()


def test_monetization_defaults(base_env):
    settings = load_settings()
    assert settings.public_mode is False
    assert settings.subscription_price_stars == 149
    assert settings.quota_starter == 3
    assert settings.quota_free_weekly == 1
    assert settings.quota_sub_monthly == 30
    assert settings.heavy_duration_sec == 3600


def test_public_mode_flag(base_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "true")
    assert load_settings().public_mode is True


def test_paid_fallback_budget_default(base_env):
    assert load_settings().paid_fallback_free_budget_sec == 180
