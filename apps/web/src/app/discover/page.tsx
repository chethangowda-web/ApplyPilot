"use client";

import React from "react";
import { useSessionUser } from "@/lib/use-session-user";
import { AppShell } from "@/components/AppShell";
import { DiscoverJobsView } from "@/components/DiscoverJobsView";

export default function DiscoverJobsPage() {
  const { user, authChecked } = useSessionUser("/discover");

  return (
    <AppShell user={user} headerNote="Select a job to start or resume its workflow">
      {authChecked ? (
        <DiscoverJobsView />
      ) : (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-sm text-[var(--ink-secondary)]">
          Checking authenticated session...
        </div>
      )}
    </AppShell>
  );
}
