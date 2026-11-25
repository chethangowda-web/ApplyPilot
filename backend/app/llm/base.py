from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ProviderError(Exception):
    """Base error raised by an LLM provider."""


class ProviderTimeoutError(ProviderError):
    """Raised when an LLM request times out."""


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider has no configured API key."""


@dataclass(frozen=True)
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMUsage:
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class LLMResponse:
    content: str
    parsed: BaseModel | None = None
    usage: LLMUsage = field(default_factory=lambda: LLMUsage(provider="unknown", model="unknown"))
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def estimate_cost_usd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    input_per_1m: float,
    output_per_1m: float,
) -> float:
    return round(
        (prompt_tokens / 1_000_000.0) * input_per_1m + (completion_tokens / 1_000_000.0) * output_per_1m,
        8,
    )


def scrub_api_key(message: str, api_key: str) -> str:
    """Ensure raw API keys never leak into exception strings or logs."""
    if api_key and api_key in message:
        return message.replace(api_key, "[REDACTED_API_KEY]")
    return message


class BaseLLMProvider(ABC):
    """Provider-independent interface for text, structured Pydantic output, and embeddings."""

    provider_name: str
    default_model: str

    def __init__(self, api_key: str = "", default_model: str | None = None) -> None:
        self._api_key = api_key.strip()
        if default_model:
            self.default_model = default_model

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.default_model} configured={self.is_configured}>"

    @abstractmethod
    async def generate(
        self,
        *,
        messages: list[LLMMessage],
        schema: type[T] | None = None,
        model: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> LLMResponse:
        """Generate text or validated structured Pydantic output."""

    async def embed(
        self,
        *,
        texts: list[str],
        dimensions: int = 768,
        timeout_seconds: float = 15.0,
    ) -> list[list[float]]:
        """Generate dense vector embeddings for input texts."""
        raise ProviderError(f"Embeddings not supported by provider {self.provider_name}")
