"""Q5/Q7: адаптивный max_tokens при обрезке вывода LLM (finish_reason=length).

Инцидент 2026-08-04 (Q5): плотный длинный ролик требовал >2000 output-токенов
на partial-стадии; КАЖДАЯ free-модель в цепочке упиралась в наш же потолок
(finish_reason=length), truncation-guard браковал ответ и переключал модель —
3 прохода по цепочке, 20+ минут, ~20 сожжённых запросов дневного лимита.
Прыжки по моделям не лечат наш собственный лимит: прежде чем переключаться на
следующую модель, повторяем ТУ ЖЕ модель с бо́льшим max_tokens.

Инцидент 2026-08-12 (Q7, I5kab8HTzUI): один повтор (Q5) не дотянулся —
транскрипт-монстр (prompt_chars=81043) требовал финальному JSON >4000
токенов, а 2000→4000 всё равно обрезалось; 102 секунды на reasoning-модели
сожжены впустую перед сдачей. Q7 добавляет (1) лестницу удвоений вместо
одного шага — 2000→4000→8000→… пока не упрёмся в
``max(original, min(LLM_MAX_TOKENS_FINAL, 8000))``, и (2) старт сразу с cap
для промптов длиннее ``LLM_BIG_PROMPT_CHARS`` — низкий старт для явно
длинного промпта бессмысленен. См. app/llm_client.py
``OpenRouterClient._generate_with_adaptive_cap``.
"""

import logging

import httpx
import pytest

