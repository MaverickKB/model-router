import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { PortalMe, PortalStatus } from "./types";

const POLL_MS = 30_000;

// The portal never requests operator state: only /api/v1/portal/* is fetched.
export function usePortalState() {
  const [status, setStatus] = useState<PortalStatus | null>(null);
  const [me, setMe] = useState<PortalMe | null>(null);
  const [signedOut, setSignedOut] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  const loadStatus = useCallback(async (signal?: AbortSignal) => {
    const value = await api<PortalStatus>("/api/v1/portal/status", { signal });
    if (signal?.aborted) return null;
    setStatus(value);
    if (!value.enabled || !value.signed_in) {
      setMe(null);
      setSignedOut(!value.signed_in);
    }
    return value;
  }, []);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      const request = ++generation.current;
      try {
        const value = await api<PortalMe>("/api/v1/portal/me", { signal });
        if (request !== generation.current || signal?.aborted) return;
        setMe(value);
        setSignedOut(false);
        setError("");
      } catch (err) {
        if (request !== generation.current || signal?.aborted) return;
        if (err instanceof ApiError && err.status === 401) {
          setMe(null);
          setSignedOut(true);
        } else if (err instanceof ApiError && err.status === 403) {
          // Accounts were switched off, or this account was suspended: the
          // status answer decides which, without parsing the detail string.
          setMe(null);
          setSignedOut(true);
          setError(err.message);
          await loadStatus(signal).catch(() => undefined);
        } else {
          setError(
            err instanceof Error
              ? err.message
              : "The router could not be reached. Reconnecting…",
          );
        }
      }
    },
    [loadStatus],
  );

  useEffect(() => {
    const controller = new AbortController();
    loadStatus(controller.signal)
      .then((value) => {
        if (value?.enabled && value.signed_in) return reload(controller.signal);
      })
      .catch((err) => {
        if (!controller.signal.aborted)
          setError(err instanceof Error ? err.message : String(err));
      });
    return () => controller.abort();
  }, [loadStatus, reload]);

  const signedIn = me !== null;
  useEffect(() => {
    if (!signedIn) return;
    const controller = new AbortController();
    const timer = setInterval(() => void reload(controller.signal), POLL_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [signedIn, reload]);

  return { status, me, signedOut, error, reload, loadStatus, setError };
}
