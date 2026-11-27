import { NextRequest, NextResponse } from "next/server";
import {
  applyNoStoreHeaders,
  getBackendBaseUrl,
  getSessionTokenFromCookies,
} from "@/lib/server-backend";

export const dynamic = "force-dynamic";

function redirectNoStore(req: NextRequest, targetPath: string): NextResponse {
  const url = new URL(targetPath, req.nextUrl.origin);
  const response = NextResponse.redirect(url, { status: 302 });
  applyNoStoreHeaders(response.headers);
  return response;
}

export async function GET(req: NextRequest) {
  const token = await getSessionTokenFromCookies();
  if (!token) {
    return redirectNoStore(req, "/login?next=%2Fsettings");
  }

  const oauthError = req.nextUrl.searchParams.get("error");
  if (oauthError) {
    return redirectNoStore(req, "/settings?gmail=error&reason=oauth_denied");
  }

  const code = req.nextUrl.searchParams.get("code")?.trim() || "";
  const state = req.nextUrl.searchParams.get("state")?.trim() || "";
  if (!code || !state) {
    return redirectNoStore(req, "/settings?gmail=error&reason=invalid_callback");
  }

  try {
    const backendUrl = `${getBackendBaseUrl()}/api/integrations/gmail/callback`;
    const upstream = await fetch(backendUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ code, state }),
      cache: "no-store",
    });

    if (upstream.status === 401) {
      return redirectNoStore(req, "/login?next=%2Fsettings");
    }
    if (!upstream.ok) {
      return redirectNoStore(req, "/settings?gmail=error&reason=callback_failed");
    }

    return redirectNoStore(req, "/settings?gmail=connected");
  } catch {
    return redirectNoStore(req, "/settings?gmail=error&reason=backend_unreachable");
  }
}
