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


class ClaudeProvider(BaseLLMProvider):
    """Anthropic Claude provider interface."""

    provider_name = "claude"
    default_model = "claude-sonnet-4-5"

    def __init__(
        self,
        api_key: str = "",
        default_model: str | None = None,
        *,
        base_url: str = "https://api.anthropic.com/v1",
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
            raise ProviderNotConfiguredError("ANTHROPIC_API_KEY is not configured")

        target_model = model or self.default_model
        system_prompt = "\n".join(m.content for m in messages if m.role == "system")
        if schema is not None:
            schema_hint = json.dumps(schema.model_json_schema())
            system_prompt = f"{system_prompt}\nRespond ONLY with valid JSON matching schema: {schema_hint}".strip()

        conv = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        body: dict[str, Any] = {
            "model": target_model,
            "max_tokens": 2048,
            "messages": conv,
        }
        if system_prompt:
            body["system"] = system_prompt

        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        start = time.monotonic()
        try:
            if self._client is not None:
                resp = await self._client.post(url, headers=headers, json=body, timeout=timeout_seconds)
            else:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Claude request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(scrub_api_key(f"Claude transport error: {type(exc).__name__}", self._api_key)) from exc

        latency_ms = round((time.monotonic() - start) * 1000, 2)
        if resp.status_code >= 400:
            raise ProviderError(scrub_api_key(f"Claude HTTP {resp.status_code}", self._api_key))

        data = resp.json()
        try:
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Malformed Claude response structure") from exc

        usage_raw = data.get("usage") or {}
        prompt_tok = int(usage_raw.get("input_tokens") or 0)
        comp_tok = int(usage_raw.get("output_tokens") or 0)
        usage = LLMUsage(
            provider=self.provider_name,
            model=target_model,
            prompt_tokens=prompt_tok,
            completion_tokens=comp_tok,
            total_tokens=prompt_tok + comp_tok,
            estimated_cost_usd=estimate_cost_usd(
                prompt_tokens=prompt_tok,
                completion_tokens=comp_tok,
                input_per_1m=3.0,
                output_per_1m=15.0,
            ),
            latency_ms=latency_ms,
        )

        parsed_obj: BaseModel | None = None
        if schema is not None:
            try:
                parsed_obj = schema.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ProviderError(f"Claude failed schema validation for {schema.__name__}") from exc

        return LLMResponse(content=text, parsed=parsed_obj, usage=usage)
