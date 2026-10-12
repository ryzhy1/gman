import { useEffect, useState } from 'react';
import { ApiError, get, type RangeResult } from './api';
import { LineChart, SERIES_COLORS, type Series } from './chart';
import type { Placed } from './components';
import { Tabs, TabsList, TabsTrigger } from '@/ui/tabs';
import { cores, num, podContainer, rate, size } from './format';

const RANGES = [['1h', '1 час'], ['24h', '24 часа'], ['7d', '7 дней']] as const;
const gpuPercent = (v: number) => `${num(v, 0)} %`;
type Kind = 'container' | 'pod' | 'gpu';
// `pod`: Kubernetes reports network per pod (shared by its containers), not per container.
// `gpu`: DCGM series of GPUs allocated to a Kubernetes container (whole-GPU values); Docker has no such series.
const METRICS: readonly (readonly [string, string, (v: number) => string, string | undefined, Kind])[] = [
  ['cpu_cores', 'CPU, ядра', cores, '1.0 — одно полностью занятое ядро', 'container'],
  ['memory_bytes', 'Память', size, undefined, 'container'],
  ['network_rx_bytes_per_second', 'Сеть, входящий трафик', rate, undefined, 'pod'],
  ['network_tx_bytes_per_second', 'Сеть, исходящий трафик', rate, undefined, 'pod'],
  ['gpu_utilization_percent', 'GPU pod-а, загрузка', gpuPercent,
    'загрузка всей карты, выделенной контейнеру (DCGM); при совместном GPU — общая', 'gpu'],
  ['gpu_memory_used_bytes', 'GPU pod-а, VRAM', size, 'VRAM выделенных контейнеру карт (DCGM)', 'gpu'],
];
// Beyond the fixed categorical slots colors would repeat; larger services belong in Grafana.
const MAX_SERIES = SERIES_COLORS.length;

type Request = { name: string; color: string; params: Record<string, string> };

/** One request per series. Kubernetes network is per pod, so containers of one pod share a single series. */
function requests(shown: Placed[], metric: string, kind: Kind): Request[] {
  const result: Request[] = [];
  const pods = new Set<string>();
  shown.forEach((i, n) => {
    const color = SERIES_COLORS[n];  // Color follows the instance's position by name, in every chart.
    if (kind === 'gpu') {
      if (i.runtime !== 'kubernetes' || !(i.config.gpu_requests as string[] | undefined)?.length) return;
      const [pod_name, container_name] = [i.name.slice(0, i.name.indexOf('/')), i.name.slice(i.name.indexOf('/') + 1)];
      result.push({ name: `${i.name} · ${i.node || i.source.hostname}`, color,
        params: { scope: 'k8s_gpu', metric, namespace: i.namespace, pod_name, container_name } });
      return;
    }
    if (i.runtime === 'docker') {
      result.push({ name: `${i.name} · ${i.source.hostname}`, color,
        params: { scope: 'container', metric, host_id: i.source.source_id, container_id: i.id } });
      return;
    }
    const { pod_uid, container_name } = podContainer(i.id);
    if (kind === 'container') {
      result.push({ name: `${i.name} · ${i.node || i.source.hostname}`, color,
        params: { scope: 'k8s_container', metric, pod_uid, container_name } });
    } else if (!pods.has(pod_uid)) {
      pods.add(pod_uid);
      result.push({ name: `pod ${i.name.split('/')[0]}`, color, params: { scope: 'k8s_pod', metric, pod_uid } });
    }
  });
  return result;
}

/** The period is owned by the service card so that its Splunk links cover the same window. */
export function Resources({ token, instances, grafanaLink, range, onRange, splunkLink }: {
  token: string; instances: Placed[]; grafanaLink: string; range: string; onRange: (range: string) => void; splunkLink?: string;
}) {
  const [data, setData] = useState<Record<string, Series[]>>({});
  const [failed, setFailed] = useState<string[]>([]);
  // Color follows the instance: sort by a stable key, never by current value.
  const running = instances.filter(i => i.status === 'running');
  const shown = [...running].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id)).slice(0, MAX_SERIES);
  const key = shown.map(i => i.source.source_id + i.id).join();
  const hasK8s = shown.some(i => i.runtime === 'kubernetes');
  // GPU charts only where some shown pod requests a GPU: Docker GPU usage per container is on the host card.
  const metrics = METRICS.filter(m => m[4] !== 'gpu' || shown.some(i => i.runtime === 'kubernetes'
    && (i.config.gpu_requests as string[] | undefined)?.length));

  useEffect(() => {
    const ctrl = new AbortController();
    const errors = new Set<string>();
    setFailed([]);
    // A failing series (e.g. a scope the metrics API does not serve yet) leaves a gap, not a broken tab.
    Promise.all(metrics.map(async ([metric, , , , kind]) => [metric, (await Promise.all(requests(shown, metric, kind).map(async r => {
      try {
        const result = await get<RangeResult>(token, `/api/metrics/range?${new URLSearchParams({ ...r.params, range })}`, ctrl.signal);
        return { name: r.name, color: r.color, points: result.points };
      } catch (e) {
        if (ctrl.signal.aborted) throw e;
        errors.add(e instanceof ApiError && e.status === 503 ? 'хранилище метрик недоступно или не настроено'
          : e instanceof ApiError && e.status === 422 && r.params.scope !== 'container'
            ? 'API метрик пока не отдает ряды Kubernetes-экземпляров'
            : `${r.params.scope === 'container' ? 'Docker' : 'Kubernetes'}: ${(e as Error).message}`);
        return { name: r.name, color: r.color, points: [] };
      }
    }))) as Series[]] as const))
      .then(entries => { setData(Object.fromEntries(entries)); setFailed([...errors]); })
      .catch(() => undefined);
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, key, range]);

  return <div>
    <div className="chart-controls">
      <Tabs value={range} onValueChange={onRange} aria-label="Период">
        <TabsList>{RANGES.map(([value, label]) => <TabsTrigger key={value} value={value}>{label}</TabsTrigger>)}</TabsList>
      </Tabs>
      {grafanaLink && <a href={grafanaLink} target="_blank" rel="noreferrer">Подробнее в Grafana ↗</a>}
      {splunkLink && <a href={splunkLink} target="_blank" rel="noopener noreferrer">Логи в Splunk ↗</a>}
    </div>
    {failed.length > 0 && <div role="alert" className="alert">Часть рядов без данных — {failed.join('; ')}.</div>}
    {!shown.length && <div className="empty">Нет запущенных экземпляров.</div>}
    {shown.length > 0 && <div className="chart-grid">
      {metrics.map(([metric, title, format, note, kind]) =>
        <LineChart key={metric} title={title} format={format} binary={format === size || format === rate} series={data[metric] ?? []}
          note={kind === 'pod' && hasK8s ? 'для Kubernetes — сеть всего pod' : note} />)}
    </div>}
    {running.length > MAX_SERIES &&
      <p className="note">Показаны первые {MAX_SERIES} экземпляров по имени; полный список — в Grafana.</p>}
  </div>;
}
