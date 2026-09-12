export interface NetworkService {
  origin: string;
  base_url: string;
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
