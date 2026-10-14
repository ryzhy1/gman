# Состояние и следующие шаги

Обновлено 11 октября 2026 года. Документ для продолжения работы на другой машине.
Архитектура по фактической реализации — [architecture.md](architecture.md), инструкции — README.

## Что сделано

1. Каталог и inventory: нормализованная схема PostgreSQL (Alembic, миграции 0001–0004),
   прием снимков Docker-хостов и Kubernetes-кластера с токеном на источник, состояние
   отдельно от health, происхождение по allowlist меток, история изменений по снимкам,
   retention. Исправлены баги из ревью (race `docker ps`/`inspect`, нулевое время старта,
   повторы Bamboo, молчаливый сбой nvidia-smi, 500 на не-ASCII токене и др.).
2. Надежность агента (agent/gman_agent, только stdlib): ограниченный spool на диске,
   повторы с backoff и jitter, неблокирующий flock, режим `--watch` по `docker events`
   с debounce и промежуточным снимком для исчезнувших контейнеров.
3. История конфигураций: версии сервиса и экземпляра, версия целиком, структурный diff
   (`GET /api/configs/diff`), миграция 0002, вкладка «Конфигурация» с выбором «Было/Стало».
4. Ресурсы: OTel Collector на узлах → gateway → Prometheus → Grafana с дашбордами
   «Хост», «Сервис», «Kubernetes»; API читает Prometheus фиксированными шаблонами,
   вкладка «Ресурсы» с графиками; OTel DaemonSet для кластера.
5. GPU: подробный снимок nvidia-smi и DCGM, процессы GPU → контейнеры Docker; в Kubernetes
   PID → pod через агент `--runtime kubernetes-node` на GPU-узле (cgroup v1/v2,
   containerd/CRI-O/cri-dockerd, static pod), DaemonSet deploy/gpu-attribution-k8s.yaml,
   GPU по pod-ам из меток DCGM.
6. Splunk: ссылки «Логи в Splunk» для сервиса и экземпляров по GMAN_SPLUNK_* с
   экранированием SPL и переопределением logs.splunk в каталоге.
7. SSO (OIDC Authorization Code + PKCE), серверные сессии (миграция 0003), CSRF, роли
   viewer/owner/admin из групп IdP, владельцы записи в `owners`, редактор каталога в
   карточке, автор изменений каталога в журнале. Bearer-токены — для автоматизации.
8. Алерты: Prometheus rules + Alertmanager, /metrics API с состоянием inventory,
   18 алертов (узел/источник/телеметрия, GPU XID/ECC/перегрев/троттлинг, OOMKilled,
   CrashLoopBackOff, перезапуски, образ), inhibition, получатели из GMAN_ALERT_*,
   страница «Алерты» с runbook.
9. Production-развертывание: compose.prod.yaml (nginx TLS 443/80, OTLP 4317 с mTLS,
   Grafana на /grafana/, ротация логов), deploy/pki/issue.sh, бэкап pg_dump с проверкой
   и ротацией, процедура восстановления, systemd-юниты агента и Collector.
10. docs/architecture.md переписан по фактической реализации.
11. Релизы (миграция 0004): событие Bamboo сверяется с runtime по image digest или
    версии/тегу и окну времени; статусы started → rolling-out → succeeded / failed
    (Bamboo failed, тайм-аут, crash loop/OOM новых экземпляров) / superseded; runtime-изменения
    релиза атрибутируются инициатору из Bamboo; тайм-ауты в housekeeping после снимков;
    `GET /api/releases`, `/api/releases/{id}`, вкладка «Релизы», ссылка из истории.
    bamboo_event.py сверен с документацией Bamboo: deployment_id из deploymentResultId
    (повторный деплой того же релиза больше не склеивается с первым), имя пользователя —
    только при ручном триггере, deploy.version и planRepository.1.revision как запасные.

12. Runtime-история: `--watch` для Kubernetes (list + watch pods и pod Events от resourceVersion,
    BOOKMARK, relist на 410 Gone, backoff, промежуточный снимок для pod-ов, удаленных между
    снимками); Kubernetes Events сервисов каталога в истории без message (allowlist причин,
    дедупликация по uid + count/времени, миграция 0005 runtime_event_marks); Docker OOM под
    restart policy по событию `oom` → last_oom_at экземпляра, событие instance-oom, метрика
    gman_instance_last_oom_timestamp_seconds и третье условие GManInstanceOOMKilled; явная
    отметка пробела в истории (history-gap: молчание > GMAN_HISTORY_GAP_SECONDS или
    переполнение spool агента) с плашкой «Данные за период могут быть неполными» в портале.

