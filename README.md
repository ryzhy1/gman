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
- Какой pod занимает GPU в Kubernetes: агент на GPU-узле (режим kubernetes-node) разбирает
  /proc/<pid>/cgroup (cgroup v1 и v2, драйверы systemd и cgroupfs, containerd, CRI-O,
  cri-dockerd, QoS besteffort/burstable/guaranteed, UID pod-а с `_` или `-`, static pod) и
  сопоставляет ID контейнера с containerStatuses pod-ов узла. Процесс показывается в карточке
  узла и в экземплярах сервиса (VRAM, вкладка «Конфигурация»), как для Docker.
- Сведения о хосте: ОС, ядро, модель CPU, версия Docker, uptime.
- Узлы Kubernetes: готовность, pressure, cordon, роли, taints, версии kubelet/ОС/runtime,
  capacity/allocatable и сумма requests активных pod-ов по CPU/RAM/GPU, модель GPU из
  меток NVIDIA GPU feature discovery.
- История наблюдаемых изменений: появление/удаление экземпляра, изменение конфигурации,
  перезапуск, изменение состояния — с таблицей «было/стало». Первый снимок источника
  записывается одним событием source-connected. Автор runtime-изменения неизвестен.
- GET /api/events с постраничной выдачей (limit, before) и фильтром service_id.
- История конфигураций: версии сервиса (GET /api/services/{id}/configs) и экземпляра
  (GET /api/instances/{source_id}/{instance_id}/configs), новые сверху, постранично
  (limit, before); версия целиком — GET /api/configs/{id}; структурный diff —
  GET /api/configs/diff?from=&to= (пути полей, added/removed/changed; элементы списков
  портов, mounts, capabilities — как «поле[]»). Вкладка «Конфигурация» в карточке
  сервиса: текущая конфигурация, список версий с фильтром по экземпляру, выбор
  «Было/Стало» и таблица различий. Секретов в конфигурациях нет (env и команды не
  собираются и отклоняются схемой), поэтому значения не маскируются.
- Endpoint событий Bamboo и скрипт отправки из deployment job, дедупликация событий.
- Интерфейс каталога, экземпляров, конфигураций, серверов/GPU и постраничного журнала.
  Отметка устаревания после трех минут, обновление интерфейса раз в 30 секунд.
- Ресурсы в портале: CPU (в ядрах) и память экземпляров против лимитов, нагрузка хостов
  (CPU, RAM, ФС, диск, сеть), GPU NVIDIA из DCGM (загрузка, VRAM, температуры, мощность,
  частоты, XID); вкладка «Ресурсы» с графиками за 1 час / 24 часа / 7 дней.
- OTel Collector на хосте → OTel Gateway → Prometheus → Grafana с готовыми дашбордами
  «GMan · Хост» и «GMan · Сервис»; кнопки Grafana в портале ведут на них по умолчанию.
- Ссылки «Логи в Splunk» на карточке сервиса и у каждого экземпляра: поиск по service/
  environment/хосту/контейнеру за выбранный период; индексы и шаблон поиска задаются
  конфигурацией (см. «Splunk»).
- Вход через корпоративный SSO (OIDC) с ролями «Просмотр», «Владелец сервиса» и
  «Администратор платформы»; владельцы правят только свои записи каталога, изменения
  каталога пишутся в журнал с автором. Без настройки SSO работает вход по токену.

## Локальный запуск через Docker

Требуются работающий Docker daemon и Docker Compose. Из корня проекта:

```sh
python3 deploy/init_env.py
docker compose --env-file .env -f deploy/compose.yaml up --build -d
```

Открыть http://127.0.0.1:8080. Без SSO войти с GMAN_READ_TOKEN (просмотр) или
GMAN_ADMIN_TOKEN (администратор) из локального .env; SSO — раздел «Вход и роли».
Prometheus: http://127.0.0.1:9090. Grafana: http://127.0.0.1:3000 (admin /
GRAFANA_ADMIN_PASSWORD из .env). Порты привязаны к loopback. Если .env создан
предыдущей версией, добавьте в него GRAFANA_ADMIN_PASSWORD вручную.
OTLP gateway принимает локально 4317/4318 без TLS: это конфигурация стенда.
Для удаленных серверов используйте production-override (раздел ниже).

