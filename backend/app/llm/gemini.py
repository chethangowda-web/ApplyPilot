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


class GeminiProvider(BaseLLMProvider):
    """Google Gemini API provider (primary discovery & JD extraction model)."""

    provider_name = "gemini"
    default_model = "gemini-2.5-flash"
    default_embed_model = "text-embedding-004"

    def __init__(
        self,
        api_key: str = "",
        default_model: str | None = None,
        *,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
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
            raise ProviderNotConfiguredError("GEMINI_API_KEY is not configured")

        target_model = model or self.default_model
        system_parts = [m.content for m in messages if m.role == "system"]
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]
        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        if schema is not None:
            body["generationConfig"] = {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema.model_json_schema(),
            }

        url = f"{self.base_url}/models/{target_model}:generateContent"
        start = time.monotonic()
        try:
            if self._client is not None:
                resp = await self._client.post(
                    url,
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                    timeout=timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(
                        url,
                        headers={"x-goog-api-key": self._api_key},
                        json=body,
                    )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Gemini request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(scrub_api_key(f"Gemini transport error: {type(exc).__name__}", self._api_key)) from exc

        latency_ms = round((time.monotonic() - start) * 1000, 2)
        if resp.status_code >= 400:
            raise ProviderError(scrub_api_key(f"Gemini HTTP {resp.status_code}", self._api_key))

        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Malformed Gemini response structure") from exc

        usage_meta = data.get("usageMetadata") or {}
        prompt_tok = int(usage_meta.get("promptTokenCount") or 0)
        comp_tok = int(usage_meta.get("candidatesTokenCount") or 0)
        total_tok = int(usage_meta.get("totalTokenCount") or (prompt_tok + comp_tok))
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
                raise ProviderError(f"Gemini failed schema validation for {schema.__name__}") from exc

        return LLMResponse(content=text, parsed=parsed_obj, usage=usage)

    async def embed(
        self,
        *,
        texts: list[str],
        dimensions: int = 768,
        timeout_seconds: float = 15.0,
    ) -> list[list[float]]:
        if not self.is_configured:
            raise ProviderNotConfiguredError("GEMINI_API_KEY is not configured")
        if not texts:
            return []

        url = f"{self.base_url}/models/{self.default_embed_model}:batchEmbedContents"
        body = {
            "requests": [
                {
                    "model": f"models/{self.default_embed_model}",
                    "content": {"parts": [{"text": t}]},
                    "outputDimensionality": dimensions,
                }
                for t in texts
            ]
        }
        try:
            if self._client is not None:
                resp = await self._client.post(
                    url,
                    headers={"x-goog-api-key": self._api_key},
                    json=body,
                    timeout=timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    resp = await client.post(
                        url,
                        headers={"x-goog-api-key": self._api_key},
                        json=body,
                    )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Gemini embedding request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(scrub_api_key(f"Gemini embedding error: {type(exc).__name__}", self._api_key)) from exc

        if resp.status_code >= 400:
            raise ProviderError(scrub_api_key(f"Gemini embedding HTTP {resp.status_code}", self._api_key))

        data = resp.json()
        embeddings = data.get("embeddings") or []
        return [list(item["values"]) for item in embeddings]
