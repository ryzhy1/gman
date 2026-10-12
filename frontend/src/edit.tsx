import { useState } from 'react';
import { send, type Service } from './api';
import { Pencil } from 'lucide-react';
import { Button } from '@/ui/button';
import { Input } from '@/ui/input';
import { Label } from '@/ui/label';

const FIELDS = [
  ['name', 'Название'], ['team', 'Команда'], ['owner', 'Ответственный'], ['repository', 'Репозиторий'],
  ['grafana', 'Grafana'], ['splunk', 'Splunk'], ['bamboo', 'Bamboo'], ['description', 'Описание'],
] as const;

/** Catalog entry editor. */
export function ServiceEditor({ service, onSaved }: { service: Service; onSaved: () => void }) {
  const [draft, setDraft] = useState<Service | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (!draft) {
    return <Button variant="outline" size="sm" className="mt-4" onClick={() => { setDraft(service); setError(''); }}>
      <Pencil /> Изменить запись каталога</Button>;
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await send('PUT', `/api/services/${encodeURIComponent(service.id)}`, draft);
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
    {error && <div role="alert" className="alert wide">{error}</div>}
    <div className="actions wide">
      <Button disabled={busy}>{busy ? 'Сохранение…' : 'Сохранить'}</Button>
      <Button type="button" variant="outline" onClick={() => setDraft(null)}>Отмена</Button>
    </div>
    <p className="note wide">Изменение записывается в журнал событий.</p>
  </form>;
}
