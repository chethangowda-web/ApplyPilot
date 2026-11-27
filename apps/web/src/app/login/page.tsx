"use client";

import React, { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ShieldCheck } from "lucide-react";
import { apiClient } from "@/lib/api-client";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const nextPath = searchParams.get("next") || "/";

  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "login") {
        await apiClient.login(email, password);
      } else {
        await apiClient.register(email, password);
      }
      router.push(nextPath.startsWith("/") ? nextPath : "/");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Authentication failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="w-full max-w-md rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 sm:p-8 shadow-sm">
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-[var(--ink-primary)] text-white">
          <ShieldCheck className="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h1 className="text-lg font-semibold text-[var(--ink-primary)]">ApplyPilot AI</h1>
          <p className="text-xs text-[var(--ink-muted)]">
            Authenticated HttpOnly Session · FastAPI Backend
          </p>
        </div>
      </div>

      <div className="mt-6 flex rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-1 text-xs font-medium">
        <button
          type="button"
          onClick={() => {
            setMode("login");
            setError(null);
          }}
          className={`flex-1 rounded-lg py-1.5 ${
            mode === "login"
              ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-sm"
              : "text-[var(--ink-muted)]"
          }`}
        >
          Sign In
        </button>
        <button
          type="button"
          onClick={() => {
            setMode("register");
            setError(null);
          }}
          className={`flex-1 rounded-lg py-1.5 ${
            mode === "register"
              ? "bg-[var(--bg-surface)] text-[var(--ink-primary)] shadow-sm"
              : "text-[var(--ink-muted)]"
          }`}
        >
          Create Account
        </button>
      </div>

      <form onSubmit={handleSubmit} className="mt-5 space-y-4">
        <div>
          <label htmlFor="auth-email" className="block text-xs font-medium text-[var(--ink-secondary)]">
            Email
          </label>
          <input
            id="auth-email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="candidate@example.com"
            className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm text-[var(--ink-primary)] focus:border-[var(--accent-primary)] focus:outline-none"
          />
        </div>

        <div>
          <label htmlFor="auth-password" className="block text-xs font-medium text-[var(--ink-secondary)]">
            Password
          </label>
          <input
            id="auth-password"
            type="password"
            required
            minLength={mode === "register" ? 8 : 1}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={mode === "register" ? "Minimum 8 characters" : "Your password"}
            className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm text-[var(--ink-primary)] focus:border-[var(--accent-primary)] focus:outline-none"
          />
        </div>

        {error && (
          <div
            role="alert"
            className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] px-3 py-2 text-xs text-[var(--state-failed-fg)]"
          >
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-xl bg-[var(--ink-primary)] py-2.5 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50"
        >
          {submitting
            ? "Authenticating..."
            : mode === "login"
              ? "Sign In to Workflows"
              : "Register & Start"}
        </button>
      </form>
    </div>
  );
}

export default function LoginPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-[var(--bg-canvas)] px-4">
      <Suspense fallback={<div className="text-xs text-[var(--ink-muted)]">Loading sign in...</div>}>
        <LoginForm />
      </Suspense>
    </div>
  );
}
