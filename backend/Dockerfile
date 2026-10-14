# GMan API. Зависимости ставит Poetry в сборочном слое; в образ попадает только готовое окружение.
FROM python:3.12-slim AS build
ENV POETRY_VIRTUALENVS_IN_PROJECT=true POETRY_NO_INTERACTION=1 PIP_NO_CACHE_DIR=1
RUN pip install "poetry==2.5.1"
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
USER 65534:65534
# Схему мигрирует само приложение при старте (lifespan) под advisory-блокировкой.
CMD ["uvicorn", "main:create_app", "--factory", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "100"]
