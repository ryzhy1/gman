# GMan — основа внутреннего PaaS

Первая версия: каталог сервисов, конфигурация workloads, мониторинг серверов и GPU,
история наблюдений и аудит деплоев. Управление запуском — следующий этап.

Исходные условия: шесть серверов, Docker и Kubernetes, ручные запуски и Bamboo для CI/CD,
NVIDIA на серверах, Bitbucket/GitLab для исходного кода. Интерфейс — каталог и
карточки сервисов со ссылками в Grafana, Splunk и систему исходного кода.

Реализован локальный каркас пилота на FastAPI + PostgreSQL + React/TypeScript.
К производственным серверам он пока не подключен.

## Что работает в каркасе

- Каталог сервисов и карточки с владельцем, командой, репозиторием и внешними ссылками.
- Прием inventory от Docker-хостов и Kubernetes-кластера, раздельные токены источников.
- Агент сбора (разовый запуск или `--watch` по `docker events`): контейнеры/pods, image/digest, requests/limits, порты, mounts,
  сети и network/pid/ipc mode, user, capabilities, ulimits, log driver, restart policy,
  probes/healthcheck (без команд), GPU requests и NVIDIA_VISIBLE_DEVICES/CUDA_VISIBLE_DEVICES.
  Значения остальных env, commands и команды проверок не отправляются.
- Состояние отдельно от health: status, health, ready, причина (OOMKilled, CrashLoopBackOff),
  exit code, причина прошлого завершения; время создания, старта и завершения.
