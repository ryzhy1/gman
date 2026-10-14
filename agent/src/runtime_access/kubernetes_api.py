"""Доступ к API Kubernetes: kubectl, если установлен, иначе API изнутри кластера с service account pod-а."""
import json
import ssl
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime_access.command_runner import CommandRunner

JsonObject = dict[str, Any]
GONE = 410
REQUEST_TIMEOUT_SECONDS = 60
# Запас сверх timeoutSeconds watch: сервер сам завершает watch, клиент не должен оборвать его раньше.
STREAM_TIMEOUT_MARGIN_SECONDS = 60
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
IPV6_SEPARATOR = ":"


class KubernetesGoneError(Exception):
    """410 Gone: resourceVersion watch устарел (сжат); восстановиться можно только новым list."""


@dataclass(frozen=True)
class InClusterAccess:
    """Адрес API изнутри кластера и каталог service account (для DaemonSet без kubectl)."""

    host: str
    port: str
    service_account: Path = SERVICE_ACCOUNT


Opener = Callable[..., Any]
ContextFactory = Callable[..., ssl.SSLContext]


class KubernetesApi:
    """GET и watch ресурсов Kubernetes."""

    def __init__(self, runner: CommandRunner, in_cluster: InClusterAccess, opener: Opener = urlopen,
                 context_factory: ContextFactory = ssl.create_default_context,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        """Запоминает запуск kubectl, доступ изнутри кластера и низкоуровневые функции HTTP/процессов."""
        self._runner = runner
        self._in_cluster = in_cluster
        self._opener = opener
        self._context_factory = context_factory
        self._popen = popen

    def items(self, kubectl_args: list[str], api_path: str) -> list[JsonObject]:
        """Объекты списка ресурсов (`kubectl get ... -o json` или GET пути API)."""
        listing = self._get([*kubectl_args, "-o", "json"], api_path)
        items = listing["items"]
        return items

    def raw(self, api_path: str) -> JsonObject:
        """GET пути API; в отличие от `kubectl get -o json` сохраняет resourceVersion списка для watch."""
        listing = self._get(["get", "--raw", api_path], api_path)
        return listing

    def stream(self, api_path: str, timeout: int) -> Iterator[JsonObject]:
        """События watch пути API (JSON по строке) через kubectl или изнутри кластера."""
        try:
            process = self._popen(["kubectl", "get", "--raw", api_path], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            yield from self._stream_in_cluster(api_path, timeout)
            return
        yield from _kubectl_lines(process)

    def _get(self, kubectl_args: list[str], api_path: str) -> JsonObject:
        """kubectl, если установлен; иначе API изнутри кластера."""
        try:
            output = self._runner.output(["kubectl", *kubectl_args])
        except FileNotFoundError:
            output = None
        if output is not None:
            document = json.loads(output)
            return document
        request, context = self._request(api_path)
        with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
            document = json.loads(response.read())
        return document

    def _stream_in_cluster(self, api_path: str, timeout: int) -> Iterator[JsonObject]:
        """Watch через API изнутри кластера; 410 — KubernetesGoneError."""
        request, context = self._request(api_path)
        try:
            response = self._opener(request, timeout=timeout + STREAM_TIMEOUT_MARGIN_SECONDS, context=context)
        except HTTPError as error:
            if error.code == GONE:
                raise KubernetesGoneError() from None
            raise
        with response:
            for line in response:
                if line.strip():
                    yield json.loads(line)

    def _request(self, api_path: str) -> tuple[Request, ssl.SSLContext]:
        """Запрос с токеном service account и TLS-контекст CA кластера; без адреса API — FileNotFoundError."""
        access = self._in_cluster
        if not access.host:
            raise FileNotFoundError("kubectl")
        token = (access.service_account / "token").read_text().strip()
        context = self._context_factory(cafile=str(access.service_account / "ca.crt"))
        host = f"[{access.host}]" if IPV6_SEPARATOR in access.host else access.host
        request = Request(f"https://{host}:{access.port}{api_path}", headers={"Authorization": "Bearer " + token})
        prepared = (request, context)
        return prepared


def _kubectl_lines(process: subprocess.Popen) -> Iterator[JsonObject]:
    """События из вывода `kubectl get --raw`; ненулевой код (в том числе на 410) — CalledProcessError."""
    try:
        for line in process.stdout:
            if line.strip():
                yield json.loads(line)
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, "kubectl get --raw")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
