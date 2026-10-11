import { useEffect, useState, type ReactNode } from 'react';
import { get, type Release, type ReleaseDetail, type ReleasePage, type ReleaseStatus } from './api';
import { EventItem } from './components';
import { date, duration, isHttp } from './format';

const STATUS: Record<ReleaseStatus, [string, string]> = {
  started: ['Начат', 'warn'], 'rolling-out': ['Раскатывается', 'warn'], succeeded: ['Успешно', ''],
  failed: ['Ошибка', 'bad'], superseded: ['Заменен', 'info'],
};

const CORRELATION: Record<string, string> = { digest: 'по digest образа', version: 'по версии/тегу', '': 'еще нет' };

function StatusBadge({ release }: { release: Release }) {
  const [label, tone] = STATUS[release.status] ?? [release.status, 'info'];
  return <span className={`badge ${tone}`} title={release.status_reason || undefined}>{label}</span>;
}

const initiator = (r: Release) => r.initiator === 'unknown' ? 'Инициатор неизвестен' : r.initiator;
const version = (r: Release) => r.version || r.release || r.image || '—';
const open = (r: Release) => r.status === 'started' || r.status === 'rolling-out';

function Progress({ release }: { release: Release }) {
  const p = release.progress, total = p.total ?? 0;
  if (!total) return <span className="muted" title="Экземпляры целевого образа еще не наблюдаются">—</span>;
  return <>{p.on_target ?? 0} / {total}<small>здоровы {p.healthy ?? 0}</small>
    <progress max={total} value={p.on_target ?? 0} /></>;
}

