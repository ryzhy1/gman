"""Dotenv-файлы рабочего каталога контейнера, которые приложение читает само при старте.

Их переменные не попадают в окружение контейнера (godotenv, python-dotenv, dotenv для Node...), поэтому
файлы читаются из рабочего каталога контейнера.
"""
import re

from runtime_access.docker_cli import DockerCli

ENV_FILES = (".env", ".env.local", ".env.development", ".env.dev", ".env.production", ".env.prod", ".env.staging",
             ".env.test")
ENV_FILE_MAX_BYTES = 256 * 1024
CACHE_LIMIT = 1000
DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(.*?)\s*$")
QUOTES = "\"'"
QUOTED_MIN_LENGTH = 2
INLINE_COMMENT = " #"
PATH_SEPARATOR = "/"

EnvFiles = dict[str, list[str]]


class EnvFileReader:
    """Читает dotenv-файлы; работающий контейнер читается один раз на запуск (приложение читает их при старте)."""

    def __init__(self, docker: DockerCli) -> None:
        """Запоминает Docker CLI; кеш — (ID контейнера, StartedAt) -> файлы."""
        self._docker = docker
        self._cache: dict[tuple[str, str], EnvFiles] = {}

    def env_files(self, container_id: str, started_at: str, working_dir: str) -> EnvFiles:
        """{путь: ["NAME=value", ...]} найденных файлов."""
        key = (container_id, started_at)
        if key in self._cache:
            cached = self._cache[key]
            return cached
        directory = working_dir.rstrip(PATH_SEPARATOR)
        found = {}
        for name in ENV_FILES:
            path = f"{directory}/{name}"
            text = self._docker.read_file(container_id, path, ENV_FILE_MAX_BYTES)
            if text is not None:
                found[path] = parse_dotenv(text)
        if len(self._cache) > CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = found
        return found


def parse_dotenv(text: str) -> list[str]:
    """"NAME=value" на каждое присваивание по порядку файла; комментарии, пустые строки и кавычки отбрасываются."""
    result = []
    for line in text.splitlines():
        match = DOTENV_LINE.match(line)
        if not match:
            continue
        name, value = match[1], match[2]
        if len(value) >= QUOTED_MIN_LENGTH and value[0] == value[-1] and value[0] in QUOTES:
            value = value[1:-1]
        elif INLINE_COMMENT in value:
            value = value.split(INLINE_COMMENT, 1)[0].rstrip()
        result.append(f"{name}={value}")
    return result
