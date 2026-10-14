"""Агент inventory GMan (только чтение). Запускается таймером systemd/cron или с --watch.

Из репозитория: python3 agent/src/collect.py ...; на хостах — сборка одним файлом (agent/scripts/build_pyz.py):
python3 gman-agent.pyz ... (те же аргументы).

Docker: python3 agent/src/collect.py --runtime docker --source-id server-01
K8s: python3 agent/src/collect.py --runtime kubernetes --source-id cluster-01 (pod-ы, узлы, Events pod-ов и узлов)
GPU-узел K8s, запуск на узле в PID namespace хоста (процессы GPU относятся к pod-ам):
  python3 agent/src/collect.py --runtime kubernetes-node --cluster cluster-01 --node gpu-1 --source-id cluster-01.gpu-1
По умолчанию печатает очищенный JSON; --send отправляет его в GMAN_URL (у API нет аутентификации).
С --send снимки идут через ограниченный spool на диске (старые первыми, повторы с backoff), а неблокирующий
flock не дает запускам перекрываться. --watch следит за `docker events` (Docker) или watch API Kubernetes
(pod-ы и Events: list + watch с resourceVersion, новый list на 410 Gone) и отправляет снимок с debounce после
изменений, поэтому короткоживущие контейнеры/pod-ы и OOM-kill Docker тоже попадают в отчет.
Docker без --watch (таймер) читает события `oom` с прошлого запуска через `docker events --since/--until`,
поэтому OOM-kill контейнера, перезапущенного политикой перезапуска, тоже сообщается.
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

from collection.container import build_collection_orchestrator, collection_target
from collection.orchestrator import CollectionOrchestrator
from config.logging import configure_logging
from config.settings import AgentSettings, load_settings
from delivery.container import DeliveryOptions, build_delivery_orchestrator
from delivery.entity import SubmissionPendingError
from delivery.orchestrator import DeliveryOrchestrator
from runtime_access.connection import RuntimeAccess, connect_runtime
from watching.container import build_docker_watching, build_kubernetes_watching
from watching.entity import WatchTimings

logger = logging.getLogger(__name__)

RUNTIMES = ("docker", "kubernetes", "kubernetes-node")
DOCKER, KUBERNETES, KUBERNETES_NODE = RUNTIMES
SOURCE_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}")
DEFAULT_SPOOL_FILES = 100
DEFAULT_SPOOL_BYTES = 50 * 1024 ** 2
DEFAULT_RETRIES = 3
DEFAULT_WATCH_TIMEOUT = 300
JSON_INDENT = 2


def build_parser(settings: AgentSettings) -> argparse.ArgumentParser:
    """Аргументы командной строки; узел и кластер по умолчанию — из окружения (unit systemd их не передает)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime", choices=RUNTIMES, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--node", default=settings.node_name,
                        help="kubernetes-node: имя этого узла в Kubernetes (по умолчанию NODE_NAME)")
    parser.add_argument("--cluster", default=settings.cluster,
                        help="kubernetes-node: --source-id агента inventory кластера (по умолчанию GMAN_CLUSTER)")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--watch", action="store_true",
                        help="следить за docker events или watch API Kubernetes (нужен --send; docker, kubernetes)")
    parser.add_argument("--state-dir", help="каталог spool и блокировок (по умолчанию GMAN_AGENT_STATE_DIR, "
                                            "/var/lib/gman-agent под root, иначе ~/.local/state/gman-agent)")
    parser.add_argument("--spool-max-files", type=int, default=DEFAULT_SPOOL_FILES)
    parser.add_argument("--spool-max-bytes", type=int, default=DEFAULT_SPOOL_BYTES)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="повторы доставки с backoff")
    parser.add_argument("--debounce", type=float, default=WatchTimings.debounce,
                        help="--watch: секунд тишины перед снимком")
    parser.add_argument("--max-delay", type=float, default=WatchTimings.max_delay,
                        help="--watch: дольше событие не ждет")
    parser.add_argument("--interval", type=float, default=WatchTimings.interval,
                        help="--watch: полный снимок не реже")
    parser.add_argument("--watch-timeout", type=int, default=DEFAULT_WATCH_TIMEOUT,
                        help="--watch kubernetes: через сколько секунд сервер завершает watch (он продолжается)")
    return parser


def parse_arguments(argv: list[str] | None, settings: AgentSettings) -> argparse.Namespace:
    """Аргументы с проверкой сочетаний; ошибка — выход с сообщением argparse."""
    parser = build_parser(settings)
    args = parser.parse_args(argv)
    if not SOURCE_ID.fullmatch(args.source_id) or not args.source_id.strip("."):
        parser.error("invalid --source-id")
    if args.runtime == KUBERNETES_NODE and not (args.node and args.cluster):
        parser.error("--runtime kubernetes-node needs --node (or NODE_NAME) and --cluster")
    if args.watch and (args.runtime not in (DOCKER, KUBERNETES) or not args.send):
        parser.error("--watch requires --runtime docker or kubernetes and --send")
    return args


def main(argv: list[str] | None = None, settings: AgentSettings | None = None,
         runtime: RuntimeAccess | None = None) -> None:
    """Собирает контейнеры модулей и выполняет режим: печать, запуск по таймеру или --watch."""
    resolved = settings or load_settings()
    args = parse_arguments(argv, resolved)
    access = runtime or connect_runtime(resolved)
    state_dir = Path(args.state_dir or resolved.state_dir)
    target = collection_target(resolved, args.runtime, args.source_id, args.node, args.cluster, state_dir)
    collection = build_collection_orchestrator(resolved, target, access)
    if not args.send:
        print(json.dumps(collection.snapshot(), indent=JSON_INDENT))
        return
    # Конфигурация доставки проверяется до сбора чего-либо.
    options = DeliveryOptions(source_id=args.source_id, state_dir=state_dir, spool_max_files=args.spool_max_files,
                              spool_max_bytes=args.spool_max_bytes, retries=args.retries)
    delivery = build_delivery_orchestrator(resolved, options)
    if not delivery.acquire_lock():
        logger.info("another run for this source holds the lock; skipping")
        return
    _send(args, collection, delivery, access)


def _send(args: argparse.Namespace, collection: CollectionOrchestrator, delivery: DeliveryOrchestrator,
          access: RuntimeAccess) -> None:
    """--watch, запуск Docker по таймеру (с OOM из событий) или прочий запуск по таймеру."""
    timings = WatchTimings(debounce=args.debounce, max_delay=args.max_delay, interval=args.interval)
    if args.watch and args.runtime == KUBERNETES:
        watching = build_kubernetes_watching(collection, access, timings, args.watch_timeout)
        watching.watch(delivery.submit)
    elif args.watch:
        watching = build_docker_watching(collection, access, timings)
        watching.watch(delivery.submit)
    elif args.runtime == DOCKER:
        delivery.run_once(collection.docker_snapshot_with_ooms)
    else:
        delivery.run_once(collection.snapshot)


def run() -> None:
    """Точка входа процесса: неудачный сбор или отправка — ненулевой код без подробностей из данных."""
    configure_logging()
    try:
        main()
    except SystemExit:
        raise
    except SubmissionPendingError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:  # noqa: BLE001 — граница процесса: в сообщение идет только тип ошибки
        # Неудачный сбор никогда не отправляется пустым «авторитетным» снимком.
        logger.debug("collection/submission failed", exc_info=True)
        raise SystemExit("Collection/submission failed: " + type(error).__name__) from None


if __name__ == "__main__":
    sys.exit(run())