13. Безопасность и эксплуатация: OTLP mTLS завершает nginx (CA + CRL) и передает CN в gateway,
    gateway отбрасывает метрики с host.id ≠ CN сертификата хоста и ставит k8s.cluster.name из
    сертификата кластера; issue.sh `cluster`, `revoke`, `crl`; GPU DaemonSet на projected
    token с audience `gman` и TokenReview (GMAN_K8S_TOKEN_REVIEW, backend/app/k8s_tokens.py),
    узел из привязки токена; pg_backup.sh: хук выгрузки BACKUP_UPLOAD_CMD и проверка
    восстановления во временную базу (`--restore-check`, раз в 7 дней), оба в healthcheck.

14. Метрики и алерты Kubernetes и GPU: kube-state-metrics v2.20.0 (deploy/kube-state-metrics.yaml,
    минимальный RBAC, allowlist ресурсов и метрик) через Collector узла его pod-а; алерты
    workloads (недоступные реплики, застрявший rollout, Pending, упавшая Job, misscheduled
    DaemonSet, молчащий kube-state-metrics) с сервисом из меток pod-ов и inhibition; раздел
    «Workloads» в дашбордах «Kubernetes» (фильтр кластера) и «Сервис». CSV счетчиков DCGM
    (deploy/dcgm-counters.csv) с ECC, CLOCKS_EVENT_REASONS и SLOWDOWN_TEMP, сверка имен тестом;
    пороги перегрева по модели GPU (по умолчанию 85/90 °C). Ссылки Grafana из портала несут
    окружение и период. Исправлено: правила читали ECC без суффикса `_total`, а
    GManTelemetryGatewayDown срабатывал бы на `up` упавшего DCGM Exporter, переотданный gateway.
    Алерт GManReleaseFailed по gman_release_status (последний релиз окружения failed).

15. Сроки TLS: API отдает на /metrics notAfter сертификатов и nextUpdate CRL из
    GMAN_TLS_WATCH_FILES (cryptography; PEM-цепочки и DER, кэш по mtime, пути с «key» не
    читаются, ошибка — gman_tls_file_error); compose.prod монтирует в API portal.pem,
    gateway.pem, ca.pem и crl.pem без ключей. Группа gman-tls: GManTlsCrlExpiring (30/7 дней),
    GManTlsCrlExpired (подавляет алерты отсутствия телеметрии), GManTlsCertExpiring (30/7 дней),
    GManTlsCertExpired, GManTlsFileError; runbook в портале, unit-тесты promtool.

16. Состояние workloads в портале: `GET /api/workloads?service_id=&environment=` или `?cluster=`
    (роль «Просмотр», фиксированные шаблоны PromQL в backend/app/workloads.py с ключом и
    join-таблицей recording rules gman-workloads): desired/ready/available/updated/unavailable,
    generation/observedGeneration, Progressing, ревизии StatefulSet, Pending-pod-ы, Failed/active
    у Job, сработавшие алерты workload и статус «ок / раскатывается / деградирован / застрял».
    Раздел «Workloads Kubernetes» на вкладке «Обзор» сервиса и в карточке кластера
    (frontend/src/workloads.tsx) со ссылкой на дашборд «Kubernetes» (кластер, namespace, период);
    работает и с сессией SSO. Rollout StatefulSet (RollingUpdate и OnDelete: update_revision ≠
    current_revision и updated < replicas) и DaemonSet (updated_number_scheduled <
    desired_number_scheduled): recording rule gman:kube_workload_replicas_outdated:max и вариант
    GManWorkloadRolloutStuck с reason OutdatedReplicas (pod-ы на прежней версии ≥ 30 минут без
    уменьшения за 30 минут). В allowlist kube-state-metrics добавлены
    kube_deployment_status_replicas_ready, kube_statefulset_status_replicas_available,
    kube_statefulset_status_current_revision/update_revision, kube_daemonset_status_number_ready;
    test_deploy.py сверяет allowlist и с шаблонами API.
