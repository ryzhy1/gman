import { useState } from 'react';
import { canEdit, send, type Me, type Service } from './api';
import { Pencil } from 'lucide-react';
import { Button } from '@/ui/button';
import { Input } from '@/ui/input';
import { Label } from '@/ui/label';

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
    return <Button variant="outline" size="sm" className="mt-4" onClick={() => { setDraft(service); setOwners((service.owners ?? []).join(', ')); setError(''); }}>
      <Pencil /> Изменить запись каталога</Button>;
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
    {FIELDS.map(([key, label]) => <Label key={key} className={key === 'description' ? 'wide' : ''}>{label}
      <Input value={draft[key] ?? ''} required={key === 'name'} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
    </Label>)}
    <Label className="wide">Владельцы (через запятую: email, логин, subject или group:имя-группы)
      <Input value={owners} disabled={!admin} onChange={e => setOwners(e.target.value)}
        title={admin ? '' : 'Список владельцев меняет только администратор платформы'} />
    </Label>
    {error && <div role="alert" className="alert wide">{error}</div>}
    <div className="actions wide">
      <Button disabled={busy}>{busy ? 'Сохранение…' : 'Сохранить'}</Button>
      <Button type="button" variant="outline" onClick={() => setDraft(null)}>Отмена</Button>
    </div>
    <p className="note wide">Изменение записывается в журнал событий с указанием автора.</p>
  </form>;
}
