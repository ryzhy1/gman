import { useEffect, useState } from 'react';
import { get } from './api';
import { grafanaLink } from './format';

export type WorkloadStatus = 'ok' | 'rolling' | 'degraded' | 'stuck' | 'unknown';
/** Kubernetes workload from kube-state-metrics (GET /api/workloads); counters are null when not exported. */
export type Workload = {
  cluster: string; namespace: string; kind: 'Deployment' | 'StatefulSet' | 'DaemonSet' | 'Job'; name: string;
  service_id: string; environment: string;
  desired: number | null; ready: number | null; available: number | null; updated: number | null;
  unavailable: number | null; generation: number | null; observed_generation: number | null; pending_pods: number | null;
  paused: boolean; progressing: boolean | null; progressing_reason: string;
  current_revision: string; update_revision: string;
  /** Jobs only. */
  failed: boolean | null; active: number | null;
  alerts: { name: string; severity: string; reason: string }[];
  status: WorkloadStatus; reasons: string[];
};
export type WorkloadList = { enabled: boolean; error: string | null; workloads: Workload[] };

const STATUS: Record<WorkloadStatus, [string, string]> = {
  ok: ['ок', ''], rolling: ['раскатывается', 'info'], degraded: ['деградирован', 'warn'],
  stuck: ['застрял', 'bad'], unknown: ['нет данных', 'info'],
};

const n = (v: number | null) => v ?? '—';

function Replicas({ w }: { w: Workload }) {
  if (w.kind === 'Job') return <>{w.failed ? 'ошибка' : `активных pod-ов: ${n(w.active)}`}</>;
  return <>{n(w.available)} из {n(w.desired)}
    <small>{w.kind === 'DaemonSet' ? 'узлов' : 'реплик'} доступно · готово {n(w.ready)}
      {w.unavailable ? ` · недоступно ${w.unavailable}` : ''}</small></>;
}

function Rollout({ w }: { w: Workload }) {
  if (w.kind === 'Job') return <>—</>;
  const lag = w.generation != null && w.observed_generation != null && w.generation > w.observed_generation;
  return <>{n(w.updated)} из {n(w.desired)}
    <small>
      {w.kind === 'StatefulSet' && w.update_revision && (w.current_revision === w.update_revision
        ? `ревизия ${w.update_revision}` : `${w.current_revision || '?'} → ${w.update_revision}`)}
      {w.kind === 'Deployment' && w.progressing != null && `Progressing ${w.progressing ? 'True' : 'False'}${w.progressing_reason ? ` · ${w.progressing_reason}` : ''}`}
      {w.paused && ' · paused'}
      {lag && ` · generation ${w.generation}, обработано ${w.observed_generation}`}
    </small></>;
}

/** Workloads of a service (service card) or of a cluster (cluster card). An empty token means the SSO cookie. */
export function Workloads({ token, serviceId, environment, cluster, grafanaUrl, range, refreshKey }: {
  token: string; serviceId?: string; environment?: string; cluster?: string; grafanaUrl: string; range: string; refreshKey: string;
}) {
  const [data, setData] = useState<WorkloadList | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const ctrl = new AbortController();
    const params = new URLSearchParams({
      ...(serviceId ? { service_id: serviceId } : {}), ...(environment ? { environment } : {}), ...(cluster ? { cluster } : {}),
    });
    get<WorkloadList>(token, `/api/workloads?${params}`, ctrl.signal)
      .then(result => { setData(result); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, serviceId, environment, cluster, refreshKey]);

  const rows = data?.workloads ?? [];
  // A service without Kubernetes workloads (Docker only) gets no empty section on its card.
  if (!cluster && !error && data?.enabled && !data.error && !rows.length) return null;
  const failing = rows.filter(w => w.status !== 'ok').length;

  return <div className="workloads">
    <h3 className="subheading">Workloads Kubernetes {rows.length > 0 && <span className="count">{rows.length}</span>}
      {failing > 0 && <span className="badge warn">требуют внимания: {failing}</span>}</h3>
    {error && <div role="alert" className="alert">Состояние workloads недоступно: {error}</div>}
    {data && !data.enabled && <p className="note">Состояние workloads недоступно: хранилище метрик не настроено (PROMETHEUS_URL).</p>}
    {data?.error && <div role="status" className="alert">Состояние workloads недоступно: {data.error}</div>}
    {data?.enabled && !data.error && !rows.length && <p className="note">kube-state-metrics не передал workloads этого кластера.</p>}
    {rows.length > 0 && <div className="table-wrap"><table>
      <thead><tr><th>Статус</th><th>Workload</th><th>{cluster ? 'Сервис' : 'Кластер'}</th><th>Доступно</th><th>Обновлено</th><th>Pending</th><th /></tr></thead>
      <tbody>{rows.map(w => {
        const [label, tone] = STATUS[w.status];
        const link = grafanaLink(grafanaUrl, 'gman-k8s', { cluster: w.cluster, namespace: w.namespace, node: null, environment: w.environment || null }, range);
        return <tr key={`${w.cluster}/${w.namespace}/${w.kind}/${w.name}`}>
          <td><span className={`badge ${tone}`} title={w.alerts.map(a => a.name + (a.reason ? ` (${a.reason})` : '')).join(', ') || undefined}>{label}</span></td>
          <td>{w.kind} {w.namespace}/{w.name}{w.reasons.length > 0 && <small>{w.reasons.join(' · ')}</small>}</td>
          <td>{cluster ? (w.service_id || 'не привязан') : w.cluster}<small>{w.environment}</small></td>
          <td><Replicas w={w} /></td>
          <td><Rollout w={w} /></td>
          <td>{w.pending_pods ?? 0}</td>
          <td>{link && <a href={link} target="_blank" rel="noreferrer">Grafana ↗</a>}</td>
        </tr>;
      })}</tbody>
    </table></div>}
    {rows.length > 0 && <p className="note">Из kube-state-metrics, обновляется вместе с карточкой. «Застрял» — сработал GManWorkloadRolloutStuck
      или Progressing=False; StatefulSet с OnDelete раскатан, когда все pod-ы на новой ревизии; DaemonSet — по updated/desired узлов.</p>}
  </div>;
}
