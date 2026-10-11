# Состояние и следующие шаги

Обновлено 11 октября 2026 года. Документ для продолжения работы на другой машине.
Архитектура по фактической реализации — [architecture.md](architecture.md), инструкции — README.

## Что сделано

1. Каталог и inventory: нормализованная схема PostgreSQL (Alembic, миграции 0001–0004),
   прием снимков Docker-хостов и Kubernetes-кластера с токеном на источник, состояние
   отдельно от health, происхождение по allowlist меток, история изменений по снимкам,
   retention. Исправлены баги из ревью (race `docker ps`/`inspect`, нулевое время старта,
   повторы Bamboo, молчаливый сбой nvidia-smi, 500 на не-ASCII токене и др.).
2. Надежность агента (agent/collect.py, только stdlib): ограниченный spool на диске,
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
11. Runtime-история: `--watch` для Kubernetes (list + watch pods и pod Events от resourceVersion,
    BOOKMARK, relist на 410 Gone, backoff, промежуточный снимок для pod-ов, удаленных между
    снимками); Kubernetes Events сервисов каталога в истории без message (allowlist причин,
    дедупликация по uid + count/времени, миграция 0004 runtime_event_marks); Docker OOM под
    restart policy по событию `oom` → last_oom_at экземпляра, событие instance-oom, метрика
    gman_instance_last_oom_timestamp_seconds и третье условие GManInstanceOOMKilled; явная
    отметка пробела в истории (history-gap: молчание > GMAN_HISTORY_GAP_SECONDS или
    переполнение spool агента) с плашкой «Данные за период могут быть неполными» в портале.

Проверено:
- Тесты: 165 passed на PostgreSQL 17 (включая запросы истории конфигураций и тест
  конкурентности), 164 passed и 1 skipped на SQLite. Frontend: `npm run build`.
- Kubernetes watch и Events — на fixtures потоков (ADDED/MODIFIED/DELETED, BOOKMARK, ERROR 410,
  сбой kubectl, HTTP 410 in-cluster), Docker `oom` — на fixtures событий; правило OOM —
  `promtool test rules` (Prometheus 3.5.0).
- Метрики: otelcol-contrib 0.162.0, Prometheus 3.5.0, Grafana 12.1.1 (бинарные релизы) с
  эмуляцией Docker Engine API, kubelet и вывода DCGM: имена метрик, метки, запросы API
  и дашбордов, provisioning Grafana.
- Алерты: `promtool check rules`/`test rules`, `amtool check-config` (Alertmanager 0.28.1)
  с получателями и без; локальный прогон Prometheus + Alertmanager + API на SQLite —
  срабатывание, подавление и доставка webhook.
- Production частично, без Docker daemon и Linux: `docker compose config` для обоих файлов
  (Compose 2.29.7); issue.sh (shellcheck, `openssl verify -purpose`); gateway — доставка
  с клиентским сертификатом и отказ без него и с чужим CA; pg_backup.sh на PostgreSQL 17
  (ротация, `--check`, восстановление `pg_restore --clean`); доверие агента пилотному CA
  через SSL_CERT_FILE; nginx-конфиги разобраны crossplane.
- SSO — против mock IdP в тестах; bamboo_event.py — против локального API; чтение AMD sysfs.

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

Стенд: `python3 deploy/init_env.py`, затем
`docker compose --env-file .env -f deploy/compose.yaml up --build -d`
(портал :8080, Grafana :3000, Prometheus :9090, Alertmanager :9093; все на 127.0.0.1).
Если .env создан старой версией, добавьте недостающие переменные (GRAFANA_ADMIN_PASSWORD;
по желанию GMAN_OIDC_*, GMAN_SPLUNK_*, GMAN_ALERT_*).

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
  при restart policy и OOM kill дочернего процесса).
- nginx: `nginx -t` и запуск с TLS, Grafana за /grafana/ (включая Live/websocket),
  limit_req на реальной нагрузке.
