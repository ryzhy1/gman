# Состояние и следующие шаги

Обновлено 11 октября 2026 года. Документ для продолжения работы на другой машине.

## Что сделано

1. Исправлены баги из ревью: race `docker ps`/`inspect`, статус против health, нулевое
   время старта, повторы Bamboo (409 и падение деплоя), молчаливый сбой nvidia-smi,
   500 на не-ASCII токене, авторизация до разбора тела, история сервиса вне окна
   200 событий, шум событий. Расширены конфигурация контейнеров/pod-ов, происхождение
   (compose, OCI-метки, field managers), причины статусов.
2. Ресурсы в портале: API читает Prometheus фиксированными шаблонами, Grafana с
   дашбордами «Хост», «Сервис», «Kubernetes», вкладка «Ресурсы» с графиками.
3. Подробные GPU (nvidia-smi + DCGM), привязка процессов GPU к контейнерам, сведения
   о хосте, узлы Kubernetes с ёмкостью и requests, OTel DaemonSet для кластера.
4. Хранилище: нормализованная схема PostgreSQL, Alembic, история конфигураций по хешу,
   retention. Проверено на PostgreSQL 17.
5. Production-развертывание (README «Production-развертывание»): deploy/compose.prod.yaml
   (443/80 через nginx, OTLP 4317 с mTLS, Grafana на /grafana/, ротация логов),
   deploy/nginx/gman.conf (TLS, HSTS, limit_req, client_max_body_size),
   deploy/otel-gateway.prod.yaml (TLS + client_ca_file), deploy/pki/issue.sh (пилотный CA,
   серверные и клиентские сертификаты), healthcheck сервисов в compose, сервис backup
   (pg_dump с проверкой и ротацией) и процедура восстановления, systemd-юниты
   gman-collect.service/.timer и gman-otelcol.service.
6. Надежность агента сбора (agent/collect.py, только stdlib): ограниченный spool на диске
   (по числу и байтам, старые удаляются с отметкой в errors), повторы с backoff и jitter,
   неблокирующий flock, режим `--watch` по `docker events` с debounce и промежуточным
   снимком для исчезнувших контейнеров (`docker run --rm`). Изменения backend не
   понадобились: прием уже идемпотентен и упорядочен по observed_at.
7. История конфигураций: API версий сервиса и экземпляра (новые сверху, курсор before),
   версия целиком и структурный diff (GET /api/configs/diff?from=&to=), миграция 0002
   (имя и окружение экземпляра в config_snapshots), вкладка «Конфигурация» с выбором
   версий «Было/Стало» и таблицей различий. На PostgreSQL новые запросы не проверялись.

8. Splunk: ссылки «Логи в Splunk» на карточке сервиса и у экземпляров, строятся API
   по конфигурации (GMAN_SPLUNK_URL, GMAN_SPLUNK_INDEXES, GMAN_SPLUNK_SEARCH) с
   экранированием SPL и переопределением logs.splunk в каталоге. См. README «Splunk».
9. Атрибуция процессов GPU в Kubernetes (PID → pod): режим агента kubernetes-node на
   GPU-узле (cgroup v1/v2, systemd/cgroupfs, containerd/CRI-O/cri-dockerd, static pod),
   источник узла без экземпляров, DaemonSet deploy/gpu-attribution-k8s.yaml, процессы в
   карточке узла и экземплярах сервиса; GPU по pod-ам из меток DCGM на вкладке «Ресурсы».
10. SSO (OIDC) и роли: вход Authorization Code + PKCE через API, серверные сессии
    (миграция 0003), CSRF, роли из групп IdP, владельцы сервиса в поле `owners`,
    редактор записи каталога в карточке, автор изменений каталога в журнале.

Тесты: 127 passed, 1 skipped (тест конкурентности только для PostgreSQL).
Frontend: `npm run build` проходит. Подробности — в README.

## Подготовка на новой машине

```sh
git clone <url> && cd GMan
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
.venv/bin/python -m pytest backend/tests -q
cd frontend && npm ci && npm run build && cd ..
```

Полный стек: `python3 deploy/init_env.py`, затем
`docker compose --env-file .env -f deploy/compose.yaml up --build -d`
(портал :8080, Grafana :3000, Prometheus :9090; все на 127.0.0.1).

