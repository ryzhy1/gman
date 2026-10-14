import { useEffect, useState, type ReactNode } from 'react';
import { get, type Release, type ReleaseDetail, type ReleasePage, type ReleaseStatus } from './api';
import { EventItem } from './components';
import { date, duration, isHttp } from './format';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/table';
import { Meter as Bar, linkButton, outlineButton, tone as badge } from '@/ui/tone';

const STATUS: Record<ReleaseStatus, [string, string]> = {
  started: ['Начат', 'warn'], 'rolling-out': ['Раскатывается', 'warn'], succeeded: ['Успешно', ''],
  failed: ['Ошибка', 'bad'], superseded: ['Заменен', 'info'],
};

const CORRELATION: Record<string, string> = { digest: 'по digest образа', version: 'по версии/тегу', '': 'еще нет' };

function StatusBadge({ release }: { release: Release }) {
  const [label, tone] = STATUS[release.status] ?? [release.status, 'info'];
  return <span className={badge(tone)} title={release.status_reason || undefined}>{label}</span>;
}

const initiator = (r: Release) => r.initiator === 'unknown' ? 'Инициатор неизвестен' : r.initiator;
const version = (r: Release) => r.version || r.release || r.image || '—';
const open = (r: Release) => r.status === 'started' || r.status === 'rolling-out';

function Progress({ release }: { release: Release }) {
  const p = release.progress, total = p.total ?? 0;
  if (!total) return <span className="muted" title="Экземпляры целевого образа еще не наблюдаются">—</span>;
  return <>{p.on_target ?? 0} / {total}<small>здоровы {p.healthy ?? 0}</small>
    <Bar max={total} value={p.on_target ?? 0} /></>;
}

/** Bamboo deployments of the service with status derived from runtime observations. */
export function Releases({ serviceId, environment, refreshKey, selected, onSelect }: {
  serviceId: string; environment?: string; refreshKey: string;
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
    get<ReleasePage>(base, ctrl.signal)
      .then(page => { setReleases(page.releases); setNext(page.next); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [base, refreshKey, selected]);

  async function more() {
    try {
      const page = await get<ReleasePage>(`${base}&before=${encodeURIComponent(next!)}`);
      setReleases(old => [...old, ...page.releases]);
      setNext(page.next);
    } catch (e) { setError((e as Error).message); }
  }

  if (selected != null) return <ReleaseView id={selected} refreshKey={refreshKey} onBack={() => onSelect(null)} />;
  return <div>
    {error && <div role="alert" className="alert">{error}</div>}
    <div className="table-wrap"><Table>
      <TableHeader><TableRow><TableHead>Релиз</TableHead><TableHead>Статус</TableHead><TableHead>Инициатор</TableHead><TableHead>Окружение</TableHead><TableHead>Экземпляры</TableHead><TableHead>Начат</TableHead><TableHead>Длительность</TableHead></TableRow></TableHeader>
      <TableBody>{releases.map(r => <TableRow key={r.id}>
        <TableCell><button className={linkButton()} onClick={() => onSelect(r.id)}>#{r.id} · {version(r)} ↗</button>
          <small>{r.build_id}{r.commit && ` · ${r.commit.slice(0, 12)}`}</small></TableCell>
        <TableCell><StatusBadge release={r} />{r.status_reason && <small>{r.status_reason}</small>}</TableCell>
        <TableCell>{initiator(r)}<small>{r.initiator_kind}</small></TableCell>
        <TableCell>{r.environment}</TableCell>
        <TableCell><Progress release={r} /></TableCell>
        <TableCell>{date(r.started_at)}</TableCell>
        <TableCell>{duration(r.duration_seconds)}{open(r) && <small>идет</small>}</TableCell>
      </TableRow>)}</TableBody>
    </Table></div>
    {!releases.length && !error && <div className="empty">
      <h3>Релизов пока нет</h3>
      <p>Релиз появляется после события Bamboo (agent/src/bamboo_event.py) для этого сервиса.</p>
    </div>}
    {next && <button className={outlineButton('mt-4')} onClick={more}>Показать еще</button>}
    <p className="note">Статус выводится из наблюдений: «Успешно» — все экземпляры целевого образа в окружении запущены с целевым digest или версией и здоровы. Ошибка — сбой в Bamboo, падения или OOM новых экземпляров либо тайм-аут раскатки.</p>
  </div>;
}

function ReleaseView({ id, refreshKey, onBack }: { id: number; refreshKey: string; onBack: () => void }) {
  const [data, setData] = useState<ReleaseDetail | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const ctrl = new AbortController();
    get<ReleaseDetail>(`/api/releases/${id}`, ctrl.signal)
      .then(d => { setData(d); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [id, refreshKey]);

  const back = <button className={linkButton('mb-4 text-muted-foreground')} onClick={onBack}>← Все релизы</button>;
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
    {!!p.total && <Bar max={p.total} value={p.on_target ?? 0} />}
    <div className="table-wrap"><Table>
      <TableHeader><TableRow><TableHead>Экземпляр</TableHead><TableHead>Версия</TableHead><TableHead>Состояние</TableHead><TableHead>Рестарты</TableHead><TableHead>Создан / запущен</TableHead></TableRow></TableHeader>
      <TableBody>{data.instances.map(i => <TableRow key={i.source_id + i.id}>
        <TableCell><b>{i.name}</b><small>{i.node || i.source_id}</small></TableCell>
        <TableCell>{i.on_target ? <span className={badge()}>Целевая · {CORRELATION[i.on_target]}</span> : <span className={badge('warn')}>Прежняя</span>}
          <small>{i.image}</small></TableCell>
        <TableCell><span className={badge(i.crash ? 'bad' : i.healthy ? '' : 'warn')}>
          {[i.status, i.health, i.ready === false ? 'not ready' : '', i.reason].filter(Boolean).join(' · ')}</span>
          {i.crash && <small>Сбой: {i.crash}</small>}</TableCell>
        <TableCell>{i.restarts}</TableCell>
        <TableCell>{date(i.created_at)}<small>{i.new ? 'создан в окне релиза' : 'создан до релиза'}</small></TableCell>
      </TableRow>)}</TableBody>
    </Table></div>
    {!data.instances.length && <div className="empty">Экземпляры целевого образа не наблюдаются.</div>}
    <p className="note">Показано текущее состояние инвентаря. После завершения релиза экземпляры могут уже относиться к следующему релизу.</p>

    <h3 className="subheading">Связанные события</h3>
    {data.events.map((e, n) => <EventItem key={n} e={e} />)}
    {!data.events.length && <div className="empty">Связанных событий нет.</div>}
  </div>;
}
