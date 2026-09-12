import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { State } from "./types";

export function useRouterState() {
  const [state, setState] = useState<State | null>(null);
  const [locked, setLocked] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const generation = useRef(0);
  const reload = useCallback(async (signal?: AbortSignal) => {
    const request = ++generation.current;
    try {
      const value = await api<State>("/api/v1/state", { signal });
      if (request !== generation.current || signal?.aborted) return;
      setState(value);
      setLocked(false);
      setConnectionError("");
    } catch (error) {
      if (request !== generation.current || signal?.aborted) return;
      if (error instanceof ApiError && error.status === 401) setLocked(true);
      else
        setConnectionError(
          error instanceof Error
            ? error.message
            : "The router could not be reached. Reconnecting…",
        );
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      await reload(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [reload]);
  return { state, locked, connectionError, reload };
}
