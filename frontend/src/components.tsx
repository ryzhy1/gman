import { useEffect, useState } from 'react';
import { get, type Audit, type CurrentMetrics, type EventPage, type GPU, type GPUProcess, type Instance, type MetricValues, type Source } from './api';
import { bytes, cores, date, describeStatus, isHttp, percent, size, stale, statusTone, value } from './format';

/** `gpuSource`: where the instance's GPU processes are reported — the Docker host itself, or for a pod the
 * node-local source (kubernetes-node) of its node; absent when no node agent reports that node. */
export type Placed = Instance & { source: Source; gpuSource?: Source };

/** Node-local GPU snapshot of `node` in cluster source `cluster`. */
export function nodeGpuSource(sources: Source[], cluster: string, node: string) {
  return sources.find(s => s.source_type === 'kubernetes-node' && s.cluster === cluster && s.hostname === node);
}

export function placeInstances(sources: Source[]): Placed[] {
  return sources.flatMap(s => s.instances.map(i => ({
    ...i, source: s,
    gpuSource: i.runtime === 'docker' ? s : i.node ? nodeGpuSource(sources, s.source_id, i.node) : undefined,
  })));
}

/** Live usage of one instance: Docker by "<host_id>/<container_id>", Kubernetes by the instance ID itself. */
export function instanceUsage(i: Placed, metrics?: CurrentMetrics | null): MetricValues | undefined {
  return i.runtime === 'kubernetes' ? metrics?.k8s_container?.[i.id] : metrics?.container?.[`${i.source.source_id}/${i.id}`];
}

export type GpuHolding = { gpu: GPU; index: number; process: GPUProcess };

/** GPU processes of this container from nvidia-smi on its host/node: Docker by container ID, pods by instance ID. */
export function gpuHoldings(i: Placed): GpuHolding[] {
  const own = (p: GPUProcess) => i.runtime === 'docker' ? !!p.container_id && p.container_id === i.id : p.instance_id === i.id;
  return (i.gpuSource?.gpus ?? []).flatMap((gpu, index) => (gpu.processes ?? [])
    .filter(own).map(process => ({ gpu, index, process })));
}

export const gpuName = (h: { index: number; gpu: GPU }) => `GPU ${h.index}`;

/** "used / limit" with the limit only when one is set; absence of metrics stays visible. */
function Consumption({ used, limit, show }: { used?: number | null; limit?: unknown; show: (v: number) => string }) {
  if (used == null) return <span className="muted" title="Нет метрик для этого экземпляра">—</span>;
  const max = typeof limit === 'number' && limit > 0 ? limit : null;
  return <>{show(used)}{max != null && <small>из {show(max)} · {percent(used / max)}</small>}</>;
}

/** VRAM held by the container's processes; never the GPU's utilization, which is shared. */
function Vram({ instance }: { instance: Placed }) {
  const held = gpuHoldings(instance);
  if (!held.length) {
    const requested = (instance.config.gpu_requests as string[] | undefined)?.length;
    return <span className="muted" title={!instance.gpuSource ? 'Процессы GPU этого узла не передаются: нет агента kubernetes-node'
      : requested ? 'GPU запрошен, но в снимке нет процессов этого контейнера на GPU' : 'Нет процессов на GPU'}>—</span>;
  }
  const known = held.filter(h => h.process.used_memory_bytes != null);
  return <>
    {known.length ? size(known.reduce((n, h) => n + h.process.used_memory_bytes!, 0)) : 'н/д'}
    {held.map(h => <small key={h.gpu.id + h.process.pid} title={h.gpu.id}>
      {gpuName(h)} · PID {h.process.pid} · {h.process.used_memory_bytes == null ? 'н/д' : size(h.process.used_memory_bytes)}
    </small>)}
  </>;
}

