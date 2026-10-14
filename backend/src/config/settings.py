"""Настройки GMan API: читаются из переменных окружения через pydantic-settings."""
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "sqlite:///./gman.db"
DEFAULT_METRICS_INTERVAL_SECONDS = 5
MIN_METRICS_INTERVAL_SECONDS = 1
# Источник, молчащий дольше 5 интервалов агента (как GManSourceSilent), оставляет разрыв в истории.
DEFAULT_HISTORY_GAP_SECONDS = 300.0
DEFAULT_RETENTION_DAYS = 180
DEFAULT_RELEASE_TIMEOUT_MINUTES = 30.0
DEFAULT_RELEASE_LEAD_MINUTES = 10.0
DEFAULT_RELEASE_CRASH_RESTARTS = 3
DEFAULT_LOG_LEVEL = "INFO"
URL_PATH_SEPARATOR = "/"


class Settings(BaseSettings):
    """Конфигурация процесса API."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: str = Field(default=DEFAULT_DATABASE_URL, validation_alias="DATABASE_URL")
    prometheus_url: str = Field(default="", validation_alias="PROMETHEUS_URL")
    alertmanager_url: str = Field(default="", validation_alias="ALERTMANAGER_URL")
    # Адрес Grafana, как его видит браузер: портал строит по нему ссылки на дашборды.
    grafana_url: str = Field(default="", validation_alias="GMAN_GRAFANA_URL")
    metrics_interval_seconds: int = Field(default=DEFAULT_METRICS_INTERVAL_SECONDS,
                                          validation_alias="GMAN_METRICS_INTERVAL")
    history_gap_seconds: float = Field(default=DEFAULT_HISTORY_GAP_SECONDS,
                                       validation_alias="GMAN_HISTORY_GAP_SECONDS")
    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, validation_alias="GMAN_RETENTION_DAYS")
    release_timeout_minutes: float = Field(default=DEFAULT_RELEASE_TIMEOUT_MINUTES,
                                           validation_alias="GMAN_RELEASE_TIMEOUT_MINUTES")
    release_lead_minutes: float = Field(default=DEFAULT_RELEASE_LEAD_MINUTES,
                                        validation_alias="GMAN_RELEASE_LEAD_MINUTES")
    release_crash_restarts: int = Field(default=DEFAULT_RELEASE_CRASH_RESTARTS,
                                        validation_alias="GMAN_RELEASE_CRASH_RESTARTS")
    splunk_url: str = Field(default="", validation_alias="GMAN_SPLUNK_URL")
    splunk_search: str = Field(default="", validation_alias="GMAN_SPLUNK_SEARCH")
    # JSON-объект «окружение -> индекс»; разбирается при сборке контейнера, ошибка отключает ссылки.
    splunk_indexes: str = Field(default="", validation_alias="GMAN_SPLUNK_INDEXES")
    tls_watch_files: str = Field(default="", validation_alias="GMAN_TLS_WATCH_FILES")
    log_level: str = Field(default=DEFAULT_LOG_LEVEL, validation_alias="GMAN_LOG_LEVEL")

    @field_validator("metrics_interval_seconds", mode="before")
    @classmethod
    def _lenient_interval(cls, value: object) -> int:
        """Приводит интервал метрик к целым секундам; некорректное значение заменяется значением по умолчанию."""
        try:
            seconds = max(MIN_METRICS_INTERVAL_SECONDS, int(float(str(value))))
        except ValueError:
            seconds = DEFAULT_METRICS_INTERVAL_SECONDS
        return seconds

    @field_validator("prometheus_url", "alertmanager_url", "grafana_url")
    @classmethod
    def _without_trailing_slash(cls, value: str) -> str:
        """Убирает завершающий слеш, чтобы пути API присоединялись без двойного слеша."""
        trimmed = value.rstrip(URL_PATH_SEPARATOR)
        return trimmed

    @field_validator("splunk_url")
    @classmethod
    def _stripped(cls, value: str) -> str:
        """Убирает пробелы вокруг адреса Splunk."""
        stripped = value.strip()
        return stripped
