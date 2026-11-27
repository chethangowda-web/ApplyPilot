import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";

export const SESSION_COOKIE_NAME = "applypilot_session";

const ALLOWED_BACKEND_PREFIXES = new Set([
  "health",
  "auth",
  "profile",
  "resume",
  "resumes",
  "jobs",
  "applications",
  "workflows",
]);

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function getBackendBaseUrl(): string {
  const raw = (process.env.BACKEND_API_URL || "http://127.0.0.1:8002").trim().replace(/\/+$/, "");
  const parsed = new URL(raw);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("BACKEND_API_URL must use http or https");
  }
  return parsed.origin;
}

export async function getSessionTokenFromCookies(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE_NAME)?.value ?? null;
}

export function buildSessionCookieOptions(req: NextRequest, maxAgeSeconds?: number) {
  const isProd = process.env.NODE_ENV === "production";
  const allowLocalHttp = process.env.APPLYPILOT_ALLOW_INSECURE_LOCAL_HTTP === "true";
  const secure =
    req.nextUrl.protocol === "https:" ||
    process.env.APPLYPILOT_SECURE_COOKIES === "true" ||
    (isProd && !allowLocalHttp);

  return {
    httpOnly: true,
    sameSite: "lax" as const,
    path: "/",
    secure,
    ...(typeof maxAgeSeconds === "number" ? { maxAge: maxAgeSeconds } : {}),
  };
}

export function applyNoStoreHeaders(headers: Headers): Headers {
  headers.set("Cache-Control", "no-store, private");
  headers.set("Pragma", "no-cache");
  return headers;
}

export function noStoreJson(body: unknown, init?: ResponseInit): NextResponse {
  const headers = applyNoStoreHeaders(new Headers(init?.headers));
  return NextResponse.json(body, { ...init, headers });
}

function isSameOriginUrl(candidateOrigin: string, req: NextRequest): boolean {
  if (candidateOrigin === req.nextUrl.origin) {
    return true;
  }
  const hostHeader = req.headers.get("host");
  if (!hostHeader) {
    return false;
  }
  try {
    const parsed = new URL(candidateOrigin);
    return parsed.protocol === req.nextUrl.protocol && parsed.host === hostHeader;
  } catch {
    return false;
  }
}

export function validateSameOriginMutation(req: NextRequest): NextResponse | null {
  if (!MUTATING_METHODS.has(req.method.toUpperCase())) {
    return null;
  }

  const secFetchSite = req.headers.get("sec-fetch-site");
  if (secFetchSite && secFetchSite.toLowerCase() === "cross-site") {
    return noStoreJson({ detail: "Cross-site mutation blocked" }, { status: 403 });
  }

  const originHeader = req.headers.get("origin");
  if (originHeader) {
    if (!isSameOriginUrl(originHeader, req)) {
      return noStoreJson({ detail: "Origin mismatch blocked" }, { status: 403 });
    }
    return null;
  }

  const refererHeader = req.headers.get("referer");
  if (refererHeader) {
    try {
      const refOrigin = new URL(refererHeader).origin;
      if (isSameOriginUrl(refOrigin, req)) {
        return null;
      }
    } catch {
      return noStoreJson({ detail: "Invalid Referer header" }, { status: 403 });
    }
    return noStoreJson({ detail: "Referer origin mismatch blocked" }, { status: 403 });
  }

  return noStoreJson(
    { detail: "Missing Origin or Referer for state-changing request" },
    { status: 403 },
  );
}

export function resolveSafeBackendPath(pathSegments: string[]): string | null {
  if (!pathSegments || pathSegments.length === 0) {
    return null;
  }

  for (const segment of pathSegments) {
    if (!segment || segment === "." || segment === "..") {
      return null;
    }
    const lowered = segment.toLowerCase();
    if (
      segment.includes("\\") ||
      segment.includes("/") ||
      lowered.includes("%2e") ||
      lowered.includes("%2f") ||
      lowered.includes("%5c") ||
      /[\s\x00-\x1f]/.test(segment)
    ) {
      return null;
    }
  }

  if (!ALLOWED_BACKEND_PREFIXES.has(pathSegments[0])) {
    return null;
  }

  const joined = pathSegments.join("/");
  return joined === "health" ? "/health" : `/api/${joined}`;
}
