"""Событие деплоя для GMan API: ID запуска, стабильный ID события и инициатор."""
import re

from deployment_report.entity import UNKNOWN, DeploymentContext, Initiator, JsonObject, MissingDeploymentFieldsError

DEPLOYMENT_RESULT = re.compile(r"[?&]deploymentResultId=(\d+)")
UNSAFE_ID_CHARACTERS = re.compile(r"[^a-zA-Z0-9_.:-]")
SAFE_ID_PUNCTUATION = "_.:-"
REPLACEMENT = "-"
RUN_ID_LIMIT = 150
EVENT_ID_LIMIT = 200
RUN_ID_PREFIX = "bamboo-deploy-"
EVENT_ID_PREFIX = "bamboo"
SCHEDULED = "Scheduled"
MANUAL = "Manual"
SCHEDULE_ACTOR = "bamboo-schedule"
HUMAN = "human"
SCHEDULE = "schedule"


class DeploymentEventBuilder:
    """Строит тело события деплоя по сведениям шага Bamboo."""

    def build(self, context: DeploymentContext) -> JsonObject:
        """Событие; без сервиса, статуса, окружения и сборки — MissingDeploymentFieldsError."""
        if not (context.status and context.environment and context.build_id and context.service_id):
            raise MissingDeploymentFieldsError(
                "GMAN_SERVICE_ID, GMAN_DEPLOYMENT_STATUS, environment and build ID are required")
        run = UNSAFE_ID_CHARACTERS.sub(REPLACEMENT, self.deployment_id(context))[:RUN_ID_LIMIT]
        initiator = self.initiator(context)
        event = {"event_id": _safe_event_id(context.event_id or _default_event_id(context, run)),
                 "deployment_id": run, "service_id": context.service_id, "environment": context.environment,
                 "build_id": context.build_id, "build_url": context.results_url, "release": context.release,
                 "version": context.version, "status": context.status, "actor": initiator.actor,
                 "actor_kind": initiator.kind, "commit": context.commit, "image": context.image,
                 "image_digest": context.image_digest}
        if context.occurred_at:
            # Иначе сервер берет время приема.
            event["occurred_at"] = context.occurred_at
        return event

    def deployment_id(self, context: DeploymentContext) -> str:
        """Один запуск деплоя Bamboo: deploymentResultId из адреса результатов деплоя."""
        if context.deployment_id:
            return context.deployment_id
        found = DEPLOYMENT_RESULT.search(context.results_url)
        run = RUN_ID_PREFIX + found.group(1) if found else ""
        return run

    def initiator(self, context: DeploymentContext) -> Initiator:
        """Инициатор: triggerReason.key документирован для деплоев, имя пользователя ручного запуска —
        только для сборок, поэтому ему верят только при ручном запуске."""
        if context.actor:
            initiator = Initiator(actor=context.actor, kind=context.actor_kind or HUMAN)
        elif SCHEDULED in context.trigger_reason:
            initiator = Initiator(actor=SCHEDULE_ACTOR, kind=context.actor_kind or SCHEDULE)
        elif context.bamboo_user and (not context.trigger_reason or MANUAL in context.trigger_reason):
            initiator = Initiator(actor=context.bamboo_user, kind=context.actor_kind or HUMAN)
        else:
            # Автоматический запуск (например, после успешной сборки): имя пользователя, если есть, — сборки.
            initiator = Initiator(actor=UNKNOWN, kind=context.actor_kind or UNKNOWN)
        return initiator


def _default_event_id(context: DeploymentContext, run: str) -> str:
    """Стабилен для повторов одного шага и различен для started/succeeded/failed.

    С ID результата деплоя передеплой того же релиза — новое событие, а не дубликат.
    """
    if run:
        event_id = f"{EVENT_ID_PREFIX}:{run}:{context.status}"
        return event_id
    parts = (EVENT_ID_PREFIX, context.build_id, context.release, context.environment, context.status)
    event_id = ":".join(filter(None, parts))
    return event_id


def _safe_event_id(value: str) -> str:
    """ID события из допустимых символов, не длиннее предела."""
    safe = "".join(ch if ch.isalnum() or ch in SAFE_ID_PUNCTUATION else REPLACEMENT for ch in value)
    limited = safe[:EVENT_ID_LIMIT]
    return limited
