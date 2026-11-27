"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiClient } from "@/lib/api-client";
import type {
  ApplicationResponse,
  FollowupResponse,
  InterviewKitResponse,
  InterviewMemoryResponse,
  InterviewSessionResponse,
} from "@/lib/types";
import { useSessionUser } from "@/lib/use-session-user";
import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";

export default function InterviewsPage() {
  const { user } = useSessionUser("/interviews");
  const [applications, setApplications] = useState<ApplicationResponse[]>([]);
  const [selectedAppId, setSelectedAppId] = useState<string>("");
  const [kits, setKits] = useState<InterviewKitResponse[]>([]);
  const [activeKit, setActiveKit] = useState<InterviewKitResponse | null>(null);
  const [session, setSession] = useState<InterviewSessionResponse | null>(null);
  const [answer, setAnswer] = useState("");
  const [memories, setMemories] = useState<InterviewMemoryResponse[]>([]);
  const [followups, setFollowups] = useState<FollowupResponse[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    try {
      const [apps, mems, fus] = await Promise.all([
        apiClient.listApplications(),
        apiClient.listInterviewMemories(),
        apiClient.listFollowups(),
      ]);
      setApplications(apps);
      setMemories(mems);
      setFollowups(fus);
      const firstId = apps[0]?.id ?? "";
      if (firstId && !selectedAppId) {
        setSelectedAppId(firstId);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load interview workspace");
    }
  }, [selectedAppId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (!selectedAppId) return;
    apiClient
      .listInterviewKits(selectedAppId)
      .then((list) => {
        setKits(list);
        setActiveKit(list.find((k) => k.is_active) ?? list[0] ?? null);
      })
      .catch(() => {});
  }, [selectedAppId]);

  async function handleGenerateKit() {
    if (!selectedAppId) return;
    setBusy(true);
    setError(null);
    try {
      const kit = await apiClient.generateInterviewKit(selectedAppId);
      setActiveKit(kit);
      setKits((prev) => [kit, ...prev]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to generate interview kit");
    } finally {
      setBusy(false);
    }
  }

  async function handleStartMock() {
    if (!activeKit) return;
    setBusy(true);
    setError(null);
    try {
      const s = await apiClient.startMockSession(activeKit.id, "mixed", "medium");
      setSession(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start mock interview");
    } finally {
      setBusy(false);
    }
  }

  async function handleSubmitAnswer(e: React.FormEvent) {
    e.preventDefault();
    if (!session || !answer.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await apiClient.submitMockAnswer(session.id, answer);
      setSession(updated);
      setAnswer("");
      setMemories(await apiClient.listInterviewMemories());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to submit answer");
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteMemory(memoryId: string) {
    setBusy(true);
    try {
      await apiClient.deleteInterviewMemory(memoryId);
      setMemories((prev) => prev.filter((m) => m.id !== memoryId));
    } finally {
      setBusy(false);
    }
  }

  async function handleCompleteFollowup(id: string) {
    setBusy(true);
    try {
      const updated = await apiClient.completeFollowup(id);
      setFollowups((prev) => prev.map((f) => (f.id === id ? updated : f)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AppShell user={user} headerNote="Milestone 6 Career Intelligence">
      <div className="space-y-6 max-w-5xl">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink-primary)]">
              Interview Lab &amp; Follow-Ups
            </h1>
            <p className="mt-1 text-sm text-[var(--ink-secondary)]">
              8-module resume-grounded Interview Kits, adaptive mock rounds, user-controlled memory, and follow-up drafts.
            </p>
          </div>
          <Link
            href="/applications"
            className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-surface)] px-3 py-1.5 text-xs font-medium text-[var(--ink-primary)]"
          >
            Back to Applications
          </Link>
        </div>

        {error && (
          <p className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] px-4 py-2 text-xs text-[var(--state-failed-fg)]">
            {error}
          </p>
        )}

        <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-4 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor="app-select" className="font-semibold text-[var(--ink-primary)]">
                Application:
              </label>
              <select
                id="app-select"
                value={selectedAppId}
                onChange={(e) => setSelectedAppId(e.target.value)}
                className="rounded-lg border border-[var(--border-hairline)] bg-[var(--bg-canvas)] px-2.5 py-1.5 text-xs"
              >
                {applications.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.status} · {a.id.slice(0, 8)}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              disabled={busy || !selectedAppId}
              onClick={() => void handleGenerateKit()}
              className="rounded-lg bg-[var(--ink-primary)] px-3.5 py-2 font-medium text-white disabled:opacity-50"
            >
              Generate 8-Module Kit
            </button>
          </div>

          {activeKit && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-[var(--ink-secondary)]">
                  Kit <code className="font-mono">v{activeKit.version}</code> · Resume SHA-256:{" "}
                  <code className="font-mono">{activeKit.resume_checksum.slice(0, 12)}...</code> · Readiness Estimate:{" "}
                  <strong>{activeKit.readiness_estimate}%</strong> (estimate only) · Versions: {kits.length}
                </p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleStartMock()}
                  className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-rail)] px-3 py-1.5 font-medium text-[var(--ink-primary)]"
                >
                  Start Adaptive Mock Session
                </button>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {Object.entries(activeKit.modules_json).map(([key, mod]) => (
                  <div key={key} className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 space-y-1">
                    <p className="font-semibold text-[var(--ink-primary)]">{String(mod.title || key)}</p>
                    <p className="text-[11px] text-[var(--ink-secondary)]">
                      Topics: {Array.isArray(mod.topics_to_study) ? (mod.topics_to_study as string[]).join(", ") : ""}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {session && (
          <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-3 text-xs">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-[var(--ink-primary)]">
                Adaptive Mock Interview ({session.round_type} · {session.difficulty})
              </h2>
              <StatusBadge
                state={session.status === "completed" ? "completed" : "current"}
                customLabel={session.status.toUpperCase()}
                mono
              />
            </div>
            {session.status === "active" ? (
              <form onSubmit={(e) => void handleSubmitAnswer(e)} className="space-y-3">
                <p className="font-medium text-[var(--ink-primary)]">
                  Q{session.current_question_index + 1}: {String(session.current_question_json.question_text || "")}
                </p>
                <textarea
                  rows={4}
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  placeholder="Write your structured response..."
                  className="w-full rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-canvas)] p-3 text-xs"
                />
                <button
                  type="submit"
                  disabled={busy || !answer.trim()}
                  className="rounded-lg bg-[var(--ink-primary)] px-3.5 py-2 font-medium text-white disabled:opacity-50"
                >
                  Submit Answer
                </button>
              </form>
            ) : (
              <div className="space-y-1 text-[var(--ink-secondary)]">
                <p>
                  Final Estimated Score: <strong>{session.overall_score ?? 0}/100</strong>
                </p>
                <p>{String(session.report_json.disclaimer || "")}</p>
              </div>
            )}
          </section>
        )}

        <div className="grid gap-6 md:grid-cols-2">
          <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-3 text-xs">
            <h2 className="text-sm font-semibold text-[var(--ink-primary)]">Interview Memory (User-Controlled)</h2>
            {memories.length === 0 ? (
              <p className="text-[var(--ink-muted)]">No interview memory entries yet.</p>
            ) : (
              memories.map((m) => (
                <div key={m.id} className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 flex justify-between gap-2">
                  <div>
                    <p className="font-medium text-[var(--ink-primary)]">
                      [{m.memory_type}] {m.topic}
                    </p>
                    <p className="text-[var(--ink-secondary)]">{m.summary}</p>
                  </div>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void handleDeleteMemory(m.id)}
                    className="shrink-0 text-[11px] text-[var(--state-failed-fg)] hover:underline"
                  >
                    Delete
                  </button>
                </div>
              ))
            )}
          </section>

          <section className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5 space-y-3 text-xs">
            <h2 className="text-sm font-semibold text-[var(--ink-primary)]">Follow-Up Reminders (Editable Drafts)</h2>
            {followups.length === 0 ? (
              <p className="text-[var(--ink-muted)]">No follow-up reminders scheduled yet.</p>
            ) : (
              followups.map((f) => (
                <div key={f.id} className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 space-y-1">
                  <div className="flex items-center justify-between">
                    <p className="font-medium text-[var(--ink-primary)]">{f.draft_subject}</p>
                    <StatusBadge state={f.status === "completed" ? "completed" : "waiting"} customLabel={f.status.toUpperCase()} mono />
                  </div>
                  <p className="text-[11px] text-[var(--ink-muted)]">Due: {new Date(f.due_at).toLocaleDateString()}</p>
                  {f.status !== "completed" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void handleCompleteFollowup(f.id)}
                      className="text-[11px] font-medium text-[var(--ink-primary)] underline"
                    >
                      Mark Completed
                    </button>
                  )}
                </div>
              ))
            )}
          </section>
        </div>
      </div>
    </AppShell>
  );
}
