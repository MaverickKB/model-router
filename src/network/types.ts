import type { CompletionPath } from "../types";

export interface NetworkService {
  /** Stable identity for one independently observed API surface. */
  surface_id?: string;
  origin: string;
  base_url: string;
  /** The exact API base that discovery observed, even when it cannot route yet. */
  observed_base_url?: string;
  port: number;
  status: string;
  protocol: string;
  name?: string;
  models: {
    id: string;
    capabilities: string[];
    loaded?: boolean;
    available?: boolean;
    availability_reason?: string;
  }[];
  capabilities: string[];
  detail: string;
  checked_at: number;
  catalog_tracked?: boolean;
  /** Existing engine that uses this observed API base, if any. */
  engine_id?: string;
  engine_name?: string;
  /** A verified catalog can be registered without operator-supplied IDs. */
  registration_eligible?: boolean;
  /** The completion API is compatible, but it published no model identity. */
  needs_model_identity?: boolean;
  /** The OpenAI-compatible base URL derived from the detected operation. */
  compatible_base_url?: string;
  /** Exact compatible completion operations proven for this API base. */
  completion_paths?: CompletionPath[];
  /** Non-secret evidence from each catalog endpoint the inspector attempted. */
  catalog_attempts?: {
    path?: string;
    url?: string;
    status?: number | string | null;
    detail?: string;
  }[];
  /** Catalog paths declared by the API but not inspected under the safety limit. */
  catalog_paths_unprobed?: number;
}
export interface NetworkHost {
  address: string;
  name: string;
  status: string;
  scope: string;
  evidence: string;
  ports: number[];
  services: NetworkService[];
  scan_complete: boolean;
  filtered_ports?: number;
}
export interface NetworkReport {
  job_id?: string;
  warnings?: string[];
  total?: number;
  offset?: number;
  counts?: {
    addresses: number;
    responding: number;
    covered: number;
    services: number;
    models: number;
  };
  phase: string;
  progress?: number;
  targets?: string[];
  ports?: string;
  hosts: NetworkHost[];
  error: string;
  started_at?: number;
  completed_at: number;
  updated_at?: number;
}
