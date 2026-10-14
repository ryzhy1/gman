"""Шаблон поиска SPL: проверка плейсхолдеров и подстановка значений как строк SPL в кавычках.

Каждое подставленное значение — строка SPL в кавычках, поэтому значение не может закрыть кавычку или добавить
команды поиска. Токен шаблона (через пробел), у плейсхолдера которого нет значения, отбрасывается: один шаблон
служит и сервису, и его экземплярам.
"""
import re

from inventory.entity import SplunkTemplateError

PLACEHOLDERS = {"index", "service_id", "environment", "instance", "container_id", "host", "namespace", "pod",
                "earliest", "latest"}
PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
TOKEN_SEPARATOR = " "


class SplunkQuery:
    """Безопасная сборка поисковых запросов Splunk из шаблона."""

    def quote(self, value: object) -> str:
        """Строка SPL в кавычках: экранированы обратный слеш и кавычка; управляющие символы не выразимы."""
        text = CONTROL_CHARACTERS.sub(TOKEN_SEPARATOR, str(value))
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        quoted = '"' + escaped + '"'
        return quoted

    def check_template(self, template: str) -> str:
        """Шаблон без неизвестных плейсхолдеров и не пустой; иначе SplunkTemplateError."""
        unknown = set(PLACEHOLDER.findall(template)) - PLACEHOLDERS
        if unknown:
            raise SplunkTemplateError("Unknown placeholders: " + ", ".join(sorted(unknown)))
        if not template.strip():
            raise SplunkTemplateError("Empty search template")
        return template

    def render(self, template: str, values: dict[str, str]) -> str:
        """Запрос с подставленными значениями; токены с пустыми плейсхолдерами отбрасываются."""
        tokens = []
        for token in self.check_template(template).split():
            names = PLACEHOLDER.findall(token)
            if any(not values.get(name) for name in names):
                continue
            tokens.append(PLACEHOLDER.sub(lambda match: self.quote(values[match.group(1)]), token))
        search = TOKEN_SEPARATOR.join(tokens)
        return search