/** `logs`: Splunk links keyed "<source_id>/<instance_id>"; the column appears only when some row has one. */
export function InstanceTable({ rows, metrics, logs }: { rows: Placed[]; metrics?: CurrentMetrics | null; logs?: Record<string, string> }) {
  // The VRAM column appears only where some row runs on a host that reports GPUs.
  const withGpu = rows.some(i => (i.gpuSource?.gpus.length ?? 0) > 0);
  const withLogs = rows.some(i => logs?.[`${i.source.source_id}/${i.id}`]);
  return <div className="table-wrap">
    <table>
      <thead><tr><th>Экземпляр</th><th>Размещение</th><th>Состояние</th><th>CPU, ядра</th><th>Память</th>
        {withGpu && <th title="Видеопамять, занятая процессами контейнера (nvidia-smi). Не загрузка GPU.">VRAM процессов</th>}
        <th>Рестарты</th><th>Создан / запущен</th>{withLogs && <th>Логи</th>}</tr></thead>
      <tbody>{rows.map(i => { const usage = instanceUsage(i, metrics); return <tr key={i.source.source_id + i.id}>
        <td><b>{i.name}</b><small>{i.environment} · {i.runtime}{i.controller && ` · ${i.controller}`}</small></td>
        <td>{i.node || i.source.hostname}<small>{i.namespace}</small></td>
        <td>
          {stale(i.source)
            ? <span className="badge warn">Данные устарели</span>
            : <span className={`badge ${statusTone(i)}`}>{describeStatus(i)}</span>}
          {i.last_termination_reason && <small>Прошлое завершение: {i.last_termination_reason}</small>}
        </td>
        <td><Consumption used={usage?.cpu_cores} limit={i.config.cpu_limit} show={cores} /></td>
        <td><Consumption used={usage?.memory_bytes} limit={i.config.memory_limit_bytes} show={size} /></td>
        {withGpu && <td><Vram instance={i} /></td>}
        <td>{i.restarts}</td>
        <td>{date(i.created_at)}<small>Старт: {date(i.started_at)}</small></td>
        {withLogs && <td>{logs?.[`${i.source.source_id}/${i.id}`]
          ? <a href={logs[`${i.source.source_id}/${i.id}`]} target="_blank" rel="noopener noreferrer">Логи в Splunk ↗</a> : '—'}</td>}
      </tr>; })}</tbody>
    </table>
    {!rows.length && <div className="empty">Экземпляры еще не обнаружены.</div>}
  </div>;
}

export const CONFIG_LABELS: [string, string, ((v: unknown) => string)?][] = [
  ['image', 'Образ'], ['image_id', 'Image ID'],
  ['cpu_request', 'CPU request, ядра'], ['cpu_limit', 'CPU limit, ядра'], ['cpuset', 'Привязка к CPU'],
  ['memory_request_bytes', 'Memory request', v => bytes(v as number | null)],
  ['memory_limit_bytes', 'Memory limit', v => bytes(v as number | null)],
  ['gpu_requests', 'GPU'], ['runtime', 'Runtime'],
  ['restart_policy', 'Restart policy'], ['probes', 'Проверки здоровья'],
  ['ports', 'Порты'], ['networks', 'Сети'], ['network_mode', 'Network mode'],
  ['mounts', 'Тома и mounts'], ['user', 'Пользователь процесса'],
  ['privileged', 'Privileged'], ['readonly_rootfs', 'Read-only rootfs'],
  ['cap_add', 'Добавленные capabilities'], ['cap_drop', 'Удаленные capabilities'],
  ['pid_mode', 'PID mode'], ['ipc_mode', 'IPC mode'],
  ['shm_size_bytes', '/dev/shm', v => bytes(v as number | null)],
  ['ulimits', 'Ulimits'], ['log_driver', 'Драйвер логов'],
];

const ORIGIN_LABELS: [keyof Instance['origin'], string][] = [
  ['image_source', 'Исходный код образа'], ['image_revision', 'Ревизия образа'], ['image_version', 'Версия образа'],
  ['image_created', 'Образ собран'], ['compose_project', 'Compose-проект'], ['compose_service', 'Compose-сервис'],
  ['compose_working_dir', 'Каталог compose'], ['app_name', 'Приложение'], ['app_version', 'Версия приложения'],
  ['managed_by', 'Управляется'], ['helm_chart', 'Helm chart'], ['field_managers', 'Изменяли объект (field managers)'],
];

