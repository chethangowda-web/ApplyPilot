"use client";

import React from "react";
import { AppShell } from "@/components/AppShell";
import { MyWorkflowsView } from "@/components/MyWorkflowsView";
import { useSessionUser } from "@/lib/use-session-user";

export default function HomePage() {
  const { user, authChecked } = useSessionUser("/");

  return (
    <AppShell user={user} headerNote="Backend-Persisted Sequential Workflows">
      {authChecked ? (
        <MyWorkflowsView />
      ) : (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-sm text-[var(--ink-secondary)]">
          Checking authenticated session...
        </div>
      )}
    </AppShell>
  );
}
