import { useState } from 'react';
import { canEdit, send, type Me, type Service } from './api';

const FIELDS = [
  ['name', 'Название'], ['team', 'Команда'], ['owner', 'Ответственный'], ['repository', 'Репозиторий'],
  ['grafana', 'Grafana'], ['splunk', 'Splunk'], ['bamboo', 'Bamboo'], ['description', 'Описание'],
] as const;

/** Catalog entry editor; shown only to roles the API would allow, which still checks every request. */
export function ServiceEditor({ service, me, token, onSaved }: { service: Service; me: Me; token: string; onSaved: () => void }) {
  const [draft, setDraft] = useState<Service | null>(null);
  const [owners, setOwners] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (!canEdit(me, service)) {
    return <p className="note">{me.role === 'viewer'
      ? 'Роль «Просмотр»: изменение каталога недоступно.'
      : 'Изменять запись могут владельцы сервиса из списка «Владельцы» и администраторы платформы.'}</p>;
  }
  if (!draft) {
    return <button className="more" onClick={() => { setDraft(service); setOwners((service.owners ?? []).join(', ')); setError(''); }}>
      Изменить запись каталога</button>;
  }
  const admin = me.role === 'admin';

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const value = { ...draft!, owners: admin ? owners.split(',').map(o => o.trim()).filter(Boolean) : service.owners ?? [] };
      await send(me, token, 'PUT', `/api/services/${encodeURIComponent(service.id)}`, value);
      setDraft(null);
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <form className="edit-form" onSubmit={save}>
    {FIELDS.map(([key, label]) => <label key={key} className={key === 'description' ? 'wide' : ''}>{label}
      <input value={draft[key] ?? ''} required={key === 'name'} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
    </label>)}
    <label className="wide">Владельцы (через запятую: email, логин, subject или group:имя-группы)
      <input value={owners} disabled={!admin} onChange={e => setOwners(e.target.value)}
        title={admin ? '' : 'Список владельцев меняет только администратор платформы'} />
    </label>
    {error && <div role="alert" className="alert wide">{error}</div>}
    <div className="actions wide">
      <button className="primary" disabled={busy}>{busy ? 'Сохранение…' : 'Сохранить'}</button>
      <button type="button" className="more" onClick={() => setDraft(null)}>Отмена</button>
    </div>
    <p className="note wide">Изменение записывается в журнал событий с указанием автора.</p>
  </form>;
}
