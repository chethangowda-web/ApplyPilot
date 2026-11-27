"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, apiClient } from "@/lib/api-client";
import type { UserResponse } from "@/lib/types";

export function useSessionUser(redirectPath = "/"): {
  user: UserResponse | null;
  authChecked: boolean;
} {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  const [user, setUser] = useState<UserResponse | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiClient
      .getSession()
      .then((res) => {
        if (!cancelled) {
          setUser(res.user);
          setAuthChecked(true);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.isUnauthorized()) {
          routerRef.current.replace(`/login?next=${encodeURIComponent(redirectPath)}`);
          return;
        }
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [redirectPath]);

  return { user, authChecked };
}
