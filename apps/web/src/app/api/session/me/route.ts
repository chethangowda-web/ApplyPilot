import {
  getBackendBaseUrl,
  getSessionTokenFromCookies,
  noStoreJson,
} from "@/lib/server-backend";

export const dynamic = "force-dynamic";

export async function GET() {
  const token = await getSessionTokenFromCookies();
  if (!token) {
    return noStoreJson({ authenticated: false, user: null }, { status: 401 });
  }

  const backendBase = getBackendBaseUrl();
  try {
    const resp = await fetch(`${backendBase}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!resp.ok) {
      return noStoreJson({ authenticated: false, user: null }, { status: resp.status });
    }
    const user = await resp.json();
    return noStoreJson({ authenticated: true, user });
  } catch {
    return noStoreJson({ detail: "ApplyPilot backend API is unreachable" }, { status: 502 });
  }
}
