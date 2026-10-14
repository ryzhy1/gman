"""Запуск внешних команд (docker, kubectl, nvidia-smi, sysctl) без вывода их stderr."""
import subprocess

DEFAULT_TIMEOUT_SECONDS = 60


class CommandRunner:
    """Выполняет команды; stderr подавляется — в нем бывают настройки CLI и контекстов с секретами."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Запоминает тайм-аут команд, ожидающих завершения."""
        self._timeout = timeout

    def output(self, args: list[str]) -> str:
        """stdout завершившейся команды; ненулевой код — CalledProcessError."""
        raw = subprocess.check_output(args, timeout=self._timeout, stderr=subprocess.DEVNULL)
        text = raw.decode()
        return text

    def run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        """Команда с бинарным stdout и кодом возврата (без исключения на ненулевой код)."""
        completed = subprocess.run(args, capture_output=True, timeout=timeout)
        return completed

    def stream(self, args: list[str]) -> subprocess.Popen:
        """Долгая команда, чей stdout читается построчно по мере вывода."""
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return process