from app.config import load_settings
from app.db import Database
from app.llm_client import GenerationUsage, OpenRouterClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_FREE_CHAIN", "chain/model-1,chain/model-2")
    monkeypatch.setenv("OPENROUTER_MODEL_PAID", "chain/paid-model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    # Явный потолок для детерминированного расчёта cap в тестах.
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


@pytest.fixture
def client_prod_passes(tmp_path, monkeypatch):
    """Как ``client``, но с прод-конфигом ретраев: retry_passes=2 (passes=3),
    5 моделей в цепочке — ровно репро ревьюера для мандатного short-circuit
    "все обрезались -> один проход"."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv(
        "OPENROUTER_MODEL_FREE_CHAIN",
        "chain/model-1,chain/model-2,chain/model-3,chain/model-4,chain/model-5",
    )
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "2")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_DELAY_SEC", "30")
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


@pytest.fixture
def client_low_big_prompt_threshold(tmp_path, monkeypatch):
    """Как ``client``, но с низким ``LLM_BIG_PROMPT_CHARS`` — короткие
    строки в тестах можно считать "большим промптом" без реальных 60K
    символов."""
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_FREE_CHAIN", "chain/model-1,chain/model-2")
    monkeypatch.setenv("OPENROUTER_MODEL_PAID", "chain/paid-model")
    monkeypatch.setenv("OPENROUTER_FALLBACK_RETRY_PASSES", "0")
    monkeypatch.setenv("LLM_MAX_TOKENS_FINAL", "8000")
    monkeypatch.setenv("LLM_BIG_PROMPT_CHARS", "100")
    settings = load_settings()
    c = OpenRouterClient(settings, Database(tmp_path / "bot.db"))
    c.set_paid_mode(False)
    return c


def _completion(content: str, finish_reason: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


def _wire_by_model_and_tokens(monkeypatch, responder):
    """responder(model, max_tokens) -> dict (тело JSON) для мока POST."""
    calls: list[tuple[str, int]] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        max_tokens = json["max_tokens"]
        calls.append((model, max_tokens))
        body = responder(model, max_tokens)
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


# ── (a) лестница удвоений: 2000 (обрезка) → 4000 (обрезка) → 8000 (успех),
# та же модель 3 раза, model-2 не звался ──


async def test_truncated_retry_ladder_climbs_to_cap_then_succeeds(client, monkeypatch):
    def responder(model, max_tokens):
        assert model == "chain/model-1", "model-2 не должна вызываться — успех после лестницы"
        if max_tokens in (2000, 4000):
            return _completion("thinking thinking thinking", "length")
        assert max_tokens == 8000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage, max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-1", 8000),
    ]
    assert usage.last_finish_reason == "stop"


# ── (в) короткий промпт: одного удвоения достаточно — лестница не тянет
# лишнюю ступень, если модель уже ответила на 4000 ──


async def test_short_prompt_single_doubling_still_sufficient(client, monkeypatch):
    def responder(model, max_tokens):
        assert model == "chain/model-1"
        if max_tokens == 2000:
            return _completion("thinking thinking thinking", "length")
        assert max_tokens == 4000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # Ровно 2 вызова — лестница остановилась на первом успехе, не долезла до 8000.
    assert calls == [("chain/model-1", 2000), ("chain/model-1", 4000)]


# ── (b) обрезка на КАЖДОЙ ступени лестницы (включая cap) → trying_next
# только после того, как лестница исчерпана ──


async def test_truncated_retry_still_truncated_at_cap_moves_to_next_model(client, monkeypatch):
    def responder(model, max_tokens):
        if model == "chain/model-1":
            return _completion("loop loop", "length")  # брак на 2000, 4000 И 8000
        assert model == "chain/model-2"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-1", 8000),
        ("chain/model-2", 2000),
    ]


# ── (c) все модели обрезались (даже после удвоения) → last-resort за ОДИН проход ──


async def test_all_models_truncated_last_resort_after_one_pass(client, monkeypatch):
    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)

    def responder(model, max_tokens):
        return _completion(f"{model}@{max_tokens}", "length")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    usage = GenerationUsage()
    result = await client.generate("p", usage=usage, max_tokens=2000)

    # OPENROUTER_FALLBACK_RETRY_PASSES=0 → один проход, без сна между
    # проходами. Last-resort — от ПОСЛЕДНЕЙ (самой длинной, на cap) попытки
    # последней модели в цепочке, после полной лестницы 2000→4000→8000.
    assert result == "chain/model-2@8000"
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-1", 8000),
        ("chain/model-2", 2000),
        ("chain/model-2", 4000),
        ("chain/model-2", 8000),
    ]
    assert usage.last_finish_reason == "length"


# ── (d) нетронутый путь: обычная ошибка → trying_next без удвоения ──


async def test_plain_error_does_not_trigger_doubling(client, monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append((model, json["max_tokens"]))
        if model == "chain/model-1":
            return httpx.Response(
                429, json={"error": {"code": 429}}, request=httpx.Request("POST", url)
            )
        return httpx.Response(
            200,
            json=_completion('{"overview": "ок"}', "stop"),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # model-1 вызвана РОВНО один раз — 429 не запускает удвоение, это не обрезка.
    assert calls == [("chain/model-1", 2000), ("chain/model-2", 2000)]


# ── нет запаса до потолка → ведёт себя как раньше (без удвоения) ──


async def test_truncated_at_cap_already_no_headroom_moves_to_next_model(client, monkeypatch):
    """max_tokens уже равен потолку (LLM_MAX_TOKENS_FINAL=8000) — удваивать
    нечего, немедленный trying_next, как до Q5."""

    def responder(model, max_tokens):
        if model == "chain/model-1":
            return _completion("loop", "length")
        assert model == "chain/model-2"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=8000)

    assert result == '{"overview": "ок"}'
    assert calls == [("chain/model-1", 8000), ("chain/model-2", 8000)]


# ── (d) Q7: промпт длиннее LLM_BIG_PROMPT_CHARS → первый (и единственный,
# при успехе) вызов сразу на cap, минуя ступени 2000/4000 ──


async def test_big_prompt_starts_at_cap_no_ladder(
    client_low_big_prompt_threshold, monkeypatch, caplog
):
    def responder(model, max_tokens):
        assert model == "chain/model-1"
        assert max_tokens == 8000, "низкий старт для длинного промпта бессмысленен"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    big_prompt = "x" * 200  # длиннее LLM_BIG_PROMPT_CHARS=100 фикстуры
    with caplog.at_level(logging.INFO, logger="app.llm_client"):
        result = await client_low_big_prompt_threshold.generate(big_prompt, max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # Один-единственный вызов — сразу на cap, без промежуточных ступеней.
    assert calls == [("chain/model-1", 8000)]
    assert "llm.generate.big_prompt_full_cap prompt_chars=200 max_tokens=8000" in caplog.text


# ── (e) Q7: промпт длиннее порога И всё равно обрезка на cap → trying_next
# без единой промежуточной ступени (лестница не запускается вовсе) ──


async def test_big_prompt_truncated_at_cap_moves_to_next_model_without_ladder(
    client_low_big_prompt_threshold, monkeypatch
):
    def responder(model, max_tokens):
        assert max_tokens == 8000, "ни одной промежуточной ступени для длинного промпта"
        if model == "chain/model-1":
            return _completion("loop", "length")
        assert model == "chain/model-2"
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    big_prompt = "x" * 200
    result = await client_low_big_prompt_threshold.generate(big_prompt, max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # Ровно 1 вызов на модель — обе сразу на cap, лестница не тратила ступени.
    assert calls == [("chain/model-1", 8000), ("chain/model-2", 8000)]


# ── Ревью-фикс Important 1: allow_big_prompt_full_cap=False (partial-стадия
# summarizer'а) отключает эвристику даже для промпта длиннее порога — большой
# chunk стартует со своего обычного лимита, лестница при обрезке работает ──


async def test_allow_big_prompt_full_cap_false_disables_heuristic_ladder_still_works(
    client_low_big_prompt_threshold, monkeypatch
):
    def responder(model, max_tokens):
        assert model == "chain/model-1"
        if max_tokens in (2000, 4000):
            return _completion("thinking", "length")
        assert max_tokens == 8000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    big_prompt = "x" * 200  # длиннее LLM_BIG_PROMPT_CHARS=100 фикстуры
    result = await client_low_big_prompt_threshold.generate(
        big_prompt, max_tokens=2000, allow_big_prompt_full_cap=False
    )

    assert result == '{"overview": "ок"}'
    # Лестница как обычно (2000→4000→8000) — НЕ немедленный прыжок на cap,
    # несмотря на то, что промпт длиннее LLM_BIG_PROMPT_CHARS.
    assert calls == [
        ("chain/model-1", 2000),
        ("chain/model-1", 4000),
        ("chain/model-1", 8000),
    ]


async def test_summarizer_marks_partial_calls_no_big_prompt_full_cap():
    """Ревью-фикс Important 1 (wiring): Summarizer передаёт
    allow_big_prompt_full_cap=False для partial-стадийных вызовов
    (почанковых и hierarchy-mid), True (по умолчанию) — для synthesis/final.
    Без этого разделения длинные ролики стартовали бы почанковые вызовы с
    cap=8000 при реальной потребности ~1200-2000 уже в дефолтном конфиге
    (OPENROUTER_TRANSCRIPT_CHUNK_MAX_CHARS=80000 > LLM_BIG_PROMPT_CHARS=60000)."""
    from app.summarizer import Summarizer

    class _RecordingLLM:
        def __init__(self):
            self.calls: list[tuple[str, bool]] = []

        @property
        def provider_name(self) -> str:
            return "fake"

        async def generate(
            self, prompt, system=None, usage=None, max_tokens=None, route="default",
            allow_big_prompt_full_cap=True,
        ):
            stage = "partial" if max_tokens == 111 else "final"
            self.calls.append((stage, allow_big_prompt_full_cap))
            return '{"overview": "x", "chapters": [], "tags": {}}'

    llm = _RecordingLLM()
    summarizer = Summarizer(
        llm, hierarchy_threshold=2, group_size=2,
        partial_max_tokens=111, final_max_tokens=222,
        system_prompt_provider=lambda: "sys",
    )
    # 4 чанка, group_size=2 → 2 группы (>1) → hierarchy-mid путь тоже задет.
    await summarizer.summarize(
        url="https://youtu.be/x", title="t",
        chunks=["чанк 1", "чанк 2", "чанк 3", "чанк 4"],
    )

    partial_flags = [flag for stage, flag in llm.calls if stage == "partial"]
    final_flags = [flag for stage, flag in llm.calls if stage == "final"]
    assert partial_flags, "не задет ни один partial-стадийный вызов — тест не показателен"
    assert all(flag is False for flag in partial_flags)
    assert final_flags, "не задет ни один final-стадийный вызов — тест не показателен"
    assert all(flag is True for flag in final_flags)


# ── Paid-путь: та же адаптивная логика при обрезке ──


async def test_paid_path_truncated_retries_with_bigger_cap(client, monkeypatch):
    client.set_paid_mode(True)

    def responder(model, max_tokens):
        assert model == "chain/paid-model"
        if max_tokens == 2000:
            return _completion("thinking...", "length")
        assert max_tokens == 4000
        return _completion('{"overview": "ок"}', "stop")

    calls = _wire_by_model_and_tokens(monkeypatch, responder)
    result = await client.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    assert calls == [("chain/paid-model", 2000), ("chain/paid-model", 4000)]


async def test_paid_path_truncated_at_cap_returns_last_resort_immediately(client, monkeypatch):
    """Платная модель без запаса до потолка — прежнее поведение: обрезанный
    текст отдаётся немедленно, без второго прохода (единственная модель, без
    цепочки — повторять нечего)."""
    client.set_paid_mode(True)
    calls = _wire_by_model_and_tokens(
        monkeypatch, lambda model, max_tokens: _completion("stuck", "length")
    )

    result = await client.generate("p", max_tokens=8000)

    assert result == "stuck"
    assert calls == [("chain/paid-model", 8000)]


# ── Ревью-фикс: прод-конфиг (retry_passes=2, 5 моделей) — тотальная обрезка ──
# должна остановиться на ОДНОМ проходе, а не крутить sleep+retry ещё 2 раза.
# Дискриминирующий тест: НЕ пинит retry_passes=0 (в отличие от фикстуры
# ``client`` выше), поэтому тавтологического "один проход" из конфига здесь
# нет — если short-circuit не сработает, тест поймает реальные 3 прохода.


async def test_all_truncated_prod_config_stops_after_one_pass(client_prod_passes, monkeypatch):
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        return httpx.Response(
            200,
            json=_completion(f"{model}@{json['max_tokens']}", "length"),
            request=httpx.Request("POST", url),
        )

    async def empty_catalog(self, url, headers=None):
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", empty_catalog)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", fake_sleep)

    usage = GenerationUsage()
    result = await client_prod_passes.generate("p", usage=usage, max_tokens=2000)

    # 5 моделей × 3 запроса (Q7 лестница: 2000→4000→8000, cap=8000), РОВНО
    # один проход — не 3 (до Q5 было 5*1*3=15 без адаптивного cap вообще;
    # Q5 (один повтор) снижал до 5*2=10; Q7 (лестница до cap) возвращает к
    # 5*3=15 — но, в отличие от до-Q5 сценария, все 15 запросов реально
    # дотягиваются до 8000 токенов вместо трёх бесполезных проходов на том
    # же 2000/4000 потолке).
    assert len(calls) == 15, calls
    assert calls == [
        "chain/model-1", "chain/model-1", "chain/model-1",
        "chain/model-2", "chain/model-2", "chain/model-2",
        "chain/model-3", "chain/model-3", "chain/model-3",
        "chain/model-4", "chain/model-4", "chain/model-4",
        "chain/model-5", "chain/model-5", "chain/model-5",
    ]
    # Ни одного sleep между проходами — цепочка не пошла на 2-й/3-й проход.
    assert sleeps == []
    # last_resort — от последней (самой длинной, на cap) попытки.
    assert result == "chain/model-5@8000"
    assert usage.last_finish_reason == "length"


async def test_mixed_pass_with_real_error_still_retries_next_pass(client_prod_passes, monkeypatch):
    """Regression: если В ТОМ ЖЕ проходе хоть одна модель дала настоящую (не
    truncation) ошибку, short-circuit не должен срабатывать — проходы
    продолжаются как раньше (настоящая ошибка может не повториться)."""
    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_post(self, url, headers=None, json=None):
        model = json["model"]
        calls.append(model)
        if model == "chain/model-3":
            # Настоящая (не truncation) ошибка — держит проход "смешанным".
            return httpx.Response(
                429, json={"error": {"code": 429}}, request=httpx.Request("POST", url)
            )
        if model == "chain/model-5" and calls.count("chain/model-5") >= 4:
            # Q7: лестница даёт до 3 вызовов на ОДНУ встречу с моделью в
            # пределах одного прохода (2000→4000→8000), поэтому порог "3"
            # (из до-Q7 теста, где на модель было максимум 2 вызова) сработал
            # бы уже внутри 1-го прохода. Порог "4" гарантирует, что первый
            # проход исчерпает всю лестницу модели-5 (3 вызова, всё ещё
            # truncated) и модель-5 ответит нормально только на 1-м вызове
            # 2-го прохода — тест продолжает проверять именно межпроходное
            # поведение, а не лестницу.
            return httpx.Response(
                200,
                json=_completion('{"overview": "ок"}', "stop"),
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json=_completion(f"{model}@{json['max_tokens']}", "length"),
            request=httpx.Request("POST", url),
        )

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.llm_client.asyncio.sleep", fake_sleep)

    result = await client_prod_passes.generate("p", max_tokens=2000)

    assert result == '{"overview": "ок"}'
    # Продолжались проходы (sleep случился между ними) — смешанный проход не
    # схлопнулся в один.
    assert len(sleeps) >= 1
