import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as gmailCallbackGet } from "@/app/api/integrations/gmail/callback/route";
import {
  GET as proxyGet,
  POST as proxyPost,
} from "@/app/api/backend/[...path]/route";

const mockCookieGet = vi.fn();

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: mockCookieGet,
  }),
}));

describe("Next.js BFF Gmail OAuth callback and integrations proxy", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    mockCookieGet.mockReturnValue(undefined);
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("redirects unauthenticated callback to /login with no-store without calling backend", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const req = new NextRequest(
      "http://localhost:3000/api/integrations/gmail/callback?code=secret-code&state=secret-state-12345",
    );
    const resp = await gmailCallbackGet(req);

    expect(resp.status).toBe(302);
    expect(resp.headers.get("location")).toBe("http://localhost:3000/login?next=%2Fsettings");
    expect(resp.headers.get("cache-control")).toContain("no-store");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("redirects Google OAuth errors and missing params safely with no-store", async () => {
    mockCookieGet.mockReturnValue({ value: "server-session-jwt" });
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const deniedReq = new NextRequest(
      "http://localhost:3000/api/integrations/gmail/callback?error=access_denied&state=s1",
    );
    const deniedResp = await gmailCallbackGet(deniedReq);
    expect(deniedResp.status).toBe(302);
    expect(deniedResp.headers.get("location")).toBe(
      "http://localhost:3000/settings?gmail=error&reason=oauth_denied",
    );
    expect(deniedResp.headers.get("cache-control")).toContain("no-store");

    const missingReq = new NextRequest("http://localhost:3000/api/integrations/gmail/callback?code=only-code");
    const missingResp = await gmailCallbackGet(missingReq);
    expect(missingResp.status).toBe(302);
    expect(missingResp.headers.get("location")).toBe(
      "http://localhost:3000/settings?gmail=error&reason=invalid_callback",
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("forwards code and state with HttpOnly session JWT to FastAPI and redirects on success", async () => {
    mockCookieGet.mockReturnValue({ value: "server-session-jwt" });

    let capturedUrl = "";
    let capturedAuth = "";
    let capturedBody = "";
    global.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl = String(input);
      capturedAuth = new Headers(init?.headers).get("authorization") || "";
      capturedBody = String(init?.body || "");
      return new Response(
        JSON.stringify({
          id: "conn-1",
          provider: "google_gmail",
          mailbox_email: "user@gmail.com",
          status: "connected",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    const req = new NextRequest(
      "http://localhost:3000/api/integrations/gmail/callback?code=auth-code-99&state=oauth-state-99",
    );
    const resp = await gmailCallbackGet(req);

    expect(resp.status).toBe(302);
    expect(resp.headers.get("location")).toBe("http://localhost:3000/settings?gmail=connected");
    expect(resp.headers.get("cache-control")).toContain("no-store");
    expect(capturedUrl).toBe("http://127.0.0.1:8002/api/integrations/gmail/callback");
    expect(capturedAuth).toBe("Bearer server-session-jwt");
    expect(JSON.parse(capturedBody)).toEqual({ code: "auth-code-99", state: "oauth-state-99" });
    const bodyText = await resp.text();
    expect(bodyText).not.toContain("server-session-jwt");
    expect(bodyText).not.toContain("auth-code-99");
  });

  it("handles backend 401 and 403 callback rejections safely", async () => {
    mockCookieGet.mockReturnValue({ value: "expired-jwt" });
    global.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: "Expired" }), { status: 401 })) as unknown as typeof fetch;

    const req = new NextRequest(
      "http://localhost:3000/api/integrations/gmail/callback?code=c1&state=s1",
    );
    const resp401 = await gmailCallbackGet(req);
    expect(resp401.status).toBe(302);
    expect(resp401.headers.get("location")).toBe("http://localhost:3000/login?next=%2Fsettings");

    global.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: "Mismatch" }), { status: 403 })) as unknown as typeof fetch;
    const resp403 = await gmailCallbackGet(req);
    expect(resp403.status).toBe(302);
    expect(resp403.headers.get("location")).toBe(
      "http://localhost:3000/settings?gmail=error&reason=callback_failed",
    );
  });

  it("allows /api/backend/integrations/* through BFF proxy with CSRF and HttpOnly cookie", async () => {
    mockCookieGet.mockReturnValue({ value: "server-session-jwt" });
    global.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ connected: false, connections: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    const statusReq = new NextRequest("http://localhost:3000/api/backend/integrations/gmail/status");
    const statusResp = await proxyGet(statusReq, {
      params: Promise.resolve({ path: ["integrations", "gmail", "status"] }),
    });
    expect(statusResp.status).toBe(200);

    const crossOriginStart = new NextRequest(
      "http://localhost:3000/api/backend/integrations/gmail/connect/start",
      { method: "POST", headers: { Origin: "https://evil.example" } },
    );
    const csrfResp = await proxyPost(crossOriginStart, {
      params: Promise.resolve({ path: ["integrations", "gmail", "connect", "start"] }),
    });
    expect(csrfResp.status).toBe(403);
  });
});
