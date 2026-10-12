export type Service = {
  id: string; name: string; description: string; owner: string; team: string;
  repository: string; grafana: string; splunk: string; bamboo: string;
  logs?: { splunk?: { index?: string | Record<string, string>; query?: string } };
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
  /** Latest OOM kill (Docker `oom` event or OOMKilled, Kubernetes termination); kept after restarts. */
  last_oom_at?: string | null;
};

/** Docker owner by container_id; Kubernetes (node source) also by pod and instance_id "<pod uid>:<container>". */
export type GPUProcess = {
  pid: number; container_id: string; used_memory_bytes: number | null;
  pod_uid?: string; namespace?: string; pod?: string; container?: string; instance_id?: string;
};

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
  name: string; internal_ip?: string; ready: boolean; unschedulable: boolean; pressure: string[]; roles: string[]; taints: string[];
  kubelet_version: string; os: string; kernel: string; runtime_version: string;
  cpu_capacity: number | null; cpu_allocatable: number | null; cpu_requested: number | null;
  memory_capacity_bytes: number | null; memory_allocatable_bytes: number | null; memory_requested_bytes: number | null;
  gpu_capacity: number; gpu_allocatable: number; gpu_requested: number;
  gpu_product: string; gpu_memory_mib: number | null; created_at: string | null;
};

/** Node Events of the last 24 hours per node and reason (kubelet, node-problem-detector); see the audit for each one. */
export type NodeEvent = { node: string; reason: string; count: number; events: number; last_at: string };

export type Source = {
  source_id: string; hostname: string; source_type: string; agent_version: string;
  /** docker-host: address of the host (agent default route or GMAN_HOST_IP). */
  ip?: string;
  /** kubernetes-node: GPU snapshot of node `hostname` of cluster source `cluster`, without instances. */
  cluster?: string;
  observed_at: string; received_at: string; cpu_count: number | null; memory_total_bytes: number | null;
  os?: string; kernel?: string; cpu_model?: string; runtime_version?: string; uptime_seconds?: number | null;
  instances: Instance[]; gpus: GPU[]; nodes?: Node[]; errors: string[];
  /** kubernetes-cluster only. */
  node_events?: NodeEvent[];
};

export type Audit = {
  type: string; service_id: string; source_id?: string; actor: string; actor_kind?: string; actor_source: string;
  occurred_at: string; received_at: string; environment?: string;
  status?: string; build_id?: string; build_url?: string; release?: string; commit?: string; image_digest?: string;
  instance_id?: string; instance_name?: string; instance_count?: number;
  before?: Instance; after?: Instance; changes?: Record<string, [unknown, unknown]>;
  /** Release this event was correlated with (Bamboo deployment ↔ runtime). */
  release_id?: number; deployment_id?: string; version?: string; image?: string;
  /** runtime-event: a Kubernetes Event of a pod or a node, without its message (only fixed phrases in `detail`).
   * A node Event names a pod only when the agent mapped an OOM kill to it. */
  kind?: 'Pod' | 'Node'; node?: string;
  uid?: string; reason?: string; event_type?: 'Normal' | 'Warning'; count?: number; occurrences?: number;
  first_at?: string | null; last_at?: string; namespace?: string; pod?: string; container?: string;
  component?: string; detail?: string;
  /** history-gap: changes between gap_from and gap_to may be missing. */
  gap_from?: string; gap_to?: string; gap_seconds?: number; reasons?: string[]; dropped_snapshots?: number;
};

export type ReleaseStatus = 'started' | 'rolling-out' | 'succeeded' | 'failed' | 'superseded';

export type Release = {
  id: number; service_id: string; environment: string; release: string; version: string;
  build_id: string; build_url: string; commit: string; image: string; image_digest: string;
  initiator: string; initiator_kind: string; status: ReleaseStatus; status_reason: string;
  bamboo_status: string; correlation: '' | 'digest' | 'version'; superseded_by: number | null;
  progress: { total?: number; on_target?: number; healthy?: number; fresh?: number };
  started_at: string; rollout_started_at: string | null; bamboo_finished_at: string | null;
  finished_at: string | null; duration_seconds: number | null;
};

export type ReleaseInstance = {
  source_id: string; id: string; name: string; node: string; status: string; health: string;
  ready: boolean | null; reason: string; restarts: number; image: string; image_id: string;
  created_at: string | null; started_at: string | null;
  on_target: 'digest' | 'version' | null; healthy: boolean; new: boolean; crash: string | null;
};

export type ReleasePage = { releases: Release[]; next: string | null };
export type ReleaseDetail = { release: Release; events: Audit[]; instances: ReleaseInstance[] };

export type Overview = { services: Service[]; sources: Source[]; events: Audit[]; server_time: string; grafana_url: string };
/** Splunk search links built by the API; instances are keyed "<source_id>/<instance_id>". */
export type SplunkLinks = { enabled: boolean; error: string | null; service: string | null; instances: Record<string, string> };
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
  constructor(public status: number, detail?: string) {
    super(`Ошибка API: ${status}${detail ? ` · ${detail}` : ''}`);
  }
}

async function failure(response: Response): Promise<ApiError> {
  const detail = await response.json().then(b => b?.detail, () => undefined);
  return new ApiError(response.status, typeof detail === 'string' ? detail : undefined);
}

export async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal });
  if (!response.ok) throw await failure(response);
  return response.json();
}

export async function send<T>(method: 'PUT' | 'POST', path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = body === undefined ? {} : { 'Content-Type': 'application/json' };
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) throw await failure(response);
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
  /** Keyed by "namespace/pod/container": DCGM series of GPUs allocated to the container. */
  k8s_gpu?: Record<string, MetricValues>;
  /** Keyed by "<service>/<model>": vLLM servers of a catalog service (model names may contain "/"). */
  llm?: Record<string, MetricValues>;
};
export type RangeResult = { points: [number, number | null][]; step: number };

/** Firing alerts from Alertmanager, without silenced and inhibited ones. */
export type ActiveAlert = {
  name: string; starts_at: string | null; labels: Record<string, string>;
  summary: string; description: string; runbook_url: string;
};
export type AlertList = { enabled: boolean; error: string | null; alerts: ActiveAlert[] };
