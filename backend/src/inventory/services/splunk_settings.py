"""Настройки ссылок Splunk из конфигурации; имена индексов и полей не согласованы, поэтому все настраивается.

- GMAN_SPLUNK_URL: страница поиска для браузера; пусто — ссылок нет.
- GMAN_SPLUNK_SEARCH: шаблон SPL с {плейсхолдерами}; иначе DEFAULT_SEARCH.
- GMAN_SPLUNK_INDEXES: JSON-объект окружение -> индекс; ключ "*" — запасной.
"""
import json
import re
from urllib.parse import urlsplit

from inventory.entity import SplunkSettings, SplunkTemplateError
from inventory.services.splunk_query import SplunkQuery

DEFAULT_SEARCH = ("search index={index} gman_service={service_id} gman_environment={environment} "
                  "host={host} container_id={container_id}")
INDEX_NAME = re.compile(r"^[A-Za-z0-9_*.:-]{1,200}$")
HTTP_SCHEMES = ("http", "https")
INDEX_PREVIEW_LIMIT = 100


class SplunkSettingsParser:
    """Проверяет и разбирает настройки ссылок Splunk."""

    def __init__(self, query: SplunkQuery) -> None:
        """Запоминает проверку шаблонов поиска."""
        self._query = query

    def parse(self, url: str, search: str, indexes: str) -> SplunkSettings | None:
        """Настройки или None, если ссылки выключены; некорректная конфигурация — SplunkTemplateError."""
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.scheme not in HTTP_SCHEMES or not parsed.netloc or parsed.username or parsed.password:
            raise SplunkTemplateError("GMAN_SPLUNK_URL must be an HTTP(S) URL without credentials")
        try:
            index_map = json.loads(indexes or "{}")
        except json.JSONDecodeError as error:
            raise SplunkTemplateError("GMAN_SPLUNK_INDEXES is not valid JSON") from error
        template = self._query.check_template(search or DEFAULT_SEARCH)
        settings = SplunkSettings(url=url, search=template, indexes=_checked_indexes(index_map))
        return settings


def _checked_indexes(value: object) -> dict[str, str]:
    """Объект строк с допустимыми именами индексов."""
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise SplunkTemplateError("GMAN_SPLUNK_INDEXES must be a JSON object of strings")
    for index in value.values():
        if not INDEX_NAME.match(index):
            raise SplunkTemplateError("Invalid index name: " + index[:INDEX_PREVIEW_LIMIT])
    return value
