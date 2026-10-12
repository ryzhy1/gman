import { useEffect, useState } from 'react';
import { get, type ConfigChange, type ConfigDiff, type ConfigVersion, type ConfigVersionPage } from './api';
import { CONFIG_LABELS, type Placed } from './components';
import { date, value } from './format';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/ui/table';
import { outlineButton, tone as badge } from '@/ui/tone';

const OPS: Record<ConfigChange['op'], [string, string]> = {
  added: ['Добавлено', ''], removed: ['Удалено', 'bad'], changed: ['Изменено', 'warn'],
};

const instanceKey = (v: { source_id: string; instance_id: string }) => `${v.source_id}/${v.instance_id}`;
const short = (v: ConfigVersion) => `#${v.id} · ${v.config_hash.slice(0, 12)}`;
const instanceName = (v: ConfigVersion) => v.instance_name || v.instance_id.slice(0, 12);

/** Field label and formatter; "ports[]" is one element of the ports list. */
function field(path: string): [string, (v: unknown) => string] {
  const item = path.endsWith('[]');
  const key = item ? path.slice(0, -2) : path;
  const known = CONFIG_LABELS.find(([k]) => k === key);
  const label = known ? `${known[1]}${item ? ' (элемент)' : ''}` : path;
  return [label, !item && known?.[2] ? known[2] : value];
}

/** Defaults: newest version against the previous version of the same instance. */
function defaults(versions: ConfigVersion[]): [number | null, number | null] {
  const to = versions[0];
  if (!to) return [null, null];
  const from = versions.slice(1).find(v => instanceKey(v) === instanceKey(to)) ?? versions[1];
  return [from?.id ?? null, to.id];
}

function DiffView({ token, from, to }: { token: string; from: number; to: number }) {
  const [diff, setDiff] = useState<ConfigDiff | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const ctrl = new AbortController();
    setDiff(null);
    get<ConfigDiff>(token, `/api/configs/diff?from=${from}&to=${to}`, ctrl.signal)
      .then(d => { setDiff(d); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, from, to]);

  if (error) return <div role="alert" className="alert">{error}</div>;
  if (!diff) return <div className="empty">Сравнение версий…</div>;
  return <>
    <div className="diff-head">
      <b>{short(diff.from)}</b><span className="muted">{instanceName(diff.from)} · {date(diff.from.first_seen_at)}</span>
      <span>→</span>
      <b>{short(diff.to)}</b><span className="muted">{instanceName(diff.to)} · {date(diff.to.first_seen_at)}</span>
      {!diff.identical && (Object.keys(OPS) as ConfigChange['op'][]).filter(op => diff.summary[op]).map(op =>
        <span key={op} className={badge(OPS[op][1])}>{OPS[op][0]}: {diff.summary[op]}</span>)}
    </div>
    {instanceKey(diff.from) !== instanceKey(diff.to) &&
      <p className="note">Версии относятся к разным экземплярам: различия могут быть не изменением, а разной настройкой экземпляров.</p>}
    {diff.identical || !diff.changes.length
      ? <div className="empty">Конфигурации совпадают.</div>
      : <div className="table-wrap"><Table className="dense">
        <TableHeader><TableRow><TableHead>Поле</TableHead><TableHead>Изменение</TableHead><TableHead>Было</TableHead><TableHead>Стало</TableHead></TableRow></TableHeader>
        <TableBody>{diff.changes.map((c, n) => { const [label, show] = field(c.path); return <TableRow key={c.path + n}>
          <TableCell>{label}<small>{c.path}</small></TableCell>
          <TableCell><span className={badge(OPS[c.op][1])}>{OPS[c.op][0]}</span></TableCell>
          <TableCell className="diff-value">{c.op === 'added' ? '' : show(c.before)}</TableCell>
          <TableCell className="diff-value">{c.op === 'removed' ? '' : show(c.after)}</TableCell>
        </TableRow>; })}</TableBody>
      </Table></div>}
  </>;
}