init_env.py не перезаписывает существующий .env. Начальные source_id:
server-01 и cluster-01; для остальных узлов добавьте отдельные токены в
GMAN_AGENT_TOKENS и пересоздайте API. Не используйте один токен для всех серверов.

## Production-развертывание

Один сервер GMan за nginx с TLS; удаленные узлы отправляют inventory по HTTPS и метрики
по OTLP с mTLS. Нужен Docker Compose не ниже 2.24.4 (теги `!override`/`!reset`;
в 2.24.3 `!override` молча игнорируется, и порты стенда не снимаются).

| Файл | Назначение |
| --- | --- |
| deploy/compose.prod.yaml | override: публикация 80/443 и 4317, сертификаты, бэкап, ротация логов |
| deploy/nginx/gman.conf | HTTPS портала/API/Grafana, limit_req, client_max_body_size |
| deploy/otel-gateway.prod.yaml | OTLP gRPC/HTTP только с TLS и проверкой клиента (client_ca_file) |
| deploy/pki/issue.sh | пилотный CA, серверные и клиентские сертификаты (openssl) |
| deploy/backup/pg_backup.sh | pg_dump с проверкой архива и ротацией |
| deploy/systemd/ | юниты агента сбора (timer раз в минуту) и otelcol-contrib на узлах |

### Сертификаты

На машине администратора (ключ CA на сервер не копируется):

```sh
deploy/pki/issue.sh ca
deploy/pki/issue.sh server portal  DNS:gman.example.org
deploy/pki/issue.sh server gateway DNS:gman.example.org
deploy/pki/issue.sh client server-01        # по одному на каждый узел
```

Файлы попадают в deploy/pki/out (исключен из репозитория, переопределяется PKI_DIR).
Существующие файлы не перезаписываются; для перевыпуска удалите старые явно.
Конечные сертификаты действуют 397 дней (CERT_DAYS), CA — 5 лет (CA_DAYS).
Если есть корпоративный CA, используйте его сертификаты с теми же именами файлов.

На сервер GMan в каталог GMAN_TLS_DIR (по умолчанию deploy/pki/out) кладутся только
portal.pem, portal-key.pem, gateway.pem, gateway-key.pem и ca.pem. Collector в контейнере
работает под uid 10001, поэтому: `sudo chown 10001 gateway-key.pem` (права 0600).

### Запуск

В .env добавьте `GMAN_PUBLIC_HOST=gman.example.org` (имя из сертификата портала).
Необязательные: GMAN_BIND_ADDRESS и GMAN_OTLP_BIND_ADDRESS (по умолчанию 0.0.0.0),
GMAN_TLS_DIR, GMAN_BACKUP_DIR (deploy/backups), GMAN_BACKUP_KEEP (14),
GMAN_BACKUP_INTERVAL_SECONDS (86400).

```sh
docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.prod.yaml up --build -d
docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.prod.yaml ps
```

Снаружи доступны: 443 (портал, /api, Grafana на /grafana/), 80 (только редирект на HTTPS)
и 4317 (OTLP gRPC с mTLS). Prometheus остается на 127.0.0.1:9090, PostgreSQL и Grafana
не публикуются. Ссылки Grafana в портале ведут на https://GMAN_PUBLIC_HOST/grafana.
Ограничьте 443 и 4317 межсетевым экраном подсетями серверов и пользователей.

nginx: HSTS, TLS 1.2/1.3; тело запроса до 1 МБ, для POST /api/inventory — до 20 МБ;
limit_req по IP: /api 20 r/s (burst 40), /api/inventory 2 r/s (burst 10), Grafana
50 r/s (burst 100), не более 64 соединений с IP; превышение — 429. Если агенты выходят
через общий NAT, увеличьте лимит зоны gman_ingest.

