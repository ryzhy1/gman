import { useEffect, useState } from 'react';
import { get, type AlertList } from './api';
import { date } from './format';

/** Runbooks for deploy/alerts/rules.yaml; alert annotations link here as <portal>/#alerts/<name>. */
const RUNBOOKS: [string, string, string[]][] = [
  ['GManNodeDown', 'Хост недоступен', [
    'Молчат оба канала — агент inventory и OTel Collector: проверить питание и сеть сервера (ping/SSH, BMC).',
    'После восстановления убедиться, что запустились gman-collect.timer и gman-otelcol.',
    'Пока алерт активен, остальные алерты этого хоста (GPU, контейнеры, отдельные каналы) подавлены.']],
  ['GManSourceSilent', 'Источник inventory молчит', [
    'Проверить на хосте таймер агента: systemctl status gman-collect.timer, journalctl -u gman-collect.',
    'Проверить доступность портала с хоста (HTTPS, сертификат) и токен источника в GMAN_AGENT_TOKENS.',
    'Пока источник молчит, алерты его контейнеров и узлов подавлены — их данные устарели.']],
  ['GManHostTelemetryAbsent', 'Нет метрик хоста', [
    'Хост, вероятно, жив (иначе был бы GManNodeDown или NotReady): проверить Collector — systemctl status gman-otelcol на Docker-хосте или pod DaemonSet gman-otel на узле Kubernetes.',
    'Проверить доставку в gateway: ошибки TLS/сети в журнале Collector, срок действия клиентского сертификата.',
    'Не создается, пока недоступен сам gateway (GManTelemetryGatewayDown).']],
  ['GManK8sNodeNotReady', 'Узел Kubernetes NotReady', [
    'kubectl describe node <узел>: условия, события kubelet, pressure.',
    'Проверить kubelet и container runtime на узле, сеть до control plane.',
    'Алерты GPU и pod-ов этого узла подавлены, пока алерт активен.']],
  ['GManTelemetryGatewayDown', 'OTel gateway недоступен', [
    'docker compose ps otel; docker compose logs otel — ошибки конфигурации или нехватка памяти.',
    'Пока gateway лежит, алерты об отсутствии метрик хостов и GPU не создаются; алерты по inventory работают.']],
  ['GManWatchdog', 'Сигнал жизни конвейера алертов', [
    'Срабатывает всегда и на портале не показывается: Alertmanager каждые ~5 минут отправляет его только во внешний сервис проверки (GMAN_ALERT_HEARTBEAT_URL).',
    'Если внешний сервис сообщил, что сигнал пропал: проверить доступность сервера GMan, docker compose ps prometheus alertmanager, исходящую сеть до сервиса проверки.',
    'Отдельно доступность портала и срок сертификата снаружи проверяет deploy/monitoring/probe.sh (cron на другой машине).']],
  ['GManTlsCrlExpiring', 'Истекает список отзыва (CRL)', [
    'Не дать CRL истечь: после nextUpdate nginx отвергает все клиентские сертификаты OTLP, и метрики перестают приходить со всех серверов.',
    'На машине администратора: deploy/pki/issue.sh crl (новый crl.pem на CRL_DAYS, по умолчанию 365 дней).',
    'scp deploy/pki/out/crl.pem gman:/tmp/; на сервере cp /tmp/crl.pem <GMAN_TLS_DIR>/crl.pem (cp, не mv: файл смонтирован по inode), права 0644.',
    'docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.prod.yaml exec portal nginx -s reload. Алерт снимется при следующем опросе /metrics.',
    'С корпоративным CA — получить свежий CRL у его владельцев и положить так же.']],
  ['GManTlsCrlExpired', 'Список отзыва (CRL) просрочен', [
    'nginx сейчас отвергает все клиентские сертификаты OTLP: метрики не приходят ни с одного сервера (алерты об отсутствии метрик подавлены этим алертом). Inventory по HTTPS не затронут.',
    'Срочно: deploy/pki/issue.sh crl на машине администратора, затем cp crl.pem в <GMAN_TLS_DIR>/crl.pem на сервере и nginx -s reload (шаги — в GManTlsCrlExpiring).',
    'Проверка: openssl crl -in <GMAN_TLS_DIR>/crl.pem -noout -nextupdate; в журнале Collector-ов пропадают ошибки TLS, GManHostTelemetryAbsent не появляется.']],
  ['GManTlsCertExpiring', 'Истекает сертификат', [
    'Метка file — какой файл: portal.pem (HTTPS портала и API, им пользуются агенты inventory), gateway.pem (OTLP 4317), ca.pem (CA пилота).',
    'Серверный сертификат: на машине администратора удалить старые deploy/pki/out/<name>.pem и <name>-key.pem, выполнить deploy/pki/issue.sh server <name> DNS:<имя> (те же SAN), скопировать оба файла в GMAN_TLS_DIR через cp и выполнить nginx -s reload.',
    'CA: перевыпуск означает новые сертификаты всех узлов и серверов и раздачу нового ca.pem — планировать заранее; срок CA (CA_DAYS) — 5 лет.',
    'С корпоративным CA — запросить новый сертификат у его владельцев, имена файлов те же.']],
  ['GManTlsCertExpired', 'Сертификат просрочен', [
    'portal.pem: агенты inventory и браузеры не доверяют порталу; gateway.pem: Collector-ы не подключаются к 4317; ca.pem: не проходят все сертификаты пилота.',
    'Перевыпустить и заменить, как в GManTlsCertExpiring, затем nginx -s reload.']],
  ['GManTlsFileError', 'Файл TLS не читается', [
    'Файл из GMAN_TLS_WATCH_FILES отсутствует, нечитаем пользователем API (65534) или не разбирается: сроки по нему не контролируются.',
    'Проверить на сервере: ls -l <GMAN_TLS_DIR>; сертификаты и crl.pem должны иметь права 0644 (ключи остаются 0600 и в API не монтируются).',
    'Пути с «key» в имени API не читает намеренно — уберите их из GMAN_TLS_WATCH_FILES. Подробности — в журнале API (docker compose logs api).']],
  ['GManApiMetricsDown', 'API не отдает /metrics', [
    'docker compose ps api; логи API, доступность PostgreSQL.',
    'Алерты по inventory (молчащие источники, OOMKilled, CrashLoopBackOff) в это время не вычисляются.']],
  ['GManGpuTelemetryAbsent', 'Нет метрик DCGM', [
    'Проверить контейнер DCGM Exporter на хосте (127.0.0.1:9400/metrics) или pod GPU Operator на узле.',
    'nvidia-smi на хосте: GPU мог пропасть с шины (тогда будет и XID 79).']],
  ['GManGpuXidError', 'Ошибка XID', [
    'Найти контекст: dmesg | grep -i xid, nvidia-smi -q на хосте.',
    'XID 13/31/43/45 обычно вызваны приложением: проверить процессы GPU в карточке хоста.',
    'XID 48/64/74/79/92/94/95/119/120 — аппаратные: вывести GPU из работы, перезагрузить или сбросить GPU, при повторе — RMA.',
    'DCGM хранит код последней XID до сброса GPU: после разбора заглушить алерт (silence) в Alertmanager.']],
  ['GManGpuEccUncorrectable', 'Неисправимые ошибки ECC', [
    'nvidia-smi -q -d ECC,ROW_REMAPPER: счетчики, ожидающие переназначения строки.',
    'Остановить нагрузку на GPU и сбросить его, чтобы применить переназначение; при сбое переназначения — RMA.']],
  ['GManGpuEccCorrectableHigh', 'Частые исправимые ошибки ECC', [
    'Наблюдать рост; сравнить с другими GPU хоста. Устойчивый рост — запланировать проверку памяти GPU.']],
  ['GManGpuOverheating', 'Перегрев GPU', [
    'Проверить обороты вентиляторов и температуру в стойке, загрузку и мощность GPU в карточке хоста.',
    'Порог — по модели GPU из группы gman-gpu-thresholds в deploy/alerts/rules.yaml, для остальных моделей 85/90 °C; температура замедления — nvidia-smi -q -d TEMPERATURE.']],
  ['GManGpuMemoryOverheating', 'Перегрев памяти GPU', [
    'Проверить охлаждение; длительная работа HBM выше 95 °C ведет к снижению частот и ошибкам ECC.']],
  ['GManGpuThermalThrottling', 'Тепловой троттлинг', [
    'GPU снижает частоты из-за температуры: проверить охлаждение и воздушный поток, затем GManGpuOverheating.']],
  ['GManGpuHardwareSlowdown', 'Аппаратное снижение частот', [
    'hw_slowdown/hw_power_brake: внешний сигнал от платформы — питание (PSU), перегрев шасси.',
    'Проверить журнал BMC/IPMI и блоки питания сервера.']],
  ['GManInstanceOOMKilled', 'OOMKilled', [
    'Сравнить потребление памяти с лимитом на вкладке «Ресурсы» сервиса.',
    'Поднять лимит или найти утечку; в Kubernetes — kubectl describe pod, раздел Last State.',
    'Время последнего OOM — в таблице экземпляров и событие «OOM kill» в «Истории». Docker с restart policy: OOM виден только при агенте с --watch.']],
  ['GManInstanceCrashLoopBackOff', 'CrashLoopBackOff', [
    'kubectl logs --previous <pod> -c <контейнер>; kubectl describe pod — exit code и события.',
    'Сверить последнюю версию конфигурации на вкладке «Конфигурация» и недавние деплои в «Истории».']],
  ['GManInstanceRestartLoop', 'Частые перезапуски', [
    'docker logs / kubectl logs --previous; причина последнего завершения видна в «Экземплярах».',
    'Проверить health/liveness probe: слишком строгая проверка тоже перезапускает контейнер.']],
  ['GManInstanceImagePullFailing', 'Образ не скачивается', [
    'kubectl describe pod: имя образа, ошибка registry, imagePullSecrets.',
    'Проверить, что тег/digest существует и registry доступен с узла.']],
  ['GManReleaseFailed', 'Релиз завершился ошибкой', [
    'Открыть сервис, вкладка «Релизы»: причина (ошибка Bamboo, сбой новых экземпляров, тайм-аут выката) и экземпляры.',
    'Решить: исправить и выпустить новый релиз или откатить через Bamboo; алерт снимается следующим релизом окружения.']],
  ['GManKubeStateMetricsAbsent', 'Нет метрик kube-state-metrics', [
    'kubectl -n gman-otel get pods -l app.kubernetes.io/name=kube-state-metrics -o wide; логи pod-а (ошибки RBAC, OOM).',
    'Проверить pod gman-otel на том же узле: его Collector забирает kube-state-metrics и отправляет в gateway.',
    'Пока алерт активен, алерты workloads кластера не вычисляются.']],
  ['GManWorkloadReplicasUnavailable', 'Недоступные реплики', [
    'kubectl -n <namespace> describe deployment|statefulset|daemonset <имя>; kubectl get pods -o wide — какие pod-ы не готовы и почему.',
    'Причины по экземплярам — на вкладке «Экземпляры» сервиса (CrashLoopBackOff, OOMKilled, образ) и в алертах GManInstance*.',
    'critical — не готова ни одна реплика: сервис недоступен. Pending-pod-ы и застрявший rollout того же workload подавляют warning; NotReady узла — warning и critical DaemonSet-ов кластера.']],
  ['GManWorkloadRolloutStuck', 'Rollout не продвигается', [
    'ProgressDeadlineExceeded: kubectl rollout status и describe — новые pod-ы не становятся Ready (probe, образ, ресурсы, квоты).',
    'Откат: kubectl rollout undo deployment/<имя>; сравнить версии на вкладке «Конфигурация».',
    'GenerationNotObserved: контроллер не обрабатывает объект — проверить kube-controller-manager.',
    'OutdatedReplicas (StatefulSet, DaemonSet): 30 минут не уменьшается число pod-ов на прежней версии. kubectl rollout status; при RollingUpdate — найти неготовый pod (для DaemonSet — number_unavailable достиг maxUnavailable).',
    'updateStrategy OnDelete: pod-ы обновляются только после удаления — kubectl delete pod <pod> по одному, дождавшись готовности. Partition StatefulSet (canary) держит часть pod-ов на прежней ревизии намеренно — заглушить алерт.']],
  ['GManWorkloadPodsPending', 'Pod-ы в Pending', [
    'kubectl describe pod <pod>: события планировщика (Insufficient cpu/memory/nvidia.com/gpu, taints, affinity, PVC).',
    'Сравнить requests с allocatable узлов в карточке кластера «Серверы и GPU».']],
  ['GManJobFailed', 'Job завершилась ошибкой', [
    'kubectl -n <namespace> describe job <имя>; kubectl logs job/<имя> — причина последних попыток.',
    'Алерт держится, пока Job не удалена (для CronJob — по failedJobsHistoryLimit); после разбора удалить Job или заглушить алерт.']],
  ['GManDaemonSetMisscheduled', 'DaemonSet на неподходящих узлах', [
    'Pod-ы остались на узлах после смены nodeSelector/affinity или меток/taints узла: kubectl get pods -o wide -l <селектор>.',
    'Удалить лишние pod-ы или вернуть метки узлов.']],
];

