from app.bot_handlers import resolve_user_lang
from app.db import Database
from app.user_lang_store import UserLangStore


class _S:
    def __init__(self, store):
        self.user_langs = store


def test_auto_detect_persists(tmp_path):
    store = UserLangStore(Database(tmp_path / "bot.db"))
    s = _S(store)
    assert resolve_user_lang(1, "fa", s) == "fa"
    assert store.get(1) == ("fa", "auto")


def test_manual_wins_over_new_code(tmp_path):
    store = UserLangStore(Database(tmp_path / "bot.db"))
    store.set(1, "es", "manual")
    assert resolve_user_lang(1, "ru", _S(store)) == "es"


def test_unknown_code_defaults_en(tmp_path):
    store = UserLangStore(Database(tmp_path / "bot.db"))
    assert resolve_user_lang(2, "de", _S(store)) == "en"


def test_no_store_no_crash():
    assert resolve_user_lang(1, "pt-br", _S(None)) == "pt"
    assert resolve_user_lang(None, None, _S(None)) == "en"