/** Bamboo deployments of the service with status derived from runtime observations. */
export function Releases({ token, serviceId, environment, refreshKey, selected, onSelect }: {
  token: string; serviceId: string; environment?: string; refreshKey: string;
  selected: number | null; onSelect: (id: number | null) => void;
}) {
  const [releases, setReleases] = useState<Release[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState('');
  const base = `/api/releases?limit=50&service_id=${encodeURIComponent(serviceId)}`
    + (environment ? `&environment=${encodeURIComponent(environment)}` : '');

  useEffect(() => {
    if (selected != null) return;
    const ctrl = new AbortController();
    get<ReleasePage>(token, base, ctrl.signal)
      .then(page => { setReleases(page.releases); setNext(page.next); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, base, refreshKey, selected]);

  async function more() {
    try {
      const page = await get<ReleasePage>(token, `${base}&before=${encodeURIComponent(next!)}`);
      setReleases(old => [...old, ...page.releases]);
      setNext(page.next);
    } catch (e) { setError((e as Error).message); }
  }

  if (selected != null) return <ReleaseView token={token} id={selected} refreshKey={refreshKey} onBack={() => onSelect(null)} />;
  return <div>
    {error && <div role="alert" className="alert">{error}</div>}
    <div className="table-wrap"><table>
      <thead><tr><th>Релиз</th><th>Статус</th><th>Инициатор</th><th>Окружение</th><th>Экземпляры</th><th>Начат</th><th>Длительность</th></tr></thead>
      <tbody>{releases.map(r => <tr key={r.id}>
        <td><button className="service-link" onClick={() => onSelect(r.id)}>#{r.id} · {version(r)} ↗</button>
          <small>{r.build_id}{r.commit && ` · ${r.commit.slice(0, 12)}`}</small></td>
        <td><StatusBadge release={r} />{r.status_reason && <small>{r.status_reason}</small>}</td>
        <td>{initiator(r)}<small>{r.initiator_kind}</small></td>
        <td>{r.environment}</td>
        <td><Progress release={r} /></td>
        <td>{date(r.started_at)}</td>
        <td>{duration(r.duration_seconds)}{open(r) && <small>идет</small>}</td>
      </tr>)}</tbody>
    </table></div>
    {!releases.length && !error && <div className="empty">
      <h3>Релизов пока нет</h3>
      <p>Релиз появляется после события Bamboo (agent/bamboo_event.py) для этого сервиса.</p>
    </div>}
    {next && <button className="more" onClick={more}>Показать еще</button>}
    <p className="note">Статус выводится из наблюдений: «Успешно» — все экземпляры целевого образа в окружении запущены с целевым digest или версией и здоровы. Ошибка — сбой в Bamboo, падения или OOM новых экземпляров либо тайм-аут раскатки.</p>
  </div>;
}

function ReleaseView({ token, id, refreshKey, onBack }: { token: string; id: number; refreshKey: string; onBack: () => void }) {
  const [data, setData] = useState<ReleaseDetail | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const ctrl = new AbortController();
    get<ReleaseDetail>(token, `/api/releases/${id}`, ctrl.signal)
      .then(d => { setData(d); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, id, refreshKey]);

  const back = <button className="back" onClick={onBack}>← Все релизы</button>;
  if (!data) return <div>{back}{error ? <div role="alert" className="alert">{error}</div> : <div className="empty">Загрузка релиза…</div>}</div>;
  const r = data.release;
  const rows: [string, ReactNode][] = [
    ['Статус', <><StatusBadge release={r} />{r.status_reason && <small>{r.status_reason}</small>}</>],
    ['Окружение', `${r.service_id} · ${r.environment}`],
    ['Версия / релиз Bamboo', `${r.version || '—'} · ${r.release || '—'}`],
    ['Инициатор', `${initiator(r)} (${r.initiator_kind})`],
    ['Сборка', isHttp(r.build_url) ? <a href={r.build_url} target="_blank" rel="noreferrer">{r.build_id} ↗</a> : r.build_id || '—'],
    ['Коммит', r.commit || '—'],
    ['Образ', r.image || '—'],
    ['Image digest', r.image_digest || '—'],
    ['Сопоставление с runtime', CORRELATION[r.correlation] ?? r.correlation],
    ['Статус в Bamboo', r.bamboo_status || '—'],
    ['Начат', date(r.started_at)],
    ['Первый экземпляр новой версии', r.rollout_started_at ? date(r.rollout_started_at) : '—'],
    ['Bamboo завершил', r.bamboo_finished_at ? date(r.bamboo_finished_at) : '—'],
    ['Завершен', r.finished_at ? `${date(r.finished_at)} · ${duration(r.duration_seconds)}` : `идет ${duration(r.duration_seconds)}`],
  ];
  if (r.superseded_by) rows.push(['Заменен релизом', `#${r.superseded_by}`]);
  const p = r.progress;
  return <div>
    {back}
    {error && <div role="alert" className="alert">{error}</div>}
    <h2>Релиз #{r.id} · {version(r)}</h2>
    <dl>{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>

    <h3 className="subheading">Экземпляры {p.total ? `· на целевой версии ${p.on_target ?? 0} из ${p.total}, здоровы ${p.healthy ?? 0}` : ''}</h3>
    {!!p.total && <progress max={p.total} value={p.on_target ?? 0} />}
    <div className="table-wrap"><table>
      <thead><tr><th>Экземпляр</th><th>Версия</th><th>Состояние</th><th>Рестарты</th><th>Создан / запущен</th></tr></thead>
      <tbody>{data.instances.map(i => <tr key={i.source_id + i.id}>
        <td><b>{i.name}</b><small>{i.node || i.source_id}</small></td>
        <td>{i.on_target ? <span className="badge">Целевая · {CORRELATION[i.on_target]}</span> : <span className="badge warn">Прежняя</span>}
          <small>{i.image}</small></td>
        <td><span className={`badge ${i.crash ? 'bad' : i.healthy ? '' : 'warn'}`}>
          {[i.status, i.health, i.ready === false ? 'not ready' : '', i.reason].filter(Boolean).join(' · ')}</span>
          {i.crash && <small>Сбой: {i.crash}</small>}</td>
        <td>{i.restarts}</td>
        <td>{date(i.created_at)}<small>{i.new ? 'создан в окне релиза' : 'создан до релиза'}</small></td>
      </tr>)}</tbody>
    </table></div>
    {!data.instances.length && <div className="empty">Экземпляры целевого образа не наблюдаются.</div>}
    <p className="note">Показано текущее состояние инвентаря. После завершения релиза экземпляры могут уже относиться к следующему релизу.</p>

    <h3 className="subheading">Связанные события</h3>
    {data.events.map((e, n) => <EventItem key={n} e={e} />)}
    {!data.events.length && <div className="empty">Связанных событий нет.</div>}
  </div>;
}
