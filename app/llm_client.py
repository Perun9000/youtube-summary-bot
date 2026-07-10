from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass
import logging
import threading
import time
from pathlib import Path
from typing import Protocol

import httpx

from app.config import Settings
from app.db import Database, retire_legacy_json


logger = logging.getLogger(__name__)
LLM_GENERATE_TIMEOUT_SEC = 1200
LLM_GENERATE_MAX_ATTEMPTS = 2
LLM_GENERATE_RETRY_DELAY_SEC = 15
OPENROUTER_BUDGET_EXCEEDED_MARKER = "OPENROUTER_BUDGET_EXCEEDED"
FREE_CHAIN_EXHAUSTED_MARKER = "OPENROUTER_FREE_CHAIN_EXHAUSTED"


class CircuitBreaker:
    """Предохранитель от бесполезных полных проходов fallback-цепочки.

    Если OpenRouter лёг целиком (N подряд задач исчерпали все модели цепочки),
    каждая следующая задача без предохранителя ждала бы 4–6 минут таймаутов.
    После ``threshold`` подряд неудач открываемся на ``cooldown_sec`` — задачи
    в этот период падают мгновенно с понятной причиной.
    """

    def __init__(self, threshold: int = 2, cooldown_sec: float = 600.0) -> None:
        self._threshold = threshold
        self._cooldown_sec = cooldown_sec
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._cooldown_sec:
            # Кулдаун истёк — half-open: даём следующей задаче попробовать.
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def remaining_sec(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._cooldown_sec - (time.monotonic() - self._opened_at))

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None


