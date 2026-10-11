"""Links to Splunk search for a service and its instances.

Index names and field names are not agreed yet, so everything comes from configuration:

- GMAN_SPLUNK_URL: browser-facing search page, e.g. https://splunk.example.com/en-US/app/search/search.
  Empty disables the links.
- GMAN_SPLUNK_SEARCH: SPL template with {placeholders} (see PLACEHOLDERS); DEFAULT_SEARCH otherwise.
- GMAN_SPLUNK_INDEXES: JSON object environment -> index; key "*" is the fallback.

A catalog entry may override both via logs.splunk.index (string or environment -> index) and
logs.splunk.query (template). Every substituted value is an SPL quoted string, so a value can
never close the quote or add search commands. A whitespace-separated template token whose
placeholder has no value is dropped: the same template serves the service and its instances.
"""
import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_SEARCH = ("search index={index} gman_service={service_id} gman_environment={environment} "
                  "host={host} container_id={container_id}")
PLACEHOLDERS = {"index", "service_id", "environment", "instance", "container_id", "host", "namespace", "pod",
                "earliest", "latest"}
RANGES = {"1h": "-1h", "24h": "-24h", "7d": "-7d"}
INDEX_NAME = r"^[A-Za-z0-9_*.:-]{1,200}$"
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ConfigError(ValueError):
    pass


def quote(value):
    """SPL quoted string: backslash and double quote escaped; control characters cannot be expressed."""
    value = _CONTROL.sub(" ", str(value))
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_template(template):
    unknown = set(_PLACEHOLDER.findall(template)) - PLACEHOLDERS
    if unknown:
        raise ConfigError("Unknown placeholders: " + ", ".join(sorted(unknown)))
    if not template.strip():
        raise ConfigError("Empty search template")
    return template


def render(template, values):
    tokens = []
    for token in check_template(template).split():
        names = _PLACEHOLDER.findall(token)
        if any(not values.get(name) for name in names):
            continue
        tokens.append(_PLACEHOLDER.sub(lambda m: quote(values[m.group(1)]), token))
    return " ".join(tokens)


def check_indexes(value):
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError("GMAN_SPLUNK_INDEXES must be a JSON object of strings")
    for index in value.values():
        if not re.match(INDEX_NAME, index):
            raise ConfigError("Invalid index name: " + index[:100])
    return value


def config():
    """None when Splunk links are disabled; ConfigError when the configuration is invalid."""
    base = os.getenv("GMAN_SPLUNK_URL", "").strip()
    if not base:
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError("GMAN_SPLUNK_URL must be an HTTP(S) URL without credentials")
    try:
        indexes = json.loads(os.getenv("GMAN_SPLUNK_INDEXES", "") or "{}")
    except json.JSONDecodeError:
        raise ConfigError("GMAN_SPLUNK_INDEXES is not valid JSON")
    return {"url": base, "search": check_template(os.getenv("GMAN_SPLUNK_SEARCH", "") or DEFAULT_SEARCH),
            "indexes": check_indexes(indexes)}


def index_for(settings, override, environment):
    if isinstance(override, str) and override:
        return override
    for mapping in (override if isinstance(override, dict) else {}, settings["indexes"]):
        if mapping.get(environment) or mapping.get("*"):
            return mapping.get(environment) or mapping["*"]
    return ""


def link(settings, service, environment="", instance=None, host="", range_="1h"):
    """Search URL for a catalog service, optionally narrowed to one inventory instance."""
    override = ((service.get("logs") or {}).get("splunk") or {})
    instance = instance or {}
    docker = instance.get("runtime") == "docker"
    kubernetes = instance.get("runtime") == "kubernetes"
    earliest = RANGES[range_]
    values = {"service_id": service["id"], "environment": environment,
              "index": index_for(settings, override.get("index"), environment),
              "instance": instance.get("name", ""), "container_id": instance.get("id", "") if docker else "",
              "host": host, "namespace": instance.get("namespace", "") if kubernetes else "",
              "pod": instance.get("name", "").split("/")[0] if kubernetes else "",
              "earliest": earliest, "latest": "now"}
    search = render(override.get("query") or settings["search"], values)
    parts = urlsplit(settings["url"])
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("q", "earliest", "latest")]
    query += [("q", search), ("earliest", earliest), ("latest", "now")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def links(service, sources, environment="", range_="1h"):
    """Service-level link and one per instance of the service, keyed "<source_id>/<instance_id>"."""
    settings = config()
    if settings is None:
        return {"enabled": False, "service": None, "instances": {}}
    result = {"enabled": True, "service": link(settings, service, environment, range_=range_), "instances": {}}
    for source in sources:
        for item in source.get("instances", []):
            if item.get("service_id") != service["id"] or (environment and item.get("environment") != environment):
                continue
            host = item.get("node") or source.get("hostname", "")
            result["instances"][source["source_id"] + "/" + item["id"]] = link(
                settings, service, item.get("environment", ""), item, host, range_)
    return result
