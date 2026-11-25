import asyncio
from dataclasses import dataclass, field
from typing import TypeVar
from pydantic import BaseModel
from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, LLMUsage, ProviderError
from app.llm.claude import ClaudeProvider
from app.llm.gemini import GeminiProvider
from app.llm.openai import OpenAICompatibleProvider

T = TypeVar("T", bound=BaseModel)
logger = get_logger("applypilot.llm_router")

DEFAULT_TASK_ROUTING: dict[str, list[str]] = {
    "jd_extraction": ["gemini", "openai", "claude"],
    "discovery": ["gemini", "openai", "claude"],
    "resume_writing": ["claude", "openai", "gemini"],
    "evaluation": ["openai", "gemini", "claude"],
    "reasoning": ["openai", "gemini", "claude"],
}


@dataclass
class UsageSummary:
    calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    history: list[LLMUsage] = field(default_factory=list)

    def record(self, usage: LLMUsage) -> None:
        self.calls += 1
        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens
        self.total_cost_usd = round(self.total_cost_usd + usage.estimated_cost_usd, 8)
        self.history.append(usage)


class ModelRouter:
    """Central multi-model router supporting task specialization, retries, fallback, and cost tracking."""

    def __init__(
        self,
        *,
        providers: dict[str, BaseLLMProvider] | None = None,
        task_routing: dict[str, list[str]] | None = None,
        max_retries_per_provider: int = 1,
        timeout_seconds: float = 20.0,
    ) -> None:
        if providers is not None:
            self.providers = providers
        else:
            settings = get_settings()
            self.providers = {
                "gemini": GeminiProvider(api_key=settings.gemini_api_key),
                "claude": ClaudeProvider(api_key=settings.anthropic_api_key),
                "openai": OpenAICompatibleProvider(api_key=settings.openai_api_key),
            }
        self.task_routing = task_routing or DEFAULT_TASK_ROUTING
        self.max_retries_per_provider = max_retries_per_provider
        self.timeout_seconds = timeout_seconds
        self.usage_summary = UsageSummary()

    def select_provider_chain(self, task: str) -> list[BaseLLMProvider]:
        names = self.task_routing.get(task, ["gemini", "openai", "claude"])
        chain: list[BaseLLMProvider] = []
        for name in names:
            prov = self.providers.get(name)
            if prov is not None and prov.is_configured:
                chain.append(prov)
        return chain

    async def generate(
        self,
        *,
        task: str,
        messages: list[LLMMessage],
        schema: type[T] | None = None,
    ) -> LLMResponse:
        chain = self.select_provider_chain(task)
        if not chain:
            raise ProviderError(f"No configured LLM providers available for task '{task}'")

        errors: list[str] = []
        for idx, provider in enumerate(chain):
            for attempt in range(self.max_retries_per_provider + 1):
                try:
                    response = await provider.generate(
                        messages=messages,
                        schema=schema,
                        timeout_seconds=self.timeout_seconds,
                    )
                    self.usage_summary.record(response.usage)
                    logger.info(
                        "llm_call_succeeded",
                        extra={
                            "extra_fields": {
                                "task": task,
                                "provider": provider.provider_name,
                                "model": response.usage.model,
                                "total_tokens": response.usage.total_tokens,
                                "estimated_cost_usd": response.usage.estimated_cost_usd,
                                "fallback_used": idx > 0,
                            }
                        },
                    )
                    return response
                except ProviderError as exc:
                    errors.append(f"{provider.provider_name}[attempt={attempt + 1}]: {exc}")
                    logger.warning(
                        "llm_provider_attempt_failed",
                        extra={
                            "extra_fields": {
                                "task": task,
                                "provider": provider.provider_name,
                                "attempt": attempt + 1,
                                "error": str(exc),
                            }
                        },
                    )
                    if attempt < self.max_retries_per_provider:
                        await asyncio.sleep(0.05 * (2**attempt))

        raise ProviderError(f"All providers failed for task '{task}': {'; '.join(errors)}")

    async def embed(self, texts: list[str], *, dimensions: int = 768) -> list[list[float]]:
        for name in ("gemini", "openai"):
            prov = self.providers.get(name)
            if prov is not None and prov.is_configured:
                try:
                    return await prov.embed(texts=texts, dimensions=dimensions, timeout_seconds=self.timeout_seconds)
                except ProviderError as exc:
                    logger.warning(
                        "embedding_provider_failed",
                        extra={"extra_fields": {"provider": name, "error": str(exc)}},
                    )
        raise ProviderError("No configured embedding provider succeeded")
