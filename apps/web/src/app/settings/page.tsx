"use client";

import React, { useEffect, useState } from "react";
import { apiClient } from "@/lib/api-client";
import type { HealthResponse } from "@/lib/types";
import { useSessionUser } from "@/lib/use-session-user";
import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";

export default function SettingsPage() {
  const { user } = useSessionUser("/settings");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    apiClient
      .getHealth()
      .then(setHealth)
      .catch((err) => setHealthError(err instanceof Error ? err.message : "Backend unreachable"));
  }, []);

  return (
    <AppShell user={user} headerNote="Backend-Enforced Policy & Session">
      <div className="space-y-6 max-w-3xl">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink-primary)]">
            Settings
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-secondary)]">
            Session identity, FastAPI connectivity, and server-side automation guardrails.
          </p>
        </div>

        <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-3 text-xs">
          <h2 className="text-sm font-semibold text-[var(--ink-primary)]">Authenticated Account</h2>
          {user ? (
            <dl className="grid gap-3 sm:grid-cols-2">
              <div>
                <dt className="text-[var(--ink-muted)]">Email</dt>
                <dd className="mt-0.5 font-medium text-[var(--ink-primary)]">{user.email}</dd>
              </div>
              <div>
                <dt className="text-[var(--ink-muted)]">Timezone</dt>
                <dd className="mt-0.5 font-mono text-[var(--ink-primary)]">{user.timezone}</dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-[var(--ink-muted)]">User ID (Server-Derived)</dt>
                <dd className="mt-0.5 font-mono text-[var(--ink-secondary)]">{user.id}</dd>
              </div>
            </dl>
          ) : (
            <p className="text-[var(--ink-muted)]">Loading session user...</p>
          )}
        </section>

        <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-3 text-xs">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-[var(--ink-primary)]">
              FastAPI Backend Health
            </h2>
            {health ? (
              <StatusBadge state="completed" customLabel={health.status.toUpperCase()} mono />
            ) : (
              <StatusBadge state="failed" customLabel="UNREACHABLE" mono />
            )}
          </div>
          {health ? (
            <p className="text-[var(--ink-secondary)]">
              Service: <code className="font-mono">{health.app}</code> · Environment:{" "}
              <code className="font-mono">{health.environment}</code> · Timezone:{" "}
              <code className="font-mono">{health.timezone}</code>
            </p>
          ) : (
            <p className="text-[var(--state-failed-fg)]">{healthError || "Checking..."}</p>
          )}
        </section>

        <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-5 space-y-2 text-xs text-[var(--ink-secondary)]">
          <h2 className="text-sm font-semibold text-[var(--ink-primary)]">
            Workflow Persistence & Security Guarantees
          </h2>
          <ul className="list-disc pl-4 space-y-1">
            <li>
              Workflow completion state is derived strictly from FastAPI backend records—never stored authoritatively in <code className="font-mono">localStorage</code>.
            </li>
            <li>
              Authentication uses an <code className="font-mono">HttpOnly</code> cookie proxy rather than browser-readable tokens.
            </li>
            <li>
              Resume approvals, application approvals, sensitive answers, and Hold-and-Confirm submission releases always require explicit human confirmation.
            </li>
          </ul>
        </section>
      </div>
    </AppShell>
  );
}
