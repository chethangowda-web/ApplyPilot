"use client";

import React from "react";
import { AlertTriangle, Ban, Check, Clock, Lock } from "lucide-react";
import type { DerivedWorkflowState, StepVisualState } from "@/lib/workflow-engine";
import { StatusBadge } from "@/components/StatusBadge";

export interface WorkflowStepperProps {
  derived: DerivedWorkflowState;
  activeViewStep: number;
  onSelectStep: (stepNumber: number) => void;
}

function StepIcon({
  state,
  stepNumber,
  isViewing,
}: {
  state: StepVisualState;
  stepNumber: number;
  isViewing: boolean;
}) {
  const ring = isViewing ? "ring-2 ring-[var(--accent-primary)] ring-offset-2 ring-offset-[var(--bg-rail)]" : "";
  if (state === "completed") {
    return (
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] text-[var(--state-completed-fg)] ${ring}`}
      >
        <Check className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  if (state === "waiting") {
    return (
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--state-waiting-border)] bg-[var(--state-waiting-bg)] text-[var(--state-waiting-fg)] ${ring}`}
      >
        <Clock className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  if (state === "blocked") {
    return (
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--state-blocked-border)] bg-[var(--state-blocked-bg)] text-[var(--state-blocked-fg)] ${ring}`}
      >
        <Ban className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  if (state === "failed") {
    return (
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] text-[var(--state-failed-fg)] ${ring}`}
      >
        <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  if (state === "current") {
    return (
      <span
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--accent-primary)] bg-[var(--accent-primary)] text-xs font-semibold text-white tabular-nums ${ring}`}
      >
        {stepNumber}
      </span>
    );
  }
  return (
    <span
      className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-[var(--border-hairline)] bg-[var(--bg-surface)] text-xs font-medium text-[var(--ink-muted)] tabular-nums ${ring}`}
    >
      {stepNumber}
    </span>
  );
}

export function WorkflowStepper({
  derived,
  activeViewStep,
  onSelectStep,
}: WorkflowStepperProps) {
  return (
    <aside
      aria-label="Application workflow progress"
      className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-4 sm:p-5"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-[var(--ink-muted)]">
            Workflow Progress
          </p>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-2xl font-semibold text-[var(--ink-primary)] tabular-nums">
              {derived.progressPercent}%
            </span>
            <span className="whitespace-nowrap text-xs text-[var(--ink-secondary)] tabular-nums">
              {derived.completedCount} / {derived.stages.length} steps
            </span>
          </div>
        </div>
        <StatusBadge state={derived.overallState} customLabel={derived.backendStatusLabel} mono />
      </div>

      <div
        role="progressbar"
        aria-valuenow={derived.progressPercent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Workflow completion percentage"
        className="mt-3 h-2 w-full overflow-hidden rounded-full bg-[var(--bg-subtle)]"
      >
        <div
          className={`h-full transition-all duration-300 ${
            derived.overallState === "failed"
              ? "bg-[var(--state-failed-fg)]"
              : derived.overallState === "blocked"
                ? "bg-[var(--state-blocked-fg)]"
                : derived.overallState === "waiting"
                  ? "bg-[var(--state-waiting-fg)]"
                  : "bg-[var(--state-completed-fg)]"
          }`}
          style={{ width: `${derived.progressPercent}%` }}
        />
      </div>

      <p className="mt-3 rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-2 text-xs leading-relaxed text-[var(--ink-secondary)]">
        <span className="font-semibold text-[var(--ink-primary)]">Next prerequisite: </span>
        {derived.nextActionRequired}
      </p>

      <ol className="mt-4 space-y-1.5">
        {derived.stages.map((stage) => {
          const num = stage.definition.stepNumber;
          const isViewing = num === activeViewStep;
          const isAuthoritative = num === derived.authoritativeStep;
          const locked = !stage.canInspect && stage.state === "upcoming";

          return (
            <li key={stage.definition.id}>
              <button
                type="button"
                disabled={locked}
                onClick={() => onSelectStep(num)}
                aria-current={isViewing ? "step" : undefined}
                title={locked ? stage.blockerReason ?? stage.definition.gateRule : stage.statusSummary}
                className={`w-full rounded-xl px-3 py-2.5 text-left transition-colors flex items-start gap-3 ${
                  isViewing
                    ? "bg-[var(--bg-surface)] shadow-[inset_0_0_0_1px_var(--accent-primary-border)]"
                    : locked
                      ? "cursor-not-allowed opacity-60"
                      : "hover:bg-[var(--bg-surface)]/80"
                }`}
              >
                <StepIcon state={stage.state} stepNumber={num} isViewing={isViewing} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs font-semibold text-[var(--ink-primary)]">
                      {num}. {stage.definition.shortTitle}
                    </span>
                    {locked ? (
                      <Lock className="h-3 w-3 shrink-0 text-[var(--ink-muted)]" aria-hidden="true" />
                    ) : isAuthoritative ? (
                      <span className="shrink-0 rounded bg-[var(--accent-primary-soft)] px-1.5 py-0.5 text-[10px] font-semibold text-[var(--accent-primary)]">
                        Active
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-[var(--ink-muted)]">
                    {stage.statusSummary}
                  </p>
                </div>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