export function ConfigView({ instance }: { instance: Placed }) {
  const origin = ORIGIN_LABELS.filter(([k]) => instance.origin?.[k]);
  const held = gpuHoldings(instance);
  return <details className="event">
    <summary><b>{instance.name}</b><span>{instance.runtime}</span><span className="muted">{instance.node || instance.source.hostname}</span>
      {held.length > 0 && <span className="badge info" title="Видеопамять процессов контейнера, не загрузка GPU">
        VRAM {size(held.reduce((n, h) => n + (h.process.used_memory_bytes ?? 0), 0))} на {new Set(held.map(h => h.gpu.id)).size} GPU</span>}</summary>
    <dl>
      <div><dt>Создан</dt><dd>{date(instance.created_at)}</dd></div>
      <div><dt>Запущен</dt><dd>{date(instance.started_at)}</dd></div>
      {instance.finished_at && <div><dt>Завершен</dt><dd>{date(instance.finished_at)}</dd></div>}
      {CONFIG_LABELS.map(([key, label, show]) => <div key={key}>
        <dt>{label}</dt><dd>{(show ?? value)(instance.config[key])}</dd>
      </div>)}
    </dl>
    {held.length > 0 && <>
      <h3 className="subheading">Процессы на GPU</h3>
      <div className="table-wrap"><table>
        <thead><tr><th>GPU</th><th>PID</th><th>VRAM процесса</th></tr></thead>
        <tbody>{held.map(h => <tr key={h.gpu.id + h.process.pid}>
          <td>{gpuName(h)} · {h.gpu.model}<small>{h.gpu.id}</small></td>
          <td>{h.process.pid}</td>
          <td>{h.process.used_memory_bytes == null ? 'н/д' : size(h.process.used_memory_bytes)}
            {h.gpu.memory_total_bytes != null && h.process.used_memory_bytes != null &&
              <small>{percent(h.process.used_memory_bytes / h.gpu.memory_total_bytes)} от {size(h.gpu.memory_total_bytes)}</small>}</td>
        </tr>)}</tbody>
      </table></div>
      <p className="note">Видеопамять, выделенная процессам контейнера, по снимку nvidia-smi от {date((instance.gpuSource ?? instance.source).observed_at)}. Загрузка GPU общая для всех его процессов и контейнеру не приписывается.</p>
    </>}
    {origin.length > 0 && <>
      <h3 className="subheading">Происхождение</h3>
      <dl>{origin.map(([k, label]) => <div key={k}>
        <dt>{label}</dt>
        <dd>{isHttp(instance.origin[k]) ? <a href={instance.origin[k]} target="_blank" rel="noreferrer">{instance.origin[k]} ↗</a> : instance.origin[k]}</dd>
      </div>)}</dl>
      <p className="note">Метки и field managers показывают, чем создан экземпляр, но не подтверждают, кто его запустил.</p>
    </>}
  </details>;
}

const TITLES: Record<string, string> = {
  'source-connected': 'Источник подключен', 'instance-appeared': 'Появился экземпляр',
  'instance-removed': 'Экземпляр удален', 'config-changed': 'Изменилась конфигурация',
  'instance-restarted': 'Перезапуск', 'state-changed': 'Изменилось состояние',
  'service-created': 'Сервис добавлен в каталог', 'service-updated': 'Изменена запись каталога',
};

function title(e: Audit) {
  if (e.type === 'service-created' || e.type === 'service-updated') return `${TITLES[e.type]} · ${e.service_id}`;
  if (e.type === 'deployment') return `Деплой · ${e.status}${e.release ? ` · ${e.release}` : ''}`;
  if (e.type === 'source-connected') return `${TITLES[e.type]} · ${e.source_id} · ${e.instance_count} экз.`;
  return `${TITLES[e.type] ?? e.type} · ${e.instance_name ?? e.instance_id}`;
}

