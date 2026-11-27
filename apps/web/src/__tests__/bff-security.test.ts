import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST as loginPost } from "@/app/api/session/login/route";
import { POST as logoutPost } from "@/app/api/session/logout/route";
import { GET as meGet } from "@/app/api/session/me/route";
import {
  GET as proxyGet,
  POST as proxyPost,
} from "@/app/api/backend/[...path]/route";
import { SESSION_COOKIE_NAME } from "@/lib/server-backend";

const mockCookieGet = vi.fn();

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: mockCookieGet,
  }),
}));

describe("Next.js BFF authentication, CSRF, and proxy security", () => {
  const originalFetch = global.fetch;
  const originalEnv = { ...process.env };

  beforeEach(() => {
    vi.clearAllMocks();
    mockCookieGet.mockReturnValue(undefined);
    process.env = { ...originalEnv };
    delete process.env.BACKEND_API_URL;
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.unstubAllEnvs();
    process.env = { ...originalEnv };
  });

  it("rejects unauthenticated session lookup and protected proxy calls with 401 and no-store", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const meResp = await meGet();
    expect(meResp.status).toBe(401);
    expect(meResp.headers.get("cache-control")).toContain("no-store");

    const req = new NextRequest("http://localhost:3000/api/backend/applications");
    const proxyResp = await proxyGet(req, {
      params: Promise.resolve({ path: ["applications"] }),
    });
    expect(proxyResp.status).toBe(401);
    expect(proxyResp.headers.get("cache-control")).toContain("no-store");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("stores JWT exclusively in HttpOnly cookie (Secure in production) and never leaks token in JSON", async () => {
    vi.stubEnv("NODE_ENV", "production");
    delete process.env.APPLYPILOT_ALLOW_INSECURE_LOCAL_HTTP;

    const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/auth/login")) {
        return new Response(
          JSON.stringify({ access_token: "super-secret-jwt-value", expires_in: 1800 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          id: "u1",
          email: "user@example.com",
          timezone: "Asia/Kolkata",
          is_active: true,
          created_at: "2026-11-26T00:00:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    global.fetch = fetchSpy as unknown as typeof fetch;

    const req = new NextRequest("http://localhost:3000/api/session/login", {
      method: "POST",
      headers: {
        Origin: "http://localhost:3000",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email: "user@example.com", password: "Password123!" }),
    });

    const resp = await loginPost(req);
    expect(resp.status).toBe(200);
    const bodyText = await resp.text();
    expect(bodyText).not.toContain("super-secret-jwt-value");
    expect(bodyText).not.toContain("access_token");

    const setCookie = resp.headers.get("set-cookie") || "";
    expect(setCookie).toContain(`${SESSION_COOKIE_NAME}=super-secret-jwt-value`);
    expect(setCookie.toLowerCase()).toContain("httponly");
    expect(setCookie.toLowerCase()).toContain("samesite=lax");
    expect(setCookie.toLowerCase()).toContain("path=/");
    expect(setCookie.toLowerCase()).toContain("secure");

    const logoutReq = new NextRequest("http://localhost:3000/api/session/logout", {
      method: "POST",
      headers: { Origin: "http://localhost:3000" },
    });
    const logoutResp = await logoutPost(logoutReq);
    expect(logoutResp.status).toBe(200);
    expect((logoutResp.headers.get("set-cookie") || "").toLowerCase()).toContain("max-age=0");
  });

  it("ignores forged Authorization, Cookie, and Host headers and uses server session cookie + configured backend origin", async () => {
    mockCookieGet.mockReturnValue({ value: "server-cookie-jwt" });

    let capturedUrl = "";
    let capturedHeaders = new Headers();
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl = String(input);
      capturedHeaders = new Headers(init?.headers);
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;

    const req = new NextRequest("http://localhost:3000/api/backend/applications", {
      method: "GET",
      headers: {
        Authorization: "Bearer forged-client-token",
        Host: "evil.attacker.com",
        Cookie: "applypilot_session=forged-raw-cookie",
      },
    });

    const resp = await proxyGet(req, {
      params: Promise.resolve({ path: ["applications"] }),
    });
    expect(resp.status).toBe(200);
    expect(capturedUrl).toBe("http://127.0.0.1:8002/api/applications");
    expect(capturedHeaders.get("authorization")).toBe("Bearer server-cookie-jwt");
    expect(capturedHeaders.get("cookie")).toBeNull();
    expect(capturedHeaders.get("host")).toBeNull();
  });

  it("blocks cross-origin mutations, cross-site Sec-Fetch-Site, and missing Origin/Referer", async () => {
    mockCookieGet.mockReturnValue({ value: "server-cookie-jwt" });
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const crossOriginReq = new NextRequest(
      "http://localhost:3000/api/backend/applications/app-1/approve",
      {
        method: "POST",
        headers: { Origin: "https://evil.example" },
      },
    );
    const r1 = await proxyPost(crossOriginReq, {
      params: Promise.resolve({ path: ["applications", "app-1", "approve"] }),
    });
    expect(r1.status).toBe(403);

    const crossSiteReq = new NextRequest("http://localhost:3000/api/session/logout", {
      method: "POST",
      headers: {
        Origin: "http://localhost:3000",
        "Sec-Fetch-Site": "cross-site",
      },
    });
    const r2 = await logoutPost(crossSiteReq);
    expect(r2.status).toBe(403);

    const missingOriginReq = new NextRequest("http://localhost:3000/api/session/login", {
      method: "POST",
      body: JSON.stringify({ email: "a@b.com", password: "x" }),
    });
    const r3 = await loginPost(missingOriginReq);
    expect(r3.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("rejects path traversal and non-allowlisted backend proxy paths", async () => {
    mockCookieGet.mockReturnValue({ value: "server-cookie-jwt" });
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    for (const badPath of [
      ["..", "etc", "passwd"],
      ["jobs", "%2e%2e", "admin"],
      ["internal", "debug"],
    ]) {
      const req = new NextRequest("http://localhost:3000/api/backend/bad");
      const resp = await proxyGet(req, {
        params: Promise.resolve({ path: badPath }),
      });
      expect(resp.status).toBe(400);
    }
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("handles invalid/expired session cookies cleanly without exposing secrets", async () => {
    mockCookieGet.mockReturnValue({ value: "expired-jwt" });
    global.fetch = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: "Could not validate credentials" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;

    const resp = await meGet();
    expect(resp.status).toBe(401);
    const data = await resp.json();
    expect(data).toEqual({ authenticated: false, user: null });
    expect(resp.headers.get("cache-control")).toContain("no-store");
  });
});
