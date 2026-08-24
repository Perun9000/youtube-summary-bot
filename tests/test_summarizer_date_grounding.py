"""Q10 layer 1: грунтовка Summarizer-промпта датами.

RED-тесты для build_date_grounding_block (пере) и её интеграции в
Summarizer._system_prompt_with_hint через summarize(upload_date=...).
"""
import datetime as _dt

from app.models import Summary, SummaryTags
from app.summarizer import Summarizer, build_date_grounding_block


VALID_JSON = (
    '{"overview": "О.", "chapters": [], '
    '"tags": {"topic": "", "speakers": [], "hosts": [], "format": ""}}'
)


class _CapturingLLM:
    """Фейковый LLM: не отвечает содержательно, только запоминает system."""

    def __init__(self):
        self.captured_system: str | None = None

    @property
    def provider_name(self) -> str:
        return "fake"

    async def generate(
        self, prompt, system=None, usage=None, max_tokens=None, route="default",
        allow_big_prompt_full_cap=True,
    ):
        self.captured_system = system
        return VALID_JSON


def today_human() -> str:
    return _dt.date.today().strftime("%d.%m.%Y")


# --- build_date_grounding_block (unit) ---

def test_block_contains_today_and_prohibition_without_upload_date():
    block = build_date_grounding_block(today_human(), None)
    assert today_human() in block
    assert "ЗАПРЕЩЕНО" in block
    assert "Ролик опубликован" not in block


def test_block_contains_upload_date_when_known():
    block = build_date_grounding_block(today_human(), "15.01.2024")
    assert today_human() in block
    assert "15.01.2024" in block
    assert "ЗАПРЕЩЕНО" in block


# --- Интеграция: summarize(upload_date=...) → system prompt, дошедший до llm.generate ---

async def test_summarize_passes_today_and_upload_date_to_system_prompt():
    llm = _CapturingLLM()
    summarizer = Summarizer(llm)
    await summarizer.summarize(
        url="https://youtu.be/x",
        title="t",
        chunks=["один чанк транскрипта"],
        upload_date="20240115",
    )
    assert llm.captured_system is not None
    assert today_human() in llm.captured_system
    assert "15.01.2024" in llm.captured_system
    assert "ЗАПРЕЩЕНО" in llm.captured_system


async def test_summarize_without_upload_date_omits_publish_date_line():
    llm = _CapturingLLM()
    summarizer = Summarizer(llm)
    await summarizer.summarize(
        url="https://youtu.be/x",
        title="t",
        chunks=["один чанк транскрипта"],
        upload_date=None,
    )
    assert llm.captured_system is not None
    assert today_human() in llm.captured_system
    assert "Ролик опубликован" not in llm.captured_system
    assert "ЗАПРЕЩЕНО" in llm.captured_system


async def test_summarize_with_garbage_upload_date_treated_as_unknown():
    llm = _CapturingLLM()
    summarizer = Summarizer(llm)
    await summarizer.summarize(
        url="https://youtu.be/x",
        title="t",
        chunks=["один чанк транскрипта"],
        upload_date="not-a-date",
    )
    assert llm.captured_system is not None
    assert "Ролик опубликован" not in llm.captured_system


async def test_parse_summary_public_wrapper_matches_internal():
    summarizer = Summarizer(_CapturingLLM())
    summary = summarizer.parse_summary(VALID_JSON)
    assert isinstance(summary, Summary)
    assert summary.overview == "О."


def test_final_max_tokens_property_exposes_private_attr():
    summarizer = Summarizer(_CapturingLLM(), final_max_tokens=1234)
    assert summarizer.final_max_tokens == 1234