17. Очередь Collector на диске и внешний мониторинг сервера: Collector узлов (Docker-хост и
    DaemonSet) держит sending_queue в file_storage (256 МиБ по объему, повторы без предела по
    времени, compaction), systemd StateDirectory /var/lib/gman-otelcol, в Kubernetes hostPath
    /var/lib/gman-otel с init-контейнером для прав. Gateway остался pull-экспортером без очереди.
    Dead man's switch: всегда активный GManWatchdog уходит отдельным маршрутом только на
    GMAN_ALERT_HEARTBEAT_URL (healthchecks.io и аналоги) каждые 5 минут и не показывается в
    портале; deploy/monitoring/probe.sh для cron на другой машине (healthz, API, сроки
    сертификатов 443 и 4317).
18. Events узлов Kubernetes: агент читает и наблюдает (третий watch-поток) Events с
    involvedObject.kind = Node по отдельному allowlist (OOMKilling от node-problem-detector,
    SystemOOM, NodeNotReady, NodeHasDiskPressure / NodeHasInsufficientMemory / NodeHasInsufficientPID,
    EvictionThresholdMet, Rebooted, KernelOops, TaskHung, FilesystemIsReadOnly, ImageGCFailed),
    без message — только фиксированные фразы. OOM узла сопоставляется с pod-ом только по UID
    pod-а или ID контейнера из message, сверенным с pod-ами этого узла; иначе событие узла.
    API: RuntimeEvent.kind/node, allowlist по kind, отметки runtime_event_marks с node и reason
    (миграция 0006), метрики gman_k8s_node_event_last_timestamp_seconds и gman_k8s_node_pressure,
    сводка за 24 часа в карточке узла («Серверы и GPU»), сопоставленные события — в истории
    сервиса. Группа gman-node-events: GManK8sNodeOOM, GManK8sNodePressure, GManK8sNodeEviction,
    GManK8sNodeKernelProblem, GManK8sNodeRebooted (scope node: подавляются NotReady узла и
    молчанием источника; OOMKilling узла подавляется OOMKilled экземпляра того же узла).
    Docker без `--watch`: OOM kill с restart policy — из `docker events --since <прошлый запуск>
    --until <сейчас> --filter event=oom` (отметка в каталоге состояния агента).
19. Авторизация удалена (миграция 0007): нет SSO, сессий, ролей, `owners`, Bearer-токенов
    (read/admin/CI/agent) и TokenReview — пп. 7 и 13 в части идентичности больше не действуют.
    Стек самодостаточен: `docker compose up --build -d` из корня без .env; конфигурации
    Prometheus/Alertmanager/Grafana/OTel собраны в образы, агент inventory и Collector метрик
    самого сервера — сервисы compose, Grafana проксируется порталом на /grafana/.

Проверено:
- Тесты: 232 passed на PostgreSQL 17 (включая запросы истории конфигураций и тест
  конкурентности), 231 passed и 1 skipped на SQLite; миграции upgrade → downgrade base →
  upgrade и `alembic check` на PostgreSQL 17. Frontend: `npm run build`.
- /api/workloads (п. 16): тесты API с mock Prometheus и классификации статусов; все шаблоны
  PromQL вычислены `promtool test rules` на входных рядах тестов rules.test.yaml (результаты
  сверены вручную, включая ALERTS); правило OutdatedReplicas — unit-тестами promtool. Против
  настоящего kube-state-metrics новые метрики allowlist и раздел портала в браузере не проверялись.
- Kubernetes watch и Events — на fixtures потоков (ADDED/MODIFIED/DELETED, BOOKMARK, ERROR 410,
  сбой kubectl, HTTP 410 in-cluster), Docker `oom` — на fixtures событий; правило OOM —
  `promtool test rules` (Prometheus 3.5.0). Events узлов — на fixtures в форматах kubelet
  (SystemOOM, статусы узла, Rebooted) и node-problem-detector (OOMKilling «Killed process»,
  строка oom-kill с task_memcg, TaskHung, KernelOops); группа gman-node-events — `promtool test
  rules`, alertmanager.yaml — `amtool check-config`; `docker events --since/--until` — на fixtures.
- Метрики: otelcol-contrib 0.162.0, Prometheus 3.5.0, Grafana 12.1.1 (бинарные релизы) с
  эмуляцией Docker Engine API, kubelet и вывода DCGM: имена метрик, метки, запросы API
  и дашбордов, provisioning Grafana.
- Алерты: `promtool check rules`/`test rules`, `amtool check-config` (Alertmanager 0.28.1)
  с получателями и без; локальный прогон Prometheus + Alertmanager + API на SQLite —
  срабатывание, подавление и доставка webhook.
