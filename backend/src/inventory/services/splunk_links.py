"""Ссылки на поиск Splunk для сервиса каталога и его экземпляров."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from inventory.entity import LogTarget, SplunkLinks, SplunkOverride, SplunkSettings
from inventory.services.splunk_query import SplunkQuery

TIME_RANGES = {"1h": "-1h", "24h": "-24h", "7d": "-7d"}
DEFAULT_RANGE = "1h"
LATEST = "now"
FALLBACK_INDEX = "*"
DOCKER = "docker"
KUBERNETES = "kubernetes"
POD_SEPARATOR = "/"
REPLACED_PARAMETERS = ("q", "earliest", "latest")
MISCONFIGURED = "Splunk links are misconfigured"


class SplunkLinkBuilder:
    """Строит ссылки по настройкам, разобранным при старте; ошибка настроек выключает ссылки."""

    def __init__(self, query: SplunkQuery, settings: SplunkSettings | None, config_error: str | None) -> None:
        """Запоминает сборку запросов, настройки и ошибку конфигурации (если была)."""
        self._query = query
        self._settings = settings
        self._config_error = config_error

    def check_override(self, override: SplunkOverride) -> None:
        """Шаблон из карточки сервиса допустим; иначе SplunkTemplateError."""
        if override.query:
            self._query.check_template(override.query)

    def links(self, service_id: str, override: SplunkOverride, targets: list[LogTarget], environment: str,
              range_name: str) -> SplunkLinks:
        """Ссылка сервиса и по ссылке на каждый его экземпляр (в окружении, если оно задано)."""
        if self._config_error is not None:
            disabled = SplunkLinks(enabled=False, service=None, instances={}, error=MISCONFIGURED)
            return disabled
        if self._settings is None:
            disabled = SplunkLinks(enabled=False, service=None, instances={})
            return disabled
        service_link = self.link(self._settings, service_id, override, environment, None, range_name)
        instances = {}
        for target in targets:
            if target.service_id != service_id or (environment and target.environment != environment):
                continue
            instances[target.key] = self.link(self._settings, service_id, override, target.environment, target,
                                              range_name)
        links = SplunkLinks(enabled=True, service=service_link, instances=instances)
        return links

    def link(self, settings: SplunkSettings, service_id: str, override: SplunkOverride, environment: str,
             target: LogTarget | None, range_name: str = DEFAULT_RANGE) -> str:
        """Адрес поиска для сервиса, при `target` — суженный до одного экземпляра."""
        earliest = TIME_RANGES[range_name]
        values = _placeholder_values(service_id, environment, target, earliest)
        values["index"] = _index_for(settings, override.index, environment)
        search = self._query.render(override.query or settings.search, values)
        parts = urlsplit(settings.url)
        query = [(key, value) for key, value in parse_qsl(parts.query) if key not in REPLACED_PARAMETERS]
        query += [("q", search), ("earliest", earliest), ("latest", LATEST)]
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        return url


def _placeholder_values(service_id: str, environment: str, target: LogTarget | None,
                        earliest: str) -> dict[str, str]:
    """Значения плейсхолдеров: у Docker — ID контейнера, у Kubernetes — namespace и pod."""
    docker = target is not None and target.runtime == DOCKER
    kubernetes = target is not None and target.runtime == KUBERNETES
    name = target.name if target is not None else ""
    values = {"service_id": service_id, "environment": environment, "instance": name,
              "container_id": target.instance_id if docker else "",
              "host": target.host if target is not None else "",
              "namespace": target.namespace if kubernetes else "",
              "pod": name.split(POD_SEPARATOR)[0] if kubernetes else "", "earliest": earliest, "latest": LATEST}
    return values


def _index_for(settings: SplunkSettings, override: str | dict[str, str], environment: str) -> str:
    """Индекс: строка из карточки, иначе по окружению из карточки, затем из настроек (с запасным "*")."""
    if isinstance(override, str) and override:
        return override
    mappings = (override if isinstance(override, dict) else {}, settings.indexes)
    for mapping in mappings:
        index = mapping.get(environment) or mapping.get(FALLBACK_INDEX)
        if index:
            return index
    return ""
