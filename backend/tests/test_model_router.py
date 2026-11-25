import json
import logging
import httpx
import pytest
from pydantic import BaseModel

from app.llm.base import LLMMessage, ProviderError
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.llm.openai import OpenAICompatibleProvider
from app.llm.router import ModelRouter


class SampleExtraction(BaseModel):
    role: str
    skills: list[str]


@pytest.mark.asyncio
async def test_model_router_gemini_primary_structured_output_and_cost(caplog):
    caplog.set_level(logging.INFO)

    def gem_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-goog-api-key") == "secret-gem-key-123"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps({"role": "AI Engineer", "skills": ["Python", "LangGraph"]})}]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 200, "candidatesTokenCount": 50, "totalTokenCount": 250},
            },
        )

    gem = GeminiProvider(api_key="secret-gem-key-123", client=httpx.AsyncClient(transport=httpx.MockTransport(gem_handler)))
    router = ModelRouter(providers={"gemini": gem})

    resp = await router.generate(
        task="jd_extraction",
        messages=[LLMMessage(role="user", content="Extract JD")],
        schema=SampleExtraction,
    )
    assert isinstance(resp.parsed, SampleExtraction)
    assert resp.parsed.role == "AI Engineer"
    assert router.usage_summary.total_tokens == 250
    assert router.usage_summary.total_cost_usd > 0
    assert "secret-gem-key-123" not in caplog.text


@pytest.mark.asyncio
async def test_model_router_retry_and_fallback_from_gemini_to_openai():
    gem_calls = {"n": 0}

    def failing_gem(request: httpx.Request) -> httpx.Response:
        gem_calls["n"] += 1
        return httpx.Response(503, json={"error": "upstream unavailable"})

    def ok_openai(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"role": "Backend Engineer", "skills": ["FastAPI"]})}}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
            },
        )

    gem = GeminiProvider(api_key="gem-secret-xyz", client=httpx.AsyncClient(transport=httpx.MockTransport(failing_gem)))
    oai = OpenAICompatibleProvider(api_key="oai-secret-xyz", client=httpx.AsyncClient(transport=httpx.MockTransport(ok_openai)))
    claude = ClaudeProvider(api_key="")  # unconfigured, skipped

    router = ModelRouter(providers={"gemini": gem, "openai": oai, "claude": claude}, max_retries_per_provider=1)
    resp = await router.generate(
        task="jd_extraction",
        messages=[LLMMessage(role="user", content="Extract JD")],
        schema=SampleExtraction,
    )
    assert gem_calls["n"] == 2
    assert resp.usage.provider == "openai"
    assert resp.parsed and resp.parsed.role == "Backend Engineer"


@pytest.mark.asyncio
async def test_model_router_timeout_and_never_leaks_api_key():
    def timeout_claude(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    claude = ClaudeProvider(
        api_key="sk-ant-very-secret-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout_claude)),
    )
    router = ModelRouter(providers={"claude": claude}, max_retries_per_provider=0)
    with pytest.raises(ProviderError) as exc:
        await router.generate(task="resume_writing", messages=[LLMMessage(role="user", content="Hi")])
    assert "sk-ant-very-secret-key" not in str(exc.value)
    assert "sk-ant-very-secret-key" not in repr(claude)
