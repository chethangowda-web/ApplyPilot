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

  let body: { email?: string; password?: string; timezone?: string };
  try {
    body = await req.json();
  } catch {
    return noStoreJson({ detail: "Invalid JSON payload" }, { status: 400 });
  }

  const backendBase = getBackendBaseUrl();
  let regResp: Response;
  try {
    regResp = await fetch(`${backendBase}/api/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    return noStoreJson({ detail: "ApplyPilot backend API is unreachable" }, { status: 502 });
  }

  const regData = await regResp.json().catch(() => ({ detail: "Registration failed" }));
  if (!regResp.ok) {
    return noStoreJson(regData, { status: regResp.status });
  }

  const loginResp = await fetch(`${backendBase}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: body.email, password: body.password }),
    cache: "no-store",
  });
  const loginData = await loginResp.json().catch(() => null);
  if (!loginResp.ok || !loginData?.access_token) {
    return noStoreJson(
      { detail: "Registered account, but automatic login failed" },
      { status: loginResp.status || 500 },
    );
  }

  const token = String(loginData.access_token);
  const expiresIn = typeof loginData.expires_in === "number" ? loginData.expires_in : 3600;
  const response = noStoreJson({ authenticated: true, user: regData }, { status: 201 });
  response.cookies.set(SESSION_COOKIE_NAME, token, buildSessionCookieOptions(req, expiresIn));
  return response;
}
