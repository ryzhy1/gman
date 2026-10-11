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
- Релизы: событие Bamboo сверяется с runtime по image digest или версии/тегу и окну
  времени. Статусы started → rolling-out → succeeded / failed (Bamboo failed, тайм-аут,
  crash loop/OOM новых экземпляров) / superseded. Runtime-изменения релиза получают
  инициатора из Bamboo; без совпадения — «ручной запуск / инициатор неизвестен».
  Вкладка «Релизы» в карточке сервиса и ссылка на релиз из истории.
- Интерфейс каталога, экземпляров, конфигураций, серверов/GPU и постраничного журнала.
  Отметка устаревания через три минуты после приема последнего снимка (received_at по часам
  сервера, а не часам агента или браузера), обновление интерфейса раз в 30 секунд.
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
- Алерты: Prometheus rules + Alertmanager (молчащий узел/источник, GPU XID/ECC, перегрев,
  троттлинг, OOMKilled, CrashLoopBackOff, частые перезапуски), подавление дочерних алертов
  упавшего узла, webhook/email из .env, страница «Алерты» с runbook в портале.

## Локальный запуск через Docker

Требуются работающий Docker daemon и Docker Compose. Из корня проекта:

```sh
python3 deploy/init_env.py
docker compose --env-file .env -f deploy/compose.yaml up --build -d
```

Открыть http://127.0.0.1:8080. Без SSO войти с GMAN_READ_TOKEN (просмотр) или
GMAN_ADMIN_TOKEN (администратор) из локального .env; SSO — раздел «Вход и роли».
Prometheus: http://127.0.0.1:9090. Alertmanager: http://127.0.0.1:9093.
Grafana: http://127.0.0.1:3000 (admin /
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
и 4317 (OTLP gRPC с mTLS). Prometheus и Alertmanager остаются на 127.0.0.1:9090/9093
(у Alertmanager нет аутентификации, поэтому nginx его не проксирует: ссылки «в Alertmanager»
из уведомлений ведут на GMAN_ALERTMANAGER_URL, по умолчанию 127.0.0.1:9093 — через SSH-туннель),
PostgreSQL и Grafana не публикуются. Получатели алертов — GMAN_ALERT_* (раздел «Алерты»),
runbook-ссылки в уведомлениях ведут на https://GMAN_PUBLIC_HOST. Ссылки Grafana в портале ведут на https://GMAN_PUBLIC_HOST/grafana.
GMAN_OIDC_REDIRECT_URI по умолчанию — https://GMAN_PUBLIC_HOST/api/auth/callback (его и регистрируйте в IdP).
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
пока не реализована; детали — docs/architecture.md, «Ручные запуски».

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
API принимает от такого источника только снимок с source_id = `<cluster>.<hostname>`
(иначе 422): токен узла не может описать другой узел или кластер. В /metrics GPU узла
помечаются host_id = имя узла, как в DCGM и gman_k8s_node_ready.

DaemonSet монтирует в каждый pod Secret с токенами всех GPU-узлов (выбрать Secret по
имени узла DaemonSet не умеет): тот, кто управляет одним GPU-узлом, может присылать
снимки GPU за другие GPU-узлы списка. Источники кластера и Docker-хостов это не
затрагивает. Если такой риск неприемлем — systemd timer на каждом узле со своим токеном.
Spool DaemonSet-а на emptyDir намеренно: у источника узла хранится только последний снимок.

Варианты запуска: DaemonSet из deploy/gpu-attribution-k8s.yaml (hostPID, без
capabilities, nvidia-smi от NVIDIA container runtime без запроса nvidia.com/gpu, токены в
Secret с ключами по именам узлов через GMAN_AGENT_TOKEN_FILE, RBAC — только list pods)
или systemd timer на узле с read-only kubeconfig (в /etc/gman/agent.env: GMAN_RUNTIME=kubernetes-node,
NODE_NAME и GMAN_CLUSTER — значения по умолчанию для --node и --cluster). GMAN_PROC_ROOT задает другой путь к
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
| build_id | GMAN_BUILD_ID | bamboo_buildResultKey (сборка, из которой создан релиз) |
| release | GMAN_RELEASE | bamboo_deploy_release, затем bamboo_deploy_version |
| build_url | GMAN_BUILD_URL | bamboo_resultsUrl |
| deployment_id | GMAN_DEPLOYMENT_ID | deploymentResultId из bamboo_resultsUrl |
| actor | GMAN_ACTOR | bamboo_ManualBuildTriggerReason_userName при ручном запуске |
| actor_kind | GMAN_ACTOR_KIND | по bamboo_triggerReason_key |
| commit | GMAN_COMMIT | bamboo_planRepository_revision, затем bamboo_planRepository_1_revision |
| version | GMAN_VERSION | — (по умолчанию сервер берет release) |
| image | GMAN_IMAGE | — |
| image_digest | GMAN_IMAGE_DIGEST | — |

