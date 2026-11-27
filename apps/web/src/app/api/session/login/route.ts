import type { NextRequest } from "next/server";
import {
  SESSION_COOKIE_NAME,
  buildSessionCookieOptions,
  getBackendBaseUrl,
  noStoreJson,
  validateSameOriginMutation,
} from "@/lib/server-backend";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const csrfError = validateSameOriginMutation(req);
  if (csrfError) return csrfError;

  let body: { email?: string; password?: string };
  try {
    body = await req.json();
  } catch {
    return noStoreJson({ detail: "Invalid JSON payload" }, { status: 400 });
  }

  const backendBase = getBackendBaseUrl();
  let loginResp: Response;
  try {
    loginResp = await fetch(`${backendBase}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: body.email, password: body.password }),
      cache: "no-store",
    });
  } catch {
    return noStoreJson({ detail: "ApplyPilot backend API is unreachable" }, { status: 502 });
  }

  const loginData = await loginResp.json().catch(() => ({ detail: "Login failed" }));
  if (!loginResp.ok || !loginData?.access_token) {
    return noStoreJson(
      { detail: typeof loginData?.detail === "string" ? loginData.detail : "Invalid credentials" },
      { status: loginResp.status || 401 },
    );
  }

  const token = String(loginData.access_token);
  const expiresIn = typeof loginData.expires_in === "number" ? loginData.expires_in : 3600;

  let user: unknown = null;
  try {
    const meResp = await fetch(`${backendBase}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (meResp.ok) {
      user = await meResp.json();
    }
  } catch {
    user = null;
  }

  const response = noStoreJson({ authenticated: true, user });
  response.cookies.set(SESSION_COOKIE_NAME, token, buildSessionCookieOptions(req, expiresIn));
  return response;
}
