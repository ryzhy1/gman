export type Service = {
  id: string; name: string; description: string; owner: string; team: string;
  repository: string; grafana: string; splunk: string; bamboo: string;
};

export type Origin = Partial<Record<
  'compose_project' | 'compose_service' | 'compose_working_dir' | 'image_source' | 'image_revision' |
  'image_version' | 'image_created' | 'app_name' | 'app_version' | 'managed_by' | 'helm_chart' | 'field_managers',
  string>>;

export type Instance = {
  id: string; name: string; service_id: string; environment: string; runtime: string;
  node: string; namespace: string; controller: string;
  status: string; health: string; ready: boolean | null; reason: string; exit_code: number | null;
  last_termination_reason: string;
  created_at: string | null; started_at: string | null; finished_at: string | null;
  restarts: number; origin: Origin; config: Record<string, unknown>;
};

export type GPUProcess = { pid: number; container_id: string; used_memory_bytes: number | null };

/** Unsupported readings are null (older snapshots may omit the field entirely), never zero. */
export type GPU = {
  id: string; vendor: string; model: string; pci_bus_id?: string;
  driver_version?: string | null; pstate?: string | null;
  utilization_percent: number | null; memory_utilization_percent?: number | null;
  memory_total_bytes: number | null; memory_used_bytes: number | null;
  temperature_c: number | null; hotspot_temperature_c?: number | null; memory_temperature_c?: number | null;
  power_w: number | null; power_limit_w?: number | null;
  sm_clock_mhz?: number | null; memory_clock_mhz?: number | null; max_sm_clock_mhz?: number | null;
  fan_percent?: number | null; fan_rpm?: number | null;
  throttle_reasons?: string[]; ecc_uncorrected_errors?: number | null;
  pcie_generation?: number | null; pcie_width?: number | null;
  mig_mode?: string | null; persistence_mode?: string | null;
  processes?: GPUProcess[];
};

export type Node = {
  name: string; ready: boolean; unschedulable: boolean; pressure: string[]; roles: string[]; taints: string[];
  kubelet_version: string; os: string; kernel: string; runtime_version: string;
  cpu_capacity: number | null; cpu_allocatable: number | null; cpu_requested: number | null;
  memory_capacity_bytes: number | null; memory_allocatable_bytes: number | null; memory_requested_bytes: number | null;
  gpu_capacity: number; gpu_allocatable: number; gpu_requested: number;
  gpu_product: string; gpu_memory_mib: number | null; created_at: string | null;
};

export type Source = {
  source_id: string; hostname: string; source_type: string; agent_version: string;
  observed_at: string; received_at: string; cpu_count: number | null; memory_total_bytes: number | null;
  os?: string; kernel?: string; cpu_model?: string; runtime_version?: string; uptime_seconds?: number | null;
  instances: Instance[]; gpus: GPU[]; nodes?: Node[]; errors: string[];
};

export type Audit = {
  type: string; service_id: string; source_id?: string; actor: string; actor_kind?: string; actor_source: string;
  occurred_at: string; received_at: string; environment?: string;
  status?: string; build_id?: string; build_url?: string; release?: string; commit?: string; image_digest?: string;
  instance_id?: string; instance_name?: string; instance_count?: number;
  before?: Instance; after?: Instance; changes?: Record<string, [unknown, unknown]>;
};

export type Overview = { services: Service[]; sources: Source[]; events: Audit[]; server_time: string; grafana_url: string };
export type EventPage = { events: Audit[]; next: string | null };

/** One distinct configuration of an instance; returning to an older config is a new version. */
export type ConfigVersion = {
  id: number; service_id: string; source_id: string; instance_id: string;
  /** Empty for versions recorded before migration 0002. */
  instance_name: string; environment: string;
  config_hash: string; first_seen_at: string; last_seen_at: string;
  /** A currently observed instance runs this version. */
  current: boolean;
};
export type ConfigVersionPage = { versions: ConfigVersion[]; next: string | null };
/** Element-level list changes use the path "<field>[]". */
export type ConfigChange = { path: string; op: 'added' | 'removed' | 'changed'; before: unknown; after: unknown };
export type ConfigDiff = {
  from: ConfigVersion; to: ConfigVersion; identical: boolean;
  summary: Record<ConfigChange['op'], number>; changes: ConfigChange[];
};

export class ApiError extends Error {
  constructor(public status: number) {
    super(status === 401 ? 'Неверный токен просмотра' : `Ошибка API: ${status}`);
  }
}

export async function get<T>(token: string, path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` }, signal });
  if (!response.ok) throw new ApiError(response.status);
  return response.json();
}

export type MetricValues = Record<string, number | null>;
export type CurrentMetrics = {
  enabled: boolean; error: string | null;
  host?: Record<string, MetricValues>; container?: Record<string, MetricValues>; gpu?: Record<string, MetricValues>;
  /** Keyed by the inventory instance ID ("<pod_uid>:<container_name>"). */
  k8s_container?: Record<string, MetricValues>;
  /** Keyed by pod UID; network is per pod, shared by its containers. */
  k8s_pod?: Record<string, MetricValues>;
};
export type RangeResult = { points: [number, number | null][]; step: number };

/** Firing alerts from Alertmanager, without silenced and inhibited ones. */
export type ActiveAlert = {
  name: string; starts_at: string | null; labels: Record<string, string>;
  summary: string; description: string; runbook_url: string;
};
export type AlertList = { enabled: boolean; error: string | null; alerts: ActiveAlert[] };
