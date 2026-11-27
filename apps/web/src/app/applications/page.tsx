"use client";

import React from "react";
import { AppShell } from "@/components/AppShell";
import { ApplicationsTrackerView } from "@/components/ApplicationsTrackerView";
import { useSessionUser } from "@/lib/use-session-user";

export default function ApplicationsTrackerPage() {
  const { user, authChecked } = useSessionUser("/applications");

  return (
    <AppShell user={user} headerNote="Submission Verification & Tracking">
      {!authChecked ? (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-sm text-[var(--ink-secondary)]">
          Checking authenticated session...
        </div>
      ) : (
        <ApplicationsTrackerView />
      )}
    </AppShell>
  );
}