Gateway принимает только клиентов с сертификатом пилотного CA. Принадлежность
сертификата хосту (CN против GMAN_HOST_ID) не проверяется: любой выпущенный клиентский
сертификат может отправлять метрики за любой host.id. Отзыв — перевыпуск CA или
отдельный CA на группу узлов; CRL пилот не поддерживает. Gateway перечитывает
сертификаты раз в час без перезапуска; nginx — после `docker compose ... restart portal`.

Healthcheck есть у db, api, portal, prometheus, grafana и backup (свежий бэкап моложе двух
интервалов). Образ otelcol distroless, проверки внутри контейнера нет: следите за
up{job="otel"} в Prometheus.

### Узлы

На каждом Docker-хосте (Linux, systemd):

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin gman-agent
sudo useradd --system --no-create-home --shell /usr/sbin/nologin gman-otel
sudo install -d -m 0755 /opt/gman/agent /etc/gman
sudo install -m 0644 agent/collect.py /opt/gman/agent/
sudo install -m 0600 deploy/systemd/agent.env.example /etc/gman/agent.env     # заполнить
sudo install -m 0644 deploy/systemd/otelcol.env.example /etc/gman/otelcol.env # заполнить
sudo install -m 0644 deploy/otel-agent.example.yaml /etc/gman/otel-agent.yaml
sudo install -m 0644 ca.pem /etc/gman/ca.pem
sudo install -m 0644 client-server-01.pem /etc/gman/client.pem
sudo install -m 0640 -g gman-otel client-server-01-key.pem /etc/gman/client-key.pem
sudo install -m 0644 deploy/systemd/gman-*.service deploy/systemd/gman-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gman-collect.timer gman-otelcol.service
journalctl -u gman-collect.service -n 20
```

otelcol-contrib ставится из релизного пакета той же версии, что и gateway (0.162.0);
его собственный otelcol-contrib.service отключите. Оба юнита получают доступ к Docker
socket через группу docker — это эквивалент root на хосте. На NVIDIA-хостах не включайте
PrivateDevices: nvidia-smi нужен /dev/nvidia*.

Агент доверяет пилотному CA через SSL_CERT_FILE в agent.env (Python с OpenSSL на Linux);
либо добавьте ca.pem в системное хранилище (update-ca-certificates) и уберите строку.
GMAN_SOURCE_ID в agent.env и GMAN_HOST_ID в otelcol.env должны совпадать.
Timer запускает сбор раз в минуту со случайной задержкой до 15 с; oneshot-юнит не
стартует повторно, пока идет предыдущий запуск, а зависший запуск снимается через 55 с.
Для Kubernetes-агента: GMAN_RUNTIME=kubernetes и KUBECONFIG в agent.env (файл читаем
для gman-agent), уберите SupplementaryGroups=docker из юнита.

### Резервное копирование и восстановление

Сервис backup выполняет `pg_dump --format=custom` сразу при старте и затем раз в сутки
в GMAN_BACKUP_DIR на хосте (по умолчанию deploy/backups; файлы
gman-YYYYMMDDTHHMMSSZ.dump, права 0600). Архив проверяется `pg_restore --list`, и только
после этого удаляются файлы сверх GMAN_BACKUP_KEEP; неудачный дамп ничего не ротирует.
Копируйте каталог за пределы сервера: бэкап на том же диске не защищает от его потери.
Внеочередной бэкап, например перед обновлением:

```sh
C="docker compose --env-file .env -f deploy/compose.yaml -f deploy/compose.prod.yaml"
$C run --rm backup --once
```

Восстановление (схема и данные заменяются целиком; версия Alembic входит в дамп):

```sh
$C stop portal api
$C run --rm --entrypoint pg_restore backup \
  --clean --if-exists --no-owner --exit-on-error -d gman /backups/gman-YYYYMMDDTHHMMSSZ.dump
