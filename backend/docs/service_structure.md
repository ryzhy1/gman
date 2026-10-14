# backend — схема структуры сервиса

Источник: корень `backend/` (см. также `README.md` в корне репозитория).

GMan API принимает снимки inventory от агентов и события деплоя от Bamboo, хранит каталог, историю изменений,
версии конфигураций и релизы, отдает порталу метрики Prometheus и алерты Alertmanager, а Prometheus — метрики
состояния inventory для правил алертов.

## Дерево репозитория

```text
backend/
├── Dockerfile                  # образ: окружение Poetry в сборочном слое, uvicorn main:create_app --factory
├── env_example                 # переменные окружения (pydantic-settings)
├── pyproject.toml              # зависимости (Poetry) и настройки pytest
├── poetry.lock
├── alembic.ini                 # CLI alembic: миграции из каталога migrations/
├── migrations/                 # схема БД; API применяет их при старте (lifespan)
├── docs/
│   ├── service_structure.md    # этот файл
│   └── orchestrator_flow.md    # схемы функций оркестраторов
├── scripts/
│   ├── build.sh                # сборка образа
│   ├── run.sh                  # локальный запуск с перезагрузкой
│   └── test.sh                 # тесты через Poetry
├── tests/
│   ├── conftest.py             # БД на тест, клиенты приложения с настройками и подменой HTTP
│   └── test_*.py
└── src/
    ├── main.py                 # контроллер: create_app — контейнеры модулей, оркестраторы, маршруты
    ├── lifespan.py             # старт: миграции; остановка: пул соединений
    ├── config/
    │   ├── settings.py         # Settings (pydantic-settings) из переменных окружения
    │   └── logging.py          # настройка логирования
    ├── api/                    # HTTP-граница: маршруты, коды ответа, HTTP-модели
    │   ├── dependencies.py     # оркестраторы из состояния приложения
    │   ├── errors.py           # 500 без подробностей
    │   ├── schemas/            # HTTP-модели запросов (самодостаточны: их проверяет и тест контракта агента)
    │   └── routes/             # inventory, configs, releases, telemetry, system (/healthz, /metrics)
    ├── database/               # общий адаптер хранилища для всех модулей
    │   ├── tables.py           # ORM-записи (сверяются с миграциями `alembic check`)
    │   ├── engine.py           # engine и применение миграций под блокировкой
    │   ├── unit_of_work.py     # транзакция сценария, общая для репозиториев; StorageError
    │   ├── advisory_lock.py    # advisory-блокировки PostgreSQL по ключу
    │   ├── keyset_cursor.py    # курсоры страниц
    │   └── column_types.py     # UTC-время, JSON/JSONB, ID
    ├── inventory/              # домен: прием снимков, каталог, аудит, обзор, ссылки Splunk
    │   ├── container.py        # сборка зависимостей, разбор настроек Splunk
    │   ├── orchestrator.py     # фасад модуля
    │   ├── entity.py           # сервис каталога, экземпляр, снимок, событие истории, ссылки на логи
    │   ├── mappers.py          # пейлоады API -> DTO, DTO -> ответы
    │   ├── ports.py            # протокол учета релизов (реализует releases)
    │   ├── services/           # прием снимка, история, разрывы, Events, каталог, обслуживание, Splunk
    │   └── adapters/           # репозитории источников, экземпляров, событий, отметок, каталога, очистка
    ├── config_history/         # домен: версии конфигураций и их сравнение
    │   ├── container.py · orchestrator.py · entity.py · mappers.py
    │   ├── services/           # каталог версий, структурное сравнение
    │   └── adapters/           # репозиторий версий
    ├── releases/               # домен: деплои Bamboo и статус релизов по наблюдениям runtime
    │   ├── container.py · orchestrator.py · entity.py · mappers.py
    │   ├── services/           # журнал CI, реестр, вытеснение, учет, оценка, связывание, чтение
    │   └── adapters/           # репозитории релизов, событий, экземпляров, событий CI
    ├── telemetry/              # домен: метрики ресурсов, workloads, активные алерты
    │   ├── container.py · orchestrator.py · entity.py · mappers.py
    │   ├── services/           # шаблоны PromQL, чтение метрик и workloads, классификация, дайджест алертов
    │   └── adapters/           # клиенты Prometheus и Alertmanager
    └── exposition/             # домен: /metrics для правил алертов
        ├── container.py · orchestrator.py · entity.py
        ├── services/           # выборки inventory и TLS, текстовый формат, наблюдение за TLS-файлами
        └── adapters/           # чтение состояния inventory, чтение TLS-файлов
```

