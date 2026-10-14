import type { Audit } from './api';
import { ConfigView, type Placed } from './components';
import { date, grafanaLink, stale } from './format';
import { tone as badge } from '@/ui/tone';

/** A deployment's author is relevant only when its runtime event matches this exact start. */
export function launchEvent(instance: Placed, events: Audit[]): Audit | undefined {
  if (!instance.started_at) return undefined;
  const startedAt = Date.parse(instance.started_at);
  return events.find(e => e.source_id === instance.source.source_id && e.instance_id === instance.id
    && ['instance-appeared', 'instance-restarted', 'state-changed'].includes(e.type)
    && e.after?.started_at != null
    && Date.parse(e.after.started_at) === startedAt
    && !!e.actor && e.actor !== 'unknown' && !!e.release_id);
}

export function LaunchDetails({ instance, events, expanded = false, grafanaUrl = '', range = '1h', serviceGrafana = '' }: {
  instance: Placed; events: Audit[]; expanded?: boolean; grafanaUrl?: string; range?: string; serviceGrafana?: string;
}) {
  const event = launchEvent(instance, events);
  const labelled = instance.origin?.started_by ?? '';
  const serviceUrl = serviceGrafana || (instance.service_id
    ? grafanaLink(grafanaUrl, 'gman-service', { service: instance.service_id, environment: instance.environment }, range) : '');
  const hostUrl = instance.runtime === 'docker'
    ? grafanaLink(grafanaUrl, 'gman-host', { host: instance.source.source_id }, range)
    : grafanaLink(grafanaUrl, 'gman-k8s', { cluster: instance.source.cluster || instance.source.source_id }, range);
  return <div>
    <h3>{instance.name}</h3>
    <div className="links grafana-actions">
      {serviceUrl && <a href={serviceUrl} target="_blank" rel="noopener noreferrer">Grafana · сервис ↗</a>}
      {hostUrl && <a href={hostUrl} target="_blank" rel="noopener noreferrer">Grafana · {instance.runtime === 'docker' ? 'хост' : 'кластер'} ↗</a>}
      {!serviceUrl && !hostUrl && <span>Grafana не настроена</span>}
    </div>
    {stale(instance.source) && <p className={badge('warn')}>Данные устарели</p>}
    <dl>
      <div><dt>Последний запуск</dt><dd>{instance.started_at ? date(instance.started_at) : 'Время запуска не получено'}</dd></div>
      <div><dt>Кем запущен</dt><dd>{event ? event.actor : labelled || 'Инициатор неизвестен'}</dd></div>
      <div><dt>Основание</dt><dd>{event ? `${event.actor_source} · релиз #${event.release_id}`
        : labelled ? 'Метка gman.started-by: имя указал запускающий при создании контейнера, платформа его не проверяет.'
        : 'В последних событиях нет подтверждения автора этого запуска. Docker и Kubernetes сами по себе не сообщают имя человека.'}</dd></div>
      <div><dt>Где запущен</dt><dd>{instance.node || instance.source.hostname} · {instance.environment}</dd></div>
      <div><dt>Конфигурация получена</dt><dd>{date(instance.source.observed_at)}</dd></div>
    </dl>
    <ConfigView instance={instance} expanded={expanded} />
    <p className="note">Показана последняя наблюдаемая конфигурация. Конфигурация до подключения агента может быть неизвестна.
      Переменные окружения и параметры запуска показаны как есть и могут содержать секреты; команды проверок здоровья не собираются.
      Пользователь процесса в конфигурации — не автор запуска.</p>
  </div>;
}