- kube-state-metrics v2.20.0 (сборка из исходников) против локального kube-apiserver 1.36
  (envtest) с объектами и статусами-фикстурами, под своим service account (RBAC без
  отказов) → Collector с конфигурацией otel-k8s.yaml (обнаружение pod-а по узлу, pod с
  той же меткой на другом узле не читается) → gateway → Prometheus 3.5.0 с rules.yaml:
  имена и метки, recording rules, алерты, все запросы дашбордов. Эмулятор DCGM по
  dcgm-counters.csv через тот же путь. Inhibition workload-алертов — живой Alertmanager
  0.28.1 с синтетическими алертами. Манифесты — kubeconform 0.7.0 и server-side dry-run.
- Production частично, без Docker daemon и Linux: `docker compose config` для обоих файлов
  (Compose 2.29.7); issue.sh (shellcheck, `openssl verify -purpose`); gateway — доставка
  с клиентским сертификатом и отказ без него и с чужим CA; pg_backup.sh на PostgreSQL 17
  (ротация, `--check`, восстановление `pg_restore --clean`); доверие агента пилотному CA
  через SSL_CERT_FILE; nginx-конфиги разобраны crossplane.
- Сроки TLS: тесты API на CA, сертификатах (в том числе просроченном и цепочке) и CRL (PEM и
  просроченный DER), созданных cryptography; битый, пустой, отсутствующий файл и путь ключа
  (не открывается); `promtool test rules` для порогов 30/7 дней и истечения.
- SSO — против mock IdP в тестах; bamboo_event.py — против локального API; чтение AMD sysfs.
- Очередь на диске: otelcol-contrib 0.162.0 `validate` всех конфигураций; прогон Collector →
  gateway, gateway остановлен, Collector перезапущен, gateway запущен — точки, собранные до
  перезапуска, доставлены с исходными метками времени. Dead man's switch: Prometheus 3.5.0 +
  Alertmanager 0.28.1 с конфигурацией из alertmanager.sh — сигнал на тестовый приемник сразу и
  повтор через 5–6 минут, в общий webhook не попадает; `amtool config routes test`. probe.sh —
  shellcheck и локальные HTTPS-серверы (истекающий сертификат, чужой CA, недоступность, mTLS-порт).
  Не проверены: systemd-юнит и DaemonSet на реальных узлах, реальный healthchecks.io.
- Привязка OTLP к сертификату: nginx 1.31 (OTLP-блок deploy/nginx/gman.conf) + otelcol-contrib
  0.162.0 (deploy/otel-gateway.prod.yaml) + отправляющие otelcol с сертификатами issue.sh:
  server-01 → host.id=server-01 принят; сертификат server-01 с host.id=server-02 и
  поддельным заголовком x-gman-otlp-host — отброшен; сертификат кластера — принят,
  k8s.cluster.name заменен на CN; отозванный (CRL) и чужой CA — 400 в nginx; прямо в gateway
  без метаданных — отброшен.
- TokenReview: настоящие kube-apiserver + etcd 1.36.2 (envtest) с RBAC, примененный
  deploy/gpu-attribution-k8s.yaml, bound token pod-а на узле gpu-1 через API GMan: свой
  узел — 200, другой узел и docker-host — 403, токен с audience API, чужой SA, не привязанный
  к pod-у и токен удаленного pod-а — 401, статический токен — 200. Ветка Kubernetes < 1.30
  (get pod) — только в тестах с MockTransport.
- pg_backup.sh на PostgreSQL 17: выгрузка (cp), `--restore-check` (успех, битый дамп, дамп без
  схемы), healthcheck при неудачной выгрузке и проверке.

## Подготовка на новой машине

```sh
git clone <url> && cd GMan
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q
cd frontend && npm ci && npm run build && cd ..
```

Проверка на PostgreSQL: задайте `GMAN_TEST_DATABASE_URL=postgresql+psycopg://...`
для пустой тестовой базы — тесты пересоздают схему (см. backend/tests/database.py).

Стенд: `docker compose up --build -d` из корня (портал :8080 с Grafana на /grafana/;
Prometheus :9090 и Alertmanager :9093 на 127.0.0.1). .env необязателен (GMAN_SPLUNK_*,
GMAN_ALERT_*, GMAN_HOST_IP и др. — по желанию).

