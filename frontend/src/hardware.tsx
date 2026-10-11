import type { CurrentMetrics, GPU, GPUProcess, MetricValues, Node, Source } from './api';
import { nodeGpuSource, type Placed } from './components';
import { bytes, cores, date, grafanaLink, num, percent, rate, size, stale, unit, uptime } from './format';

type Open = (serviceId: string) => void;

function Gauge({ label, ratio, detail }: { label: string; ratio: number | null | undefined; detail?: string }) {
  return <div className="gauge">
    <span>{label}</span><b>{percent(ratio)}</b>
    {ratio != null && <progress max="1" value={Math.min(1, ratio)} />}
    {detail && <small>{detail}</small>}
  </div>;
}

/** Host load from the metrics store; absent metrics are reported, not drawn as zero. */
function HostGauges({ host, cpuCount, memoryTotal, missing }: {
  host?: MetricValues; cpuCount?: number | null; memoryTotal?: number | null; missing: React.ReactNode;
}) {
  if (!host) return <p className="note">{missing}</p>;
  return <div className="gauges">
    <Gauge label="CPU" ratio={host.cpu_utilization} detail={`${cpuCount ?? '?'} логических CPU · load ${num(host.load_1m, 2)} · iowait ${percent(host.cpu_iowait)}`} />
    <Gauge label="Память" ratio={host.memory_utilization} detail={`${size(host.memory_used_bytes)} из ${memoryTotal == null ? '—' : size(memoryTotal)}`} />
    <Gauge label="Самая заполненная ФС" ratio={host.filesystem_max_utilization} detail={`Диск ${rate(host.disk_io_bytes_per_second)}`} />
    <div className="gauge"><span>Сеть</span><b>↓ {rate(host.network_rx_bytes_per_second)}</b><small>↑ {rate(host.network_tx_bytes_per_second)}</small></div>
  </div>;
}

// nvidia-smi clocks_throttle_reasons; gpu_idle and the informational ones are normal operation.
const THROTTLE: Record<string, [string, '' | 'info' | 'warn' | 'bad']> = {
  gpu_idle: ['Простой: частоты снижены без нагрузки', 'info'],
  applications_clocks: ['Частоты ограничены настройкой приложений', 'info'],
  display_clocks: ['Частоты ограничены дисплеем', 'info'],
  sync_boost: ['Sync Boost: частоты выровнены с другими GPU', 'info'],
  sw_power_cap: ['Ограничение по мощности (power cap)', 'warn'],
  sw_thermal: ['Программное тепловое ограничение', 'warn'],
  hw_slowdown: ['Аппаратное замедление', 'bad'],
  hw_thermal: ['Аппаратное тепловое ограничение (перегрев)', 'bad'],
  hw_power_brake: ['Аппаратный тормоз по питанию (внешний сигнал)', 'bad'],
};

