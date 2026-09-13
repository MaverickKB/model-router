import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { AccountSummary } from "./types";

// Account rows live outside the revisioned configuration, so they are fetched
// on demand (the GET /network precedent) and reloaded after every action.
export function useAccounts() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    try {
      const value = await api<{ accounts?: AccountSummary[] }>(
        "/api/v1/accounts",
      );
      setAccounts(Array.isArray(value.accounts) ? value.accounts : []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { accounts, reload, error };
}
