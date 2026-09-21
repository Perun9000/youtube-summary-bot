"""Блок описания ролика в промпте: фильтрация мусора, главы, лимиты.

Мотивация фичи (2026-09-21): в описании автора имена и фамилии написаны
правильно — модель сверяет по ним искажённые Whisper'ом имена из транскрипта.
Спонсорский мусор (реклама, промокоды, ссылки) выфильтровывается.
"""

from app.description_hint import build_description_block, clean_description
from app.models import VideoChapter, VideoMetadata


def _meta(description="", chapters=()):
    return VideoMetadata(
        video_id="abcABC12345",
        title="Тестовый ролик",
        channel_name="Канал",
        channel_url="",
        description=description,
        chapters=tuple(chapters),
    )


def test_clean_drops_urls_and_promo_lines():
    raw = (
        "Гость выпуска — Екатерина Шульман, политолог.\n"
        "Наш спонсор — Хостинг X, промокод SUMMARY даёт скидку 20%!\n"
        "Подпишись на канал: https://youtube.com/@channel\n"
        "Телеграм: t.me/somechannel\n"
        "Реклама. ООО «Ромашка», erid: 2VtzqxKA\n"
        "Обсуждаем закон о такси и мобилизацию.\n"
        "#политика #новости\n"
    )
    cleaned = clean_description(raw)
    assert "Екатерина Шульман" in cleaned
    assert "закон о такси" in cleaned
    assert "промокод" not in cleaned.lower()
    assert "http" not in cleaned and "t.me" not in cleaned
    assert "erid" not in cleaned.lower()
    assert "#политика" not in cleaned


def test_clean_truncates_long_text():
    raw = "Первое предложение о содержании. " * 100
    cleaned = clean_description(raw, max_chars=800)
    assert len(cleaned) <= 800


def test_block_contains_guardrail_description_and_chapters():
    meta = _meta(
        description="Гость — Владимир Милов.",
        chapters=[VideoChapter(start=0.0, title="Вступление"),
                  VideoChapter(start=95.0, title="Экономика")],
    )
    block = build_description_block(meta)
    assert "Владимир Милов" in block
    assert "не пересказыва" in block.lower()
    assert "01:35 Экономика" in block


def test_block_empty_when_no_useful_content():
    assert build_description_block(_meta(description="")) == ""
    only_junk = _meta(description="Промокод SUMMARY скидка!\nhttps://spam.example\n#tags")
    assert build_description_block(only_junk) == ""


def test_block_chapters_only_still_useful():
    meta = _meta(description="", chapters=[VideoChapter(start=60.0, title="Начало")])
    block = build_description_block(meta)
    assert "01:00 Начало" in block


# --- Интеграция: блок доезжает до промпта модели ---

from app.summarizer import Summarizer  # noqa: E402


class _PromptCapturingLLM:
    def __init__(self):
        self.prompts: list[str] = []

    @property
    def provider_name(self) -> str:
        return "fake"

    async def generate(self, prompt, system=None, usage=None, max_tokens=None, route="default"):
        self.prompts.append(prompt)
        return '{"overview": "ок", "chapters": [], "tags": {}}'


async def test_description_block_reaches_llm_prompt():
    llm = _PromptCapturingLLM()
    summarizer = Summarizer(llm, system_prompt_provider=lambda: "sys")
    block = build_description_block(_meta(description="Гость — Екатерина Шульман."))
    await summarizer.summarize(
        url="https://youtu.be/x", title="t", chunks=["один чанк"],
        description_block=block,
    )
    assert any("Екатерина Шульман" in p for p in llm.prompts)
    assert any("не пересказыва" in p.lower() for p in llm.prompts)


async def test_no_block_no_placeholder_leftover():
    llm = _PromptCapturingLLM()
    summarizer = Summarizer(llm, system_prompt_provider=lambda: "sys")
    await summarizer.summarize(url="https://youtu.be/x", title="t", chunks=["чанк"])
    assert all("{description_block}" not in p for p in llm.prompts)