- Происхождение по allowlist меток: compose project/service/working dir, OCI image
  source/revision/version, app.kubernetes.io/*, helm chart, Kubernetes field managers.
  Это подсказки о способе запуска, а не доказательство автора.
- Ошибки частичного сбора (например, сбой nvidia-smi) передаются в снимке и видны в UI.
- Подробный снимок GPU на Docker-хосте. NVIDIA (nvidia-smi): модель, PCI, драйвер,
  pstate, загрузка GPU и памяти, VRAM, температуры GPU/памяти, мощность и лимит, частоты
  и максимум, вентилятор, причины троттлинга, ECC, PCIe gen/width, MIG, persistence.
  Если драйвер не знает расширенные поля, собирается базовый набор и пишется предупреждение.
  AMD (sysfs, для локальной проверки): датчики edge/junction/mem по меткам, частоты, вентилятор.
  Неподдерживаемые показатели передаются как отсутствующие, не как ноль.
- Какой контейнер занимает GPU: процессы nvidia-smi --query-compute-apps связываются с
  контейнерами по /proc/<pid>/cgroup, с объемом VRAM процесса. Это VRAM процесса, а не
  доля загрузки GPU. Агент должен работать в host PID namespace (как host process).
- Сведения о хосте: ОС, ядро, модель CPU, версия Docker, uptime.
- Узлы Kubernetes: готовность, pressure, cordon, роли, taints, версии kubelet/ОС/runtime,
  capacity/allocatable и сумма requests активных pod-ов по CPU/RAM/GPU, модель GPU из
  меток NVIDIA GPU feature discovery.
- История наблюдаемых изменений: появление/удаление экземпляра, изменение конфигурации,
  перезапуск, изменение состояния — с таблицей «было/стало». Первый снимок источника
  записывается одним событием source-connected. Автор runtime-изменения неизвестен.
- GET /api/events с постраничной выдачей (limit, before) и фильтром service_id.
- Endpoint событий Bamboo и скрипт отправки из deployment job, дедупликация событий.
- Интерфейс каталога, экземпляров, конфигураций, серверов/GPU и постраничного журнала.
  Отметка устаревания после трех минут, обновление интерфейса раз в 30 секунд.
- Ресурсы в портале: CPU (в ядрах) и память экземпляров против лимитов, нагрузка хостов
  (CPU, RAM, ФС, диск, сеть), GPU NVIDIA из DCGM (загрузка, VRAM, температуры, мощность,
  частоты, XID); вкладка «Ресурсы» с графиками за 1 час / 24 часа / 7 дней.
- OTel Collector на хосте → OTel Gateway → Prometheus → Grafana с готовыми дашбордами
  «GMan · Хост» и «GMan · Сервис»; кнопки Grafana в портале ведут на них по умолчанию.

## Локальный запуск через Docker

Требуются работающий Docker daemon и Docker Compose. Из корня проекта:

```sh
python3 deploy/init_env.py
docker compose --env-file .env -f deploy/compose.yaml up --build -d
```

Открыть http://127.0.0.1:8080. Войти с GMAN_READ_TOKEN из локального .env.
Prometheus: http://127.0.0.1:9090. Grafana: http://127.0.0.1:3000 (admin /
GRAFANA_ADMIN_PASSWORD из .env). Порты привязаны к loopback. Если .env создан
предыдущей версией, добавьте в него GRAFANA_ADMIN_PASSWORD вручную.
OTLP gateway принимает локально 4317/4318 без TLS: это конфигурация стенда.
Не открывать ее удаленным хостам без настройки аутентифицированного TLS ingress.

init_env.py не перезаписывает существующий .env. Начальные source_id:
server-01 и cluster-01; для остальных узлов добавьте отдельные токены в
GMAN_AGENT_TOKENS и пересоздайте API. Не используйте один токен для всех серверов.

## Регистрация сервиса

Скопируйте deploy/service.example.json, заполните реальные поля. Ссылки необязательны.
Загрузите запись через PUT /api/services/{id} с Bearer GMAN_ADMIN_TOKEN.
Пример для локальной оболочки (токен задается в окружении):

```sh
curl --fail-with-body -X PUT http://127.0.0.1:8080/api/services/example-api \
  -H "Authorization: Bearer $GMAN_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @deploy/service.example.json
```

Для Docker задайте labels gman.service=example-api и gman.environment=prod.
Для Kubernetes эти labels должны быть у Pod (в spec.template.metadata.labels
Deployment/StatefulSet), а не только у контроллера. Существующие контейнеры
переименовывать или пересоздавать ради пилота не обязательно: сначала они
будут видны в разделе непривязанных экземпляров.

### Сервисы, запущенные вручную

Агент обнаруживает контейнеры/pods независимо от Bamboo. Ручной docker run,
docker compose или kubectl не требует CI-события для появления экземпляра
в интерфейсе. Без gman.service он отображается как непривязанный; сведения
о команде, владельце и репозитории задаются в каталоге отдельно.

Для наблюдения ручных запусков достаточно inventory-агента: конфигурация и
доступное время старта приходят из runtime. Автор остается неизвестным,
если нет подтверждения отдельного аудита. Отсутствие события CI само по себе
не означает, что запуск ручной. Ссылка на Bamboo необязательна.
Атрибуция ручных действий через аудит сервера или аутентифицированный посредник
пока не реализована; детали — в разделе «Ручные запуски» архитектурного проекта.

## Подключение inventory

На Docker-хосте нужны Python 3 и Docker CLI с правом чтения runtime:

```sh
python3 agent/collect.py --runtime docker --source-id server-01
```

Без --send выводится нормализованный JSON, без отправки в сеть. Для доставки
задайте GMAN_URL и GMAN_AGENT_TOKEN этого source_id и добавьте --send.
Для удаленного адреса скрипт требует HTTPS. Не передавайте токены в аргументах CLI.
Настройте запуск раз в минуту через ваш планировщик. При ошибке полного сбора снимок
не отправляется, чтобы не отметить все контейнеры удаленными.

Надежность доставки (только с --send):

- Каждый снимок сначала пишется в spool на диске и отправляется оттуда по порядку, от
  старых к новым. Каталог состояния — --state-dir, иначе GMAN_AGENT_STATE_DIR, иначе
  /var/lib/gman-agent для root (~/.local/state/gman-agent для остальных); права 0700/0600.
  Spool ограничен: --spool-max-files (100) и --spool-max-bytes (50 MiB). При переполнении
  удаляются самые старые снимки, а в errors нового снимка пишется, сколько потеряно.
- Повторы с экспоненциальным backoff и jitter (--retries, по умолчанию 3) на сетевые
  ошибки, таймауты, 5xx и 429. Неотправленное остается в spool до следующего запуска,
  код выхода ненулевой. 401/403 не повторяются, снимки сохраняются. Снимок, отвергнутый
  как некорректный (например, 422), удаляется, чтобы не блокировать очередь.
- Повтор безопасен: API отвечает accepted=false на дубликат или более старый снимок
  и не затирает им новый; события получают время observed_at, а не время доставки.
- flock на <state-dir>/<source-id>.lock: если предыдущий запуск еще идет, новый
  пишет предупреждение и завершается с кодом 0.

Режим наблюдения для Docker-хоста вместо таймера:

```sh
python3 agent/collect.py --runtime docker --source-id server-01 --send --watch
```

Агент читает `docker events` и отправляет снимок через --debounce (2 с) тишины после
изменения контейнера, но не позже --max-delay (10 с) от первого события и не реже
--interval (60 с). exec-события healthcheck не учитываются. На start/die контейнер
сразу инспектируется (если он уже удален — состояние берется из атрибутов события).
Контейнер, исчезнувший до снимка (`docker run --rm`), отправляется промежуточным
снимком с последним состоянием (exit code, время завершения), за которым следует
обычный, — в истории появляются и его запуск, и удаление. После разрыва потока событий
выполняется полная сверка. Блокировка держится все время работы, поэтому таймер
одновременно с --watch просто пропускает запуски. Kubernetes пока только по опросу.

Для Kubernetes нужен kubectl с отдельным read-only kubeconfig:

```sh
python3 agent/collect.py --runtime kubernetes --source-id cluster-01 --send
```

Минимальный пример прав на pods и nodes (get/list) — deploy/kubernetes-rbac.yaml; применяется
администратором кластера после выбора namespace. Агент собирает pods всего
кластера, включая init containers. Он не читает Secrets, не выполняет exec,
но доступ к Pod spec сам по себе может открывать plain-text env в API Kubernetes;
агент не передает эти значения в GMan.

## Bamboo

В deployment job добавьте Script task `python3 agent/bamboo_event.py`. Обязательны
GMAN_URL, защищенный GMAN_CI_TOKEN, GMAN_SERVICE_ID и GMAN_DEPLOYMENT_STATUS
(started/succeeded/failed: отдельные tasks в начале и в final tasks).

Остальное по умолчанию берется из переменных, которые Bamboo экспортирует в script
tasks; GMAN_* переопределяет любое значение:

| Поле | GMAN_* | Переменная Bamboo |
| --- | --- | --- |
| environment | GMAN_ENVIRONMENT | bamboo_deploy_environment |
| build_id | GMAN_BUILD_ID | bamboo_buildResultKey |
| release | GMAN_RELEASE | bamboo_deploy_release |
| actor | GMAN_ACTOR | bamboo_ManualBuildTriggerReason_userName |
| commit | GMAN_COMMIT | bamboo_planRepository_revision |
| build_url | GMAN_BUILD_URL | bamboo_resultsUrl |

Наличие этих переменных зависит от версии Bamboo и типа запуска: проверьте их
задачей `env | grep ^bamboo_` перед подключением. Если пользователь Bamboo известен,
actor_kind = human, иначе unknown (GMAN_ACTOR_KIND переопределяет). Сведения об авторе —
утверждение доверенной CI-интеграции; они не подтверждаются запросом к Bamboo.

GMAN_EVENT_ID по умолчанию строится из build, release, environment и status, поэтому
повтор той же задачи дает тот же payload и не создает второе событие. GMAN_OCCURRED_AT
необязателен: без него используется время получения сервером. Задавайте его только
фиксированным значением, иначе повтор вернет 409.

Сбой отправки печатает предупреждение и не валит деплой. Чтобы сделать отправку
обязательной, задайте GMAN_REQUIRED=true.

## Телеметрия

deploy/otel-agent.example.yaml — Collector contrib на Docker-хосте как host process:
метрики хоста (CPU по состояниям, память, swap, load, ФС, диски, сеть, процессы),
контейнеров (docker_stats: CPU, память, сеть, block I/O, throttling, pids, рестарты)
и NVIDIA DCGM Exporter с 127.0.0.1:9400. Нужны OTEL_GATEWAY, GMAN_HOST_ID и клиентские
TLS-сертификаты. **GMAN_HOST_ID должен совпадать с --source-id inventory-агента**:
по нему портал связывает метрики с хостом, а по ID контейнера — с экземпляром.
Docker labels gman.service/gman.environment попадают в метрики как gman_service/
gman_environment, на них построен дашборд сервиса.

DCGM Exporter ставится на NVIDIA-хосты отдельно (контейнер nvcr.io/nvidia/k8s/dcgm-exporter
с --gpus all, порт 9400 только на 127.0.0.1); версию выбирайте под драйвер. Без него
GPU-метрики DCGM отсутствуют, остальное работает, а портал показывает GPU из nvidia-smi.

API читает Prometheus по PROMETHEUS_URL фиксированными шаблонами запросов; произвольный
PromQL от клиента не принимается. Нет метрик — портал показывает «—» и предупреждение,
а не нули. Gateway удаляет ряды молчащих источников через 90 секунд.

Проверено: на otelcol-contrib 0.162.0, Prometheus 3.5.0 и Grafana 12.1.1 (бинарные
релизы) с эмуляцией Docker Engine API и статическим выводом в формате DCGM: имена
метрик, метки, все запросы API и дашбордов, provisioning Grafana. На настоящих Docker,
NVIDIA и DCGM еще не проверено.

Kubernetes: deploy/otel-k8s.yaml (DaemonSet: host_metrics, kubelet_stats, k8s_attributes,
DCGM на узле); портал показывает CPU/RAM контейнеров и сеть pod-ов, есть дашборд
«GMan · Kubernetes». Имена метрик сверялись на эмуляторе kubelet; в реальном кластере,
включая k8s_attributes и обнаружение DCGM, не проверено.
Логи приложений и traces не включены; Splunk не разворачивается.

## Проверка

```sh
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q
cd frontend
npm ci
npm run build
```

Для разработки API: python -m uvicorn backend.app.main:app --port 8000;
без DATABASE_URL применяется SQLite только для локальной разработки/тестов.
Задайте токены, как в Compose. Frontend: npm run dev, /api проксируется на 8000.

Проверено: тесты API/агента/метрик (включая spool, backoff, flock и debounce `--watch`), TypeScript и production build,
синтаксис Docker Compose; чтение AMD sysfs; браузерная проверка портала на тестовых
данных с API на SQLite; bamboo_event.py против локального API. PostgreSQL,
Docker end-to-end, Kubernetes, NVIDIA/DCGM и настоящий Bamboo еще не проверены.

## Хранилище

PostgreSQL со схемой из Alembic-миграций (backend/migrations), применяются при старте API
под advisory lock, поэтому несколько реплик могут стартовать одновременно. Таблицы:
services, sources (последний снимок), instances (текущее состояние), config_snapshots
(история различающихся конфигураций экземпляра по хешу — основа для diff версий), events,
deployments. Новые поля снимков хранятся в JSON и не требуют миграций.
GMAN_RETENTION_DAYS (по умолчанию 180, 0 — отключить) удаляет события и неиспользуемые
снимки конфигурации старше срока; деплои не удаляются.

Переход с первой версии каркаса: старая таблица records не переносится. Агенты пришлют
снимки заново (по одному событию source-connected), сервисы каталога нужно загрузить
повторно через PUT /api/services. Курсор постраничной выдачи событий непрозрачный;
некорректный before возвращает 422.

## Границы пилота

Это основа для развития, не завершенный production PaaS. Еще нужны SSO/RBAC,
backup/restore, ограничение размера/частоты входящих запросов на ingress,
поток runtime-событий Kubernetes и Kubernetes audit, нормализованные controllers/nodes, requests/probes/security config,
проверка источников ссылок и тонкая редакция чувствительных строк конфигурации.
Текущая история — изменения между снимками: без --watch (и в Kubernetes) короткие
события между опросами могут быть пропущены; перезапуски по restart policy видны
по счетчику, но промежуточное состояние exited — нет. Полная инвентаризация и события выводятся в одном overview,
поэтому этот API предназначен для небольшого пилота.

Подробная целевая архитектура и критерии приемки: [docs/architecture.md](docs/architecture.md).
