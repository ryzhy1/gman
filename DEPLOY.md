# Деплой GMan

Два шага: поднять сервер GMan (портал, API, база, метрики, алерты), затем подключить к нему
остальные серверы. Везде нужен только Docker с плагином `docker compose` (≥ 2.20).

> Портал и API работают без входа. Любой, кто достучится до портов GMan, видит все данные,
> включая переменные окружения и `.env`-файлы контейнеров, и может их отправлять. Открывайте
> порты только своим серверам и пользователям (файрвол, VPN).

## 1. Сервер GMan

```sh
git clone git@github.com:ryzhy1/gman.git gman && cd gman   # или https://github.com/ryzhy1/gman.git
docker compose up --build -d
docker compose ps        # все сервисы running / healthy
```

Портал: `http://<сервер>:8080`, Grafana — `http://<сервер>:8080/grafana/`.
Конфигурации Prometheus, Alertmanager, Grafana и OTel собраны в образы, `.env` не обязателен.
Сервер GMan сразу показывает свои контейнеры и метрики (сервисы `agent` и `node-otel`).

### Настройка (необязательно)

```sh
cp .env.example .env     # в примере все переменные с их значениями по умолчанию
```

Измените нужное **до первого запуска**. Основные:

```sh
# Порты на хосте
GMAN_PORT=8080               # портал, API, Grafana
GMAN_OTLP_PORT=4317          # метрики с других серверов (OTLP gRPC)
GMAN_OTLP_HTTP_PORT=4318     # OTLP HTTP
GMAN_PROMETHEUS_PORT=9090    # только 127.0.0.1
GMAN_ALERTMANAGER_PORT=9093  # только 127.0.0.1
GMAN_GRAFANA_PORT=3000       # только 127.0.0.1 (снаружи Grafana доступна через портал)

# Интервал метрик в секундах: сбор, Prometheus, алерты, обновление портала
GMAN_METRICS_INTERVAL=5

# Имя и адрес самого сервера GMan в портале
GMAN_SOURCE_ID=gman-server
GMAN_HOST_IP=10.0.0.5        # иначе показывается адрес контейнера агента

# Пароли (по умолчанию gman / admin), например из `openssl rand -hex 24`
POSTGRES_PASSWORD=...
GRAFANA_ADMIN_PASSWORD=...
```

Пароли действуют только при первом запуске: PostgreSQL и Grafana запоминают пароль, с которым
созданы их volumes, и потом изменение `.env` на них не влияет. Остальные настройки применяются
командой `docker compose up -d`.
`GMAN_SOURCE_ID` у каждого сервера, включая сервер GMan, должен быть своим.

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

### Видеокарты NVIDIA

Если на сервере GMan есть NVIDIA, раскомментируйте в `.env`:

```sh
COMPOSE_FILE=compose.yaml:deploy/compose.nvidia.yaml
```

и выполните `docker compose up --build -d`. Добавятся:

- доступ агента к GPU — модель, загрузка, VRAM, температура, мощность, частоты, ECC, а также какой
  контейнер и сколько VRAM занимает на карте (`nvidia-smi`);
- контейнер `dcgm-exporter` (NVIDIA DCGM Exporter) — графики GPU в портале и Grafana, алерты по GPU.

На хосте должны быть драйвер NVIDIA и NVIDIA Container Toolkit
(`docker run --rm --gpus all ubuntu nvidia-smi` должен показать карты). Без них контейнеры с GPU
не запустятся, поэтому на серверах без NVIDIA эту строку не включайте.

## 2. Остальные серверы

На каждом сервере работают два контейнера (и `dcgm-exporter` на серверах с NVIDIA):

- `agent` — контейнеры с конфигурацией, переменными окружения и `.env`-файлами, сведения о
  сервере (ОС, ядро, CPU, память, аптайм), видеокарты;
- `node-otel` — загрузка CPU, памяти, дисков и сети сервера и каждого контейнера, метрики GPU.

Все в сети хоста, поэтому портал показывает настоящий IP сервера.

### Архив

В репозитории, на своей машине:

```sh
deploy/host/pack.sh                       # создаст gman-host.tar.gz (~30 КБ)
scp gman-host.tar.gz user@server-02:
```

### Запуск на сервере

Все команды — из каталога `gman-host/deploy/host`:

```sh
tar xzf gman-host.tar.gz && cd gman-host/deploy/host
cp .env.example .env
vi .env                                   # GMAN_SERVER и GMAN_SOURCE_ID, при необходимости порты и GPU
docker compose up -d --build
```

Пример `.env`:

```sh
GMAN_SERVER=10.0.0.5
GMAN_SOURCE_ID=server-02
#GMAN_PORT=8080
#GMAN_OTLP_PORT=4317
#GMAN_HOST_IP=
# сервер с NVIDIA (драйвер и NVIDIA Container Toolkit, как выше):
#COMPOSE_FILE=compose.yaml:compose.nvidia.yaml
```

| Переменная | Значение |
| --- | --- |
| `GMAN_SERVER` | адрес сервера GMan (IP или домен, без порта) |
| `GMAN_SOURCE_ID` | уникальное имя сервера в портале, например его hostname |
| `GMAN_PORT` | порт портала сервера GMan, если менялся (8080) |
| `GMAN_OTLP_PORT` | OTLP-порт сервера GMan, если менялся (4317) |
| `GMAN_HOST_IP` | адрес для портала, если нужен не адрес маршрута по умолчанию |
| `GMAN_METRICS_INTERVAL` | интервал метрик в секундах, как на сервере GMan (5) |
| `COMPOSE_FILE` | `compose.yaml:compose.nvidia.yaml` — включить видеокарты NVIDIA |

С сервера должны быть доступны `GMAN_PORT` и `GMAN_OTLP_PORT` сервера GMan. Сервер появится в
«Серверы и GPU» через несколько секунд, метрики — примерно через минуту. Если сервер не появился:
`docker compose logs agent` — при успехе там строки `200 {"accepted":true}`. Метрики GPU:
`docker compose logs dcgm-exporter`.

### Обновление и отключение

```sh
# после изменений агента: пересобрать архив, скопировать, распаковать поверх и
docker compose up -d --build
# отключить сервер
docker compose down -v
```

Данные отключенного сервера остаются в портале с отметкой «Устарело».

## Ограничения

- Видеокарты AMD агент показывает по sysfs (модель, загрузка, VRAM, температуры, частоты) без
  графиков: метрики GPU в Prometheus есть только для NVIDIA (DCGM).
- Kubernetes-кластеры подключаются отдельно: агент с `--runtime kubernetes` и
  `deploy/otel-k8s.yaml` (README, «Подключение inventory»).
- Данные с серверов идут по HTTP и OTLP без шифрования. Через интернет — VPN или
  production-режим с TLS.
