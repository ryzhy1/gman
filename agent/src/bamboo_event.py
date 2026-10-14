"""Входящий порт для Bamboo: явное утверждение о деплое из script task deployment job.

Запуск: python3 agent/src/bamboo_event.py (переменные — см. deployment_report/mappers.py). Мониторинг
не должен ломать деплои: неудачная отправка печатает предупреждение и завершается с кодом 0, если не задан
GMAN_REQUIRED=true.
"""
import logging
import os
from collections.abc import Mapping

from config.logging import configure_logging
from config.settings import load_settings
from deployment_report.container import build_deployment_report

logger = logging.getLogger(__name__)


def main(environ: Mapping[str, str] = os.environ) -> None:
    """Отправляет событие деплоя; при сбое — предупреждение или ненулевой код (GMAN_REQUIRED=true)."""
    configure_logging()
    settings = load_settings(environ)
    try:
        orchestrator = build_deployment_report(settings)
        status = orchestrator.send(environ)
    except Exception as error:  # noqa: BLE001 — граница процесса: мониторинг не должен ломать деплой
        message = f"GMan deployment event submission failed: {type(error).__name__}: {error}"
        if settings.deployment_required:
            raise SystemExit(message) from error
        logger.warning("WARNING: %s", message)
        return
    print("GMan deployment event:", status)


if __name__ == "__main__":
    main()
