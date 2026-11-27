"use client";

import React from "react";
import { useParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ApplicationDetailTimelineView } from "@/components/ApplicationDetailTimelineView";
import { useSessionUser } from "@/lib/use-session-user";

export default function ApplicationDetailPage() {
  const params = useParams<{ applicationId: string }>();
  const { user, authChecked } = useSessionUser(`/applications/${params.applicationId}`);

  return (
    <AppShell user={user} headerNote="Application Detail & Timeline">
      {!authChecked ? (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-8 text-sm text-[var(--ink-secondary)]">
          Checking authenticated session...
        </div>
      ) : (
        <ApplicationDetailTimelineView applicationId={params.applicationId} />
      )}
    </AppShell>
  );
}