- systemd: `systemd-analyze verify`, sandboxing с docker/nvidia-smi/kubectl, timer на Linux.
- Восстановление из бэкапа на реальном сервере.
- NVIDIA/DCGM: настоящие поля DCGM (XID, ECC, CLOCKS_EVENT_REASONS, метки
  pod/namespace/container), nvidia-smi на реальных драйверах, сверка показателей с источником.
- Kubernetes/CRI-O: k8s_attributes, обнаружение DCGM, RBAC, kubelet_stats в реальном
  кластере; `--watch` кластера (потоковый вывод `kubectl get --raw` для watch, частота
  BOOKMARK, 410 после compaction, нагрузка на API server), формат и series реальных Events; для kubernetes-node — nvidia-smi через NVIDIA runtime без запроса GPU, список
  процессов от non-root, PSA privileged для hostPID, формат cgroup на CRI-O/containerd.
- SSO с настоящим IdP (Keycloak, AD FS, Okta): claim групп, end_session, cookie `__Host-`.
- Интеграции: настоящий Bamboo (переменные `bamboo_*`), Splunk (индексы и поля),
  SMTP и webhook получателей алертов.
- Браузер: новые страницы и элементы портала — «Алерты», история конфигураций и diff,
  редактор каталога, вход через SSO и выход, процессы GPU в карточке кластера.
- Накладные расходы, cardinality и прирост диска на пилоте.

Первое, что стоит сделать на машине с Docker: `docker compose ... up --build`, пройти README
«Локальный запуск», затем «Production-развертывание» на тестовом сервере, запустить агент
на хосте с NVIDIA.

## Дальше по плану

Безопасность и эксплуатация:
- Привязать клиентский сертификат OTLP к host.id (CN/SAN против GMAN_HOST_ID на gateway);
  сейчас любой сертификат пилотного CA отправляет метрики за любой узел.
- Отзыв сертификатов (CRL) вместо перевыпуска CA.
- Отдельный Secret или projected token на каждый GPU-узел в deploy/gpu-attribution-k8s.yaml:
  сейчас токены всех узлов смонтированы в каждый pod (узел может слать снимки GPU за другой
  GPU-узел; API лишь требует source_id = `<cluster>.<hostname>`). Обход сейчас — systemd на узле.
- Выгрузка бэкапов за пределы сервера; регулярная проверка восстановления.
- Внешний мониторинг самого сервера GMan; постоянная очередь Collector на диске (file_storage).

Inventory и аудит:
- ~~`--watch` для Kubernetes (watch API вместо опроса раз в минуту), Kubernetes Events.~~ Сделано (п. 11).
- Kubernetes audit log для API caller; аудит или аутентифицированный посредник для
  атрибуции ручных запусков Docker.
- Корреляция событий Bamboo с runtime (digest, время) и сущность релиза со статусами.
- Нормализованные controllers/workloads и nodes; ~~явная отметка пробела в истории~~ (сделано, п. 11).
- ~~OOM в Docker с restart policy ловить по событию `oom` из `--watch`.~~ Сделано (п. 11).
- Kubernetes Events узлов (OOMKilling от node-problem-detector, NodeNotReady) — сейчас только
  события pod-ов; сопоставление OOM узла с pod-ом требует разбора message.
- OOM в Docker без `--watch` (timer) виден, только пока контейнер остановлен.

Метрики и алерты:
- kube-state-metrics (desired/available replicas, Pending, rollout).
- Пороги температуры по моделям GPU; включить поля ECC и CLOCKS_EVENT_REASONS в CSV
  счетчиков DCGM Exporter.
- Подключить реальный канал уведомлений.
- Ссылки Grafana с окружением и периодом карточки.

Интеграции:
- Согласовать индексы и имена полей Splunk и задать их в GMAN_SPLUNK_*.
- Проверить переменные `bamboo_*` на используемой версии Bamboo.

Дальнейший PaaS — architecture.md «Дальнейший PaaS».
