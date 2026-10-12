import { useEffect, useState } from 'react';
import { get, type CurrentMetrics, type MetricValues, type RangeResult } from './api';
import { LineChart, SERIES_COLORS, type Series } from './chart';
import { grafanaLink, num, percent } from './format';
import { Meter } from '@/ui/tone';

/** Seconds as people read latencies: milliseconds below one second. */
export const latency = (v: number | null | undefined) =>
  v == null ? '—' : v < 1 ? `${Math.round(v * 1000)} мс` : `${num(v, v < 10 ? 2 : 1)} с`;
const perSecond = (v: number | null | undefined) => v == null ? '—' : `${num(v, v < 10 ? 1 : 0)}/с`;

/** vLLM models of a catalog service: metrics.llm is keyed "<service>/<model>", and models may contain "/". */
export function serviceModels(metrics: CurrentMetrics | null | undefined, serviceId: string): [string, MetricValues][] {
  const prefix = serviceId + '/';
  return Object.entries(metrics?.llm ?? {}).filter(([k]) => k.startsWith(prefix))
    .map(([k, v]) => [k.slice(prefix.length), v] as [string, MetricValues]).sort(([a], [b]) => a.localeCompare(b));
}

const llmDashboard = (grafanaUrl: string, serviceId: string, range: string) =>
  grafanaLink(grafanaUrl, 'gman-llm', { service: serviceId, model: null }, range);

/** Current state of the service's models; nothing for services that do not run vLLM. */
export function LlmSummary({ metrics, serviceId, grafanaUrl, range }: {
  metrics?: CurrentMetrics | null; serviceId: string; grafanaUrl: string; range: string;
}) {
  const models = serviceModels(metrics, serviceId);
  if (!models.length) return null;
  const dashboard = llmDashboard(grafanaUrl, serviceId, range);
  return <>
    <div className="section-head subheading">
      <h3>Модели (vLLM)</h3>
      {dashboard && <a href={dashboard} target="_blank" rel="noreferrer">Подробнее в Grafana ↗</a>}
    </div>
    {models.map(([model, v]) => <div className="node" key={model}>
      <div className="node-head"><b>{model}</b><small>за 5 минут, все реплики</small></div>
      <div className="meters">
        <div className="meter"><span>Первый токен (TTFT)</span><b>{latency(v.ttft_p95_seconds)} <i>p95</i></b>
          <small>медиана {latency(v.ttft_p50_seconds)}</small></div>
        <div className="meter"><span>Время на токен (TPOT)</span><b>{latency(v.tpot_p95_seconds)} <i>p95</i></b>
          <small>полный запрос {latency(v.e2e_p95_seconds)} p95</small></div>
        <div className="meter"><span>Запросы</span><b>{perSecond(v.requests_per_second)}</b>
          <small>в работе {num(v.requests_running, 0)} · в очереди {num(v.requests_waiting, 0)}</small></div>
        <div className="meter"><span>Токены</span><b>{perSecond(v.generation_tokens_per_second)} <i>генерация</i></b>
          <small>промпт {perSecond(v.prompt_tokens_per_second)}</small></div>
        <div className="meter"><span>KV-кэш</span><b>{percent(v.kv_cache_usage)}</b>
          {v.kv_cache_usage != null && <Meter value={v.kv_cache_usage} hot={v.kv_cache_usage > 0.95} />}
          <small>самая загруженная реплика</small></div>
      </div>
    </div>)}
    <p className="note">Задержки отсутствуют, пока модель не получает запросов. TTFT — ожидание в очереди и обработка
      промпта до первого токена ответа; TPOT — интервал между следующими токенами.</p>
  </>;
}

const CHARTS = [
  ['ttft_p95_seconds', 'Время до первого токена, p95', latency],
  ['tpot_p95_seconds', 'Время на токен, p95', latency],
  ['generation_tokens_per_second', 'Генерация, токенов/с', perSecond],
  ['requests_waiting', 'Запросы в очереди', (v: number) => num(v, 0)],
  ['kv_cache_usage', 'KV-кэш', percent],
] as const;

/** History of the service's models for the Resources tab; one series per model. */
export function LlmCharts({ metrics, serviceId, range }: {
  metrics?: CurrentMetrics | null; serviceId: string; range: string;
}) {
  const models = serviceModels(metrics, serviceId).map(([m]) => m).slice(0, SERIES_COLORS.length);
  const key = models.join('\n');
  const [data, setData] = useState<Record<string, Series[]>>({});
  useEffect(() => {
    if (!models.length) return;
    const ctrl = new AbortController();
    Promise.all(CHARTS.map(async ([metric]) => [metric, await Promise.all(models.map(async (model, n) => {
      const params = new URLSearchParams({ scope: 'llm', metric, service_id: serviceId, model, range });
      const result = await get<RangeResult>(`/api/metrics/range?${params}`, ctrl.signal)
        .catch(() => ({ points: [] as RangeResult['points'] }));
      return { name: model, color: SERIES_COLORS[n], points: result.points };
    }))] as const)).then(entries => setData(Object.fromEntries(entries))).catch(() => undefined);
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceId, key, range]);
  if (!models.length) return null;
  return <>
    <h3 className="subheading">Модели (vLLM)</h3>
    <div className="chart-grid">
      {CHARTS.map(([metric, title, format]) =>
        <LineChart key={metric} title={title} format={format} series={data[metric] ?? []} />)}
    </div>
  </>;
}