const OBJECT_LABELS = [['host_id', 'Хост'], ['source_id', 'Источник'], ['UUID', 'GPU'], ['instance_name', 'Экземпляр'],
  ['namespace', 'Namespace'], ['workload_kind', 'Тип'], ['workload', 'Workload'], ['service_id', 'Сервис'],
  ['environment', 'Окружение'], ['reason', 'Причина'], ['file', 'Файл'], ['subject', 'Сертификат'],
  ['issuer', 'Издатель CRL']] as const;

export function Alerts({ token, refreshKey }: { token: string; refreshKey: string }) {
  const [data, setData] = useState<AlertList | null>(null);
  const [error, setError] = useState('');
  const [hash, setHash] = useState(location.hash);
  const focus = decodeURIComponent(hash.replace(/^#alerts\/?/, ''));

  useEffect(() => {
    const update = () => setHash(location.hash);
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    get<AlertList>(token, '/api/alerts', ctrl.signal)
      .then(value => { setData(value); setError(''); })
      .catch(e => { if (!ctrl.signal.aborted) setError((e as Error).message); });
    return () => ctrl.abort();
  }, [token, refreshKey]);

  useEffect(() => {
    if (focus) document.getElementById(`runbook-${focus}`)?.scrollIntoView();
  }, [focus]);

  const problem = error || data?.error || '';
  return <>
    <section>
      <h2>Активные алерты <span className="count">{data?.alerts.length ?? 0}</span></h2>
      <p className="muted">Из Alertmanager, без заглушенных (silence) и подавленных: при падении узла показывается сам узел, а не его GPU и контейнеры.</p>
      {problem && <div role="status" className="alert">Алерты недоступны: {problem}</div>}
      {data && !problem && !data.alerts.length && <div className="empty">Активных алертов нет.</div>}
      {data && data.alerts.length > 0 && <div className="table-wrap"><table>
        <thead><tr><th>Важность</th><th>Алерт</th><th>Объект</th><th>С</th><th>Что делать</th></tr></thead>
        <tbody>{data.alerts.map(a => <tr key={a.name + JSON.stringify(a.labels)}>
          <td><span className={`badge ${a.labels.severity === 'critical' ? 'bad' : 'warn'}`}>{a.labels.severity ?? '—'}</span></td>
          <td><b>{a.summary || a.name}</b><small>{a.name}</small><small>{a.description}</small></td>
          <td>{OBJECT_LABELS.filter(([k]) => a.labels[k]).map(([k, label]) =>
            <small key={k}>{label}: {a.labels[k]}</small>)}</td>
          <td>{date(a.starts_at)}</td>
          <td><a href={`#alerts/${a.name}`}>Runbook</a></td>
        </tr>)}</tbody>
      </table></div>}
    </section>
    <section>
      <h2>Runbooks</h2>
      <p className="muted">Правила — deploy/alerts/rules.yaml; уведомления и подавление — deploy/alerts/alertmanager.yaml.</p>
      {RUNBOOKS.map(([name, title, steps]) => <div key={name} id={`runbook-${name}`} className={focus === name ? 'event chosen' : 'event'}>
        <h3>{title} <small>{name}</small></h3>
        <ol>{steps.map(s => <li key={s}>{s}</li>)}</ol>
      </div>)}
    </section>
  </>;
}
