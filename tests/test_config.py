import pytest
from app.config import load_settings


@pytest.fixture
def base_env(monkeypatch, tmp_path):
    # chdir в tmp — чтобы load_dotenv() не подцепил реальный .env репозитория.
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
