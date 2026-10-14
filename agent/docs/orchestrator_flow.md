# Оркестраторы агента — схема функций

Источник: `src/<модуль>/orchestrator.py` четырех доменных модулей. Входящие порты: `src/collect.py` (CLI)
и `src/bamboo_event.py` (Bamboo).

## CollectionOrchestrator

Источник: `src/collection/orchestrator.py`.

### Карта методов

| Метод | Видимость | Назначение |
| --- | --- | --- |
| snapshot | public | Снимок настроенного runtime (Docker, кластер, GPU-узел) |
| docker_snapshot | public | Снимок Docker-хоста: контейнеры с dotenv-файлами, GPU, хост |
| docker_snapshot_with_ooms | public | Снимок Docker по таймеру плюс OOM-kill из событий демона |
| cluster_snapshot | public | Снимок кластера; --watch передает pod-ы из кеша |
| node_snapshot | public | Снимок GPU-узла с процессами, отнесенными к pod-ам |
| capture_container | public | Состояние контейнера на момент события (для --watch) |
| pod_instances / pod_ref / pod_refs | public | Разбор pod-а для --watch |
| map_events / relevant_event | public | Сопоставление и фильтр Events Kubernetes |
| _docker_snapshot | private | Снимок Docker с общим списком ошибок |
| _with_env_files | private | Экземпляр из inspect с dotenv-файлами |

### Граф вызовов

```mermaid
flowchart TB
  cli["collect.py<br/>контроллер CLI"]
  watch["WatchingOrchestrator<br/>через SnapshotSource"]
  snapshot["snapshot<br/>выбор по runtime"]
  docker["docker_snapshot<br/>Docker-хост"]
  ooms["docker_snapshot_with_ooms<br/>Docker по таймеру"]
  cluster["cluster_snapshot<br/>кластер"]
  node["node_snapshot<br/>GPU-узел"]
  inner["_docker_snapshot<br/>сбор Docker"]
  files["_with_env_files<br/>dotenv-файлы"]
  capture["capture_container<br/>контейнер события"]
  cli --> snapshot
  cli --> ooms
  watch --> snapshot
  watch --> cluster
  watch --> capture
  snapshot --> docker
  snapshot --> cluster
  snapshot --> node
  docker --> inner
  ooms --> inner
  inner --> files
  capture --> files
  inner -- "отказ демона" --> fail["DaemonInspectError<br/>снимок не отправляется"]
```

### `docker_snapshot_with_ooms` — последовательность шагов

```mermaid
flowchart TD
  base["base_snapshot<br/>общие поля и список ошибок"]
  inspect["DockerInventory.inspect_containers<br/>inspect всех, удаленные пропускаются"]
  map["docker_instance + EnvFileReader<br/>экземпляры с dotenv-файлами"]
  gpus["GpuInventory.gpus<br/>AMD и NVIDIA с владельцами процессов"]
  host["HostInventory.facts<br/>ОС, CPU, память, Docker, адрес"]
  doc["docker_snapshot<br/>документ снимка"]
  oom["DockerOomHistory.apply<br/>OOM с отметки прошлого запуска"]
  base --> inspect --> map --> gpus --> host --> doc --> oom
```

### Внешние зависимости

```text
docker_snapshot / docker_snapshot_with_ooms / capture_container
  ├─ mappers: base_snapshot, docker_snapshot, docker_instance, env_file_location, attach_env_files, event_instance,
  │           instances_of
  └─ services: DockerInventory, EnvFileReader, GpuInventory, HostInventory, DockerOomHistory
cluster_snapshot / node_snapshot
  ├─ mappers: cluster_snapshot, node_snapshot, kube_events, pod_ref, pod_index
  └─ services: KubernetesInventory, GpuInventory, PodAttribution
```

## WatchingOrchestrator

Источник: `src/watching/orchestrator.py`.

| Метод | Видимость | Назначение |
| --- | --- | --- |
| watch | public | Цикл: события, debounce, сбор и отправка снимков |
| _snapshot | private | Сбор и отправка снимков одного цикла; неудача — повтор позже |

```mermaid
flowchart TD
  start["EventInbox.start<br/>потоки событий в нитях"]
  next["EventInbox.next<br/>событие или срок снимка"]
  observe["DebouncedWatcher.observe<br/>событие, разрыв потока — сверка"]
  due{"пора снимок?"}
  take["watcher.take<br/>увиденные экземпляры"]
  collect["watcher.collect<br/>снимок через SnapshotSource"]
  failed{"сбор удался?"}
  retry["watcher.retry<br/>повтор через retry_delay"]
  snapshots["watcher.snapshots<br/>эфемерные экземпляры, OOM, Events"]
  submit["submit<br/>DeliveryOrchestrator.submit"]
  start --> next --> observe --> due
  due -- "нет" --> next
  due -- "да" --> take --> collect --> failed
  failed -- "нет" --> retry --> next
  failed -- "да" --> snapshots --> submit --> next
```

```text
watch
  ├─ services: DockerWatcher или KubeWatcher (DebouncedWatcher, with_ephemeral), EventInbox
  ├─ adapters (через EventInbox): DockerEventFeed, KubernetesWatchFeed
  └─ ports: SnapshotSource (оркестратор collection)
```

## DeliveryOrchestrator

Источник: `src/delivery/orchestrator.py`.

| Метод | Видимость | Назначение |
| --- | --- | --- |
| acquire_lock | public | Блокировка источника на время процесса |
| run_once | public | Запуск по таймеру: собрать, положить в spool, отправить накопленное |
| submit | public | Положить снимки в spool и отправить накопленное (--watch) |

```mermaid
flowchart TD
  collect["collect()<br/>снимок от collection"]
  failed{"сбор удался?"}
  flush_fail["SnapshotSpool.flush<br/>отправить накопленное и пробросить ошибку"]
  put["SnapshotSpool.put<br/>с пределами по числу и размеру"]
  flush["SnapshotSpool.flush + DeliveryRetry<br/>по порядку до первого сбоя"]
  remaining{"остались неотправленные?"}
  pending["SubmissionPendingError<br/>ненулевой код, снимки ждут следующего запуска"]
  collect --> failed
  failed -- "нет" --> flush_fail
  failed -- "да" --> put --> flush --> remaining
  remaining -- "да" --> pending
  remaining -- "нет" --> done["успех"]
```

```text
acquire_lock / run_once / submit
  └─ services: RunGuard, SnapshotSpool, DeliveryRetry
```

## DeploymentReportOrchestrator

Источник: `src/deployment_report/orchestrator.py`.

| Метод | Видимость | Назначение |
| --- | --- | --- |
| event | public | Событие деплоя по окружению шага Bamboo |
| send | public | Построить и отправить событие |

```mermaid
flowchart TD
  map["context_from_environment<br/>GMAN_* или переменные Bamboo"]
  build["DeploymentEventBuilder.build<br/>ID запуска, стабильный ID события, инициатор"]
  publish["DeploymentPublisher.publish<br/>POST /api/deployments"]
  map --> build --> publish
```

```text
event / send
  ├─ mappers: context_from_environment
  └─ services: DeploymentEventBuilder, DeploymentPublisher
```
