"""HTTP-модели каталога сервисов."""
import re

from pydantic import Field, field_validator

from .validators import Strict, http_url

ID_PATTERN = r"^[a-zA-Z0-9_.-]{1,100}$"
SPLUNK_INDEX_NAME = re.compile(r"^[A-Za-z0-9_*.:-]{1,200}$")
NAME_LENGTH = 200
TEXT_LENGTH = 2000


class SplunkLogs(Strict):
    """Переопределение индекса и шаблона поиска Splunk для сервиса (шаблон проверяет домен)."""

    index: str | dict[str, str] = ""
    query: str = Field(default="", max_length=TEXT_LENGTH)

    @field_validator("index")
    @classmethod
    def _index_names(cls, value: str | dict[str, str]) -> str | dict[str, str]:
        """Имена индексов — идентификаторы Splunk, без кавычек и команд."""
        names = [value] if isinstance(value, str) else list(value.values())
        for name in names:
            if name and not SPLUNK_INDEX_NAME.match(name):
                raise ValueError("Invalid index name")
        return value


class Logs(Strict):
    """Настройки логов сервиса."""

    splunk: SplunkLogs | None = None


class Service(Strict):
    """Сервис каталога."""

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=NAME_LENGTH)
    description: str = Field(default="", max_length=TEXT_LENGTH)
    owner: str = Field(default="", max_length=NAME_LENGTH)
    team: str = Field(default="", max_length=NAME_LENGTH)
    repository: str = Field(default="", max_length=TEXT_LENGTH)
    grafana: str = Field(default="", max_length=TEXT_LENGTH)
    splunk: str = Field(default="", max_length=TEXT_LENGTH)
    bamboo: str = Field(default="", max_length=TEXT_LENGTH)
    logs: Logs | None = None

    _urls = field_validator("repository", "grafana", "splunk", "bamboo")(http_url)
