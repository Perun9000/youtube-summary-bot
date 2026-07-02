from __future__ import annotations

import logging
import threading
from pathlib import Path


logger = logging.getLogger(__name__)


class SystemPromptStore:
    """Persistent override for the summarizer's system prompt.

    Если файла нет — эффективным промптом считаем дефолтный (тот, что зашит в
    ``summarizer.SUMMARY_SYSTEM_PROMPT``). Если файл есть и непустой — берём
    его содержимое. Owner может править кастомный промпт через бот-команды
    ``/prompt_set`` / ``/prompt_reset`` / ``/prompt_show``; изменения сразу
    подхватываются следующим запуском саммари — Summarizer читает промпт через
    ``current()`` на каждый вызов.
    """

    def __init__(self, path: Path, default_prompt: str) -> None:
        self._path = path
        self._default_prompt = default_prompt
        self._lock = threading.Lock()
        # Кэш содержимого — чтобы не бить в файловую систему на каждый LLM-запрос.
        # Инвалидируется в set()/reset(); при boot'е читаем один раз.
        self._cached: str | None = None
        self._load()

    def default_prompt(self) -> str:
        return self._default_prompt

    def current(self) -> str:
        """Эффективный system prompt: кастомный, если задан, иначе дефолт."""
        with self._lock:
            if self._cached is not None:
                return self._cached
            return self._default_prompt

    def is_custom(self) -> bool:
        with self._lock:
            return self._cached is not None

    def set(self, text: str) -> None:
        """Сохранить новый кастомный промпт. Пустая строка = reset."""
        prepared = text.strip()
        if not prepared:
            self.reset()
            return

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(prepared + "\n", encoding="utf-8")
            tmp_path.replace(self._path)
            self._cached = prepared
            logger.info(
                "system_prompt.set path=%s chars=%s",
                self._path,
                len(prepared),
            )

    def reset(self) -> bool:
        """Удалить кастомный промпт (вернуться к дефолту). ``True``, если было что удалить."""
        with self._lock:
            existed = self._cached is not None
            self._cached = None
            if self._path.exists():
                try:
                    self._path.unlink()
                    existed = True
                except OSError:
                    logger.exception("system_prompt.reset.unlink_failed path=%s", self._path)
            if existed:
                logger.info("system_prompt.reset path=%s", self._path)
            return existed

    def _load(self) -> None:
        if not self._path.exists():
            self._cached = None
            return
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.exception("system_prompt.load.failed path=%s", self._path)
            self._cached = None
            return
        if not raw:
            self._cached = None
            return
        self._cached = raw
        logger.info(
            "system_prompt.load.done path=%s chars=%s",
            self._path,
            len(raw),
        )
