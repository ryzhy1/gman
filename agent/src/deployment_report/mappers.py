"""Переменные окружения шага Bamboo -> сведения о деплое; событие -> тело запроса API.

Переменные GMAN_* переопределяют значения по умолчанию. Без них используются переменные, которые Bamboo
экспортирует script task: каждая точка становится подчеркиванием (bamboo.deploy.environment ->
$bamboo_deploy_environment). Документированные переменные деплоя и релиза
(https://confluence.atlassian.com/bamboo/bamboo-variables-289277087.html): deploy.environment, deploy.release
(синоним deploy.version), resultsUrl, triggerReason.key, buildResultKey и planRepository.revision.
ManualBuildTriggerReason.userName документирована только для сборок: проверьте ее на своей версии Bamboo
командой `env | grep ^bamboo_` в тестовой задаче.
"""
from collections.abc import Mapping

from deployment_report.entity import DeploymentContext

OVERRIDE_PREFIX = "GMAN_"
BAMBOO_PREFIX = "bamboo_"


def context_from_environment(environ: Mapping[str, str]) -> DeploymentContext:
    """Сведения о деплое из окружения шага."""
    def variable(name: str, *bamboo: str) -> str:
        """Значение GMAN_<name>, иначе первой непустой переменной Bamboo."""
        for key in (OVERRIDE_PREFIX + name, *(BAMBOO_PREFIX + alias for alias in bamboo)):
            value = environ.get(key, "")
            if value:
                return value
        return ""

    context = DeploymentContext(
        service_id=variable("SERVICE_ID"), status=variable("DEPLOYMENT_STATUS"),
        environment=variable("ENVIRONMENT", "deploy_environment"), build_id=variable("BUILD_ID", "buildResultKey"),
        release=variable("RELEASE", "deploy_release", "deploy_version"), version=variable("VERSION"),
        results_url=variable("BUILD_URL", "resultsUrl"),
        commit=variable("COMMIT", "planRepository_revision", "planRepository_1_revision"), image=variable("IMAGE"),
        image_digest=variable("IMAGE_DIGEST"), event_id=variable("EVENT_ID"),
        deployment_id=variable("DEPLOYMENT_ID"), occurred_at=variable("OCCURRED_AT"), actor=variable("ACTOR"),
        actor_kind=variable("ACTOR_KIND"), trigger_reason=variable("TRIGGER_REASON", "triggerReason_key"),
        bamboo_user=variable("BAMBOO_USER", "ManualBuildTriggerReason_userName"))
    return context
