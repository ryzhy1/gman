# Агент inventory хоста, на котором работает GMan (сервис Compose `agent`): читает сокет Docker хоста.
# Сторонних зависимостей у агента нет (только stdlib), поэтому в образ копируется только src.
FROM docker:28.5-cli AS docker
FROM python:3.12-slim
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY src ./
ENV PYTHONUNBUFFERED=1 GMAN_AGENT_STATE_DIR=/var/lib/gman-agent
ENTRYPOINT ["python", "/app/collect.py"]
CMD ["--runtime", "docker", "--source-id", "gman-server", "--send", "--watch"]
