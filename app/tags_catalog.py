"""Persistent catalog of canonical tags used in summary messages.

Categories
----------
- ``topic``    — основная тема ролика (одно слово или ``snake_case``).
- ``speaker``  — фамилия гостя-эксперта (TitleCase).
- ``format``   — формат ролика (closed-set: интервью / расследование / разбор / новости / дискуссия).
- ``channel``  — название YouTube-канала (TitleCase, ``_`` вместо пробела).

Lookup logic
------------
``lookup_or_add(category, raw_tag)`` сначала нормализует строку, потом ищет
наиболее близкое совпадение среди существующих тегов через
``difflib.SequenceMatcher``. Если коэффициент сходства ≥
``TAG_SIMILARITY_THRESHOLD`` (по умолчанию 0.82) — возвращает каноничную
форму из каталога. Иначе записывает новый тег в каталог и возвращает его.

Это даёт «теги повторяются»: первое упоминание добавляется в каталог,
следующие близкие варианты приводятся к этой каноничной форме.

Storage layout
--------------
JSON-файл (``data/tags_catalog.json``) с одним словарём::

    {"topic": [...], "speaker": [...], "format": [...], "channel": [...]}
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path


logger = logging.getLogger(__name__)


TAG_SIMILARITY_THRESHOLD = 0.82
# host = ведущий / интервьюер / автор канала (тот, кто задаёт вопросы);
# speaker = приглашённый гость / эксперт (тот, кто отвечает).
KNOWN_CATEGORIES = ("topic", "speaker", "host", "format", "channel")
CANONICAL_FORMATS = ("интервью", "расследование", "разбор", "новости", "дискуссия")


@dataclass
class TagsCatalogState:
    topic: list[str] = field(default_factory=list)
    speaker: list[str] = field(default_factory=list)
    host: list[str] = field(default_factory=list)
    format: list[str] = field(default_factory=list)
    channel: list[str] = field(default_factory=list)


class TagsCatalog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state = TagsCatalogState()
        self._load()
        self._seed_formats()

    @property
    def path(self) -> Path:
        return self._path

    def _seed_formats(self) -> None:
        """Гарантируем, что закрытый набор форматов всегда есть в каталоге."""
        with self._lock:
            for f in CANONICAL_FORMATS:
                if f not in self._state.format:
                    self._state.format.append(f)
            self._save_locked()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("tags_catalog.load_failed path=%s error=%s", self._path, exc)
            return
        if not isinstance(data, dict):
            return
        for cat in KNOWN_CATEGORIES:
            tags = data.get(cat) or []
            if isinstance(tags, list):
                cleaned = [str(t).strip() for t in tags if str(t).strip()]
                # Снимаем дубликаты, сохраняя порядок добавления.
                seen: set[str] = set()
                unique: list[str] = []
                for t in cleaned:
                    if t in seen:
                        continue
                    seen.add(t)
                    unique.append(t)
                setattr(self._state, cat, unique)
        logger.info(
            "tags_catalog.loaded path=%s topic=%s speaker=%s format=%s channel=%s",
            self._path,
            len(self._state.topic),
            len(self._state.speaker),
            len(self._state.format),
            len(self._state.channel),
        )

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self._state)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tags_catalog.save_failed path=%s error=%s", self._path, exc)

    def all_tags(self, category: str) -> list[str]:
        if category not in KNOWN_CATEGORIES:
            return []
        with self._lock:
            return list(getattr(self._state, category))

    def lookup_or_add(self, category: str, raw_tag: str) -> str | None:
        """Возвращает каноничный тег (existing или свежедобавленный).

        ``None`` — если входная строка после нормализации пустая.
        Для ``format`` ограничиваемся closed-set'ом: если новый тег ни на что
        не похож — возвращаем ``"новости"`` как default.
        """
        if category not in KNOWN_CATEGORIES:
            return None
        cleaned = _normalize(raw_tag, category)
        if not cleaned:
            return None

        with self._lock:
            existing_list = getattr(self._state, category)
            match = _best_match(cleaned, existing_list)
            if match is not None:
                return match

            # Format — closed set. Новые мы НЕ добавляем; если ни один из
            # 5 канонических не близок к LLM-выдаче, кидаем default.
            if category == "format":
                return "новости"

            existing_list.append(cleaned)
            self._save_locked()
        logger.info("tags_catalog.new_tag category=%s tag=%s", category, cleaned)
        return cleaned


def _normalize(raw: str, category: str) -> str:
    """Базовая нормализация: убираем '#', пробелы → '_', применяем casing-правила."""
    if not raw:
        return ""
    s = raw.strip().lstrip("#").strip()
    if not s:
        return ""
    # Спецсимволы, кроме _, не нужны в Telegram-тегах.
    s = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in s)
    # Сжать подряд идущие подчёркивания.
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_")
    if not s:
        return ""

    if category in ("speaker", "host"):
        # Фамилия — только первая буква заглавная, остальные сохраняем как пришли.
        # Кейсы вроде "ВанДерБеллен" пользователь сам аккуратно введёт в каталог.
        return s[:1].upper() + s[1:].lower()
    if category == "channel":
        # Канал — TitleCase первой буквы. Многословные имена через _.
        # Не lower'им остаток: «вДудь» / «ФейгинLive» сохраняют свой стиль,
        # если LLM их так и записал.
        return s[:1].upper() + s[1:]
    # topic, format → нижний регистр
    return s.lower()


def _best_match(candidate: str, existing: list[str]) -> str | None:
    if not existing:
        return None
    best = None
    best_ratio = 0.0
    cand_lc = candidate.lower()
    for tag in existing:
        ratio = SequenceMatcher(None, cand_lc, tag.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = tag
    if best_ratio >= TAG_SIMILARITY_THRESHOLD:
        return best
    return None
