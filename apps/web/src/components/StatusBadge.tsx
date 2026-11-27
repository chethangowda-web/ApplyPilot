import type { StepVisualState } from "@/lib/workflow-engine";

const STATE_STYLES: Record<
  StepVisualState,
  { label: string; bg: string; fg: string; border: string; dot: string }
> = {
  completed: {
    label: "Completed",
    bg: "bg-[var(--state-completed-bg)]",
    fg: "text-[var(--state-completed-fg)]",
    border: "border-[var(--state-completed-border)]",
    dot: "bg-[var(--state-completed-fg)]",
  },
  current: {
    label: "In Progress",
    bg: "bg-[var(--accent-primary-soft)]",
    fg: "text-[var(--accent-primary)]",
    border: "border-[var(--accent-primary-border)]",
    dot: "bg-[var(--accent-primary)]",
  },
  waiting: {
    label: "Action Needed",
    bg: "bg-[var(--state-waiting-bg)]",
    fg: "text-[var(--state-waiting-fg)]",
    border: "border-[var(--state-waiting-border)]",
    dot: "bg-[var(--state-waiting-fg)]",
  },
  blocked: {
    label: "Blocked",
    bg: "bg-[var(--state-blocked-bg)]",
    fg: "text-[var(--state-blocked-fg)]",
    border: "border-[var(--state-blocked-border)]",
    dot: "bg-[var(--state-blocked-fg)]",
  },
  failed: {
    label: "Failed",
    bg: "bg-[var(--state-failed-bg)]",
    fg: "text-[var(--state-failed-fg)]",
    border: "border-[var(--state-failed-border)]",
    dot: "bg-[var(--state-failed-fg)]",
  },
  upcoming: {
    label: "Upcoming",
    bg: "bg-[var(--state-upcoming-bg)]",
    fg: "text-[var(--state-upcoming-fg)]",
    border: "border-[var(--state-upcoming-border)]",
    dot: "bg-[var(--state-upcoming-fg)]",
  },
};

export interface StatusBadgeProps {
  state: StepVisualState;
  customLabel?: string;
  mono?: boolean;
}

export function StatusBadge({ state, customLabel, mono = false }: StatusBadgeProps) {
  const s = STATE_STYLES[state];
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${s.bg} ${s.fg} ${s.border} ${mono ? "font-mono tracking-tight" : ""}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden="true" />
      <span>{customLabel ?? s.label}</span>
    </span>
  );
}