Проверка на PostgreSQL: задайте `GMAN_TEST_DATABASE_URL=postgresql+psycopg://...`
для пустой тестовой базы — тесты пересоздают схему (см. backend/tests/database.py).

## Не проверено

Сборка Docker-образов и compose end-to-end (на исходной машине не было Docker daemon),
настоящие NVIDIA/DCGM, реальный Kubernetes (k8s_attributes, обнаружение DCGM, RBAC),
настоящий Bamboo. Метрики проверялись на otelcol-contrib 0.162.0, Prometheus 3.5.0,
Grafana 12.1.1 с эмуляцией Docker API, kubelet и вывода DCGM.

Production-развертывание проверено частично, без Docker daemon и Linux:
- `docker compose config` для compose.yaml и compose.yaml + compose.prod.yaml (Compose
  2.29.7; на 2.24.3 `!override` у ports молча не срабатывает — нужен ≥ 2.24.4);
- issue.sh (shellcheck, выпуск в временный каталог, `openssl verify -purpose`);
- gateway на otelcol-contrib 0.162.0: `validate`, доставка метрик от узлового Collector
  с клиентским сертификатом, отказ без сертификата и с сертификатом чужого CA;
- pg_backup.sh на PostgreSQL 17 (shellcheck, ротация, `--check`, отсутствие ротации при
  сбое, `pg_restore --clean` в испорченную базу со схемой GMan);
- urllib агента доверяет пилотному CA через SSL_CERT_FILE (Python с OpenSSL);
- nginx-конфиги разобраны crossplane (контексты и число аргументов), не `nginx -t`.

Не проверено: `nginx -t` и запуск nginx с TLS, подъем стека с override (healthcheck
внутри образов: wget в grafana/prometheus, путь /grafana/api/health при
serve_from_sub_path), Grafana за /grafana/ (вкл. Live/websocket), права uid 10001 на
ключ gateway, systemd-юниты (`systemd-analyze verify`, sandboxing с docker/nvidia-smi/
kubectl), сервис backup в контейнере (busybox sh/find).

Первое, что стоит сделать на машине с Docker: `docker compose ... up --build`,
пройти README «Локальный запуск», затем «Production-развертывание» на тестовом сервере,
запустить агент на хосте с NVIDIA.

## Незавершенные задачи

Все три задачи закрыты (11 октября 2026); остались непроверенные на реальной инфраструктуре части.

1. ~~Production-развертывание~~ — сделано (см. «Что сделано», п. 5, и «Не проверено»).
   Остается: проверка на реальном сервере, привязка клиентского сертификата OTLP
   к host.id (сейчас любой сертификат пилотного CA принимается за любой узел), отзыв
   сертификатов (CRL), выгрузка бэкапов за пределы сервера.
2. ~~Надежность агента сбора~~ — сделано (см. «Что сделано», п. 6). Не проверено
   на настоящем Docker daemon; `--watch` для Kubernetes не реализован (только опрос).
3. ~~История конфигураций~~ — сделано, см. «Что сделано», п. 7.

## Дальше по плану

- ~~SSO (OIDC) и роли viewer / service owner / platform admin вместо общего токена.~~
  Сделано: вход Authorization Code + PKCE через API, серверные сессии (миграция 0003),
  CSRF, роли из групп IdP, владельцы сервиса в поле `owners`, автор изменений каталога
  в журнале. Bearer-токены остаются для автоматизации и режима без SSO. Проверено
  против mock IdP в тестах; с настоящим IdP (Keycloak/AD FS/Okta) еще не проверялось.
- ~~Атрибуция процессов GPU в Kubernetes (PID → pod)~~ — сделано (п. 9 выше). Не проверено
  в реальном кластере: nvidia-smi через NVIDIA runtime без запроса GPU, список процессов
  от non-root, PSA privileged для hostPID, метки DCGM pod/namespace/container.
- Алерты (Grafana alerting или Alertmanager): узел/источник молчит, GPU XID/ECC,
  перегрев, троттлинг, OOMKilled, CrashLoopBackOff.
- ~~Splunk: поиск по service/environment по ссылке из карточки~~ — сделано (п. 8 выше);
  осталось согласовать индексы и имена полей и задать их в GMAN_SPLUNK_*.
- Обновить docs/architecture.md по фактической реализации.
