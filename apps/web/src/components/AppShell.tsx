"use client";

import React, { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Compass,
  FileUser,
  GitBranch,
  LogOut,
  Menu,
  Settings,
  ShieldCheck,
  X,
} from "lucide-react";
import { apiClient } from "@/lib/api-client";
import type { UserResponse } from "@/lib/types";

const NAV_ITEMS = [
  { href: "/", label: "My Workflows", icon: GitBranch },
  { href: "/discover", label: "Discover Jobs", icon: Compass },
  { href: "/profile", label: "Profile and Resumes", icon: FileUser },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

export interface AppShellProps {
  user: UserResponse | null;
  children: React.ReactNode;
  headerNote?: string;
}

export function AppShell({ user, children, headerNote }: AppShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  const isNavActive = (href: string) => {
    if (href === "/") {
      return pathname === "/" || pathname.startsWith("/workflows");
    }
    return pathname === href || pathname.startsWith(`${href}/`);
  };

  async function handleSignOut() {
    setSigningOut(true);
    try {
      await apiClient.logout();
      router.push("/login");
      router.refresh();
    } finally {
      setSigningOut(false);
    }
  }

  return (
    <div className="min-h-screen flex flex-col lg:flex-row bg-[var(--bg-canvas)] text-[var(--ink-primary)]">
      {/* Mobile Header */}
      <header className="sticky top-0 z-30 border-b border-[var(--border-hairline)] bg-[var(--bg-surface)]/95 backdrop-blur-sm lg:hidden">
        <div className="flex items-center justify-between gap-3 px-4 py-3">
          <Link
            href="/"
            className="flex items-center gap-2.5 text-sm font-semibold tracking-tight text-[var(--ink-primary)]"
          >
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-[var(--ink-primary)] text-white">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            </span>
            <span>ApplyPilot AI</span>
          </Link>

          <button
            type="button"
            aria-label={mobileMenuOpen ? "Close navigation menu" : "Open navigation menu"}
            onClick={() => setMobileMenuOpen((v) => !v)}
            className="inline-flex items-center justify-center rounded-lg border border-[var(--border-hairline)] p-2 text-[var(--ink-secondary)]"
          >
            {mobileMenuOpen ? (
              <X className="h-4 w-4" aria-hidden="true" />
            ) : (
              <Menu className="h-4 w-4" aria-hidden="true" />
            )}
          </button>
        </div>

        {mobileMenuOpen && (
          <div className="border-t border-[var(--border-hairline)] bg-[var(--bg-surface)] px-4 py-3">
            <nav aria-label="Mobile navigation" className="flex flex-col gap-1">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                const active = isNavActive(item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    onClick={() => setMobileMenuOpen(false)}
                    className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium ${
                      active
                        ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)]"
                        : "text-[var(--ink-secondary)]"
                    }`}
                  >
                    <Icon className="h-4 w-4" aria-hidden="true" />
                    <span>{item.label}</span>
                  </Link>
                );
              })}
              {user && (
                <div className="mt-2 flex items-center justify-between border-t border-[var(--border-hairline)] pt-2">
                  <span className="truncate text-xs text-[var(--ink-muted)]">{user.email}</span>
                  <button
                    type="button"
                    onClick={handleSignOut}
                    className="text-xs font-medium text-[var(--state-failed-fg)]"
                  >
                    Sign out
                  </button>
                </div>
              )}
            </nav>
          </div>
        )}
      </header>

      {/* Left Minimal Navigation Sidebar (Desktop) */}
      <aside className="hidden lg:flex lg:w-60 lg:shrink-0 lg:flex-col lg:justify-between lg:border-r lg:border-[var(--border-hairline)] lg:bg-[var(--bg-surface)] lg:sticky lg:top-0 lg:h-screen lg:px-4 lg:py-5">
        <div className="space-y-6">
          <Link
            href="/"
            className="flex items-center gap-2.5 px-2 text-sm font-semibold tracking-tight text-[var(--ink-primary)]"
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--ink-primary)] text-white">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="flex flex-col leading-tight">
              <span>ApplyPilot AI</span>
              <span className="text-[11px] font-normal text-[var(--ink-muted)]">
                Guided Application Studio
              </span>
            </span>
          </Link>

          <nav aria-label="Primary navigation" className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = isNavActive(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`flex items-center gap-2.5 whitespace-nowrap rounded-xl px-3 py-2 text-sm font-medium transition-colors ${
                    active
                      ? "bg-[var(--bg-subtle)] text-[var(--ink-primary)] shadow-[inset_0_0_0_1px_var(--border-strong)]"
                      : "text-[var(--ink-secondary)] hover:bg-[var(--bg-rail)] hover:text-[var(--ink-primary)]"
                  }`}
                >
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </nav>

          {headerNote && (
            <div className="rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-[11px] leading-relaxed text-[var(--ink-muted)]">
              {headerNote}
            </div>
          )}
        </div>

        <div className="border-t border-[var(--border-hairline)] pt-4">
          {user ? (
            <div className="space-y-2.5 px-1">
              <p className="truncate text-xs font-medium text-[var(--ink-secondary)]" title={user.email}>
                {user.email}
              </p>
              <button
                type="button"
                onClick={handleSignOut}
                disabled={signingOut}
                className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-[var(--border-hairline)] bg-[var(--bg-rail)] px-3 py-2 text-xs font-medium text-[var(--ink-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--ink-primary)] disabled:opacity-50"
              >
                <LogOut className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span>{signingOut ? "Signing out..." : "Sign out"}</span>
              </button>
            </div>
          ) : (
            <Link
              href="/login"
              className="flex w-full items-center justify-center rounded-xl bg-[var(--ink-primary)] px-3 py-2 text-xs font-semibold text-white hover:opacity-90"
            >
              Sign in
            </Link>
          )}
        </div>
      </aside>

      <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        <div className="mx-auto max-w-6xl">{children}</div>
      </main>
    </div>
  );
}