$C start api portal
```

Проверка дампа без изменения рабочей базы — восстановление в отдельную:

```sh
$C exec db createdb -U gman gman_restore_check
$C run --rm --entrypoint pg_restore backup --no-owner -d gman_restore_check /backups/<файл>
$C exec db psql -U gman -d gman_restore_check -c 'select count(*) from services'
$C exec db dropdb -U gman gman_restore_check
```

Prometheus и Grafana не бэкапятся: метрики хранятся 30 дней, дашборды и datasource
описаны в deploy/grafana. Сертификаты и .env храните отдельно, в бэкап они не входят.

## Вход и роли

Люди входят через корпоративный SSO (OIDC Authorization Code + PKCE). Обмен кода,
проверка ID token (подпись по JWKS, issuer, audience, nonce, срок) и сессия — на стороне
API (конфиденциальный клиент). Браузер получает только HttpOnly cookie сессии
(SameSite=Lax, Secure и префикс `__Host-` при HTTPS); в базе хранится лишь хеш ее значения.
Изменяющие запросы из портала передают CSRF-токен сессии в заголовке X-CSRF-Token.
«Выйти» удаляет сессию на сервере.

Роль берется из групп IdP при входе и действует до конца сессии (по умолчанию 8 часов):

| Роль | Права | Группы |
| --- | --- | --- |
| Просмотр (viewer) | только чтение | GMAN_OIDC_VIEWER_GROUPS (`*` — любой вошедший) |
| Владелец сервиса (owner) | чтение; изменение записей каталога, где он в `owners` | GMAN_OIDC_OWNER_GROUPS |
| Администратор платформы (admin) | все, включая новые сервисы и список `owners` | GMAN_OIDC_ADMIN_GROUPS |

Группы перечисляются через запятую, при нескольких совпадениях действует старшая роль.
Пользователь без подходящей группы не входит. Владение задается полем `owners` записи
каталога: email, логин (preferred_username), subject или `group:<группа IdP>`, без учета
регистра. Владелец не может создать сервис и изменить `owners` — это делает администратор.
Поле `owner` («Ответственный») остается описанием и прав не дает.

Настройка (переменные API, в compose они берутся из .env):

| Переменная | Назначение |
| --- | --- |
| GMAN_OIDC_ISSUER | issuer IdP; пустое значение — режим только токенов |
| GMAN_OIDC_CLIENT_ID, GMAN_OIDC_CLIENT_SECRET | конфиденциальный клиент в IdP |
| GMAN_OIDC_REDIRECT_URI | `https://<портал>/api/auth/callback`, зарегистрировать в IdP |
| GMAN_OIDC_POST_LOGOUT_URI | необязательно: `https://<портал>/?logged_out=1` для выхода и из IdP |
| GMAN_OIDC_SCOPES | по умолчанию `openid profile email` |
| GMAN_OIDC_GROUPS_CLAIM | claim с группами, по умолчанию `groups`; путь через точку, например `realm_access.roles` |
| GMAN_OIDC_ADMIN_GROUPS, GMAN_OIDC_OWNER_GROUPS, GMAN_OIDC_VIEWER_GROUPS | группы ролей |
| GMAN_SESSION_HOURS | срок сессии, по умолчанию 8 |

Если групп нет в ID token, они читаются из userinfo. Частичная настройка OIDC
останавливает запуск API с перечнем недостающих переменных. Endpoints: GET
/api/auth/login, GET /api/auth/callback, POST /api/auth/logout, GET /api/me.

Bearer-токены остаются для автоматизации и режима без SSO: GMAN_READ_TOKEN дает чтение,
GMAN_ADMIN_TOKEN — права администратора; пустое значение отключает токен. Изменения
через admin-токен пишутся в журнал с автором GMAN_ADMIN_TOKEN (человек неизвестен).
GMAN_CI_TOKEN и GMAN_AGENT_TOKENS не изменились. Без GMAN_OIDC_ISSUER портал
показывает форму токена, как раньше.

## Регистрация сервиса

Скопируйте deploy/service.example.json, заполните реальные поля. Ссылки необязательны.
Раздел logs.splunk (index, query) тоже необязателен — см. «Splunk»; пустой можно удалить.
Загрузите запись через PUT /api/services/{id} с Bearer GMAN_ADMIN_TOKEN или войдите в
портал администратором и задайте `owners`; дальше владельцы правят запись в карточке
сервиса («Изменить запись каталога»). Создание и изменения записи видны в журнале событий.
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
Запуск раз в минуту без пересечений — systemd timer из deploy/systemd (раздел
«Production-развертывание»).
При ошибке полного сбора снимок не отправляется, чтобы не отметить все контейнеры удаленными.

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

