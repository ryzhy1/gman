export type Service = {
  id: string; name: string; description: string; owner: string; team: string;
  repository: string; grafana: string; splunk: string; bamboo: string;
  logs?: { splunk?: { index?: string | Record<string, string>; query?: string } };
  /** Who may edit the entry with the service owner role: subject, email, username or "group:<name>". */
  owners?: string[];
};

export type Role = 'viewer' | 'owner' | 'admin';
/** Signed-in user: an SSO session (cookie + CSRF token) or a shared Bearer token. */
export type Me = {
  auth: 'oidc' | 'token'; role: Role; subject: string; email: string; name: string;
  identities: string[]; csrf_token: string | null;
};
export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Просмотр', owner: 'Владелец сервиса', admin: 'Администратор платформы',
};

/** Mirrors the API check; the API remains the authority. */
export function canEdit(me: Me, service: Service): boolean {
  if (me.role === 'admin') return true;
  return me.role === 'owner' && me.auth === 'oidc'
    && (service.owners ?? []).some(o => me.identities.includes(o.trim().toLowerCase()));
}

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
  name: string; ready: boolean; unschedulable: boolean; pressure: string[]; roles: string[]; taints: string[];
  kubelet_version: string; os: string; kernel: string; runtime_version: string;
  cpu_capacity: number | null; cpu_allocatable: number | null; cpu_requested: number | null;
  memory_capacity_bytes: number | null; memory_allocatable_bytes: number | null; memory_requested_bytes: number | null;
  gpu_capacity: number; gpu_allocatable: number; gpu_requested: number;
  gpu_product: string; gpu_memory_mib: number | null; created_at: string | null;
};

export type Source = {
  source_id: string; hostname: string; source_type: string; agent_version: string;
  /** kubernetes-node: GPU snapshot of node `hostname` of cluster source `cluster`, without instances. */
  cluster?: string;
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
    super(status === 401 ? 'Требуется вход: токен неверен или сессия истекла'
      : status === 403 ? `Недостаточно прав${detail ? `: ${detail}` : ''}`
      : `Ошибка API: ${status}${detail ? ` · ${detail}` : ''}`);
  }
}

/** Empty token: the SSO session cookie is sent instead (same origin). */
function authHeaders(token: string): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function failure(response: Response): Promise<ApiError> {
  const detail = await response.json().then(b => b?.detail, () => undefined);
  return new ApiError(response.status, typeof detail === 'string' ? detail : undefined);
}

export async function get<T>(token: string, path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: authHeaders(token), signal, credentials: 'same-origin' });
  if (!response.ok) throw await failure(response);
  return response.json();
}

/** Mutating request; a cookie session must prove the request comes from the portal with its CSRF token. */
export async function send<T>(me: Me, token: string, method: 'PUT' | 'POST', path: string, body?: unknown): Promise<T> {
  const headers = authHeaders(token);
  if (me.csrf_token) headers['X-CSRF-Token'] = me.csrf_token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body),
  });
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
};
export type RangeResult = { points: [number, number | null][]; step: number };

/** Firing alerts from Alertmanager, without silenced and inhibited ones. */
export type ActiveAlert = {
  name: string; starts_at: string | null; labels: Record<string, string>;
  summary: string; description: string; runbook_url: string;
};
export type AlertList = { enabled: boolean; error: string | null; alerts: ActiveAlert[] };
