import { useEffect, useState } from "react";
import { api } from "./api";
import type { PerformanceSummary } from "./types";

/**
 * Load the per-engine performance summary for a window of hours.
 * `refreshKey` changes when request history changes, which reloads it.
 */
export function usePerformance(
  hours: number,
  active: boolean,
  refreshKey: string,
) {
  const [summary, setSummary] = useState<PerformanceSummary | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    async function load() {
      try {
        const value = await api<PerformanceSummary>(
          `/api/v1/performance?hours=${hours}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted) return;
        setSummary(value);
        setError("");
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure instanceof Error) setError(failure.message);
        else setError("Performance history could not be loaded");
      }
    }
    void load();
    return () => controller.abort();
  }, [hours, active, refreshKey]);
  return { summary, error };
}