### Процессы GPU в Kubernetes

Агент кластера работает через kubectl, часто вне узлов, и не видит PID узла. Поэтому
на каждом GPU-узле запускается отдельный экземпляр агента в режиме kubernetes-node:

```sh
python3 agent/collect.py --runtime kubernetes-node --cluster cluster-01 --node gpu-1 \
  --source-id cluster-01.gpu-1 --send
```

Он читает nvidia-smi и /proc/<pid>/cgroup узла (нужен host PID namespace: nvidia-smi
сообщает PID своего namespace) и список pod-ов этого узла (`kubectl get pods
--field-selector spec.nodeName=...`; без kubectl внутри pod — Kubernetes API с service
account). Процесс получает namespace, pod, контейнер и ID экземпляра `<pod uid>:<контейнер>`.
Если контейнера нет в containerStatuses, процесс привязывается только к pod по UID из cgroup;
если pod не найден — остаются ID контейнера и UID pod-а. Сбой получения pod-ов попадает в
ошибки снимка, данные GPU все равно отправляются.

Узел — отдельный источник `<кластер>.<узел>` (source_type kubernetes-node) со своим токеном
в GMAN_AGENT_TOKENS. Экземпляров в нем нет: pod-ы по-прежнему сообщает источник кластера,
поэтому история не дублируется. Портал показывает GPU узла внутри карточки кластера.

Варианты запуска: DaemonSet из deploy/gpu-attribution-k8s.yaml (hostPID, без
capabilities, nvidia-smi от NVIDIA container runtime без запроса nvidia.com/gpu, токены в
Secret с ключами по именам узлов через GMAN_AGENT_TOKEN_FILE, RBAC — только list pods)
или systemd timer на узле с read-only kubeconfig. GMAN_PROC_ROOT задает другой путь к
/proc хоста, если он смонтирован отдельно.

Загрузку GPU по pod-ам дают метки namespace/pod/container DCGM Exporter в режиме
Kubernetes (по умолчанию в GPU Operator): вкладка «Ресурсы» показывает загрузку и VRAM
выделенных контейнеру карт для pod-ов с GPU requests. Это значения всей карты: при
time-slicing/MPS они общие для pod-ов на ней.

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
включая k8s_attributes и обнаружение DCGM, не проверено. Метки pod/namespace/container
DCGM передаются без изменений и используются для GPU по pod-ам (см. «Процессы GPU в Kubernetes»).
Логи приложений и traces не включены; Splunk не разворачивается.

## Splunk

Портал не читает логи, а строит ссылки на поиск в существующем Splunk. Индексы еще не
согласованы, поэтому все задается окружением API (в compose пробрасываются из .env):

| Переменная | Назначение |
| --- | --- |
| GMAN_SPLUNK_URL | Страница поиска для браузера, например `https://splunk.example.com/en-US/app/search/search`. Пусто — ссылки скрыты. |
| GMAN_SPLUNK_INDEXES | JSON «окружение → индекс», ключ `*` — индекс по умолчанию: `{"prod":"app_prod","*":"app_nonprod"}`. |
| GMAN_SPLUNK_SEARCH | Шаблон SPL. По умолчанию `search index={index} gman_service={service_id} gman_environment={environment} host={host} container_id={container_id}`. |

Плейсхолдеры: {index}, {service_id}, {environment}, {instance} (имя экземпляра),
{container_id} (Docker), {host} (хост Docker или узел Kubernetes), {namespace} и {pod}
(Kubernetes), {earliest}, {latest}. Каждое значение подставляется строкой SPL в кавычках
с экранированием `\` и `"` (управляющие символы заменяются пробелом), поэтому значение
из inventory или каталога не может закрыть кавычку и добавить команду. Слово шаблона
(часть между пробелами), в котором плейсхолдер пуст, удаляется целиком: один шаблон
работает и для сервиса (без host/container_id), и для экземпляра, и для режима «Все
окружения» (без environment; индекс — из `*` или условие индекса пропадает). Скобки и OR
вокруг такого слова лучше не ставить. Неизвестный плейсхолдер — ошибка конфигурации.