/** One card per GPU: headline numbers, details, throttling, processes with container attribution. */
function GpuCard({ title, id, inventory, live, observedAt, instances, onService, absent }: {
  title: string; id: string; inventory?: GPU; live?: MetricValues; observedAt?: string; absent?: string;
  instances: Placed[]; onService?: Open;
}) {
  // Live DCGM readings win; inventory fills in what DCGM does not report. Nulls never override values.
  const g: Record<string, number | null | undefined> = { ...(inventory as unknown as Record<string, number | null> | undefined) };
  for (const [k, v] of Object.entries(live ?? {})) if (v != null) g[k] = v;
  // Rows that a source does not report (AMD sysfs, DCGM-only cards) are hidden rather than shown as columns of dashes.
  const nvidia = inventory?.vendor === 'NVIDIA';
  const memRatio = g.memory_used_bytes != null && g.memory_total_bytes ? g.memory_used_bytes / g.memory_total_bytes : null;
  const powerRatio = g.power_w != null && g.power_limit_w ? g.power_w / g.power_limit_w : null;
  const throttles = inventory?.throttle_reasons ?? [];
  const details: [string, React.ReactNode, boolean?][] = [
    ['Драйвер', inventory?.driver_version ?? '—'],
    ['P-state', inventory?.pstate ?? '—'],
    ['Частота SM', g.sm_clock_mhz == null ? '—' : `${num(g.sm_clock_mhz, 0)}${g.max_sm_clock_mhz != null ? ` из ${num(g.max_sm_clock_mhz, 0)}` : ''} MHz`, true],
    ['Частота памяти', unit(g.memory_clock_mhz, 'MHz'), true],
    ['Контроллер памяти', unit(g.memory_utilization_percent, '%'), true],
    ['Вентилятор', g.fan_percent == null && g.fan_rpm == null ? '—'
      : [g.fan_percent != null && `${num(g.fan_percent, 0)} %`, g.fan_rpm != null && `${num(g.fan_rpm, 0)} RPM`].filter(Boolean).join(' · '), true],
    ['PCIe', g.pcie_generation == null && g.pcie_width == null ? '—' : `Gen ${g.pcie_generation ?? '?'} ×${g.pcie_width ?? '?'}`],
    ['ECC, неисправл. ошибки', g.ecc_uncorrected_errors == null ? '—'
      : g.ecc_uncorrected_errors! > 0 ? <span className="badge bad">{g.ecc_uncorrected_errors}</span> : '0'],
    ['MIG', inventory?.mig_mode ?? '—'],
    ['Persistence mode', inventory?.persistence_mode ?? '—'],
  ];
  if (live) details.push(
    ['Копирование памяти', unit(live.memory_copy_utilization_percent, '%'), true],
    ['Последняя XID-ошибка', live.last_xid_error == null ? '—' : live.last_xid_error ? <span className="badge bad">XID {live.last_xid_error}</span> : 'нет', true]);
  const shown = details.filter(([, v, always]) => nvidia || always || v !== '—');
  return <div className="gpu">
    <div className="gpu-head">
      <b>{title}</b>
      <small title={id}>{[id, inventory?.pci_bus_id && inventory.pci_bus_id !== id && `PCI ${inventory.pci_bus_id}`].filter(Boolean).join(' · ')}</small>
      <small>{live ? 'Текущие значения DCGM' : ''}{live && inventory ? ', остальное — ' : ''}
        {inventory ? `снимок агента от ${date(observedAt)}` : absent ? ` · ${absent}` : ''}</small>
    </div>
    <div className="gpu-numbers">
      <span>Загрузка<b>{unit(g.utilization_percent, '%')}</b>
        {g.utilization_percent != null && <progress max="100" value={Math.min(100, g.utilization_percent)} />}</span>
      <span>VRAM<b>{g.memory_used_bytes == null ? '—' : size(g.memory_used_bytes)}</b>
        {memRatio != null && <progress max="1" value={Math.min(1, memRatio)} />}
        <small>из {g.memory_total_bytes == null ? '—' : size(g.memory_total_bytes)}{memRatio != null && ` · ${percent(memRatio)}`}</small></span>
      <span>Температура<b>{unit(g.temperature_c, '°C')}</b>
        <small>{[g.hotspot_temperature_c != null && `hotspot ${num(g.hotspot_temperature_c, 0)} °C`,
          g.memory_temperature_c != null && `память ${num(g.memory_temperature_c, 0)} °C`].filter(Boolean).join(' · ') || 'другие датчики —'}</small></span>
      <span>Мощность<b>{unit(g.power_w, 'W')}</b>
        {powerRatio != null && <progress className={powerRatio > 0.95 ? 'hot' : ''} max="1" value={Math.min(1, powerRatio)} />}
        <small>{g.power_limit_w == null ? 'лимит —' : `лимит ${num(g.power_limit_w, 0)} W · ${percent(powerRatio)}`}</small></span>
    </div>
    {throttles.length > 0 && <div className="badges" aria-label="Причины снижения частот">
      {throttles.map(t => <span key={t} className={`badge ${THROTTLE[t]?.[1] ?? 'warn'}`} title={t}>{THROTTLE[t]?.[0] ?? t}</span>)}
    </div>}
    <dl className="compact">{shown.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
    {inventory && nvidia && <GpuProcesses gpu={inventory} instances={instances} onService={onService} />}
  </div>;
}

/** Docker containers match by container ID; Kubernetes pods by the instance ID the node agent attributed. */
function Owner({ process, instances, onService }: { process: GPUProcess; instances: Placed[]; onService?: Open }) {
  const cid = process.container_id;
  const i = process.instance_id ? instances.find(x => x.id === process.instance_id)
    : cid ? instances.find(x => x.id === cid) ?? instances.find(x => x.id.startsWith(cid)) : undefined;
  if (!i) {
    if (process.pod) return <>pod {process.namespace}/{process.pod}{process.container && ` · ${process.container}`}
      <small>нет в инвентаре кластера · PID {process.pid}</small></>;
    if (process.pod_uid) return <>pod {process.pod_uid.slice(0, 8)}…<small>pod не найден на узле · PID {process.pid}</small></>;
    if (!cid) return <>процесс хоста (PID {process.pid})</>;
    return <>контейнер {cid.slice(0, 12)}<small>нет в инвентаре · PID {process.pid}</small></>;
  }
  return <>
    <b>{i.name}</b>
    <small>{i.service_id
      ? onService ? <button className="service-link" onClick={() => onService(i.service_id)}>{i.service_id}</button> : i.service_id
      : 'не привязан к сервису'} · PID {process.pid}</small>
  </>;
}

function GpuProcesses({ gpu, instances, onService }: { gpu: GPU; instances: Placed[]; onService?: Open }) {
  if (!gpu.processes) return <p className="note">Список процессов не передан агентом.</p>;
  if (!gpu.processes.length) return <p className="note">Процессов на GPU нет по последнему снимку.</p>;
  const rows = [...gpu.processes].sort((a, b) => (b.used_memory_bytes ?? -1) - (a.used_memory_bytes ?? -1));
  const owners = new Set(rows.map(p => p.container_id || p.pod_uid || `pid:${p.pid}`));
  return <>
    <div className="table-wrap"><table className="dense">
      <thead><tr><th>Владелец процесса</th><th>VRAM процесса</th></tr></thead>
      <tbody>{rows.map(p => <tr key={p.pid}>
        <td><Owner process={p} instances={instances} onService={onService} /></td>
        <td>{p.used_memory_bytes == null ? 'н/д' : size(p.used_memory_bytes)}
          {p.used_memory_bytes != null && gpu.memory_total_bytes ? <small>{percent(p.used_memory_bytes / gpu.memory_total_bytes)} VRAM</small> : null}</td>
      </tr>)}</tbody>
    </table></div>
    {owners.size > 1 && <p className="note">GPU используется совместно: загрузка, мощность и температура относятся ко всей карте и не делятся между процессами.</p>}
  </>;
}

function gpuTitle(g: { vendor: string; model: string }, index: number) {
  return `GPU ${index} · ${g.vendor}${g.model ? ` ${g.model}` : ''}`;
}

function SourceHead({ source, grafana }: { source: Source; grafana: string }) {
  return <>
    <div className="section-head">
      <h2>{source.hostname}</h2>
      <span className={`badge ${stale(source) ? 'warn' : ''}`}>{stale(source) ? 'Устарело' : 'Получены данные'}</span>
    </div>
    <p className="muted">{source.source_type} · {source.source_id}{source.agent_version && ` · агент ${source.agent_version}`} · снимок {date(source.observed_at)}
      {grafana && <> · <a href={grafana} target="_blank" rel="noreferrer">Grafana ↗</a></>}</p>
    {source.errors?.map(e => <div role="alert" className="alert" key={e}>Ошибка сбора: {e}</div>)}
  </>;
}

export function HostCard({ source, metrics, grafanaUrl, range, onService }: {
  source: Source; metrics?: CurrentMetrics | null; grafanaUrl: string; range: string; onService?: Open;
}) {
  const prefix = source.source_id + '/';
  const live = metrics?.gpu ?? {};
  const instances = source.instances.map(i => ({ ...i, source }));
  // DCGM-only GPUs (e.g. nvidia-smi failed) are still shown, marked as absent from inventory.
  const extra = Object.keys(live).filter(k => k.startsWith(prefix) && !source.gpus.some(g => prefix + g.id === k));
  const boot = source.uptime_seconds != null ? Date.parse(source.observed_at) - source.uptime_seconds * 1000 : null;
  const running = source.instances.filter(i => i.status === 'running').length;
  return <section>
    <SourceHead source={source} grafana={source.source_type === 'docker-host'
      ? grafanaLink(grafanaUrl, 'gman-host', { host: source.source_id }, range) : ''} />
    <HostGauges host={metrics?.host?.[source.source_id]} cpuCount={source.cpu_count} memoryTotal={source.memory_total_bytes}
      missing={<>Метрики хоста не получены: проверьте OTel Collector с GMAN_HOST_ID={source.source_id}.</>} />
    <dl>
      <div><dt>ОС</dt><dd>{source.os || '—'}</dd></div>
      <div><dt>Ядро</dt><dd>{source.kernel || '—'}</dd></div>
      <div><dt>Процессор</dt><dd>{source.cpu_model || '—'}<small>{source.cpu_count ?? '—'} логических CPU</small></dd></div>
      <div><dt>Оперативная память</dt><dd>{bytes(source.memory_total_bytes)}</dd></div>
      <div><dt>Runtime</dt><dd>{source.runtime_version || '—'}</dd></div>
      <div><dt>Аптайм</dt><dd>{uptime(source.uptime_seconds)}{boot != null && <small>загружен {date(new Date(boot).toISOString())}</small>}</dd></div>
      <div><dt>Экземпляры</dt><dd>{running} запущено из {source.instances.length}</dd></div>
    </dl>
    {source.gpus.map((g, n) => <GpuCard key={g.id} title={gpuTitle(g, n)} id={g.id} inventory={g} live={live[prefix + g.id]}
      observedAt={source.observed_at} instances={instances} onService={onService} />)}
    {extra.map((k, n) => <GpuCard key={k} title={gpuTitle({ vendor: 'NVIDIA', model: '' }, source.gpus.length + n)} id={k.slice(prefix.length)}
      live={live[k]} instances={instances} absent="нет в инвентаре агента" />)}
    {!source.gpus.length && !extra.length && <p className="note">Данные GPU не получены этим источником.</p>}
    {source.gpus.length > 0 && <p className="note">VRAM процесса — видеопамять, выделенная процессу по nvidia-smi. Это не загрузка GPU: она относится ко всей карте.</p>}
  </section>;
}

const PRESSURE: Record<string, string> = {
  MemoryPressure: 'Нехватка памяти', DiskPressure: 'Нехватка диска', PIDPressure: 'Нехватка PID',
  NetworkUnavailable: 'Сеть недоступна',
};

/** Requested vs allocatable; overcommit beyond allocatable is highlighted. */
function Meter({ label, used, total, show, capacity }: {
  label: string; used: number | null; total: number | null; show: (v: number) => string; capacity?: number | null;
}) {
  const ratio = used != null && total ? used / total : null;
  return <div className="meter">
    <span>{label}</span>
    <b>{used == null ? '—' : show(used)} <i>из {total == null ? '—' : show(total)}</i></b>
    {ratio != null && <progress className={ratio > 1 ? 'hot' : ''} max="1" value={Math.min(1, ratio)} />}
    <small>{ratio == null ? 'запрошено —' : `запрошено ${percent(ratio)} от доступного`}
      {capacity != null && capacity !== total && ` · емкость ${show(capacity)}`}</small>
  </div>;
}

const coreUnit = (v: number) => `${cores(v)} ядер`;
const gpuUnit = (v: number) => `${v} GPU`;

/** `gpuSource`: the node-local agent's snapshot (nvidia-smi with processes attributed to pods), if connected. */
function NodeCard({ node, source, gpuSource, metrics, onService }: {
  node: Node; source: Source; gpuSource?: Source; metrics?: CurrentMetrics | null; onService?: Open;
}) {
  const host = metrics?.host?.[node.name];
  const prefix = node.name + '/';
  const dcgm = Object.entries(metrics?.gpu ?? {}).filter(([k]) => k.startsWith(prefix));
  const inventory = gpuSource?.gpus ?? [];
  const dcgmOnly = dcgm.filter(([k]) => !inventory.some(g => prefix + g.id === k));
  const instances = source.instances.filter(i => i.node === node.name).map(i => ({ ...i, source }));
  const pods = new Set(source.instances.filter(i => i.node === node.name).map(i => i.id.split(':')[0])).size;
  return <div className="node">
    <div className="node-head">
      <b>{node.name}</b>
      <span className={`badge ${node.ready ? '' : 'bad'}`}>{node.ready ? 'Ready' : 'NotReady'}</span>
      {node.unschedulable && <span className="badge warn" title="kubectl cordon: новые pod не планируются">Cordon</span>}
      {node.pressure.map(p => <span key={p} className="badge bad" title={p}>{PRESSURE[p] ?? p}</span>)}
    </div>
    <small>{node.roles.length ? node.roles.join(', ') : 'worker'} · {pods} pod · kubelet {node.kubelet_version || '—'}</small>
    <small>{[node.os, node.kernel, node.runtime_version].filter(Boolean).join(' · ') || '—'}</small>
    {node.taints.length > 0 && <small>Taints: {node.taints.join(', ')}</small>}
    <div className="meters">
      <Meter label="CPU requests" used={node.cpu_requested} total={node.cpu_allocatable} capacity={node.cpu_capacity} show={coreUnit} />
      <Meter label="Memory requests" used={node.memory_requested_bytes} total={node.memory_allocatable_bytes} capacity={node.memory_capacity_bytes} show={size} />
      {(node.gpu_capacity > 0 || node.gpu_requested > 0) &&
        <Meter label="GPU requests" used={node.gpu_requested} total={node.gpu_allocatable} capacity={node.gpu_capacity} show={gpuUnit} />}
    </div>
    {node.gpu_product && <p className="muted">GPU: {node.gpu_product}{node.gpu_memory_mib != null && ` · ${size(node.gpu_memory_mib * 1024 ** 2)}`}</p>}
    <HostGauges host={host} cpuCount={node.cpu_capacity} memoryTotal={node.memory_capacity_bytes}
      missing={<>Метрики узла не получены (нет рядов host_id={node.name}).</>} />
    {gpuSource?.errors?.map(e => <div role="alert" className="alert" key={e}>Ошибка сбора GPU на узле: {e}</div>)}
    {gpuSource && stale(gpuSource) && <p className="note">Снимок GPU узла устарел: агент kubernetes-node не присылал данные больше трех минут.</p>}
    {inventory.map((g, n) => <GpuCard key={g.id} title={gpuTitle(g, n)} id={g.id} inventory={g} live={metrics?.gpu?.[prefix + g.id]}
      observedAt={gpuSource!.observed_at} instances={instances} onService={onService} />)}
    {dcgmOnly.map(([k, m], n) => <GpuCard key={k} title={`GPU ${inventory.length + n} · ${node.gpu_product || 'NVIDIA'}`} id={k.slice(prefix.length)}
      live={m} instances={[]} />)}
    {node.gpu_capacity > 0 && !dcgm.length && <p className="note">Метрики DCGM для GPU этого узла не получены.</p>}
    {node.gpu_capacity > 0 && !gpuSource && <p className="note">Процессы GPU не привязаны к pod: на узле не подключен агент
      kubernetes-node (deploy/gpu-attribution-k8s.yaml).</p>}
  </div>;
}

export function ClusterCard({ source, sources, metrics, grafanaUrl, range, onService, children }: {
  source: Source; sources: Source[]; metrics?: CurrentMetrics | null; grafanaUrl: string; range: string; onService?: Open;
  /** Rendered after the cluster summary, before the nodes (workloads from kube-state-metrics). */
  children?: React.ReactNode;
}) {
  const nodes = source.nodes ?? [];
  const sum = (f: (n: Node) => number | null) => nodes.some(n => f(n) == null) ? null : nodes.reduce((s, n) => s + f(n)!, 0);
  const gpus = sum(n => n.gpu_allocatable);
  return <section>
    <SourceHead source={source} grafana={grafanaLink(grafanaUrl, 'gman-k8s', { cluster: source.source_id }, range)} />
    <div className="meters">
      <div className="meter"><span>Узлы</span><b>{nodes.filter(n => n.ready).length} <i>из {nodes.length} Ready</i></b>
        <small>{nodes.filter(n => n.unschedulable).length} cordon · {nodes.filter(n => n.pressure.length).length} с нехваткой ресурсов</small></div>
      <Meter label="CPU requests" used={sum(n => n.cpu_requested)} total={sum(n => n.cpu_allocatable)} show={coreUnit} />
      <Meter label="Memory requests" used={sum(n => n.memory_requested_bytes)} total={sum(n => n.memory_allocatable_bytes)} show={size} />
      {!!gpus && <Meter label="GPU requests" used={sum(n => n.gpu_requested)} total={gpus} show={gpuUnit} />}
    </div>
    <p className="note">Requests — зарезервированное планировщиком, а не фактическое потребление; текущая нагрузка узла — ниже по каждому узлу.</p>
    {children}
    {nodes.map(n => <NodeCard key={n.name} node={n} source={source} gpuSource={nodeGpuSource(sources, source.source_id, n.name)}
      metrics={metrics} onService={onService} />)}
    {!nodes.length && <p className="note">Агент не передал список узлов.</p>}
  </section>;
}