@dataclass
class GenerationUsage:
    """Mutable accumulator для метрик LLM-вызовов в рамках одной задачи."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    duration_sec: float = 0.0
    # finish_reason последнего вызова: "length" означает обрыв по max_tokens —
    # downstream (summarizer) по нему отличает зацикленную модель от битого JSON.
    last_finish_reason: str | None = None

    def add(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        duration_sec: float,
    ) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.calls += 1
        self.duration_sec += duration_sec


class LLMClient(Protocol):
    @property
    def provider_name(self) -> str:
        ...

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        usage: GenerationUsage | None = None,
        max_tokens: int | None = None,
        route: str = "default",
    ) -> str:
        ...

    async def list_models(self) -> list[str]:
        ...

    async def active_model(self) -> str:
        ...

    async def loaded_context_length(self) -> int | None:
        ...


def create_llm_client(settings: Settings, db: Database) -> LLMClient:
    if settings.llm_provider == "openrouter":
        return OpenRouterClient(settings, db)
    return LMStudioClient(settings)


async def health_check_with_reason(client: "LLMClient", timeout_sec: float = 10.0) -> tuple[bool, str]:
    """Return ``(ok, reason)`` — provider-aware health probe.

    For OpenRouter we additionally verify the daily budget hasn't been spent.
    Used by both manual /scan_now and the daily scheduler to defer the entire
    scan-tick when the upstream is unusable, instead of marking videos as seen
    and losing them when the LLM finally comes back.
    """
    if isinstance(client, OpenRouterClient):
        ok, reason = client.budget.check()
        if not ok:
            return False, reason
    try:
        await asyncio.wait_for(client.list_models(), timeout=timeout_sec)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


class OpenRouterBudget:
    """Persistent daily budget tracker for OpenRouter calls.

    Stores ``{"date": "YYYY-MM-DD", "spent_usd": ..., "request_count": ...}``
    as JSON under the ``openrouter_budget`` key of the ``kv`` SQLite table.
    Resets on the first call of a new local day. Thread-safe; safe across
    multiple asyncio tasks since updates go through a process-local lock and
    a tiny critical section (the ``Database`` itself also serializes access).
    """

    _KV_KEY = "openrouter_budget"

    def __init__(
        self,
        db: Database,
        *,
        daily_budget_usd: float,
        daily_request_limit: int,
        legacy_json_path: Path | None = None,
    ) -> None:
        self._db = db
        self._daily_budget_usd = max(0.0, daily_budget_usd)
        self._daily_request_limit = max(0, daily_request_limit)
        self._lock = threading.Lock()
        self._state = {"date": "", "spent_usd": 0.0, "request_count": 0}
        self._load(legacy_json_path)

    def _today(self) -> str:
        return dt.date.today().isoformat()

    def _load(self, legacy_json_path: Path | None) -> None:
        row = self._db.query_one("SELECT value FROM kv WHERE key = ?", (self._KV_KEY,))
        data: dict | None = None
        if row is not None:
            try:
                data = json.loads(row["value"])
            except Exception as exc:
                logger.warning("openrouter.budget.load_failed key=%s error=%s", self._KV_KEY, exc)
                data = None
        elif legacy_json_path is not None and legacy_json_path.exists():
            try:
                data = json.loads(legacy_json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "openrouter.budget.legacy_load_failed path=%s error=%s", legacy_json_path, exc
                )
                data = None
            if data is not None:
                self._state = {
                    "date": str(data.get("date") or self._today()),
                    "spent_usd": float(data.get("spent_usd") or 0.0),
                    "request_count": int(data.get("request_count") or 0),
                }
                self._save()
            retire_legacy_json(legacy_json_path)
            return

        if data is None:
            self._state = {"date": self._today(), "spent_usd": 0.0, "request_count": 0}
            return

        if data.get("date") != self._today():
            self._state = {"date": self._today(), "spent_usd": 0.0, "request_count": 0}
            return

        self._state = {
            "date": str(data.get("date") or self._today()),
            "spent_usd": float(data.get("spent_usd") or 0.0),
            "request_count": int(data.get("request_count") or 0),
        }

    def _save(self) -> None:
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                (self._KV_KEY, json.dumps(self._state, ensure_ascii=False)),
            )
        except Exception as exc:
            logger.warning("openrouter.budget.save_failed key=%s error=%s", self._KV_KEY, exc)

    def _rollover_if_needed(self) -> None:
        if self._state.get("date") != self._today():
            self._state = {"date": self._today(), "spent_usd": 0.0, "request_count": 0}

    def check(self) -> tuple[bool, str]:
        """Return ``(can_proceed, reason)``. Reason is empty when ``can_proceed`` is True."""
        with self._lock:
            self._rollover_if_needed()
            spent = float(self._state.get("spent_usd", 0.0))
            requests = int(self._state.get("request_count", 0))

            if self._daily_budget_usd > 0 and spent >= self._daily_budget_usd:
                return False, (
                    f"Дневной бюджет OpenRouter исчерпан: "
                    f"${spent:.4f}/${self._daily_budget_usd:.2f}."
                )
            if self._daily_request_limit > 0 and requests >= self._daily_request_limit:
                return False, (
                    f"Дневной лимит запросов OpenRouter исчерпан: "
                    f"{requests}/{self._daily_request_limit}."
                )
            return True, ""

    def record(self, cost_usd: float) -> None:
        """Account a single completed request (with whatever cost OpenRouter reported)."""
        with self._lock:
            self._rollover_if_needed()
            self._state["spent_usd"] = float(self._state.get("spent_usd", 0.0)) + max(0.0, cost_usd)
            self._state["request_count"] = int(self._state.get("request_count", 0)) + 1
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            self._rollover_if_needed()
            return dict(self._state)


@dataclass(frozen=True)
class _OpenRouterUsageInfo:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float


class OpenRouterRuntimeState:
    """Persistent toggle for paid vs free model mode.

    Survives container restarts; written atomically. Default = free.
    Single field today (paid_mode) but kept as a tiny class so we can
    add more runtime-tunable LLM knobs without breaking storage shape.
    """

    _KV_KEY = "openrouter_paid_mode"

    def __init__(self, db: Database, legacy_json_path: Path | None = None) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._paid_mode = False
        self._load(legacy_json_path)

    def _load(self, legacy_json_path: Path | None) -> None:
        row = self._db.query_one("SELECT value FROM kv WHERE key = ?", (self._KV_KEY,))
        if row is not None:
            self._paid_mode = row["value"] == "true"
            return
        if legacy_json_path is not None and legacy_json_path.exists():
            try:
                data = json.loads(legacy_json_path.read_text(encoding="utf-8"))
                self._paid_mode = bool(data.get("paid_mode", False))
                self._save()
            except Exception as exc:
                logger.warning(
                    "openrouter.runtime.legacy_load_failed path=%s error=%s", legacy_json_path, exc
                )
            retire_legacy_json(legacy_json_path)

    def _save(self) -> None:
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)",
                (self._KV_KEY, "true" if self._paid_mode else "false"),
            )
        except Exception as exc:
            logger.warning("openrouter.runtime.save_failed key=%s error=%s", self._KV_KEY, exc)

    def is_paid_mode(self) -> bool:
        with self._lock:
            return self._paid_mode

    def set_paid_mode(self, paid: bool) -> None:
        with self._lock:
            self._paid_mode = bool(paid)
            self._save()


class OpenRouterClient:
    """LLM client backed by OpenRouter's OpenAI-compatible API.

    Two run-time modes, switchable via /llm_paid and /llm_free in the bot:

    - **Free mode** (default): cycles through ``openrouter_model_free_chain``.
      On HTTP 429 / 5xx / ReadTimeout, falls through to the next model in
      the chain. After a full pass through the chain fails, sleeps
      ``openrouter_fallback_retry_delay_sec`` and retries the chain up to
      ``openrouter_fallback_retry_passes`` more times before raising.

    - **Paid mode**: single model ``openrouter_model_paid``, with the standard
      ``LLM_GENERATE_MAX_ATTEMPTS`` timeout-retry behavior. No fallback —
      paid endpoints don't really 429 short of account-level limits.

    Cost / request guards:
    - ``OPENROUTER_DAILY_BUDGET_USD`` — soft $ cap (set 0 to disable; useful
      for free where cost is always 0).
    - ``OPENROUTER_DAILY_REQUEST_LIMIT`` — soft request count cap.
    """

    def __init__(self, settings: Settings, db: Database) -> None:
        self._settings = settings
        self._budget = OpenRouterBudget(
            db,
            daily_budget_usd=settings.openrouter_daily_budget_usd,
            daily_request_limit=settings.openrouter_daily_request_limit,
            legacy_json_path=settings.openrouter_budget_state_path,
        )
        self._runtime = OpenRouterRuntimeState(db, legacy_json_path=settings.openrouter_runtime_state_path)
        self._cached_context_length: dict[str, int] = {}
        self._breaker = CircuitBreaker()

    @property
    def provider_name(self) -> str:
        return "OpenRouter"

    @property
    def budget(self) -> OpenRouterBudget:
        return self._budget

    @property
    def runtime(self) -> OpenRouterRuntimeState:
        return self._runtime

    def is_paid_mode(self) -> bool:
        return self._runtime.is_paid_mode()

    def set_paid_mode(self, paid: bool) -> None:
        self._runtime.set_paid_mode(paid)
        self._cached_context_length.clear()

    def has_paid_model(self) -> bool:
        return bool(self._settings.openrouter_model_paid)

    def current_chain(self) -> tuple[str, ...]:
        """Return the ordered list of models the next generate() will try."""
        if self.is_paid_mode():
            return (self._settings.openrouter_model_paid,) if self._settings.openrouter_model_paid else ()
        return tuple(self._settings.openrouter_model_free_chain)

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        usage: GenerationUsage | None = None,
        max_tokens: int | None = None,
        route: str = "default",
    ) -> str:
        ok, reason = self._budget.check()
        if not ok:
            logger.warning("llm.generate.budget_block provider=openrouter reason=%s", reason)
            raise RuntimeError(f"{OPENROUTER_BUDGET_EXCEEDED_MARKER}: {reason}")

        if route == "paid_fallback":
            return await self._generate_with_paid_fallback(prompt, system, usage, max_tokens)

        if self._breaker.is_open():
            raise RuntimeError(
                "OpenRouter временно недоступен (circuit breaker), "
                f"следующая попытка через ~{int(self._breaker.remaining_sec() / 60) + 1} мин."
            )

        # free_only (внешний без подписки): всегда free-цепочка, глобальный
        # /llm_paid игнорируется — платные токены только платящим.
        if route != "free_only" and self.is_paid_mode():
            if not self._settings.openrouter_model_paid:
                raise RuntimeError("OpenRouter: платная модель не задана. Проверь .env.")
            return await self._generate_paid(prompt, system, usage, max_tokens)

        if not self._settings.openrouter_model_free_chain:
            raise RuntimeError("OpenRouter: список моделей пуст. Проверь .env.")
        return await self._generate_free_chain(prompt, system, usage, max_tokens)

    async def _generate_free_chain(
        self,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
    ) -> str:
        chain = self.current_chain()
        # Free mode — cycle through the chain, then sleep + retry full chain.
        passes = self._settings.openrouter_fallback_retry_passes + 1
        delay = self._settings.openrouter_fallback_retry_delay_sec
        last_error: Exception | None = None
        truncated_text: str | None = None
        for pass_idx in range(passes):
            for model in chain:
                try:
                    result = await self._generate_one_attempt(
                        model, prompt, system, usage, max_tokens
                    )
                    self._breaker.record_success()
                    return result
                except _OpenRouterRetriable as exc:
                    last_error = exc.cause
                    if isinstance(exc, _OpenRouterTruncated):
                        truncated_text = exc.text
                    logger.warning(
                        "llm.generate.fallback provider=openrouter model=%s pass=%s/%s "
                        "trying_next reason=%s",
                        model, pass_idx + 1, passes, exc.short_reason,
                    )
                    continue
            if pass_idx + 1 < passes:
                logger.info(
                    "llm.generate.chain_exhausted pass=%s/%s sleep_sec=%s",
                    pass_idx + 1, passes, delay,
                )
                await asyncio.sleep(delay)

        if truncated_text is not None:
            # Вся цепочка либо отказала, либо обрезала ответ по max_tokens.
            # Обрезанный текст — лучшее, что есть: отдаём его, downstream-парсер
            # (damaged-JSON recovery в summarizer) решит, пригоден ли он.
            logger.warning(
                "llm.generate.truncated_last_resort provider=openrouter chars=%s",
                len(truncated_text),
            )
            return truncated_text

        models_tried = ", ".join(chain)
        # Если последняя ошибка — 402, это почти всегда дневной USD-cap
        # OpenRouter free-tier (50 запросов/день без депозита, 1000/день с
        # депозитом ≥ $10). Сброс — в 00:00 UTC. Подсказка пользователю это
        # отдельной строкой.
        last_err_str = str(last_error or "")
        is_quota_402 = "402" in last_err_str or "spend limit exceeded" in last_err_str.lower()
        suffix = (
            "Дневной free-tier лимит OpenRouter исчерпан (сброс в 00:00 UTC, "
            "= 03:00 МСК). Варианты: подождать, /llm_paid, или положить ≥ $10 "
            "на OpenRouter (Settings → Credits) — поднимет cap до 1000 req/day."
            if is_quota_402
            else "Попробуй позже или переключись на платную через /llm_paid."
        )
        self._breaker.record_failure()
        # Маркер — машиночитаемый признак «free-цепочка исчерпана»: pipeline
        # по нему подменяет техническое сообщение дружелюбным для внешних
        # пользователей (владелец видит полный текст с подсказками).
        raise RuntimeError(
            f"{FREE_CHAIN_EXHAUSTED_MARKER}: все free-модели в цепочке отказались отвечать "
            f"за {passes} проходов ({models_tried}). Последняя ошибка: {last_error}. {suffix}"
        )

    async def _generate_paid(
        self,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
        *,
        record_success: bool = True,
    ) -> str:
        """Одна платная модель со стандартной retry-политикой.

        record_success=False — для fallback-пути подписчика: успех платной
        модели ничего не говорит о здоровье free-цепочки, breaker не трогаем.
        """
        result = await self._generate_with_retries(
            self._settings.openrouter_model_paid, prompt, system, usage, max_tokens
        )
        if record_success:
            self._breaker.record_success()
        return result

    async def _generate_with_paid_fallback(
        self,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
    ) -> str:
        """Маршрут подписчика: free сначала, платная — когда free плоха.

        Триггеры платной попытки: открытый breaker (free-цепочка лежит),
        исчерпание цепочки, превышение бюджета времени
        PAID_FALLBACK_FREE_BUDGET_SEC на весь free-этап. Бюджетная ошибка
        OpenRouter (дневной cap) пробрасывается — это стоп для всех маршрутов.
        """
        paid_model = self._settings.openrouter_model_paid
        if not paid_model:
            # Фолбэчить не на что — ведём себя как free_only.
            return await self._generate_free_chain(prompt, system, usage, max_tokens)

        if self.is_paid_mode():
            # Владелец включил платный режим глобально — подписчик тоже сразу
            # на платной (это его tier).
            return await self._generate_paid(prompt, system, usage, max_tokens)

        if self._breaker.is_open():
            logger.info("llm.paid_fallback.trigger reason=breaker_open")
        else:
            budget_sec = self._settings.paid_fallback_free_budget_sec
            try:
                return await asyncio.wait_for(
                    self._generate_free_chain(prompt, system, usage, max_tokens),
                    timeout=budget_sec,
                )
            except asyncio.TimeoutError:
                logger.info(
                    "llm.paid_fallback.trigger reason=slow budget_sec=%s", budget_sec
                )
            except RuntimeError as exc:
                if OPENROUTER_BUDGET_EXCEEDED_MARKER in str(exc):
                    raise
                # Цепочка исчерпана; record_failure уже сделан внутри —
                # breaker честно отражает здоровье free для остальных.
                logger.info("llm.paid_fallback.trigger reason=free_exhausted")

        result = await self._generate_paid(
            prompt, system, usage, max_tokens, record_success=False
        )
        logger.info("llm.paid_fallback.success model=%s", paid_model)
        return result

    async def _generate_with_retries(
        self,
        model: str,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
    ) -> str:
        """Single-model invocation with the standard timeout-retry policy.

        Used for paid mode where there's no fallback chain.
        """
        last_exc: Exception | None = None
        for attempt in range(1, LLM_GENERATE_MAX_ATTEMPTS + 1):
            try:
                return await self._generate_one_attempt(
                    model, prompt, system, usage, max_tokens
                )
            except _OpenRouterTruncated as exc:
                # Одиночная модель без цепочки: альтернативы нет, повтор того же
                # запроса вряд ли поможет — отдаём обрезанный текст downstream'у.
                logger.warning(
                    "llm.generate.truncated provider=openrouter model=%s chars=%s",
                    model, len(exc.text),
                )
                return exc.text
            except _OpenRouterRetriable as exc:
                last_exc = exc.cause
                if attempt >= LLM_GENERATE_MAX_ATTEMPTS:
                    self._breaker.record_failure()
                    raise RuntimeError(
                        f"OpenRouter ({model}) не ответил после {attempt} попыток: "
                        f"{exc.short_reason}"
                    ) from exc.cause
                logger.warning(
                    "llm.generate.retry provider=openrouter model=%s attempt=%s/%s reason=%s "
                    "delay_sec=%s",
                    model, attempt, LLM_GENERATE_MAX_ATTEMPTS, exc.short_reason,
                    LLM_GENERATE_RETRY_DELAY_SEC,
                )
                await asyncio.sleep(LLM_GENERATE_RETRY_DELAY_SEC)
        if last_exc is not None:
            self._breaker.record_failure()
            raise RuntimeError(f"OpenRouter ({model}) не ответил.") from last_exc
        self._breaker.record_failure()
        raise RuntimeError(f"OpenRouter ({model}) не ответил.")

    async def _generate_one_attempt(
        self,
        model: str,
        prompt: str,
        system: str | None,
        usage: GenerationUsage | None,
        max_tokens: int | None,
    ) -> str:
        """One HTTP call to OpenRouter for a specific model.

        Returns response text on success, raises ``_OpenRouterRetriable`` for
        rate limits / upstream errors / timeouts (so callers can fall through
        to a different model or retry), or a plain ``RuntimeError`` for
        non-retriable problems (auth, bad request, etc.).
        """
        started = time.monotonic()
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        effective_max_tokens = (
            max_tokens if max_tokens is not None else self._settings.llm_max_tokens
        )
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": effective_max_tokens,
            "stream": False,
            "usage": {"include": True},
        }

        async with httpx.AsyncClient(timeout=LLM_GENERATE_TIMEOUT_SEC) as client:
            try:
                response = await client.post(
                    f"{self._settings.openrouter_base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.ConnectError as exc:
                raise _OpenRouterRetriable("connect_error", exc) from exc
            except httpx.ReadTimeout as exc:
                raise _OpenRouterRetriable("timeout", exc) from exc
            except httpx.HTTPError as exc:
                raise _OpenRouterRetriable(f"http_error:{type(exc).__name__}", exc) from exc

            status = response.status_code
            # Retriable statuses:
            # - 429: rate-limit от самого OpenRouter
            # - 402: «Payment Required» от провайдера. На free-моделях
            #   OpenRouter возвращает его, когда даунстрим-провайдер (Venice,
            #   Chutes, DeepInfra) упёрся в свой суточный USD-cap для free-trafic.
            #   У разных моделей в цепочке — разные провайдеры, поэтому
            #   следующая в chain'е может ответить нормально.
            # - 5xx: серверные сбои.
            if status == 429 or status == 402 or 500 <= status < 600:
                detail = response.text.strip().replace("\n", " ")[:300]
                exc = RuntimeError(f"OpenRouter HTTP {status}: {detail}")
                raise _OpenRouterRetriable(f"http_{status}", exc)
            try:
                _raise_for_status(response, "OpenRouter")
            except RuntimeError as exc:
                # Non-retriable: 401, 403, 404, 4xx (кроме 429/402), etc.
                raise

            data = response.json()

        duration_sec = time.monotonic() - started
        choices = data.get("choices", [])
        if not choices:
            result = ""
            finish_reason = ""
        else:
            message = choices[0].get("message", {})
            result = _strip_thinking(str(message.get("content", ""))).strip()
            finish_reason = str(choices[0].get("finish_reason") or "")

        usage_info = _extract_openrouter_usage(data.get("usage") or {})
        if usage is not None:
            usage.add(
                prompt_tokens=usage_info.prompt_tokens,
                completion_tokens=usage_info.completion_tokens,
                total_tokens=usage_info.total_tokens,
                duration_sec=duration_sec,
            )
            usage.last_finish_reason = finish_reason or None

        self._budget.record(usage_info.cost_usd)
        snap = self._budget.snapshot()

        logger.info(
            "llm.generate.done provider=openrouter model=%s prompt_chars=%s response_chars=%s "
            "prompt_tokens=%s completion_tokens=%s total_tokens=%s cost_usd=%.6f "
            "duration_sec=%.1f finish_reason=%s budget_today_usd=%.4f budget_today_requests=%s "
            "mode=%s",
            model,
            len(prompt),
            len(result),
            usage_info.prompt_tokens,
            usage_info.completion_tokens,
            usage_info.total_tokens,
            usage_info.cost_usd,
            duration_sec,
            finish_reason or "-",
            float(snap.get("spent_usd", 0.0)),
            int(snap.get("request_count", 0)),
            "paid" if self.is_paid_mode() else "free",
        )
        if finish_reason == "length":
            # Модель упёрлась в лимит completion-токенов: вывод обрезан и почти
            # наверняка непригоден (частый случай — зацикленный reasoning).
            raise _OpenRouterTruncated(result)
        return result

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{self._settings.openrouter_base_url}/models",
                    headers=self._headers(),
                )
                _raise_for_status(response, "OpenRouter")
            except httpx.ConnectError as exc:
                raise RuntimeError(_openrouter_connection_error()) from exc
            data = response.json()
        out: list[str] = []
        for item in data.get("data", []):
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            ctx = item.get("context_length")
            ctx_str = f"; ctx={int(ctx)}" if isinstance(ctx, (int, float)) and ctx else ""
            out.append(f"{mid}{ctx_str}")
        return out

    async def active_model(self) -> str:
        chain = self.current_chain()
        return chain[0] if chain else ""

    async def loaded_context_length(self) -> int | None:
        target = await self.active_model()
        if not target:
            return None
        if target in self._cached_context_length:
            return self._cached_context_length[target]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self._settings.openrouter_base_url}/models",
                    headers=self._headers(),
                )
                _raise_for_status(response, "OpenRouter")
                data = response.json()
        except Exception:
            return None
        for item in data.get("data", []):
            if str(item.get("id") or "") == target:
                ctx = item.get("context_length")
                try:
                    parsed = int(ctx)
                    self._cached_context_length[target] = parsed
                    return parsed
                except (TypeError, ValueError):
                    return None
        return None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.openrouter_api_key or ''}",
        }
        if self._settings.openrouter_http_referer:
            headers["HTTP-Referer"] = self._settings.openrouter_http_referer
        if self._settings.openrouter_x_title:
            headers["X-Title"] = self._settings.openrouter_x_title
        return headers


class _OpenRouterRetriable(Exception):
    """Wraps a retriable OpenRouter failure (HTTP 429/5xx, timeout, conn err).

    Carries the original cause + a short tag for log lines.
    """

    def __init__(self, short_reason: str, cause: Exception) -> None:
        super().__init__(short_reason)
        self.short_reason = short_reason
        self.cause = cause


class _OpenRouterTruncated(_OpenRouterRetriable):
    """Ответ обрезан по max_tokens (finish_reason=length).

    Типичный сценарий — reasoning-модель зациклилась и выжгла весь лимит,
    не дойдя до полезного вывода. Free-цепочка пробует следующую модель;
    обрезанный текст сохраняется как last resort на случай, когда вся
    цепочка вернула такой же брак.
    """

    def __init__(self, text: str) -> None:
        super().__init__(
            "truncated_at_max_tokens", RuntimeError("finish_reason=length")
        )
        self.text = text


def _extract_openrouter_usage(usage_data: dict) -> _OpenRouterUsageInfo:
    prompt_tokens = int(usage_data.get("prompt_tokens") or 0)
    completion_tokens = int(usage_data.get("completion_tokens") or 0)
    total_tokens = int(
        usage_data.get("total_tokens") or (prompt_tokens + completion_tokens)
    )
    cost_raw = usage_data.get("cost", 0)
    try:
        cost_usd = float(cost_raw)
    except (TypeError, ValueError):
        cost_usd = 0.0
    return _OpenRouterUsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


def _openrouter_connection_error() -> str:
    return (
        "OpenRouter недоступен. Проверь интернет/прокси из контейнера и доступ к "
        "https://openrouter.ai. Если используешь VPN, убедись, что docker идёт через него."
    )


def _openrouter_timeout_error(model: str, timeout_sec: int, attempts: int) -> str:
    minutes = max(1, round(timeout_sec / 60))
    return (
        f"OpenRouter не вернул ответ для модели {model} за {minutes} мин "
        f"(попыток: {attempts}). Возможно перегружен провайдер: попробуй другую модель "
        "через OPENROUTER_MODEL или вернись на LM Studio через LLM_PROVIDER=lmstudio."
    )


@dataclass(frozen=True)
class LMStudioModel:
    key: str
    display_name: str
    loaded: bool
    context_length: int | None = None


class LMStudioClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._resolved_model: str | None = None

    @property
    def provider_name(self) -> str:
        return "LM Studio"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        usage: GenerationUsage | None = None,
        max_tokens: int | None = None,
        route: str = "default",
    ) -> str:
        """route игнорируется: локальная модель бесплатна — маршрутизация не применяется."""
        started = time.monotonic()
        model = await self._resolve_model()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": self._settings.llm_temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._settings.llm_max_tokens,
            "stream": False,
        }

        data = None
        for attempt in range(1, LLM_GENERATE_MAX_ATTEMPTS + 1):
            attempt_started = time.monotonic()
            async with httpx.AsyncClient(timeout=LLM_GENERATE_TIMEOUT_SEC) as client:
                try:
                    response = await client.post(
                        f"{self._settings.lmstudio_base_url}/v1/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
                    _raise_for_status(response, "LM Studio")
                except httpx.ConnectError as exc:
                    raise RuntimeError(_lmstudio_connection_error(self._settings.lmstudio_base_url)) from exc
                except httpx.ReadTimeout as exc:
                    attempt_duration = time.monotonic() - attempt_started
                    if attempt >= LLM_GENERATE_MAX_ATTEMPTS:
                        raise RuntimeError(
                            _lmstudio_timeout_error(
                                base_url=self._settings.lmstudio_base_url,
                                timeout_sec=LLM_GENERATE_TIMEOUT_SEC,
                                attempts=LLM_GENERATE_MAX_ATTEMPTS,
                            )
                        ) from exc

                    logger.warning(
                        "llm.generate.timeout_retry provider=lmstudio model=%s attempt=%s/%s "
                        "timeout_sec=%s prompt_chars=%s duration_sec=%.1f retry_delay_sec=%s",
                        model,
                        attempt,
                        LLM_GENERATE_MAX_ATTEMPTS,
                        LLM_GENERATE_TIMEOUT_SEC,
                        len(prompt),
                        attempt_duration,
                        LLM_GENERATE_RETRY_DELAY_SEC,
                    )
                    await asyncio.sleep(LLM_GENERATE_RETRY_DELAY_SEC)
                    continue
                data = response.json()
                if attempt > 1:
                    logger.info(
                        "llm.generate.retry_success provider=lmstudio model=%s attempt=%s/%s",
                        model,
                        attempt,
                        LLM_GENERATE_MAX_ATTEMPTS,
                    )
                break

        if data is None:
            raise RuntimeError("LM Studio не вернул ответ после повторных попыток.")

        duration_sec = time.monotonic() - started
        choices = data.get("choices", [])
        if not choices:
            result = ""
            finish_reason = ""
        else:
            message = choices[0].get("message", {})
            result = _strip_thinking(str(message.get("content", ""))).strip()
            finish_reason = str(choices[0].get("finish_reason") or "")
        if usage is not None:
            usage.last_finish_reason = finish_reason or None

        usage_data = data.get("usage") or {}
        prompt_tokens = int(usage_data.get("prompt_tokens") or 0)
        completion_tokens = int(usage_data.get("completion_tokens") or 0)
        total_tokens = int(usage_data.get("total_tokens") or (prompt_tokens + completion_tokens))
        if usage is not None:
            usage.add(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                duration_sec=duration_sec,
            )

        logger.info(
            "llm.generate.done provider=lmstudio model=%s prompt_chars=%s response_chars=%s "
            "prompt_tokens=%s completion_tokens=%s total_tokens=%s duration_sec=%.1f",
            model,
            len(prompt),
            len(result),
            prompt_tokens,
            completion_tokens,
            total_tokens,
            duration_sec,
        )
        return result

    async def list_models(self) -> list[str]:
        models = await self._list_lmstudio_models()
        if models:
            return [
                _format_lmstudio_model(model)
                for model in models
            ]

        # Older LM Studio setups may expose only the OpenAI-compatible list.
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self._settings.lmstudio_base_url}/v1/models", headers=self._headers())
                _raise_for_status(response, "LM Studio")
            except httpx.ConnectError as exc:
                raise RuntimeError(_lmstudio_connection_error(self._settings.lmstudio_base_url)) from exc
            data = response.json()
        return [str(item.get("id")) for item in data.get("data", []) if item.get("id")]

    async def active_model(self) -> str:
        return await self._resolve_model()

    async def loaded_context_length(self) -> int | None:
        try:
            model_key = await self._resolve_model()
        except Exception:
            return None
        try:
            models = await self._list_lmstudio_models()
        except Exception:
            return None
        for model in models:
            if model.key == model_key and model.loaded and model.context_length:
                return model.context_length
        # если точного совпадения по ключу нет, но есть одна загруженная модель — вернём её
        loaded = [m for m in models if m.loaded and m.context_length]
        if len(loaded) == 1:
            return loaded[0].context_length
        return None

    async def _resolve_model(self) -> str:
        if self._resolved_model:
            return self._resolved_model

        configured = self._settings.lmstudio_model
        if configured and configured.lower() != "auto":
            models = await self._list_lmstudio_models()
            for model in models:
                if model.key == configured and model.loaded:
                    self._resolved_model = configured
                    logger.info("llm.model.resolved provider=lmstudio model=%s source=configured_loaded", configured)
                    return configured

            if self._settings.lmstudio_auto_load:
                logger.info("llm.model.load.start provider=lmstudio model=%s source=configured", configured)
                await self._load_model(configured)
                logger.info("llm.model.load.done provider=lmstudio model=%s source=configured", configured)
            self._resolved_model = configured
            logger.info("llm.model.resolved provider=lmstudio model=%s source=configured", configured)
            return configured

        models = await self._list_lmstudio_models()
        loaded = [model for model in models if model.loaded]
        if loaded:
            self._resolved_model = loaded[0].key
            logger.info("llm.model.resolved provider=lmstudio model=%s source=loaded", loaded[0].key)
            return loaded[0].key

        if self._settings.lmstudio_auto_load and models:
            self._resolved_model = models[0].key
            logger.info("llm.model.load.start provider=lmstudio model=%s", models[0].key)
            await self._load_model(models[0].key)
            logger.info("llm.model.load.done provider=lmstudio model=%s", models[0].key)
            return models[0].key

        openai_models = await self._list_openai_models()
        if openai_models:
            self._resolved_model = openai_models[0]
            logger.info("llm.model.resolved provider=lmstudio model=%s source=openai_models", openai_models[0])
            return openai_models[0]

        raise RuntimeError(
            "LM Studio не вернул доступных моделей. Запусти LM Studio server и загрузи модель "
            "или укажи LMSTUDIO_MODEL в .env."
        )

    async def _list_lmstudio_models(self) -> list[LMStudioModel]:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self._settings.lmstudio_base_url}/api/v1/models", headers=self._headers())
                _raise_for_status(response, "LM Studio")
            except httpx.HTTPError:
                return []
            data = response.json()

        models = []
        for item in data.get("models", []):
            if item.get("type") != "llm":
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            models.append(
                LMStudioModel(
                    key=key,
                    display_name=str(item.get("display_name") or key),
                    loaded=bool(item.get("loaded_instances")),
                    context_length=_loaded_context_length(item),
                )
            )
        return models

    async def _list_openai_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(f"{self._settings.lmstudio_base_url}/v1/models", headers=self._headers())
                _raise_for_status(response, "LM Studio")
            except httpx.ConnectError as exc:
                raise RuntimeError(_lmstudio_connection_error(self._settings.lmstudio_base_url)) from exc
            data = response.json()
        return [str(item.get("id")) for item in data.get("data", []) if item.get("id")]

    async def _load_model(self, model: str) -> None:
        payload = {
            "model": model,
            "context_length": self._settings.lmstudio_num_ctx,
        }
        async with httpx.AsyncClient(timeout=1200) as client:
            response = await client.post(
                f"{self._settings.lmstudio_base_url}/api/v1/models/load",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response, "LM Studio")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._settings.lmstudio_api_key:
            headers["Authorization"] = f"Bearer {self._settings.lmstudio_api_key}"
        return headers


def _lmstudio_connection_error(base_url: str) -> str:
    return (
        f"LM Studio server недоступен по адресу {base_url}. "
        "Открой LM Studio, включи Developer -> Start server на порту 1234 и загрузи chat/instruct модель."
    )


def _lmstudio_timeout_error(base_url: str, timeout_sec: int, attempts: int) -> str:
    minutes = max(1, round(timeout_sec / 60))
    return (
        f"LM Studio не вернул ответ по адресу {base_url} за {minutes} мин. "
        f"Попыток: {attempts}. Попробуй уменьшить размер чанка transcript, включить /no_think "
        "или использовать более лёгкую модель."
    )


def _format_lmstudio_model(model: LMStudioModel) -> str:
    state = "loaded" if model.loaded else "installed"
    context = f"; ctx={model.context_length}" if model.context_length else ""
    return f"{model.key} ({state}{context}; {model.display_name})"


def _loaded_context_length(item: dict) -> int | None:
    instances = item.get("loaded_instances") or []
    if not instances:
        return None
    config = instances[0].get("config") or {}
    value = config.get("context_length")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _raise_for_status(response: httpx.Response, provider: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text.strip().replace("\n", " ")
        if len(detail) > 800:
            detail = f"{detail[:800]}..."
        raise RuntimeError(f"{provider} вернул HTTP {response.status_code}: {detail}") from exc


def _strip_thinking(text: str) -> str:
    while "<think>" in text and "</think>" in text:
        start = text.find("<think>")
        end = text.find("</think>", start)
        text = f"{text[:start]}{text[end + len('</think>'):]}"
    return text