/** Config versions of a service or one of its instances, with a diff between two chosen versions. */
export function ConfigHistory({ token, serviceId, instances, refreshKey }:
  { token: string; serviceId: string; instances: Placed[]; refreshKey: string }) {
  const [scope, setScope] = useState('');
  const [versions, setVersions] = useState<ConfigVersion[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [known, setKnown] = useState<Record<string, string>>({});

  const at = scope.indexOf('/');
  const base = scope
    ? `/api/instances/${encodeURIComponent(scope.slice(0, at))}/${encodeURIComponent(scope.slice(at + 1))}/configs?limit=100`
    : `/api/services/${encodeURIComponent(serviceId)}/configs?limit=100`;

  useEffect(() => {
    const ctrl = new AbortController();
    get<ConfigVersionPage>(token, base, ctrl.signal)
      .then(page => {
        setVersions(page.versions); setNext(page.next); setError(''); setLoaded(true);
        // Keep the reader's choice across the 30-second refresh while both versions are still listed.
        const ids = new Set(page.versions.map(v => v.id));
        setFrom(old => old != null && ids.has(old) ? old : defaults(page.versions)[0]);
        setTo(old => old != null && ids.has(old) ? old : defaults(page.versions)[1]);
        if (!scope) setKnown(Object.fromEntries(page.versions.map(v => [instanceKey(v), instanceName(v)])));
      })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, base, refreshKey]);

  async function more() {
    try {
      const page = await get<ConfigVersionPage>(token, `${base}&before=${encodeURIComponent(next!)}`);
      setVersions(old => [...old, ...page.versions]);
      setNext(page.next);
    } catch (e) { setError((e as Error).message); }
  }

  const choose = (key: string) => { setScope(key); setFrom(null); setTo(null); setLoaded(false); };
  // Removed instances stay selectable while their versions are kept.
  const options = { ...known, ...Object.fromEntries(instances.map(i => [instanceKey({ source_id: i.source.source_id, instance_id: i.id }), i.name])) };

  return <div>
    <div className="chart-controls">
      <label>Экземпляр <select value={scope} onChange={e => choose(e.target.value)}>
        <option value="">Все экземпляры сервиса</option>
        {Object.entries(options).map(([key, name]) => <option key={key} value={key}>{name} · {key.split('/')[0]}</option>)}
      </select></label>
      <span className="muted">Отметьте две версии: «Было» и «Стало».</span>
    </div>
    {error && <div role="alert" className="alert">{error}</div>}
    {loaded && !versions.length && <div className="empty">Версий конфигурации пока нет. Они появляются после первого снимка агента.</div>}
    {versions.length > 0 && <div className="table-wrap"><Table className="dense">
      <TableHeader><TableRow><TableHead>Было</TableHead><TableHead>Стало</TableHead><TableHead>Версия</TableHead><TableHead>Экземпляр</TableHead><TableHead>Наблюдалась</TableHead></TableRow></TableHeader>
      <TableBody>{versions.map(v => <TableRow key={v.id} className={v.id === from || v.id === to ? 'picked' : ''}>
        <TableCell><input type="radio" name="config-from" aria-label={`Было: ${short(v)}`} checked={v.id === from} onChange={() => setFrom(v.id)} /></TableCell>
        <TableCell><input type="radio" name="config-to" aria-label={`Стало: ${short(v)}`} checked={v.id === to} onChange={() => setTo(v.id)} /></TableCell>
        <TableCell><b>{short(v)}</b>{v.current && <span className={badge()}>Текущая</span>}</TableCell>
        <TableCell>{instanceName(v)}<small>{v.environment || '—'} · {v.source_id}</small></TableCell>
        <TableCell>{date(v.first_seen_at)}<small>до {date(v.last_seen_at)}</small></TableCell>
      </TableRow>)}</TableBody>
    </Table></div>}
    {next && <button className={outlineButton('mt-4')} onClick={more}>Показать еще</button>}

    {versions.length > 0 && <>
      <h3 className="subheading">Различия</h3>
      {from == null || to == null || from === to
        ? <div className="empty">{versions.length < 2 ? 'Есть только одна версия — сравнивать не с чем.' : 'Выберите две разные версии.'}</div>
        : <DiffView token={token} from={from} to={to} />}
    </>}
    <p className="note">Версия — каждая отличающаяся конфигурация экземпляра; возврат к прежней конфигурации записывается новой версией. «Наблюдалась» — от первого до последнего снимка с этой конфигурацией; изменения между опросами агента не видны. Значения переменных окружения и команды не собираются, поэтому в истории их нет.</p>
  </div>;
}