/** Runtime changes a deployment causes; without a correlated release their initiator is unknown. */
const LAUNCHES = ['instance-appeared', 'config-changed'];

function actorLabel(e: Audit) {
  if (e.actor !== 'unknown') return `${e.actor} (${e.actor_kind})`;
  return LAUNCHES.includes(e.type) ? 'Ручной запуск / инициатор неизвестен' : 'Автор неизвестен';
}

export function EventItem({ e, onRelease }: { e: Audit; onRelease?: (id: number, serviceId: string) => void }) {
  const tone = e.type === 'deployment' && e.status === 'failed' || e.type === 'instance-removed' ? 'bad'
    : e.type === 'state-changed' || e.type === 'instance-restarted' ? 'warn' : '';
  return <details className="event">
    <summary>
      <span className={`dot ${tone}`} /><b>{title(e)}</b>
      <span>{actorLabel(e)}</span>
      {e.release_id != null && (onRelease
        ? <button className="badge info" onClick={ev => { ev.preventDefault(); onRelease(e.release_id!, e.service_id); }}>Релиз #{e.release_id} →</button>
        : <span className="badge info">Релиз #{e.release_id}</span>)}
      <time>{date(e.occurred_at)}</time>
    </summary>
    <p>{e.actor_source} · получено {date(e.received_at)}</p>
    {e.type === 'deployment' && <dl>
      <div><dt>Окружение</dt><dd>{e.service_id} · {e.environment}</dd></div>
      <div><dt>Сборка</dt><dd>{isHttp(e.build_url) ? <a href={e.build_url} target="_blank" rel="noreferrer">{e.build_id} ↗</a> : e.build_id}</dd></div>
      <div><dt>Коммит</dt><dd>{e.commit || '—'}</dd></div>
      <div><dt>Image digest</dt><dd>{e.image_digest || '—'}</dd></div>
    </dl>}
    {e.changes && <div className="table-wrap"><table>
      <thead><tr><th>Поле</th><th>Было</th><th>Стало</th></tr></thead>
      <tbody>{Object.entries(e.changes).map(([field, [before, after]]) => <tr key={field}>
        <td>{field}</td><td>{value(before)}</td><td>{value(after)}</td>
      </tr>)}</tbody>
    </table></div>}
    {(e.after || e.before) && <pre>{JSON.stringify(e.after ?? e.before, null, 2)}</pre>}
  </details>;
}

/** Paged history straight from the API, so a quiet service is not crowded out by busy ones. */
export function History({ token, serviceId, refreshKey, onRelease }: {
  token: string; serviceId?: string; refreshKey: string; onRelease?: (id: number, serviceId: string) => void;
}) {
  const [events, setEvents] = useState<Audit[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [hideState, setHideState] = useState(true);
  const base = `/api/events?limit=100${serviceId ? `&service_id=${encodeURIComponent(serviceId)}` : ''}`;

  useEffect(() => {
    const ctrl = new AbortController();
    get<EventPage>(token, base, ctrl.signal)
      .then(page => { setEvents(page.events); setNext(page.next); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, base, refreshKey]);

  async function more() {
    try {
      const page = await get<EventPage>(token, `${base}&before=${encodeURIComponent(next!)}`);
      setEvents(old => [...old, ...page.events]);
      setNext(page.next);
    } catch (e) { setError((e as Error).message); }
  }

  const shown = hideState ? events.filter(e => e.type !== 'state-changed') : events;
  return <div>
    <label className="toggle"><input type="checkbox" checked={hideState} onChange={e => setHideState(e.target.checked)} /> Скрыть изменения health/ready</label>
    {error && <div role="alert" className="alert">{error}</div>}
    {shown.map(e => <EventItem key={e.received_at + (e.instance_id ?? '') + (e.build_id ?? '') + e.type} e={e} onRelease={onRelease} />)}
    {!shown.length && <div className="empty">Событий пока нет. История появляется после подключения источников.</div>}
    {next && <button className="more" onClick={more}>Показать еще</button>}
  </div>;
}

