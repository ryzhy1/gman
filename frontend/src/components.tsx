import { useEffect, useState, type ReactNode } from 'react';
import { get, type Audit, type CurrentMetrics, type EventPage, type GPU, type GPUProcess, type Instance, type MetricValues, type Source } from './api';
import { bytes, cores, date, describeStatus, isHttp, percent, size, stale, statusTone, value } from './format';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/table';
import { linkButton, outlineButton, tone as badge } from '@/ui/tone';
import { ConfigSections } from './config-sections';

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

/** Server address of an instance: the Docker host, or the node a pod runs on; hostname/node name until the agent reports IPs. */
export function serverAddress(i: Placed): string {
  if (i.runtime === 'docker') return i.source.ip || i.source.hostname;
  return i.source.nodes?.find(n => n.name === i.node)?.internal_ip || i.node;
}

/** Services page filtered to one server: a Docker host, or a Kubernetes node of a cluster source. */
export const serverHref = (sourceId: string, node?: string) =>
  `#server/${encodeURIComponent(sourceId)}${node ? `/${encodeURIComponent(node)}` : ''}`;

/** Published ports: the host side of Docker bindings ("0.0.0.0:8080->80/tcp" → 8080), container ports of a pod. */
export function servicePorts(i: Placed): string[] {
  const ports = Array.isArray(i.config.ports) ? i.config.ports.map(String) : [];
  return [...new Set(ports.map(p => i.runtime === 'docker'
    ? p.split('->')[0].split(':').pop() || p.split('->')[1] || p
    : p.replace(/\/TCP$/, '')))];
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
export function instanceHref(i: Placed): string {
  return `#instances/${encodeURIComponent(i.source.source_id)}/${encodeURIComponent(i.id)}`;
}

export function InstanceTable({ rows, metrics, logs }: { rows: Placed[]; metrics?: CurrentMetrics | null; logs?: Record<string, string> }) {
  // The VRAM column appears only where some row runs on a host that reports GPUs.
  const withGpu = rows.some(i => (i.gpuSource?.gpus.length ?? 0) > 0);
  const withLogs = rows.some(i => logs?.[`${i.source.source_id}/${i.id}`]);
  return <div className="table-wrap">
    <Table>
      <TableHeader><TableRow><TableHead>Экземпляр</TableHead><TableHead>Размещение</TableHead><TableHead>Состояние</TableHead><TableHead>CPU, ядра</TableHead><TableHead>Память</TableHead>
        {withGpu && <TableHead title="Видеопамять, занятая процессами контейнера (nvidia-smi). Не загрузка GPU.">VRAM процессов</TableHead>}
        <TableHead>Рестарты</TableHead><TableHead>Создан / запущен</TableHead>{withLogs && <TableHead>Логи</TableHead>}</TableRow></TableHeader>
      <TableBody>{rows.map(i => { const usage = instanceUsage(i, metrics); return <TableRow key={i.source.source_id + i.id}>
        <TableCell><a className={linkButton()} href={instanceHref(i)}>{i.name} ↗</a><small>{i.environment} · {i.runtime}{i.controller && ` · ${i.controller}`}</small></TableCell>
        <TableCell>{i.node || i.source.hostname}<small>{i.namespace}</small></TableCell>
        <TableCell>
          {stale(i.source)
            ? <span className={badge('warn')}>Данные устарели</span>
            : <span className={badge(statusTone(i))}>{describeStatus(i)}</span>}
          {i.last_termination_reason && <small>Прошлое завершение: {i.last_termination_reason}</small>}
          {i.last_oom_at && <small>Последний OOM: {date(i.last_oom_at)}</small>}
        </TableCell>
        <TableCell><Consumption used={usage?.cpu_cores} limit={i.config.cpu_limit} show={cores} /></TableCell>
        <TableCell><Consumption used={usage?.memory_bytes} limit={i.config.memory_limit_bytes} show={size} /></TableCell>
        {withGpu && <TableCell><Vram instance={i} /></TableCell>}
        <TableCell>{i.restarts}</TableCell>
        <TableCell>{date(i.created_at)}<small>Старт: {date(i.started_at)}</small></TableCell>
        {withLogs && <TableCell>{logs?.[`${i.source.source_id}/${i.id}`]
          ? <a href={logs[`${i.source.source_id}/${i.id}`]} target="_blank" rel="noopener noreferrer">Логи в Splunk ↗</a> : '—'}</TableCell>}
      </TableRow>; })}</TableBody>
    </Table>
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
  ['mounts', 'Тома и mounts'], ['user', 'Пользователь процесса'], ['env', 'Переменные окружения'], ['env_files', 'Переменные из файлов'],
  ['command', 'Команда (entrypoint)'], ['args', 'Аргументы запуска'],
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

/** Pretty-printed JSON with token colors; React escapes the text, no HTML injection. */
function Json({ value: data }: { value: unknown }) {
  const text = JSON.stringify(data, null, 2);
  const parts: ReactNode[] = [];
  const token = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}[\],])/g;
  let last = 0;
  for (const m of text.matchAll(token)) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const [all, str, colon, num, lit] = m;
    const cls = str ? (colon ? 'tk-key' : 'tk-str') : num ? 'tk-num' : lit ? 'tk-lit' : 'tk-punct';
    parts.push(<span key={m.index} className={cls}>{str ?? all}</span>);
    if (colon) parts.push(colon);
    last = m.index + all.length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

