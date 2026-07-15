"""Реферальный шеринг, ступень 0: шер-сообщение, кнопка, парсер payload.

Спека: docs/superpowers/specs/2026-07-15-referral-share-step0-design.md
"""

from app.delivery import _build_summary_keyboard, build_share_message
from app.summary_cache import CachedSummary


def _cached(**overrides):
    base = dict(
        video_id="abcABC12345",
        url="https://www.youtube.com/watch?v=abcABC12345",
        title="Заголовок <ролика>",
        channel_name="Канал & Ко",
        channel_url="",
        summary_overview=(
            "Первое предложение о главном тезисе ролика, сформулированное "
            "достаточно развёрнуто, чтобы занять заметную часть тизера. "
            "Второе предложение с важными деталями, контекстом происходящего "
            "и дополнительными подробностями для полноты картины. Третье "
            "предложение добавляет ещё больше нюансов к общей картине "
            "обсуждения и растягивает текст. Четвёртое предложение уже "
            "точно не влезает в тизер и должно быть отрезано."
        ),
        summary_key_points=[],
        summary_chapters=[{"start": "00:00", "title": "Глава", "notes": "Текст " * 200}],
        summary_raw_text="",
        telegraph_url="https://telegra.ph/x",
        transcript_url=None,
        transcript_source="youtube",
        model="test/model",
        created_at_iso="2026-07-15T00:00:00",
        created_at_unix=0.0,
    )
    base.update(overrides)
    return CachedSummary(**base)


def test_share_message_has_ref_link_and_escaping():
    text = build_share_message(_cached(), bot_username="TestBot", referrer_id=42)
    assert "https://t.me/TestBot?start=r42_abcABC12345" in text
    assert "Заголовок &lt;ролика&gt;" in text
    assert "Канал &amp; Ко" in text


def test_share_message_trims_overview_to_sentences():
    text = build_share_message(_cached(), bot_username="TestBot", referrer_id=42)
    assert "Первое предложение о главном тезисе ролика." in text
    assert "не влезает в тизер" not in text
    # обрезка по границе предложения — нет оборванных хвостов перед точкой
    assert "…" not in text or text.count(".") >= 1


def test_share_button_only_for_owner():
    owner_kb = _build_summary_keyboard(
        telegraph_url="https://telegra.ph/x", video_id="abcABC12345",
        is_owner=True, lang="ru",
    )
    owner_actions = [
        b.callback_data or b.url
        for row in owner_kb.inline_keyboard
        for b in row
    ]
    assert "share:abcABC12345" in owner_actions

    user_kb = _build_summary_keyboard(
        telegraph_url="https://telegra.ph/x", video_id="abcABC12345",
        is_owner=False, lang="ru",
    )
    user_actions = [
        b.callback_data or b.url
        for row in user_kb.inline_keyboard
        for b in row
    ]
    assert "share:abcABC12345" not in user_actions
