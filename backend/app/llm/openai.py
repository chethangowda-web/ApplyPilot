import json
import time
from typing import Any, TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from app.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    LLMUsage,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    estimate_cost_usd,
    scrub_api_key,
)

T = TypeVar("T", bound=BaseModel)


class OpenAICompatibleProvider(BaseLLMProvider):
    """OpenAI-compatible provider interface (supports OpenAI and compatible endpoints)."""

    provider_name = "openai"
    default_model = "gpt-4o-mini"
    default_embed_model = "text-embedding-3-small"

    def __init__(
        self,
        api_key: str = "",
        default_model: str | None = None,
        *,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key=api_key, default_model=default_model)
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def generate(
        self,
        *,
        messages: list[LLMMessage],
        schema: type[T] | None = None,
        model: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> LLMResponse:
        if not self.is_configured:
            raise ProviderNotConfiguredError("OPENAI_API_KEY is not configured")

        target_model = model or self.default_model
        msg_payload = [{"role": m.role, "content": m.content} for m in messages]
        body: dict[str, Any] = {"model": target_model, "messages": msg_payload}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                },
            }

        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        start = time.monotonic()
        try:
            if self._client is not None:
                resp = await self._client.post(url, headers=headers, json=body, timeout=timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("OpenAI-compatible request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(scrub_api_key(f"OpenAI transport error: {type(exc).__name__}", self._api_key)) from exc

        latency_ms = round((time.monotonic() - start) * 1000, 2)
        if resp.status_code >= 400:
            raise ProviderError(scrub_api_key(f"OpenAI HTTP {resp.status_code}", self._api_key))

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Malformed OpenAI response structure") from exc

        usage_raw = data.get("usage") or {}
        prompt_tok = int(usage_raw.get("prompt_tokens") or 0)
        comp_tok = int(usage_raw.get("completion_tokens") or 0)
        total_tok = int(usage_raw.get("total_tokens") or (prompt_tok + comp_tok))
        usage = LLMUsage(
            provider=self.provider_name,
            model=target_model,
            prompt_tokens=prompt_tok,
            completion_tokens=comp_tok,
            total_tokens=total_tok,
            estimated_cost_usd=estimate_cost_usd(
                prompt_tokens=prompt_tok,
                completion_tokens=comp_tok,
                input_per_1m=0.15,
                output_per_1m=0.60,
            ),
            latency_ms=latency_ms,
        )

        parsed_obj: BaseModel | None = None
        if schema is not None:
            try:
                parsed_obj = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ProviderError(f"OpenAI failed schema validation for {schema.__name__}") from exc

        return LLMResponse(content=text, parsed=parsed_obj, usage=usage)

    async def embed(
        self,
        *,
        texts: list[str],
        dimensions: int = 768,
        timeout_seconds: float = 15.0,
    ) -> list[list[float]]:
        if not self.is_configured:
            raise ProviderNotConfiguredError("OPENAI_API_KEY is not configured")
        if not texts:
            return []

        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = {"model": self.default_embed_model, "input": texts, "dimensions": dimensions}
        try:
            if self._client is not None:
                resp = await self._client.post(url, headers=headers, json=body, timeout=timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("OpenAI embedding timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(scrub_api_key(f"OpenAI embedding error: {type(exc).__name__}", self._api_key)) from exc

        if resp.status_code >= 400:
            raise ProviderError(scrub_api_key(f"OpenAI embedding HTTP {resp.status_code}", self._api_key))

        data = resp.json()
        return [list(item["embedding"]) for item in data.get("data", [])]
