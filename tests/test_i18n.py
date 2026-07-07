import json
import re
from pathlib import Path

from app.db import Database
from app.i18n import DEFAULT_LANG, LANG_NATIVE_NAMES, SUPPORTED_LANGS, normalize_language_code, t
from app.user_lang_store import UserLangStore

LOCALES_DIR = Path(__file__).resolve().parent.parent / "app" / "locales"
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def load(lang):
    return json.loads((LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_normalize_language_code():
    assert normalize_language_code("ru") == "ru"
    assert normalize_language_code("pt-br") == "pt"
    assert normalize_language_code("ru-RU") == "ru"
    assert normalize_language_code("de") == "en"      # неподдерживаемый → en
    assert normalize_language_code(None) == "en"
    assert normalize_language_code("") == "en"


def test_all_locales_exist_with_same_keys():
    base_keys = set(load("ru").keys())
    assert base_keys, "ru.json пуст"
    for lang in SUPPORTED_LANGS:
        assert set(load(lang).keys()) == base_keys, f"расхождение ключей в {lang}"


def test_placeholders_match_across_locales():
    ru = load("ru")
    for lang in SUPPORTED_LANGS:
        data = load(lang)
        for key, ru_text in ru.items():
            assert set(PLACEHOLDER_RE.findall(data[key])) == set(PLACEHOLDER_RE.findall(ru_text)), (
                f"плейсхолдеры расходятся: {lang}:{key}"
            )


def test_commands_preserved_across_locales():
    ru = load("ru")
    commands = re.compile(r"/(subscribe|limits|help|paysupport|language|start)\b")
    for lang in SUPPORTED_LANGS:
        data = load(lang)
        for key, ru_text in ru.items():
            assert set(commands.findall(data[key])) == set(commands.findall(ru_text)), (
                f"/команды расходятся: {lang}:{key}"
            )


def test_t_lookup_fallback_and_format():
    assert t("limits.unlimited", "ru")            # существующий ключ
    assert t("nonexistent.key", "ru") == "nonexistent.key"   # fallback на ключ
    ru = t("summary.reading_time", "ru", minutes=5)
    assert "5" in ru
    fa = t("summary.reading_time", "fa", minutes=5)
    assert "5" in fa and fa != ru


def test_t_bad_format_does_not_raise():
    # Недостающий плейсхолдер не должен ронять доставку.
    out = t("summary.reading_time", "en")
    assert "{minutes}" in out


def test_user_lang_store(tmp_path):
    store = UserLangStore(Database(tmp_path / "bot.db"))
    assert store.get(1) is None
    store.set(1, "fa", "auto")
    assert store.get(1) == ("fa", "auto")
    store.set(1, "es", "manual")
    assert store.get(1) == ("es", "manual")
