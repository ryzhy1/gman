# Деплой GMan

Два шага: поднять сервер GMan (портал, API, база, метрики, алерты), затем подключить к нему
остальные серверы. Везде нужен только Docker с плагином `docker compose` (≥ 2.20).

> Портал и API работают без входа. Любой, кто достучится до портов GMan, видит все данные,
> включая переменные окружения и `.env`-файлы контейнеров, и может их отправлять. Открывайте
> порты только своим серверам и пользователям (файрвол, VPN).

## 1. Сервер GMan

```sh
git clone git@github.com:ryzhy1/gman.git && cd gman
docker compose up --build -d
docker compose ps        # все сервисы running / healthy
```

Портал: `http://<сервер>:8080`, Grafana — `http://<сервер>:8080/grafana/`.
Конфигурации Prometheus, Alertmanager, Grafana и OTel собраны в образы, `.env` не обязателен.
Сервер GMan сразу показывает свои контейнеры и метрики (сервисы `agent` и `node-otel`).

### Настройка (необязательно)

Создайте `.env` в корне репозитория **до первого запуска**:

```sh
# Порты на хосте
GMAN_PORT=8080               # портал, API, Grafana
GMAN_OTLP_PORT=4317          # метрики с других серверов (OTLP gRPC)
GMAN_OTLP_HTTP_PORT=4318     # OTLP HTTP
GMAN_PROMETHEUS_PORT=9090    # только 127.0.0.1
GMAN_ALERTMANAGER_PORT=9093  # только 127.0.0.1
GMAN_GRAFANA_PORT=3000       # только 127.0.0.1 (снаружи Grafana доступна через портал)

# Имя и адрес самого сервера GMan в портале
GMAN_SOURCE_ID=gman-server
GMAN_HOST_IP=10.0.0.5        # иначе показывается адрес контейнера агента

# Пароли (по умолчанию gman / admin)
POSTGRES_PASSWORD=...
GRAFANA_ADMIN_PASSWORD=...
```

Случайные пароли создает `python3 deploy/init_env.py`. Пароль PostgreSQL задается только до
первого запуска: база запоминает пароль, с которым создан volume.

Получатели алертов (webhook, email) и ссылки на Splunk — переменные `GMAN_ALERT_*` и
`GMAN_SPLUNK_*`, описаны в README («Алерты», «Splunk»).

### Обслуживание

```sh
git pull && docker compose up --build -d   # обновление
docker compose logs -f api                 # журналы сервиса
docker compose down                        # остановка, данные сохраняются
docker compose down -v                     # остановка с удалением всех данных
```

Production с TLS и mTLS для метрик, бэкапами и nginx — README, «Production-развертывание».

## 2. Остальные серверы

На каждом сервере работают два контейнера:

- `agent` — контейнеры с конфигурацией, переменными окружения и `.env`-файлами, сведения о
  сервере (ОС, ядро, CPU, память, аптайм);
- `node-otel` — загрузка CPU, памяти, дисков и сети сервера и каждого контейнера.

Оба в сети хоста, поэтому портал показывает настоящий IP сервера.

### Архив

В репозитории, на своей машине:

```sh
deploy/host/pack.sh                       # создаст gman-host.tar.gz (~30 КБ)
scp gman-host.tar.gz user@server-02:
```

### Запуск на сервере

```sh
tar xzf gman-host.tar.gz && cd gman-host
GMAN_SERVER=10.0.0.5 GMAN_SOURCE_ID=server-02 \
  docker compose -f deploy/host/compose.yaml up -d --build
```

| Переменная | Значение |
| --- | --- |
| `GMAN_SERVER` | адрес сервера GMan |
| `GMAN_SOURCE_ID` | уникальное имя сервера в портале, например его hostname |
| `GMAN_PORT` | порт портала сервера GMan, если менялся (8080) |
| `GMAN_OTLP_PORT` | OTLP-порт сервера GMan, если менялся (4317) |
| `GMAN_HOST_IP` | адрес для портала, если нужен не адрес маршрута по умолчанию |

Чтобы не передавать их в каждой команде, запишите их в `gman-host/deploy/host/.env`.

С сервера должны быть доступны `GMAN_PORT` и `GMAN_OTLP_PORT` сервера GMan. Сервер появится в
«Серверы и GPU» через несколько секунд, метрики — примерно через минуту. Если сервер не появился:
`docker compose -f deploy/host/compose.yaml logs agent` — при успехе там строки
`200 {"accepted":true}`.

### Обновление и отключение

```sh
# после изменений агента: пересобрать архив, скопировать, распаковать поверх и
docker compose -f deploy/host/compose.yaml up -d --build
# отключить сервер
docker compose -f deploy/host/compose.yaml down -v
```

Данные отключенного сервера остаются в портале с отметкой «Устарело».

## Ограничения

- GPU других серверов (nvidia-smi, процессы на GPU) агент в контейнере не видит; метрики GPU
  дает DCGM Exporter (README, «DCGM Exporter»).
- Kubernetes-кластеры подключаются отдельно: агент с `--runtime kubernetes` и
  `deploy/otel-k8s.yaml` (README, «Подключение inventory»).
- Данные с серверов идут по HTTP и OTLP без шифрования. Через интернет — VPN или
  production-режим с TLS.
