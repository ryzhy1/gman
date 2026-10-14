# agent — схема структуры сервиса

Источник: корень `agent/` (см. также `README.md` в корне репозитория, раздел об агенте).

Агент inventory GMan (только чтение) собирает снимки Docker-хоста, кластера Kubernetes или GPU-узла
кластера и отправляет их в GMan API; второй входящий порт отправляет событие деплоя из Bamboo.
Во время выполнения агенту нужен только python3: на хосты он ставится одним файлом `gman-agent.pyz`,
поэтому сторонних зависимостей нет, а настройки читает `config/settings.py` (stdlib-dataclass вместо
pydantic-settings). Poetry управляет только инструментами тестов.

## Дерево репозитория

```text
agent/
├── Dockerfile                  # образ агента хоста GMan (Docker CLI + src), запуск collect.py
├── env_example                 # переменные окружения
├── pyproject.toml              # Poetry: зависимостей времени выполнения нет, dev — pytest, pydantic
├── poetry.lock
├── docs/
│   ├── service_structure.md    # этот файл
│   └── orchestrator_flow.md    # схемы функций оркестраторов
├── scripts/
│   ├── build_pyz.py            # сборка gman-agent.pyz (zipapp)
│   ├── build.sh                # pyz и образ
│   ├── run.sh                  # запуск из репозитория
│   └── test.sh                 # тесты через Poetry
├── tests/
│   ├── conftest.py             # поддельный запуск команд, сборка модулей, HTTP-модели API для контракта
│   └── test_*.py
└── src/
    ├── collect.py              # контроллер CLI: печать снимка, запуск по таймеру или --watch
    ├── bamboo_event.py         # контроллер Bamboo: событие деплоя
    ├── config/
    │   ├── settings.py         # AgentSettings из окружения
    │   └── logging.py          # логи в stderr (stdout занят JSON)
    ├── runtime_access/         # общий адаптер доступа к runtime для collection и watching
    │   ├── command_runner.py   # запуск команд без stderr
    │   ├── docker_cli.py       # docker ps/inspect/cp/events/version; ContainerEvent
    │   ├── kubernetes_api.py   # kubectl или API изнутри кластера; KubernetesGoneError
    │   └── connection.py       # RuntimeAccess: один набор адаптеров на процесс
    ├── collection/             # домен: снимки
    │   ├── container.py        # цель сбора и сборка зависимостей
    │   ├── orchestrator.py     # фасад: снимки Docker, кластера, GPU-узла; разбор объектов для watch
    │   ├── entity.py           # документы экземпляра/GPU, владельцы процессов, ссылки на pod-ы, время
    │   ├── mappers/            # inspect, pod, узел, Events -> документы; сборка снимка
    │   ├── services/           # inventory Docker, dotenv-файлы, GPU, атрибуция, OOM, Kubernetes, хост
    │   └── adapters/           # nvidia-smi, sysfs AMD, cgroup, сведения о хосте, отметка OOM
    ├── watching/               # домен: --watch (снимки по событиям с debounce)
    │   ├── container.py        # сборка для Docker или Kubernetes
    │   ├── orchestrator.py     # фасад: цикл событий и снимков
    │   ├── entity.py           # тайминги, элементы watch, документ снимка
    │   ├── ports.py            # протокол SnapshotSource (реализует оркестратор collection)
    │   ├── services/           # debounce, watcher Docker и Kubernetes, эфемерные снимки, очередь
    │   └── adapters/           # поток docker events, протокол list+watch Kubernetes
    ├── delivery/               # домен: доставка в API
    │   ├── container.py        # адрес API, spool и блокировка в каталоге состояния
    │   ├── orchestrator.py     # фасад: блокировка, запуск по таймеру, отправка через spool
    │   ├── entity.py           # исходы доставки, лимиты, снимок в spool
    │   ├── services/           # spool, повторы с backoff, блокировка запуска
    │   └── adapters/           # HTTP API, каталог spool, flock
    └── deployment_report/      # домен: событие деплоя Bamboo
        ├── container.py · orchestrator.py · entity.py · mappers.py
        ├── services/           # построение события, публикация
        └── adapters/           # HTTP API приема деплоев
```