Период передается параметрами URL `earliest` (`-1h`, `-24h`, `-7d` — как на вкладке
«Ресурсы», по умолчанию 1 час) и `latest=now`; параметры, уже заданные в
GMAN_SPLUNK_URL (кроме q/earliest/latest), сохраняются. Пример ссылки:
`https://splunk.example.com/en-US/app/search/search?q=search+index%3D%22app_prod%22+gman_service%3D%22api%22+gman_environment%3D%22prod%22&earliest=-1h&latest=now`.

Переопределение для сервиса в каталоге:
`"logs": {"splunk": {"index": "api_prod", "query": "search index={index} app={service_id}"}}`.
index — строка или объект «окружение → индекс» (с `*`; ненайденное берется из общей
настройки), query заменяет GMAN_SPLUNK_SEARCH. Имена индексов проверяются
(`[A-Za-z0-9_*.:-]`), шаблон — на известные плейсхолдеры; хранится в JSON-поле extra
таблицы services, миграция не нужна. Явная ссылка `splunk` в каталоге имеет приоритет
для кнопки на карточке.

Ссылки строит API: GET /api/logs/splunk?service_id=…&environment=…&range=1h|24h|7d
(токен просмотра) возвращает ссылку сервиса и ссылки экземпляров по ключу
`<source_id>/<instance_id>`. Ошибка конфигурации пишется в лог API, ссылки скрываются.
Ссылки открываются в новой вкладке с rel=noopener. Имена полей Splunk (gman_service,
container_id и т. д.) — предположение: согласуйте их вместе с индексами.

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

Проверено: тесты API/агента/метрик/истории конфигураций/ссылок Splunk/SSO (включая spool, backoff, flock и debounce `--watch`; SSO — против mock IdP), TypeScript и production build,
синтаксис Docker Compose; чтение AMD sysfs; браузерная проверка портала на тестовых
данных с API на SQLite; bamboo_event.py против локального API. PostgreSQL,
Docker end-to-end, Kubernetes, NVIDIA/DCGM и настоящий Bamboo еще не проверены.

## Хранилище

PostgreSQL со схемой из Alembic-миграций (backend/migrations), применяются при старте API
под advisory lock, поэтому несколько реплик могут стартовать одновременно. Таблицы:
services, sources (последний снимок), instances (текущее состояние), config_snapshots
(история различающихся конфигураций экземпляра по хешу с именем и окружением экземпляра —
основа для версий и diff; возврат к прежней конфигурации — новая версия), events,
deployments, sessions (SSO-сессии, только хеш cookie). Новые поля снимков и каталога
(например, owners, logs) хранятся в JSON и не требуют миграций.
GMAN_RETENTION_DAYS (по умолчанию 180, 0 — отключить) удаляет события и неиспользуемые
снимки конфигурации старше срока; деплои не удаляются.

Переход с первой версии каркаса: старая таблица records не переносится. Агенты пришлют
снимки заново (по одному событию source-connected), сервисы каталога нужно загрузить
повторно через PUT /api/services. Курсор постраничной выдачи событий непрозрачный;
некорректный before возвращает 422.

## Границы пилота

Это основа для развития, не завершенный production PaaS. Еще нужны проверка SSO с настоящим IdP,
проверка production-стека и восстановления из бэкапа на реальном сервере, привязка
клиентских сертификатов OTLP к host.id, поток runtime-событий Kubernetes и
Kubernetes audit, нормализованные controllers/nodes, requests/probes/security config,
проверка источников ссылок и тонкая редакция чувствительных строк конфигурации.
Текущая история — изменения между снимками: без --watch (и в Kubernetes) короткие
события между опросами могут быть пропущены; перезапуски по restart policy видны
по счетчику, но промежуточное состояние exited — нет. Полная инвентаризация и события выводятся в одном overview,
поэтому этот API предназначен для небольшого пилота.

Подробная целевая архитектура и критерии приемки: [docs/architecture.md](docs/architecture.md).
