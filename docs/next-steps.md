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

Тесты: 31 passed, 1 skipped (тест конкурентности только для PostgreSQL).
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

Первое, что стоит сделать на машине с Docker: `docker compose ... up --build`,
пройти README «Локальный запуск», запустить агент на хосте с NVIDIA.

## Незавершенные задачи

Эти задачи были начаты параллельными агентами и прерваны; изменений от них в коде нет.

1. Production-развертывание. Агенты требуют HTTPS, а стек слушает только 127.0.0.1
   без TLS; узловые Collector настроены на mTLS, а у gateway нет TLS receiver —
   удаленные серверы пока не могут доставлять данные. Нужно: HTTPS для портала/API
   (nginx), OTLP receiver gateway с TLS и проверкой клиентских сертификатов
   (client_ca_file), compose override для production, скрипт выпуска пилотного CA
   и сертификатов, limit_req и body size в nginx, healthcheck сервисов в compose,
   бэкап PostgreSQL (pg_dump с ротацией) и процедура восстановления, systemd-юниты
   для агента сбора (timer раз в минуту) и otelcol-contrib.
2. Надежность агента сбора (agent/collect.py, только stdlib): ограниченный spool
   неотправленных снимков, повторы с backoff, flock против пересечения запусков,
   режим `--watch` по `docker events` с debounce, чтобы короткие перезапуски и
   `docker run --rm` не терялись между опросами. Неудачный сбор по-прежнему
   не отправлять как пустой снимок.
3. История конфигураций: API версий конфигурации сервиса и экземпляра и diff между
   версиями на основе таблицы config_snapshots (миграция 0002 при необходимости),
   затем вкладка «Конфигурация» в портале с выбором версий и diff.

## Дальше по плану

- SSO (OIDC) и роли viewer / service owner / platform admin вместо общего токена.
- Атрибуция процессов GPU в Kubernetes (PID → pod) — сейчас только Docker-хосты.
- Алерты (Grafana alerting или Alertmanager): узел/источник молчит, GPU XID/ECC,
  перегрев, троттлинг, OOMKilled, CrashLoopBackOff.
- Splunk: поиск по service/environment по ссылке из карточки после согласования индексов.
- Обновить docs/architecture.md по фактической реализации.
