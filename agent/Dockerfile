# Inventory agent for the Docker host GMan runs on (compose service `agent`): reads the host's Docker socket.
FROM docker:28.5-cli AS docker
FROM python:3.12-slim
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY collect.py bamboo_event.py ./
COPY gman_agent ./gman_agent
ENV PYTHONUNBUFFERED=1 GMAN_AGENT_STATE_DIR=/var/lib/gman-agent
ENTRYPOINT ["python", "/app/collect.py"]
CMD ["--runtime", "docker", "--source-id", "gman-server", "--send", "--watch"]
