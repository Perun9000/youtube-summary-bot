from __future__ import annotations

import logging
import re
import time

from app.models import VideoContext
from app.llm_client import LLMClient


logger = logging.getLogger(__name__)


QA_SYSTEM_PROMPT = """
Ты отвечаешь на вопросы пользователя по конкретному YouTube-ролику.
Отвечай на русском языке. Используй только предоставленный контекст.
Если ответа в контексте нет, так и скажи. Не выдумывай.
По возможности добавляй таймкоды, на которые опираешься.
""".strip()


QA_PROMPT = """
Вопрос пользователя:
{question}

Ролик:
{title}
{url}

Краткое summary:
{summary}

Релевантные фрагменты транскрипции:
{chunks}
""".strip()


class QAService:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def answer(self, context: VideoContext, question: str) -> str:
        started = time.monotonic()
        chunks = "\n\n".join(_rank_chunks(context.chunks, question, limit=4))
        summary_text = _summary_to_text(context)
        answer = await self._llm.generate(
            QA_PROMPT.format(
                question=question,
                title=context.title,
                url=context.url,
                summary=summary_text,
                chunks=chunks,
            ),
            system=QA_SYSTEM_PROMPT,
        )
        logger.info(
            "qa.done title=%r question_chars=%s context_chars=%s answer_chars=%s duration_sec=%.1f",
            context.title,
            len(question),
            len(chunks),
            len(answer),
            time.monotonic() - started,
        )
        return answer


def _summary_to_text(context: VideoContext) -> str:
    points = "\n".join(f"- {point}" for point in context.summary.key_points)
    return f"{context.summary.overview}\n{points}".strip()


def _rank_chunks(chunks: list[str], question: str, limit: int) -> list[str]:
    question_terms = set(_terms(question))
    scored = []
    for chunk in chunks:
        score = len(question_terms.intersection(_terms(chunk)))
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [chunk for score, chunk in scored[:limit] if score > 0]
    return selected or chunks[:limit]


def _terms(text: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[\wа-яА-ЯёЁ]{3,}", text)]
