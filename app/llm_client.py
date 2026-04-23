from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Protocol

import httpx

from app.config import Settings


logger = logging.getLogger(__name__)
LLM_GENERATE_TIMEOUT_SEC = 1200
LLM_GENERATE_MAX_ATTEMPTS = 2
LLM_GENERATE_RETRY_DELAY_SEC = 15


@dataclass
class GenerationUsage:
    """Mutable accumulator для метрик LLM-вызовов в рамках одной задачи."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    duration_sec: float = 0.0

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
    ) -> str:
        ...

    async def list_models(self) -> list[str]:
        ...

    async def active_model(self) -> str:
        ...

    async def loaded_context_length(self) -> int | None:
        ...


def create_llm_client(settings: Settings) -> LLMClient:
    return LMStudioClient(settings)


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
    ) -> str:
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
            "max_tokens": self._settings.llm_max_tokens,
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
        else:
            message = choices[0].get("message", {})
            result = _strip_thinking(str(message.get("content", ""))).strip()

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
