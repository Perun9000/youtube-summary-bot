"""R2: закалить загрузку бота против сетевых сбоев на старте.

Инцидент 2026-08-25: краш-петля при обрыве сети на старте контейнера.
``await configure_bot_commands(bot, settings)`` в app/main.py::main() не был
обёрнут — TelegramNetworkError из bot.set_my_commands (Telegram недоступен на
старте) валила main() целиком, контейнер рестартовал по кругу, пока сеть
лежала. Сосед в том же main() — ``bot.get_me()`` — уже был обёрнут в
try/except Exception с тем же паттерном (см. app/main.py).

Тестируем не сам main() (тяжёлый, требует поднять кучу сервисов), а новую
обёртку-хелпер _configure_bot_commands_safely — как и советует бриф, если
тестов main() ещё не было.
"""
from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError

from app.main import _configure_bot_commands_safely


class _FakeSettings:
    def __init__(self, owner_user_id=None):
        self.owner_user_id = owner_user_id


class _FakeBot:
    """set_my_commands/set_chat_menu_button raise whatever configure_bot_commands
    triggers them with — here always a network error, to simulate the
    2026-08-25 incident (Telegram unreachable at container boot)."""

    def __init__(self):
        self.calls = 0

    async def set_my_commands(self, *args, **kwargs):
        self.calls += 1
        raise TelegramNetworkError(method=None, message="Connection reset by peer")

    async def set_chat_menu_button(self, *args, **kwargs):  # pragma: no cover — unreachable
        self.calls += 1


async def test_configure_bot_commands_safely_absorbs_network_error(caplog):
    bot = _FakeBot()
    settings = _FakeSettings()

    with caplog.at_level("ERROR"):
        # Must NOT raise — this is exactly what crashed main() in the incident.
        await _configure_bot_commands_safely(bot, settings)

    assert bot.calls == 1
    assert any("configure_commands" in record.message.lower() or "commands" in record.message.lower() for record in caplog.records)


async def test_configure_bot_commands_safely_succeeds_when_network_is_fine():
    calls = []

    class _OkBot:
        async def set_my_commands(self, *args, **kwargs):
            calls.append(("set_my_commands", kwargs))

        async def set_chat_menu_button(self, *args, **kwargs):
            calls.append(("set_chat_menu_button", kwargs))

    await _configure_bot_commands_safely(_OkBot(), _FakeSettings(owner_user_id=555))

    # PUBLIC_BOT_COMMANDS x2 scopes + OWNER_BOT_COMMANDS (owner set) + menu button.
    assert len(calls) == 4