Production (Docker Compose ≥ 2.24.4: в 2.24.3 `!override` у ports молча не срабатывает):
сертификаты `deploy/pki/issue.sh`, `GMAN_PUBLIC_HOST` в .env, затем
`docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.prod.yaml up --build -d`.
Подробно — README «Production-развертывание».

Проверка алертов без стека:

```sh
promtool check rules deploy/alerts/rules.yaml
promtool test rules deploy/alerts/rules.test.yaml
promtool check config deploy/prometheus.yaml
deploy/alerts/alertmanager.sh --render /tmp/am.yml && amtool check-config /tmp/am.yml
```

## Не проверено

Все перечисленное реализовано, но не запускалось на реальной инфраструктуре.

- Docker: сборка образов и `docker compose up` end-to-end (на исходной машине не было
  Docker daemon), healthcheck внутри образов (wget в grafana/prometheus/alertmanager,
  /grafana/api/health при serve_from_sub_path), entrypoint alertmanager.sh под busybox sh
  и права тома /alertmanager, сервис backup в контейнере (busybox sh/find), права uid 10001
  на ключ gateway; агент и `--watch` против настоящего Docker daemon (включая событие `oom`
  при restart policy и OOM kill дочернего процесса); `docker events --since/--until` по таймеру
  (формат времени, размер буфера событий daemon).
- nginx: `nginx -t` и запуск с TLS, Grafana за /grafana/ (включая Live/websocket),
  limit_req на реальной нагрузке.
- systemd: `systemd-analyze verify`, sandboxing с docker/nvidia-smi/kubectl, timer на Linux.
- Восстановление из бэкапа на реальном сервере; выгрузка на настоящий сетевой ресурс/S3;
  `--restore-check` внутри контейнера postgres:17-alpine (busybox find/sed).
- OTLP через nginx в контейнере nginx:1.28-alpine и Compose-сеть (проверено нативными
  бинарниками); TokenReview против настоящего кластера (не envtest) и Kubernetes < 1.30;
  projected token и права файла при runAsUser 65534 в настоящем pod-е.
- NVIDIA/DCGM: настоящие поля DCGM (XID, ECC, CLOCKS_EVENT_REASONS, SLOWDOWN_TEMP, метки
  pod/namespace/container), запуск exporter с deploy/dcgm-counters.csv (неизвестное версии
  DCGM поле не дает ему стартовать), пороги A100 в gman-gpu-thresholds против
  `nvidia-smi -q -d TEMPERATURE`, nvidia-smi на реальных драйверах, сверка показателей.
- Kubernetes/CRI-O: k8s_attributes, обнаружение DCGM и kube-state-metrics, RBAC,
  kubelet_stats, настоящие контроллеры (статусы rollout, Pending) в реальном кластере; `--watch` кластера (потоковый вывод `kubectl get --raw` для watch, частота
  BOOKMARK, 410 после compaction, нагрузка на API server), формат и series реальных Events;
  Events узлов от настоящих kubelet и node-problem-detector (тексты message по версиям, есть ли
  в OOMKilling путь cgroup с настроенным правилом), миграция 0006 на PostgreSQL; для kubernetes-node — nvidia-smi через NVIDIA runtime без запроса GPU, список
  процессов от non-root, PSA privileged для hostPID, формат cgroup на CRI-O/containerd.
- SSO с настоящим IdP (Keycloak, AD FS, Okta): claim групп, end_session, cookie `__Host-`.
- Интеграции: настоящий Bamboo (переменные `bamboo_*`), Splunk (индексы и поля),
  SMTP и webhook получателей алертов.
- Браузер: новые страницы и элементы портала — «Алерты», история конфигураций и diff,
  редактор каталога, вход через SSO и выход, процессы GPU в карточке кластера, ссылки
  Grafana с окружением и периодом (открытие в Grafana 12.1.1 с `$__all`), новые разделы
  дашбордов в самой Grafana (запросы проверены только через API Prometheus).
- Накладные расходы, cardinality и прирост диска на пилоте.

Первое, что стоит сделать на машине с Docker: `docker compose ... up --build`, пройти README
«Локальный запуск», затем «Production-развертывание» на тестовом сервере, запустить агент
на хосте с NVIDIA.

## Дальше по плану

Безопасность и эксплуатация:
- ~~Привязать клиентский сертификат OTLP к host.id~~ Сделано (п. 13: nginx + gateway). Остается:
  сертификат кластера общий для DaemonSet, host.id узлов им не привязан; вариант —
  сертификат на pod через cert-manager csi-driver (CN = имя узла, `OU=client`).
