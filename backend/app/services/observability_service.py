from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any


class CostBudgetExceededError(RuntimeError):
    """Raised when a workflow exceeds its configured LLM cost budget."""


@dataclass(frozen=True)
class LLMCallMetric:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    estimated_cost_usd: float
    retry_count: int
    fallback_activated: bool
    error_category: str | None
    workflow_id: str | None
    task_id: str | None
    recorded_at: str


class ObservabilityRegistry:
    """Thread-safe AI observability & per-workflow cost budget tracker (zero PII/prompt storage)."""

    def __init__(self, *, max_events: int = 500, default_workflow_budget_usd: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._max_events = max_events
        self._default_budget_usd = default_workflow_budget_usd
        self._events: list[LLMCallMetric] = []
        self._workflow_costs: dict[str, float] = {}

    def check_budget(self, workflow_id: str | None, *, budget_usd: float | None = None) -> None:
        if not workflow_id:
            return
        limit = budget_usd if budget_usd is not None else self._default_budget_usd
        with self._lock:
            spent = self._workflow_costs.get(workflow_id, 0.0)
            if spent >= limit:
                raise CostBudgetExceededError(
                    f"Workflow '{workflow_id}' exceeded cost budget (${spent:.4f} >= ${limit:.4f})"
                )

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        estimated_cost_usd: float,
        retry_count: int = 0,
        fallback_activated: bool = False,
        error_category: str | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> LLMCallMetric:
        metric = LLMCallMetric(
            provider=provider,
            model=model,
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
            latency_ms=round(max(0.0, float(latency_ms)), 2),
            estimated_cost_usd=round(max(0.0, float(estimated_cost_usd)), 6),
            retry_count=max(0, int(retry_count)),
            fallback_activated=bool(fallback_activated),
            error_category=error_category,
            workflow_id=workflow_id,
            task_id=task_id,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._events.insert(0, metric)
            if len(self._events) > self._max_events:
                self._events = self._events[: self._max_events]
            if workflow_id:
                self._workflow_costs[workflow_id] = round(
                    self._workflow_costs.get(workflow_id, 0.0) + metric.estimated_cost_usd, 6
                )
        return metric

    def summary(self) -> dict[str, Any]:
        with self._lock:
            total_cost = round(sum(e.estimated_cost_usd for e in self._events), 6)
            total_tokens = sum(e.input_tokens + e.output_tokens for e in self._events)
            fallbacks = sum(1 for e in self._events if e.fallback_activated)
            errors = sum(1 for e in self._events if e.error_category)
            return {
                "total_calls": len(self._events),
                "total_tokens": total_tokens,
                "total_estimated_cost_usd": total_cost,
                "fallback_activations": fallbacks,
                "error_count": errors,
                "recent_calls": [e.__dict__ for e in self._events[:20]],
            }


observability_registry = ObservabilityRegistry()