export function ConfigView({ instance, expanded = false }: { instance: Placed; expanded?: boolean }) {
  const origin = ORIGIN_LABELS.filter(([k]) => instance.origin?.[k]);
  const held = gpuHoldings(instance);
  return <details className="event" open={expanded || undefined}>
    <summary><b>{instance.name}</b><span>{instance.runtime}</span><span className="muted">{instance.node || instance.source.hostname}</span>
      {held.length > 0 && <span className={badge('info')} title="Видеопамять процессов контейнера, не загрузка GPU">
        VRAM {size(held.reduce((n, h) => n + (h.process.used_memory_bytes ?? 0), 0))} на {new Set(held.map(h => h.gpu.id)).size} GPU</span>}</summary>
    <dl>
      <div><dt>Создан</dt><dd>{date(instance.created_at)}</dd></div>
      <div><dt>Запущен</dt><dd>{date(instance.started_at)}</dd></div>
      {instance.finished_at && <div><dt>Завершен</dt><dd>{date(instance.finished_at)}</dd></div>}
    </dl>
    <ConfigSections instance={instance} />
    {held.length > 0 && <>
      <h3 className="subheading">Процессы на GPU</h3>
      <div className="table-wrap"><Table>
        <TableHeader><TableRow><TableHead>GPU</TableHead><TableHead>PID</TableHead><TableHead>VRAM процесса</TableHead></TableRow></TableHeader>
        <TableBody>{held.map(h => <TableRow key={h.gpu.id + h.process.pid}>
          <TableCell>{gpuName(h)} · {h.gpu.model}<small>{h.gpu.id}</small></TableCell>
          <TableCell>{h.process.pid}</TableCell>
          <TableCell>{h.process.used_memory_bytes == null ? 'н/д' : size(h.process.used_memory_bytes)}
            {h.gpu.memory_total_bytes != null && h.process.used_memory_bytes != null &&
              <small>{percent(h.process.used_memory_bytes / h.gpu.memory_total_bytes)} от {size(h.gpu.memory_total_bytes)}</small>}</TableCell>
        </TableRow>)}</TableBody>
      </Table></div>
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
    <p className="note">Наблюдаемая конфигурация из снимка агента. Это не исходный Compose-файл или манифест Kubernetes.</p>
    <details className="raw-config">
      <summary>Исходный JSON</summary>
      <pre className="config-code" tabIndex={0} aria-label={`Конфигурация ${instance.name} в JSON`}><code><Json value={instance.config} /></code></pre>
    </details>
  </details>;
}

const TITLES: Record<string, string> = {
  'source-connected': 'Источник подключен', 'instance-appeared': 'Появился экземпляр',
  'instance-removed': 'Экземпляр удален', 'config-changed': 'Изменилась конфигурация',
  'instance-restarted': 'Перезапуск', 'state-changed': 'Изменилось состояние',
  'service-created': 'Сервис добавлен в каталог', 'service-updated': 'Изменена запись каталога',
  'instance-oom': 'OOM kill', 'runtime-event': 'Событие Kubernetes', 'history-gap': 'Пробел в истории',
};

const GAP_REASONS: Record<string, string> = {
  'source-silent': 'источник не присылал снимки',
  'spool-overflow': 'агент отбросил неотправленные снимки (переполнение spool)',
};

function title(e: Audit) {
  if (e.type === 'service-created' || e.type === 'service-updated') return `${TITLES[e.type]} · ${e.service_id}`;
  if (e.type === 'deployment') return `Деплой · ${e.status}${e.release ? ` · ${e.release}` : ''}`;
  if (e.type === 'source-connected') return `${TITLES[e.type]} · ${e.source_id} · ${e.instance_count} экз.`;
  if (e.type === 'history-gap') return `${TITLES[e.type]} · ${e.source_id}`;
  if (e.type === 'runtime-event') {
    const object = e.kind === 'Node' ? `узел ${e.node}${e.instance_name ? ` · ${e.instance_name}` : ''}` : e.instance_name;
    return `${e.reason}${e.count && e.count > 1 ? ` ×${e.count}` : ''} · ${object}`;
  }
  return `${TITLES[e.type] ?? e.type} · ${e.instance_name ?? e.instance_id}`;
}

function tone(e: Audit) {
  if (e.type === 'deployment' && e.status === 'failed' || e.type === 'instance-removed' || e.type === 'instance-oom') return 'bad';
  if (e.type === 'runtime-event') return e.event_type === 'Warning' ? 'warn' : '';
  return e.type === 'state-changed' || e.type === 'instance-restarted' || e.type === 'history-gap' ? 'warn' : '';
}

/** Runtime changes a deployment causes; without a correlated release their initiator is unknown. */
const LAUNCHES = ['instance-appeared', 'config-changed'];

function actorLabel(e: Audit) {
  if (e.actor !== 'unknown') return `${e.actor} (${e.actor_kind})`;
  return LAUNCHES.includes(e.type) ? 'Ручной запуск / инициатор неизвестен' : 'Автор неизвестен';
}

export function EventItem({ e, onRelease }: { e: Audit; onRelease?: (id: number, serviceId: string) => void }) {
  return <details className="event">
    <summary>
      <span className={`dot ${tone(e)}`} /><b>{title(e)}</b>
      {e.type === 'history-gap'
        ? <span className={badge('warn')}>Данные за период могут быть неполными</span>
        : <span>{actorLabel(e)}</span>}
      {e.release_id != null && (onRelease
        ? <button className={badge('info', 'cursor-pointer')} onClick={ev => { ev.preventDefault(); onRelease(e.release_id!, e.service_id); }}>Релиз #{e.release_id} →</button>
        : <span className={badge('info')}>Релиз #{e.release_id}</span>)}
      <time>{date(e.occurred_at)}</time>
    </summary>
    <p>{e.actor_source} · получено {date(e.received_at)}</p>
    {e.type === 'history-gap' && <>
      <p className="note">С {date(e.gap_from)} по {date(e.gap_to)} изменения экземпляров могли быть не записаны:
        {' '}{(e.reasons ?? []).map(r => GAP_REASONS[r] ?? r).join('; ')}
        {e.dropped_snapshots ? ` (${e.dropped_snapshots} шт.)` : ''}. Состояние после пробела актуально.</p>
    </>}
    {e.type === 'runtime-event' && <dl>
      <div><dt>Причина</dt><dd>{e.reason} ({e.event_type}){e.detail ? ` · ${e.detail}` : ''}</dd></div>
      {e.kind === 'Node' && <div><dt>Узел</dt><dd>{e.node}</dd></div>}
      {e.pod
        ? <div><dt>Pod</dt><dd>{e.namespace}/{e.pod}{e.container ? ` · контейнер ${e.container}` : ''}</dd></div>
        : e.kind === 'Node' && <div><dt>Pod</dt><dd>не определен: событие относится к узлу</dd></div>}
      <div><dt>Повторов</dt><dd>{e.count} всего, новых {e.occurrences}</dd></div>
      <div><dt>Впервые / последний раз</dt><dd>{date(e.first_at)} / {date(e.last_at)}</dd></div>
      <div><dt>Компонент</dt><dd>{e.component || '—'}</dd></div>
    </dl>}
    {e.type === 'deployment' && <dl>
      <div><dt>Окружение</dt><dd>{e.service_id} · {e.environment}</dd></div>
      <div><dt>Сборка</dt><dd>{isHttp(e.build_url) ? <a href={e.build_url} target="_blank" rel="noreferrer">{e.build_id} ↗</a> : e.build_id}</dd></div>
      <div><dt>Коммит</dt><dd>{e.commit || '—'}</dd></div>
      <div><dt>Image digest</dt><dd>{e.image_digest || '—'}</dd></div>
    </dl>}
    {e.changes && <div className="table-wrap"><Table>
      <TableHeader><TableRow><TableHead>Поле</TableHead><TableHead>Было</TableHead><TableHead>Стало</TableHead></TableRow></TableHeader>
      <TableBody>{Object.entries(e.changes).map(([field, [before, after]]) => <TableRow key={field}>
        <TableCell>{field}</TableCell><TableCell>{value(before)}</TableCell><TableCell>{value(after)}</TableCell>
      </TableRow>)}</TableBody>
    </Table></div>}
    {(e.after || e.before) && <pre>{JSON.stringify(e.after ?? e.before, null, 2)}</pre>}
  </details>;
}

/** Paged history straight from the API, so a quiet service is not crowded out by busy ones. */
export function History({ serviceId, refreshKey, onRelease }: {
  serviceId?: string; refreshKey: string; onRelease?: (id: number, serviceId: string) => void;
}) {
  const [events, setEvents] = useState<Audit[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [hideState, setHideState] = useState(true);
  const base = `/api/events?limit=100${serviceId ? `&service_id=${encodeURIComponent(serviceId)}` : ''}`;

  useEffect(() => {
    const ctrl = new AbortController();
    get<EventPage>(base, ctrl.signal)
      .then(page => { setEvents(page.events); setNext(page.next); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [base, refreshKey]);

  async function more() {
    try {
      const page = await get<EventPage>(`${base}&before=${encodeURIComponent(next!)}`);
      setEvents(old => [...old, ...page.events]);
      setNext(page.next);
    } catch (e) { setError((e as Error).message); }
  }

  // The API records a history gap once per affected service; the audit shows each gap once.
  const gaps = new Set<string>();
  const shown = events.filter(e => {
    if (hideState && e.type === 'state-changed') return false;
    if (e.type !== 'history-gap' || serviceId) return true;
    const key = `${e.source_id}|${e.gap_from}|${e.gap_to}`;
    return !gaps.has(key) && !!gaps.add(key);
  });
  return <div>
    <label className="toggle"><input type="checkbox" checked={hideState} onChange={e => setHideState(e.target.checked)} /> Скрыть изменения health/ready</label>
    {error && <div role="alert" className="alert">{error}</div>}
    {shown.map(e => <EventItem key={[e.received_at, e.source_id, e.instance_id, e.build_id, e.uid, e.service_id, e.type].join('|')} e={e} onRelease={onRelease} />)}
    {!shown.length && <div className="empty">Событий пока нет. История появляется после подключения источников.</div>}
    {next && <button className={outlineButton('mt-4')} onClick={more}>Показать еще</button>}
  </div>;
}