Сверено с [документацией Bamboo по переменным](https://confluence.atlassian.com/bamboo/bamboo-variables-289277087.html):
в script task точки заменяются на подчеркивания; для deployment документированы
deploy.environment, deploy.project, deploy.release / deploy.version (синонимы),
deploy.release.previous, deploy.rollback, resultsUrl и triggerReason.key; buildResultKey,
planKey, buildNumber — переменные релиза (сборка, из которой он создан);
planRepository.&lt;position&gt;.revision без позиции означает первый репозиторий.
ManualBuildTriggerReason.userName документирован только для сборок. Поэтому:

- имя пользователя берется, только если triggerReason.key ручной или отсутствует;
  при автоматическом триггере (например, после успешной сборки) это имя автора сборки,
  а не деплоя, и инициатор остается unknown; Scheduled → actor_kind = schedule;
- buildResultKey одинаков для всех деплоев одного релиза, поэтому прежний event_id
  (build, release, environment, status) склеивал повторный деплой того же релиза с первым.
  Теперь event_id = bamboo:bamboo-deploy-&lt;deploymentResultId&gt;:&lt;status&gt;, если
  bamboo_resultsUrl содержит deploymentResultId (формат URL результата деплоя не
  документирован как контракт — при его отсутствии используется прежняя схема).

Проверьте переменные задачей `env | grep ^bamboo_` перед подключением. Сведения об
авторе — утверждение доверенной CI-интеграции; они не подтверждаются запросом к Bamboo.

Для сверки релиза с runtime передайте GMAN_IMAGE_DIGEST (после push: digest из
`docker buildx imagetools inspect` для Kubernetes; для Docker-хостов — ID образа
`docker image inspect --format '{{.Id}}'`, так как агент видит ID, а не manifest digest)
или GMAN_VERSION, совпадающий с тегом образа либо меткой
org.opencontainers.image.version / app.kubernetes.io/version. Окружение Bamboo должно
совпадать с меткой gman.environment экземпляров (иначе задайте GMAN_ENVIRONMENT).
Параметры: GMAN_RELEASE_TIMEOUT_MINUTES (30), GMAN_RELEASE_LEAD_MINUTES (10),
GMAN_RELEASE_CRASH_RESTARTS (3). Правила статусов — в docs/architecture.md, «Релизы».
Новые поля payload (deployment_id, version, image) требуют обновленного API:
старый сервер ответит 422; скрипт и API обновляйте вместе.

API: GET /api/releases?service_id=&environment=&status=&limit=&before= (новые сначала,
курсор `next`), GET /api/releases/{id} — релиз, связанные события и текущие экземпляры
раскатки. Нужен доступ просмотра (роль viewer по SSO или GMAN_READ_TOKEN). Ответ
POST /api/deployments содержит release_id. /metrics API отдает gman_release_status
(1 для статуса последнего релиза каждой пары сервис/окружение) и
gman_release_started_timestamp_seconds — для правила алерта на failed (правило пока не добавлено).

GMAN_EVENT_ID по умолчанию стабилен для повтора той же задачи (тот же payload, второе
событие не создается). GMAN_OCCURRED_AT необязателен: без него используется время
получения сервером. Задавайте его только фиксированным значением, иначе повтор вернет 409.

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

## Алерты

Prometheus rules + Alertmanager, а не Grafana alerting: правила лежат в репозитории
(deploy/alerts/rules.yaml) и проверяются `promtool test rules` без запуска стека,
подавление дочерних алертов (inhibition) — штатная функция Alertmanager, а Grafana
остается только визуализацией без собственного состояния алертов.

| Файл | Назначение |
| --- | --- |
| deploy/alerts/rules.yaml | правила и recording rules; подключены в deploy/prometheus.yaml |
| deploy/alerts/rules.test.yaml | unit-тесты правил для promtool |
| deploy/alerts/alertmanager.yaml | маршрутизация, группировка, inhibition |
| deploy/alerts/alertmanager.sh | entrypoint: добавляет получателей из GMAN_ALERT_* и запускает Alertmanager |
| backend/app/exposition.py | /metrics API: состояние inventory для правил |

| Алерт | Условие | Важность |
| --- | --- | --- |
| GManNodeDown | Docker-хост: нет ни inventory дольше 5 минут, ни метрик OTel | critical |
| GManSourceSilent | источник не присылал снимок дольше 5 минут | warning |
| GManHostTelemetryAbsent | хост известен inventory или слал метрики за 6 часов, а метрик хоста нет 5 минут | warning |
| GManK8sNodeNotReady | узел Kubernetes NotReady 2 минуты (по inventory) | critical |
| GManTelemetryGatewayDown / GManApiMetricsDown | Prometheus не читает gateway / /metrics API | critical |
| GManGpuTelemetryAbsent | DCGM-метрики GPU пропали, а Collector работает | warning |
| GManGpuXidError | XID ≠ 0; аппаратные коды (48, 64, 74, 79, 92, 94, 95, 119, 120) — critical | warning / critical |
| GManGpuEccUncorrectable | рост DBE ECC (DCGM или nvidia-smi) либо сбой переназначения строк | critical |
| GManGpuEccCorrectableHigh | более 100 SBE ECC за час | warning |
| GManGpuOverheating | GPU выше 85 °C 5 минут / выше 90 °C 2 минуты | warning / critical |
| GManGpuMemoryOverheating | память GPU выше 95 °C 5 минут | warning |
| GManGpuThermalThrottling | sw_thermal или hw_thermal 5 минут | warning |
| GManGpuHardwareSlowdown | hw_slowdown или hw_power_brake 5 минут | warning |
| GManInstanceOOMKilled | контейнер остановлен OOM (Docker) или перезапущен после OOM за 30 минут (Kubernetes) | warning |
| GManInstanceCrashLoopBackOff | pod в CrashLoopBackOff 5 минут | warning |
| GManInstanceRestartLoop | не менее 3 перезапусков за 30 минут | warning |
| GManInstanceImagePullFailing | ImagePullBackOff/ErrImagePull 10 минут | warning |

Источники данных. Хосты и GPU — телеметрия OTel (host_metrics, DCGM); где DCGM нет,
температура, ECC и причины троттлинга берутся из nvidia-smi последнего снимка inventory
(не старше 5 минут; DCGM при наличии важнее). Контейнеры и pod-ы — inventory:
kubelet_stats и docker_stats не сообщают OOMKilled/CrashLoopBackOff, а kube-state-metrics
в пилоте нет. API отдает их на /metrics: gman_source_last_snapshot_timestamp_seconds,
gman_k8s_node_ready, gman_instance_restarts_total, gman_instance_state_reason и
gman_instance_last_termination_reason (reason только из фиксированного списка),
gman_gpu_*. Путь /metrics без токена, но nginx его не проксирует: Prometheus читает
api:8000 внутри сети Compose, снаружи endpoint недоступен.

Подавление: GManNodeDown и GManK8sNodeNotReady скрывают алерты GPU, контейнеров и
каналов своего хоста (метка host_id); GManSourceSilent — алерты экземпляров и узлов
своего источника (данные устарели); GManHostTelemetryAbsent — GManGpuTelemetryAbsent;
critical скрывает warning того же алерта; CrashLoopBackOff — RestartLoop. При
недоступном gateway алерты об отсутствии метрик хостов и GPU не создаются вовсе
(иначе они подавили бы алерты по inventory всех узлов). Группировка по объекту
(alertname, source_id, host_id, UUID, instance_id); повтор через 4 часа, critical — через час.

Получатели задаются в .env и применяются при пересоздании контейнера alertmanager:
GMAN_ALERT_WEBHOOK_URL (JSON Alertmanager, send_resolved) и/или GMAN_ALERT_EMAIL_TO с
GMAN_ALERT_EMAIL_FROM, GMAN_ALERT_SMTP_SMARTHOST (host:port), необязательными
GMAN_ALERT_SMTP_USERNAME/GMAN_ALERT_SMTP_PASSWORD и GMAN_ALERT_SMTP_REQUIRE_TLS (true).
Без них получатель пустой: алерты видны в Alertmanager (http://127.0.0.1:9093) и в
портале, но никуда не отправляются. Пароль SMTP попадает в сгенерированный файл в томе
alertmanager с правами 0600.

Портал: страница «Алерты» (GET /api/alerts читает Alertmanager по ALERTMANAGER_URL,
без заглушенных и подавленных) и runbook по каждому алерту. Аннотация runbook_url
ведет на GMAN_PORTAL_URL/#alerts/<имя> (стенд — http://127.0.0.1:8080, production —
https://GMAN_PUBLIC_HOST); адрес передается в правила внешней меткой gman_portal
(`--enable-feature=expand-external-labels`). Заглушить алерт (silence) — в UI Alertmanager.

Ограничения. Пороги температуры общие для серверных GPU; температура замедления зависит
от модели. DCGM хранит код последней XID до сброса GPU, поэтому алерт XID держится до
сброса — после разбора его глушат. Поля ECC (DCGM_FI_DEV_ECC_*_VOL_TOTAL,
DCGM_FI_DEV_ROW_REMAP_FAILURE) и причины троттлинга (DCGM_FI_DEV_CLOCKS_EVENT_REASONS,
в старых версиях DCGM_FI_DEV_CLOCK_THROTTLE_REASONS) должны быть включены в CSV
счетчиков DCGM Exporter; без них эти алерты работают только по nvidia-smi из inventory.
Docker сбрасывает признак OOMKilled при автоматическом перезапуске контейнера, поэтому
OOM с restart policy виден как RestartLoop, а не OOMKilled. Состояние контейнеров
обновляется со снимками inventory (раз в минуту или по событиям `--watch`).

Проверка:

```sh
promtool check rules deploy/alerts/rules.yaml
promtool test rules deploy/alerts/rules.test.yaml
promtool check config deploy/prometheus.yaml
deploy/alerts/alertmanager.sh --render /tmp/am.yml && amtool check-config /tmp/am.yml
```

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

Проверено: тесты API/агента/метрик/истории конфигураций/ссылок Splunk/SSO/алертов (включая spool, backoff, flock и debounce `--watch`; SSO — против mock IdP), TypeScript и production build,
синтаксис Docker Compose; чтение AMD sysfs; браузерная проверка портала на тестовых
данных с API на SQLite; bamboo_event.py против локального API. Тесты проходят на PostgreSQL 17
(155 passed) и SQLite (154 passed, 1 skipped). Docker end-to-end, Kubernetes, NVIDIA/DCGM
и настоящий Bamboo еще не проверены.

## Хранилище

PostgreSQL со схемой из Alembic-миграций (backend/migrations), применяются при старте API
под advisory lock, поэтому несколько реплик могут стартовать одновременно. Таблицы:
services, sources (последний снимок), instances (текущее состояние), config_snapshots
(история различающихся конфигураций экземпляра по хешу с именем и окружением экземпляра —
основа для версий и diff; возврат к прежней конфигурации — новая версия), events (с release_id),
deployments, releases (релизы Bamboo со статусом сверки), sessions (SSO-сессии, только хеш
cookie). Новые поля снимков и каталога (например, owners, logs) хранятся в JSON и не требуют миграций.
GMAN_RETENTION_DAYS (по умолчанию 180, 0 — отключить) удаляет события, неиспользуемые
снимки конфигурации и завершенные релизы старше срока; запросы деплоев (deployments) не
удаляются. Истекшие SSO-сессии удаляются той же ежечасной очисткой (и при каждом входе)
независимо от GMAN_RETENTION_DAYS.

Переход с первой версии каркаса: старая таблица records не переносится. Агенты пришлют
снимки заново (по одному событию source-connected), сервисы каталога нужно загрузить
повторно через PUT /api/services. Курсор постраничной выдачи событий непрозрачный;
некорректный before возвращает 422.

## Границы пилота

Это основа для развития, не завершенный production PaaS. Все компоненты реализованы и
проверены тестами и на эмуляторах, но не на реальной инфраструктуре: Docker daemon и
compose end-to-end, nginx и systemd на Linux, NVIDIA/DCGM, Kubernetes/CRI-O, настоящие IdP,
Bamboo, Splunk и SMTP еще не проверены (полный список — docs/next-steps.md, «Не проверено»).

Ограничения реализации:
- Клиентский сертификат OTLP не привязан к host.id, CRL нет; бэкапы остаются на сервере.
- Kubernetes только опрашивается раз в минуту: нет watch, Kubernetes Events и audit log;
  `--watch` есть только для Docker. Короткие события между опросами могут быть пропущены;
  перезапуски по restart policy видны по счетчику, но промежуточное состояние exited — нет.
- Автор ручного запуска неизвестен: аудит сервера или аутентифицированный посредник не
  реализованы. События Bamboo не коррелируются с runtime автоматически.
- Controllers и nodes не нормализованы; kube-state-metrics нет, состояние pod-ов для алертов
  берется из inventory. Пороги алертов GPU общие для всех моделей.
- Индексы и поля Splunk не согласованы.
- Полная инвентаризация и последние события выводятся одним overview, поэтому этот API
  рассчитан на небольшой пилот.

Архитектура по фактической реализации, этапы и приемка: [docs/architecture.md](docs/architecture.md).
Текущее состояние и план: [docs/next-steps.md](docs/next-steps.md).
