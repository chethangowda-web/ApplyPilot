"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Upload } from "lucide-react";
import { ApiError, apiClient } from "@/lib/api-client";
import type { CandidateProfileResponse, ResumeMetadataResponse } from "@/lib/types";
import { useSessionUser } from "@/lib/use-session-user";
import { AppShell } from "@/components/AppShell";
import { StatusBadge } from "@/components/StatusBadge";

export default function ProfileAndResumesPage() {
  const { user, authChecked } = useSessionUser("/profile");
  const [profile, setProfile] = useState<CandidateProfileResponse | null>(null);
  const [resumes, setResumes] = useState<ResumeMetadataResponse[]>([]);
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [location, setLocation] = useState("");
  const [summary, setSummary] = useState("");
  const [newSkill, setNewSkill] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [profResp, resResp] = await Promise.all([
        apiClient.getProfile().catch((err) => {
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }),
        apiClient.listResumeVersions(),
      ]);
      setProfile(profResp);
      setResumes(resResp);
      if (profResp) {
        setFullName(profResp.full_name);
        setEmail(profResp.email);
        setLocation(profResp.location ?? "");
        setSummary(profResp.summary ?? "");
      } else if (user) {
        setEmail(user.email);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load profile & resumes");
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    if (authChecked) {
      void loadData();
    }
  }, [authChecked, loadData]);

  async function handleSaveProfile(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setNotice(null);
    setError(null);
    try {
      const saved = await apiClient.upsertProfile({
        full_name: fullName.trim(),
        email: email.trim(),
        location: location.trim() || null,
        summary: summary.trim() || null,
        education: profile?.education ?? [],
        achievements: profile?.achievements ?? [],
        confirmed_preferences: profile?.confirmed_preferences ?? {},
      });
      setProfile(saved);
      setNotice("Candidate profile saved to backend.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save profile");
    } finally {
      setSaving(false);
    }
  }

  async function handleResumeUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const uploaded = await apiClient.uploadResume(file, true);
      setNotice(`Master resume "${uploaded.original_filename}" uploaded and parsed cleanly.`);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Resume upload failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <AppShell user={user} headerNote="Verified Ground-Truth Profile & Master Resumes">
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[var(--ink-primary)]">
            Profile and Resumes
          </h1>
          <p className="mt-1 text-sm text-[var(--ink-secondary)]">
            All workflow tailoring, eligibility checks, and form answers trace directly to facts saved here.
          </p>
        </div>

        {notice && (
          <div className="rounded-xl border border-[var(--state-completed-border)] bg-[var(--state-completed-bg)] p-3 text-xs text-[var(--state-completed-fg)]">
            {notice}
          </div>
        )}
        {error && (
          <div className="rounded-xl border border-[var(--state-failed-border)] bg-[var(--state-failed-bg)] p-3 text-xs text-[var(--state-failed-fg)]">
            {error}
          </div>
        )}

        {loading ? (
          <div className="rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-6 text-sm text-[var(--ink-secondary)]">
            Loading profile and resume vault...
          </div>
        ) : (
          <div className="grid gap-6 lg:grid-cols-2">
            <form
              onSubmit={handleSaveProfile}
              className="space-y-4 rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5"
            >
              <h2 className="text-base font-semibold text-[var(--ink-primary)]">
                Master Candidate Facts
              </h2>
              <div>
                <label className="block text-xs font-medium text-[var(--ink-secondary)]">
                  Full Name
                </label>
                <input
                  type="text"
                  required
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-[var(--ink-secondary)]">
                  Email
                </label>
                <input
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-[var(--ink-secondary)]">
                  Location
                </label>
                <input
                  type="text"
                  value={location}
                  onChange={(e) => setLocation(e.target.value)}
                  placeholder="Bengaluru, India"
                  className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-[var(--ink-secondary)]">
                  Summary
                </label>
                <textarea
                  rows={3}
                  value={summary}
                  onChange={(e) => setSummary(e.target.value)}
                  className="mt-1 w-full rounded-xl border border-[var(--border-hairline)] px-3 py-2 text-sm"
                />
              </div>
              <button
                type="submit"
                disabled={saving}
                className="rounded-xl bg-[var(--ink-primary)] px-4 py-2 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "Saving..." : "Save Master Profile"}
              </button>

              {profile && (
                <div className="border-t border-[var(--border-hairline)] pt-4 space-y-2">
                  <p className="text-xs font-semibold text-[var(--ink-primary)]">
                    Verified Skills ({profile.skills.length})
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {profile.skills.map((sk) => (
                      <span
                        key={sk.id}
                        className="rounded-md border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-2 py-0.5 text-xs text-[var(--ink-primary)]"
                      >
                        {sk.skill_name}
                      </span>
                    ))}
                  </div>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={newSkill}
                      onChange={(e) => setNewSkill(e.target.value)}
                      placeholder="Add verified skill (e.g. FastAPI)"
                      className="flex-1 rounded-lg border border-[var(--border-hairline)] px-3 py-1.5 text-xs"
                    />
                    <button
                      type="button"
                      disabled={saving || !newSkill.trim()}
                      onClick={async () => {
                        setSaving(true);
                        try {
                          await apiClient.addSkill({ skill_name: newSkill.trim(), verified: true });
                          setNewSkill("");
                          await loadData();
                        } finally {
                          setSaving(false);
                        }
                      }}
                      className="rounded-lg border border-[var(--border-strong)] bg-[var(--bg-rail)] px-3 py-1.5 text-xs font-medium text-[var(--ink-primary)] disabled:opacity-50"
                    >
                      Add Skill
                    </button>
                  </div>
                </div>
              )}
            </form>

            <div className="space-y-4 rounded-2xl border border-[var(--border-hairline)] bg-[var(--bg-surface)] p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-base font-semibold text-[var(--ink-primary)]">
                  Resume Vault ({resumes.length})
                </h2>
                <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-xl bg-[var(--accent-primary)] px-3.5 py-2 text-xs font-semibold text-white hover:opacity-90">
                  <Upload className="h-3.5 w-3.5" aria-hidden="true" />
                  <span>Upload Master PDF/DOCX</span>
                  <input
                    type="file"
                    accept=".pdf,.docx"
                    onChange={handleResumeUpload}
                    className="hidden"
                  />
                </label>
              </div>

              {resumes.length === 0 ? (
                <p className="text-xs text-[var(--ink-muted)]">
                  Upload at least one Master PDF or DOCX resume to unlock resume tailoring.
                </p>
              ) : (
                <ul className="space-y-2.5 text-xs">
                  {resumes.map((r) => (
                    <li
                      key={r.id}
                      className="flex items-center justify-between gap-3 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] p-3"
                    >
                      <div className="min-w-0">
                        <p className="truncate font-semibold text-[var(--ink-primary)]">
                          {r.version_label} · {r.original_filename}
                        </p>
                        <p className="mt-0.5 truncate font-mono text-[11px] text-[var(--ink-muted)]">
                          {r.sha256_checksum.slice(0, 16)}… · {(r.file_size_bytes / 1024).toFixed(1)} KB
                        </p>
                      </div>
                      <StatusBadge
                        state={r.validation_passed ? "completed" : "waiting"}
                        customLabel={r.is_master ? "MASTER" : "TAILORED"}
                        mono
                      />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