Снимок — JSON-документ для API: поля, добавленные в API, проходят без изменений агента. Поэтому экземпляр,
GPU и снимок — документы, обернутые сущностями с доступом к полям, которые использует логика агента.
Мапперы строят документы из сырых данных runtime; адаптеры читают внешние форматы (CSV nvidia-smi, sysfs,
cgroup, JSON событий) в сущности.

## Граф компонентов

### 1. Сборка зависимостей

```mermaid
flowchart LR
  collect["collect.py<br/>контроллер CLI"]
  bamboo["bamboo_event.py<br/>контроллер Bamboo"]
  settings["config/settings.py<br/>AgentSettings"]
  runtime["runtime_access/connection.py<br/>адаптеры runtime"]
  col_c["collection/container.py<br/>сборка collection"]
  watch_c["watching/container.py<br/>сборка watching"]
  del_c["delivery/container.py<br/>сборка delivery"]
  dep_c["deployment_report/container.py<br/>сборка deployment_report"]
  collect --> settings
  collect --> runtime
  collect --> col_c
  collect --> del_c
  collect --> watch_c
  col_c -- "оркестратор collection как SnapshotSource" --> watch_c
  bamboo --> settings
  bamboo --> dep_c
```

### 2. Runtime

```mermaid
flowchart LR
  collect["collect.py<br/>контроллер CLI"]
  watching["WatchingOrchestrator<br/>фасад: --watch"]
  collection["CollectionOrchestrator<br/>фасад: снимки"]
  delivery["DeliveryOrchestrator<br/>фасад: доставка"]
  col_s["collection/services<br/>сервисы"]
  watch_s["watching/services<br/>сервисы"]
  del_s["delivery/services<br/>сервисы"]
  col_a["collection/adapters + runtime_access<br/>адаптеры: docker, kubectl, nvidia-smi, /proc, /sys"]
  watch_a["watching/adapters<br/>адаптеры: потоки событий"]
  del_a["delivery/adapters<br/>адаптеры: HTTP, spool, flock"]
  collect --> collection
  collect --> delivery
  collect --> watching
  watching -- "SnapshotSource" --> collection
  watching -- "submit" --> delivery
  collection --> col_s --> col_a
  watching --> watch_s --> watch_a
  delivery --> del_s --> del_a
```

## Поток данных runtime

```mermaid
flowchart LR
  docker[("Docker daemon<br/>сокет")]
  kube[("API Kubernetes<br/>kubectl или service account")]
  gpu[("nvidia-smi, /sys, /proc<br/>GPU и владельцы процессов")]
  agent["агент"]
  spool[("spool на диске<br/>каталог состояния")]
  api["GMan API<br/>POST /api/inventory"]
  bamboo["Bamboo script task"]
  deploy_api["GMan API<br/>POST /api/deployments"]
  docker --> agent
  kube --> agent
  gpu --> agent
  agent --> spool --> api
  bamboo --> deploy_api
```

## Запрещённые зависимости

- `orchestrator → container`, `orchestrator → adapters` (блокировка и HTTP — через сервисы `RunGuard`,
  `DeploymentPublisher`).
- `services → mappers`, `services → orchestrator` своего модуля.
- `adapters → services`, `adapters → mappers`.
- `entity → services/adapters/mappers`; `config → orchestrator/services/adapters`.
- Между модулями — через оркестратор: watching получает снимки и разбор объектов runtime только через
  протокол `watching/ports.py:SnapshotSource` (его реализует оркестратор collection) и использует типы этого
  контракта (`collection.entity`: документ экземпляра, ссылка на pod, время RFC 3339). Общий адаптер
  `runtime_access/` используют адаптеры collection и watching.
