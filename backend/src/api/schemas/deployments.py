"""HTTP-модель события деплоя из Bamboo (agent/src/bamboo_event.py)."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from .validators import Strict, http_url, optional_not_future

EVENT_ID_PATTERN = r"^[a-zA-Z0-9_.:-]{1,200}$"
DEPLOYMENT_ID_PATTERN = r"^[a-zA-Z0-9_.:-]{0,200}$"
SHORT_TEXT = 100
MEDIUM_TEXT = 200
NAME_TEXT = 300
IMAGE_TEXT = 1000
URL_TEXT = 2000
UNKNOWN = "unknown"


class Deployment(Strict):
    """Утверждение Bamboo о деплое сервиса в окружение."""

    event_id: str = Field(pattern=EVENT_ID_PATTERN)
    service_id: str = Field(max_length=SHORT_TEXT)
    environment: str = Field(max_length=SHORT_TEXT)
    # Необязательно: повторы того же шага CI должны присылать идентичный запрос.
    occurred_at: datetime | None = None
    actor: str = Field(default=UNKNOWN, max_length=NAME_TEXT)
    actor_kind: Literal["human", "service-account", "schedule", "unknown"] = UNKNOWN
    status: Literal["started", "succeeded", "failed"]
    commit: str = Field(default="", max_length=MEDIUM_TEXT)
    image_digest: str = Field(default="", max_length=IMAGE_TEXT)
    # Образ и версия артефакта, которые раскатывает деплой: по ним деплой сопоставляется с runtime.
    image: str = Field(default="", max_length=IMAGE_TEXT)
    version: str = Field(default="", max_length=NAME_TEXT)
    release: str = Field(default="", max_length=NAME_TEXT)
    # Один запуск деплоя Bamboo: у started/succeeded/failed одного запуска он общий.
    deployment_id: str = Field(default="", pattern=DEPLOYMENT_ID_PATTERN)
    build_id: str = Field(max_length=NAME_TEXT)
    build_url: str = Field(default="", max_length=URL_TEXT)

    _url = field_validator("build_url")(http_url)
    _time = field_validator("occurred_at")(optional_not_future)
