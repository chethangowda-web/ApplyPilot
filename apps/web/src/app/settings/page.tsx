"use client";

import React, { useCallback, useEffect, useState } from "react";
import { apiClient } from "@/lib/api-client";
import type { GmailStatusResponse, HealthResponse } from "@/lib/types";
import { useSessionUser } from "@/lib/use-session-user";
import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";

export default function SettingsPage() {
  const { user } = useSessionUser("/settings");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [gmailStatus, setGmailStatus] = useState<GmailStatusResponse | null>(null);
  const [gmailNotice, setGmailNotice] = useState<string | null>(null);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [gmailBusy, setGmailBusy] = useState(false);

  const loadGmailStatus = useCallback(async () => {
    try {
      const res = await apiClient.getGmailStatus();
      setGmailStatus(res);
    } catch (err) {
      setGmailError(err instanceof Error ? err.message : "Unable to load Gmail status");
    }
  }, []);

  useEffect(() => {
    apiClient
      .getHealth()
      .then(setHealth)
      .catch((err) => setHealthError(err instanceof Error ? err.message : "Backend unreachable"));
    void loadGmailStatus();
    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      const gmailParam = params.get("gmail");
      const reason = params.get("reason");
      if (gmailParam === "connected") {
        setGmailNotice("Gmail connected with read-only confirmation access.");
      } else if (gmailParam === "error") {
        setGmailError(`Gmail connection failed (${reason || "oauth_error"}).`);
      }
    }
  }, [loadGmailStatus]);

  async function handleConnectGmail() {
    setGmailBusy(true);
    setGmailError(null);
    setGmailNotice(null);
    try {
      const res = await apiClient.startGmailConnect();
      window.location.assign(res.authorization_url);
    } catch (err) {
      setGmailError(err instanceof Error ? err.message : "Failed to start Gmail OAuth");
      setGmailBusy(false);
    }
  }

  async function handleSyncGmail() {
    setGmailBusy(true);
    setGmailError(null);
    setGmailNotice(null);
    try {
      const res = await apiClient.syncGmailConfirmations();
      setGmailNotice(res.message);
      await loadGmailStatus();
    } catch (err) {
      setGmailError(err instanceof Error ? err.message : "Gmail sync failed");
    } finally {
      setGmailBusy(false);
    }
  }

  async function handleDisconnectGmail(connectionId?: string) {
    setGmailBusy(true);
    setGmailError(null);
    setGmailNotice(null);
    try {
      await apiClient.disconnectGmail(connectionId);
      setGmailNotice("Gmail disconnected and encrypted credentials cleared.");
      await loadGmailStatus();
    } catch (err) {
      setGmailError(err instanceof Error ? err.message : "Failed to disconnect Gmail");
    } finally {
      setGmailBusy(false);
    }
  }

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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-[var(--ink-primary)]">
                Gmail Read-Only Confirmation Sync
              </h2>
              <p className="mt-0.5 text-[var(--ink-secondary)]">
                Independently verifies employer application receipts (<code className="font-mono">gmail.readonly</code> only).
              </p>
            </div>
            <StatusBadge
              state={gmailStatus?.connected ? "completed" : "waiting"}
              customLabel={gmailStatus?.connected ? "CONNECTED" : "NOT CONNECTED"}
              mono
            />
          </div>

          {gmailNotice && (
            <p className="rounded-lg border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] px-3 py-2 text-[var(--state-completed-fg)]">
              {gmailNotice}
            </p>
          )}
          {gmailError && (
            <p className="rounded-lg border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] px-3 py-2 text-[var(--state-failed-fg)]">
              {gmailError}
            </p>
          )}

          {gmailStatus?.connections.map((conn) => (
            <div
              key={conn.id}
              className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3 flex flex-wrap items-center justify-between gap-3"
            >
              <div className="space-y-1">
                <p className="font-medium text-[var(--ink-primary)]">{conn.mailbox_email}</p>
                <p className="text-[11px] text-[var(--ink-secondary)]">
                  Status: <code className="font-mono">{conn.status}</code> · Scopes:{" "}
                  <code className="font-mono">{conn.granted_scopes.join(", ") || "none"}</code>
                </p>
                {conn.last_synced_at && (
                  <p className="text-[11px] text-[var(--ink-muted)]">
                    Last sync: {new Date(conn.last_synced_at).toLocaleString()} ({conn.last_sync_status || "idle"})
                  </p>
                )}
                {conn.last_sync_error && (
                  <p className="text-[11px] text-[var(--state-failed-fg)]">{conn.last_sync_error}</p>
                )}
              </div>
              {conn.status === "connected" && (
                <button
                  type="button"
                  disabled={gmailBusy}
                  onClick={() => void handleDisconnectGmail(conn.id)}
                  className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3 py-1.5 font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-canvas)] disabled:opacity-50"
                >
                  Disconnect
                </button>
              )}
            </div>
          ))}

          <div className="flex flex-wrap gap-2 pt-1">
            <button
              type="button"
              disabled={gmailBusy}
              onClick={() => void handleConnectGmail()}
              className="rounded-lg bg-[var(--ink-primary)] px-3.5 py-2 font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {gmailStatus?.connected ? "Reconnect Gmail" : "Connect Gmail"}
            </button>
            {gmailStatus?.connected && (
              <button
                type="button"
                disabled={gmailBusy}
                onClick={() => void handleSyncGmail()}
                className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-surface)] px-3.5 py-2 font-medium text-[var(--ink-primary)] hover:bg-[var(--bg-rail)] disabled:opacity-50"
              >
                Check Gmail Now
              </button>
            )}
          </div>
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