Сущности модулей — dataclass без зависимостей от внешних форматов. Снимок и экземпляр приходят от агента
JSON-документом, который хранится и сравнивается целиком (поля, добавленные в API, проходят без миграции),
поэтому их сущности оборачивают документ и дают типизированный доступ к полям бизнес-логики. Адаптеры
переводят внешние форматы (строки БД, ответы Prometheus и Alertmanager, файлы) в сущности; мапперы переводят
входящие пейлоады API в DTO и DTO в ответы.

## Граф компонентов

### 1. Сборка зависимостей

```mermaid
flowchart LR
  main["main.py<br/>контроллер: create_app"]
  settings["config/settings.py<br/>Settings из окружения"]
  uow["database/unit_of_work.py<br/>адаптер: граница транзакции"]
  inv_c["inventory/container.py<br/>сборка inventory"]
  rel_c["releases/container.py<br/>сборка releases"]
  cfg_c["config_history/container.py<br/>сборка config_history"]
  tel_c["telemetry/container.py<br/>сборка telemetry"]
  exp_c["exposition/container.py<br/>сборка exposition"]
  orch["api/dependencies.py<br/>Orchestrators в app.state"]
  main --> settings
  main --> uow
  main --> rel_c
  main --> inv_c
  main --> cfg_c
  main --> tel_c
  main --> exp_c
  rel_c -- "оркестратор releases как ReleaseTracker" --> inv_c
  main --> orch
```

### 2. Runtime

```mermaid
flowchart LR
  routes["api/routes<br/>контроллер: HTTP"]
  inv["InventoryOrchestrator<br/>фасад: снимки, каталог, аудит"]
  rel["ReleasesOrchestrator<br/>фасад: деплои и релизы"]
  cfg["ConfigHistoryOrchestrator<br/>фасад: версии конфигураций"]
  tel["TelemetryOrchestrator<br/>фасад: метрики и алерты"]
  exp["ExpositionOrchestrator<br/>фасад: /metrics"]
  inv_s["inventory/services<br/>сервисы"]
  rel_s["releases/services<br/>сервисы"]
  cfg_s["config_history/services<br/>сервисы"]
  tel_s["telemetry/services<br/>сервисы"]
  exp_s["exposition/services<br/>сервисы"]
  db["database + */adapters<br/>адаптеры: PostgreSQL"]
  http["telemetry/adapters<br/>адаптеры: Prometheus, Alertmanager"]
  files["exposition/adapters<br/>адаптер: TLS-файлы"]
  routes --> inv
  routes --> rel
  routes --> cfg
  routes --> tel
  routes --> exp
  inv --> inv_s
  inv -- "track_snapshot" --> rel
  rel --> rel_s
  cfg --> cfg_s
  tel --> tel_s
  exp --> exp_s
  inv_s --> db
  rel_s --> db
  cfg_s --> db
  exp_s --> db
  exp_s --> files
  tel_s --> http
```

## Поток данных runtime

```mermaid
flowchart LR
  agent["агент inventory<br/>POST /api/inventory"]
  bamboo["Bamboo<br/>POST /api/deployments"]
  portal["портал<br/>GET /api/*"]
  prometheus_scrape["Prometheus<br/>GET /metrics"]
  api["GMan API"]
  pg[("PostgreSQL<br/>снимки, история, версии, релизы")]
  prom[("Prometheus<br/>метрики ресурсов")]
  am[("Alertmanager<br/>активные алерты")]
  tls[("TLS-файлы nginx<br/>только чтение")]
  agent --> api
  bamboo --> api
  portal --> api
  prometheus_scrape --> api
  api --> pg
  api --> prom
  api --> am
  api --> tls
```

## Запрещённые зависимости

- `orchestrator → container`, `orchestrator → adapters` (транзакцию оркестратор получает как функцию
  `transaction`, данные — только через сервисы), `orchestrator → api`.
- `services → mappers`, `services → orchestrator` своего модуля, `services → api`.
- `adapters → services`, `adapters → mappers`, `adapters → orchestrator`.
- `entity → services/adapters/mappers`; `config → orchestrator/services/adapters`.
- Между модулями — только через оркестратор: inventory зависит от releases через протокол
  `inventory/ports.py:ReleaseTracker` (примитивы и время), а не через сервисы или адаптеры releases.
  Общий адаптер хранилища `database/` используют адаптеры всех модулей.
- `api/` обращается к доменам только через оркестраторы (и импортирует типы ошибок из entity для кодов ответа).
