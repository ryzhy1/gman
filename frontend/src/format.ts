import type { Instance, Source } from './api';

export const bytes = (v: number | null | undefined) => v == null ? 'Нет данных' : `${(v / 1024 ** 3).toFixed(1)} GiB`;
export const date = (s: string | null | undefined) => s ? new Date(s).toLocaleString('ru-RU') : 'Неизвестно';
export const stale = (s: Source) => Date.now() - Date.parse(s.observed_at) > 180_000;
export const num = (v: number | null | undefined, digits = 1) => v == null ? '—' : String(Number(v.toFixed(digits)));
export const isHttp = (s: string | undefined) => !!s && /^https?:\/\//i.test(s);

/** Status shown to people: runtime state first, then health/readiness and the reason. */
export function describeStatus(i: Instance) {
  const parts = [i.status];
  if (i.health) parts.push(i.health);
  if (i.ready === false && i.status === 'running') parts.push('not ready');
  if (i.reason) parts.push(i.reason);
  if (i.exit_code != null) parts.push(`exit ${i.exit_code}`);
  return parts.join(' · ');
}

export function statusTone(i: Instance) {
  if (i.status === 'running' && (i.health === '' || i.health === 'healthy') && i.ready !== false) return '';
  if (['exited', 'dead', 'terminated', 'waiting'].includes(i.status) || i.health === 'unhealthy' || i.reason) return 'bad';
  return 'warn';
}

export function value(v: unknown): string {
  if (v == null || v === '') return '—';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '—';
  if (typeof v === 'boolean') return v ? 'да' : 'нет';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export const percent = (ratio: number | null | undefined) => ratio == null ? '—' : `${(ratio * 100).toFixed(ratio < 0.1 ? 1 : 0)} %`;
export const cores = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(v < 1 ? 2 : 1)}`;
export function rate(v: number | null | undefined) {
  if (v == null) return '—';
  const units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${units[i]}`;
}
export function size(v: number | null | undefined) {
  if (v == null) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${units[i]}`;
}

/** "12 д 4 ч" / "3 ч 15 мин"; null stays unknown. */
export function uptime(seconds: number | null | undefined) {
  if (seconds == null) return '—';
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  return d ? `${d} д ${h} ч` : h ? `${h} ч ${m} мин` : `${m} мин`;
}

/** Number with a unit, or "—" when the reading is absent/unsupported. */
export const unit = (v: number | null | undefined, u: string, digits = 0) => v == null ? '—' : `${num(v, digits)} ${u}`;

/** Kubernetes pod UID and container name from an inventory instance ID "<pod_uid>:<container>". */
export function podContainer(id: string) {
  const at = id.indexOf(':');
  return { pod_uid: id.slice(0, at), container_name: id.slice(at + 1) };
}

/** "45 с" / "12 мин 5 с" / "2 ч 10 мин"; null stays unknown. */
export function duration(seconds: number | null | undefined) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.round(seconds)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h} ч ${m} мин` : m ? `${m} мин ${s % 60} с` : `${s} с`;
}
