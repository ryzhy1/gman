"""Обработка непредвиденных ошибок на HTTP-границе."""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

INTERNAL_ERROR_STATUS = 500


async def _internal_error(request: Request, error: Exception) -> JSONResponse:
    """Не раскрывает в ответе параметры SQL, учетные данные и inventory; подробности — в логе."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=error)
    response = JSONResponse(status_code=INTERNAL_ERROR_STATUS, content={"detail": "Internal error"})
    return response


def register_error_handlers(app: FastAPI) -> None:
    """Подключает обработчик непредвиденных ошибок."""
    app.add_exception_handler(Exception, _internal_error)
