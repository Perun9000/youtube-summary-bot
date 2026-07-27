"""Одноразовый кастомный промпт пользователя (/myprompt) — чистая логика.

Промпт применяется к ОДНОМУ следующему видео и сгорает. Никогда не заменяет
системный промпт — только секция-пожелание в context_hint суммаризатора
(см. docs/superpowers/specs/2026-07-23-custom-user-prompt-design.md).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.utils import extract_youtube_url

CUSTOM_PROMPT_MAX_CHARS = 500
AWAITING_INPUT_TTL_SEC = 300   # 5 минут на ввод промпта после /myprompt
ARMED_TTL_SEC = 900            # 15 минут на ссылку после принятого промпта

# Пожелания пользователя должны ПОБЕЖДАТЬ стилевые правила системного промпта
# (иначе модель гасит «пиши саркастично» как противоречие нейтральному тону —
# инцидент 2026-07-27). Незыблемым остаётся только контракт: JSON-схема,
# язык вывода, достоверность.
_WRAPPER = (
    "ПЕРСОНАЛЬНЫЕ ПОЖЕЛАНИЯ ПОЛЬЗОВАТЕЛЯ к этому саммари: \"{prompt}\"\n"
    "Эти пожелания ГЛАВНЕЕ стилистических правил выше: тон, манера речи, "
    "подача, расстановка акцентов и фокус — по пожеланиям пользователя, даже "
    "если правила выше требуют другого стиля. Неизменными остаются только: "
    "формат ответа (та же JSON-схема и поля), язык вывода, достоверность "
    "фактов из транскрипта и запрет выдумывать содержание."
)


def parse_prompt_message(text: str) -> tuple[str | None, str]:
    """Сообщение пользователя → (youtube_url | None, текст промпта).

    Основной путь фичи — промпт и ссылка одним сообщением: URL вырезается,
    остальное (после trim) считается промптом.
    """
    text = (text or "").strip()
    url = extract_youtube_url(text)
    if url is None:
        return None, text
    prompt = text.replace(url, " ")
    prompt = " ".join(prompt.split()).strip()
    return url, prompt


def wrap_custom_prompt(prompt: str) -> str:
    return _WRAPPER.format(prompt=prompt)


@dataclass
class PendingCustomPrompt:
    """Состояние диалога /myprompt для одного чата (ленивое протухание)."""

    stage: str            # "awaiting_input" | "armed"
    prompt: str = ""
    started_at: float = 0.0

    def expired(self, now: float) -> bool:
        ttl = AWAITING_INPUT_TTL_SEC if self.stage == "awaiting_input" else ARMED_TTL_SEC
        return now - self.started_at > ttl
