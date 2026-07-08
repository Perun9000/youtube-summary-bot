"""Локализация пользовательских строк.

Переводы живут в app/locales/{lang}.json (плоский key→text) и грузятся при
импорте — это часть кода, ревьюится в git и деплоится атомарно (осознанное
решение против переводов в БД). В БД — только выбор языка пользователя
(user_langs). Русский — канонический источник; en — база для остальных.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_LANGS: tuple[str, ...] = ("ru", "en", "es", "fa", "pt", "ar", "id")
DEFAULT_LANG = "en"

LANG_NATIVE_NAMES: dict[str, str] = {
    "ru": "Русский", "en": "English", "es": "Español", "fa": "فارسی",
    "pt": "Português", "ar": "العربية", "id": "Bahasa Indonesia",
}
# Английские имена — для языковой директивы LLM (см. summarizer).
LANG_ENGLISH_NAMES: dict[str, str] = {
    "ru": "Russian", "en": "English", "es": "Spanish", "fa": "Persian (Farsi)",
    "pt": "Portuguese", "ar": "Arabic", "id": "Indonesian",
}

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"


def _load_locales() -> dict[str, dict[str, str]]:
    locales: dict[str, dict[str, str]] = {}
    for lang in SUPPORTED_LANGS:
        path = _LOCALES_DIR / f"{lang}.json"
        try:
            locales[lang] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.exception("i18n.locale_load_failed lang=%s path=%s", lang, path)
            locales[lang] = {}
    return locales


_LOCALES = _load_locales()


def normalize_language_code(code: str | None) -> str:
    """Telegram language_code (IETF, напр. 'pt-br') → поддерживаемый язык."""
    if not code:
        return DEFAULT_LANG
    primary = code.strip().lower().split("-")[0]
    return primary if primary in SUPPORTED_LANGS else DEFAULT_LANG


def t(key: str, lang: str, **fmt) -> str:
    """Перевод ключа: lang → en → сам ключ. Ошибка format не роняет вызов."""
    text = _LOCALES.get(lang, {}).get(key) or _LOCALES.get(DEFAULT_LANG, {}).get(key) or key
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("i18n.format_failed key=%s lang=%s error=%s", key, lang, exc)
        return text
