import type { NextRequest } from "next/server";
import {
  SESSION_COOKIE_NAME,
  buildSessionCookieOptions,
  noStoreJson,
  validateSameOriginMutation,
} from "@/lib/server-backend";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const csrfError = validateSameOriginMutation(req);
  if (csrfError) return csrfError;

  const response = noStoreJson({ authenticated: false });
  response.cookies.set(SESSION_COOKIE_NAME, "", buildSessionCookieOptions(req, 0));
  return response;
}
