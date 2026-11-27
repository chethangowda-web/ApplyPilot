import { NextRequest, NextResponse } from "next/server";
import {
  applyNoStoreHeaders,
  getBackendBaseUrl,
  getSessionTokenFromCookies,
  noStoreJson,
  resolveSafeBackendPath,
  validateSameOriginMutation,
} from "@/lib/server-backend";

export const dynamic = "force-dynamic";

async function proxyRequest(
  req: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const csrfError = validateSameOriginMutation(req);
  if (csrfError) return csrfError;

  const { path } = await context.params;
  const targetPath = resolveSafeBackendPath(path);
  if (!targetPath) {
    return noStoreJson({ detail: "Invalid or disallowed backend API path" }, { status: 400 });
  }

  const isHealthEndpoint = targetPath === "/health";
  const token = await getSessionTokenFromCookies();
  if (!isHealthEndpoint && !token) {
    return noStoreJson({ detail: "Not authenticated" }, { status: 401 });
  }

  const backendBase = getBackendBaseUrl();
  const targetUrl = `${backendBase}${targetPath}${req.nextUrl.search}`;

  // Never forward client-supplied Authorization, Cookie, or Host headers upstream
  const headers = new Headers();
  const incomingContentType = req.headers.get("content-type");
  if (incomingContentType) {
    headers.set("content-type", incomingContentType);
  }
  if (token) {
    headers.set("authorization", `Bearer ${token}`);
  }

  const hasBody = !["GET", "HEAD"].includes(req.method.toUpperCase());
  const bodyBuffer = hasBody ? await req.arrayBuffer() : undefined;

  let backendResp: Response;
  try {
    backendResp = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: hasBody ? bodyBuffer : undefined,
      cache: "no-store",
    });
  } catch {
    return noStoreJson({ detail: "ApplyPilot backend API is unreachable" }, { status: 502 });
  }

  const respHeaders = applyNoStoreHeaders(new Headers());
  const passthroughHeaders = ["content-type", "content-disposition"];
  for (const name of passthroughHeaders) {
    const val = backendResp.headers.get(name);
    if (val) {
      respHeaders.set(name, val);
    }
  }

  const payload = await backendResp.arrayBuffer();
  return new NextResponse(payload, {
    status: backendResp.status,
    headers: respHeaders,
  });
}

export const GET = proxyRequest;
export const POST = proxyRequest;
export const PUT = proxyRequest;
export const PATCH = proxyRequest;
export const DELETE = proxyRequest;
