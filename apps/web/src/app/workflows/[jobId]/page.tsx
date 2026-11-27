"use client";

import React, { Suspense, use } from "react";
import { AppShell } from "@/components/AppShell";
import { WorkflowWorkspace } from "@/components/WorkflowWorkspace";
import { useSessionUser } from "@/lib/use-session-user";

export default function JobWorkflowPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = use(params);
  const { user, authChecked } = useSessionUser(`/workflows/${jobId}`);

  return (
    <AppShell user={user} headerNote="11-Stage Guided Execution">
      {authChecked ? (
        <Suspense
          fallback={
            <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-sm text-[var(--ink-secondary)]">
              Loading workflow step view...
            </div>
          }
        >
          <WorkflowWorkspace jobId={jobId} />
        </Suspense>
      ) : (
        <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-sm text-[var(--ink-secondary)]">
          Checking authenticated session...
        </div>
      )}
    </AppShell>
  );
}