- ~~Отзыв сертификатов (CRL) вместо перевыпуска CA~~ Сделано (п. 13: issue.sh revoke/crl, ssl_crl).
- ~~Напоминание о переподписи CRL до nextUpdate и сроках сертификатов~~ Сделано (п. 15, алерты GManTls*).
  Переподпись остается ручной (ключ CA офлайн); клиентские сертификаты узлов на сервере не
  видны — их срок контролирует только GManHostTelemetryAbsent после истечения.
- ~~Projected token на каждый GPU-узел в deploy/gpu-attribution-k8s.yaml~~ Сделано (п. 13, TokenReview).
- ~~Выгрузка бэкапов за пределы сервера; регулярная проверка восстановления~~ Сделано (п. 13)
  (BACKUP_UPLOAD_CMD, `--restore-check`); подключить реальное хранилище.
- ~~Внешний мониторинг самого сервера GMan; постоянная очередь Collector на диске (file_storage).~~
  Сделано (п. 17: GManWatchdog → GMAN_ALERT_HEARTBEAT_URL, probe.sh, file_storage на узлах).
  Остается: история за время недоступности gateway не восполняется — pull-экспортер gateway
  отдает Prometheus только последнее значение ряда; нужна push-доставка (remote write или OTLP
  receiver Prometheus с out_of_order_time_window) с переносом `up`/relabel-логики правил.
  Самонаблюдение Collector узлов (otelcol_exporter_queue_size, enqueue_failed) не собирается.

Inventory и аудит:
- ~~`--watch` для Kubernetes (watch API вместо опроса раз в минуту), Kubernetes Events.~~ Сделано (п. 12).
- Kubernetes audit log для API caller; аудит или аутентифицированный посредник для
  атрибуции ручных запусков Docker.
- ~~Корреляция событий Bamboo с runtime (digest, время) и сущность релиза со статусами.~~ Сделано (п. 11).
- Нормализованные controllers/workloads и nodes; ~~явная отметка пробела в истории~~ (сделано, п. 12).
- ~~OOM в Docker с restart policy ловить по событию `oom` из `--watch`.~~ Сделано (п. 12).
- ~~Kubernetes Events узлов (OOMKilling от node-problem-detector, NodeNotReady).~~ Сделано (п. 18).
  Сопоставление OOM с pod-ом работает, только если в message есть путь cgroup; надежнее —
  агент kubernetes-node на узле читает /dev/kmsg (pid → cgroup), но это права на журнал ядра.
- ~~OOM в Docker без `--watch` (timer) виден, только пока контейнер остановлен.~~ Сделано (п. 18,
  `docker events --since/--until`); OOM удаленного (`--rm`) контейнера по таймеру не виден.

Метрики и алерты:
- ~~kube-state-metrics (desired/available replicas, Pending, rollout).~~ Сделано (п. 14).
- ~~Пороги температуры по моделям GPU; включить поля ECC и CLOCKS_EVENT_REASONS в CSV
  счетчиков DCGM Exporter.~~ Сделано (п. 14); сверить пороги A100 и добавить модели пилота.
- Подключить реальный канал уведомлений.
- ~~Ссылки Grafana с окружением и периодом карточки.~~ Сделано (п. 14).
- ~~Состояние workloads из kube-state-metrics в карточках портала (сейчас — алерты и Grafana);
  rollout StatefulSet с OnDelete и DaemonSet по updated/desired.~~ Сделано (п. 16); проверить
  шаблоны /api/workloads и новые метрики allowlist на настоящем кластере, раздел — в браузере.
- Partition StatefulSet и намеренно незавершенный OnDelete дают GManWorkloadRolloutStuck
  (kube-state-metrics не экспортирует updateStrategy): при ложных срабатываниях на пилоте —
  исключение по метке объекта.

Интеграции:
- Согласовать индексы и имена полей Splunk и задать их в GMAN_SPLUNK_*.
- Проверить переменные `bamboo_*` на используемой версии Bamboo: формат resultsUrl деплоя
  (deploymentResultId), наличие ManualBuildTriggerReason.userName и triggerReason.key в
  deployment job; совпадение окружения Bamboo с gman.environment.

Дальнейший PaaS — architecture.md «Дальнейший PaaS».
