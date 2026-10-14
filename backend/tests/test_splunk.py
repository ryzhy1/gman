from urllib.parse import parse_qs, urlsplit

import pytest

from inventory.entity import LogTarget, SplunkLinks, SplunkOverride, SplunkTemplateError
from inventory.services.splunk_links import SplunkLinkBuilder
from inventory.services.splunk_query import SplunkQuery
from inventory.services.splunk_settings import SplunkSettingsParser
from snapshot_factory import post, snapshot

BASE = "https://splunk.example.com/en-US/app/search/search"
INDEXES = '{"prod": "app_prod", "*": "app_all"}'


def settings(url=BASE, search="", indexes=INDEXES):
    return SplunkSettingsParser(SplunkQuery()).parse(url, search, indexes)


def link(parsed, service_id="api", environment="", target=None, range_name="1h", override=SplunkOverride()):
    builder = SplunkLinkBuilder(SplunkQuery(), parsed, None)
    return builder.link(parsed, service_id, override, environment, target, range_name)


def target(instance_id, name, runtime, host, namespace=""):
    return LogTarget(key="", service_id="api", instance_id=instance_id, name=name, runtime=runtime,
                     namespace=namespace, environment="prod", host=host)


@pytest.fixture
def configured(make_client):
    return lambda **extra: make_client(splunk_url=BASE, splunk_indexes=INDEXES, **extra)


def params(url):
    parts = urlsplit(url)
    assert parts.scheme + "://" + parts.netloc + parts.path == BASE
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_quote_escapes_backslash_quote_and_control_characters():
    quote = SplunkQuery().quote
    assert quote('a"b') == '"a\\"b"'
    assert quote("a\\") == '"a\\\\"'
    assert quote('x" | delete') == '"x\\" | delete"'
    assert quote("a\nb") == '"a b"'


def test_injection_stays_inside_quoted_value():
    q = params(link(settings(), 'api" OR index=* | delete "', "prod"))["q"]
    assert q == ('search index="app_prod" gman_service="api\\" OR index=* | delete \\"" '
                 'gman_environment="prod"')


def test_missing_config_disables_links():
    assert settings(url="") is None
    builder = SplunkLinkBuilder(SplunkQuery(), None, None)
    assert builder.links("api", SplunkOverride(), [], "", "1h") == SplunkLinks(enabled=False, service=None,
                                                                                 instances={})


@pytest.mark.parametrize("field,value", [
    ("url", "javascript:alert(1)"), ("url", "https://user:pw@splunk"),
    ("indexes", "{bad"), ("indexes", '{"prod": "a b"}'), ("search", "search index={idx}")])
def test_invalid_config_is_rejected(field, value):
    with pytest.raises(SplunkTemplateError):
        settings(**{field: value})


def test_index_per_environment_and_fallback():
    assert 'index="app_prod"' in params(link(settings(), "api", "prod"))["q"]
    assert 'index="app_all"' in params(link(settings(), "api", "test"))["q"]
    only_prod = settings(indexes='{"prod": "app_prod"}')
    assert params(link(only_prod, "api", "test"))["q"] == 'search gman_service="api" gman_environment="test"'


def test_instance_placeholders_time_range_and_existing_query():
    parsed = settings(url=BASE + "?display.page.search.mode=fast&q=old",
                      search="search {index} sourcetype=docker host={host} pod={pod} ns={namespace} "
                             "cid={container_id} name={instance}")
    docker = target("abc", "api-1", "docker", "host-1")
    result = parse_qs(urlsplit(link(parsed, "api", "prod", docker, "7d")).query)
    assert result["q"] == ['search "app_prod" sourcetype=docker host="host-1" cid="abc" name="api-1"']
    assert result["earliest"] == ["-7d"] and result["latest"] == ["now"]
    assert result["display.page.search.mode"] == ["fast"]
    pod = target("uid:app", "api-7f/app", "kubernetes", "node-1", namespace="team")
    q = params(link(parsed, "api", "prod", pod))["q"]
    assert q == 'search "app_prod" sourcetype=docker host="node-1" pod="api-7f" ns="team" name="api-7f/app"'


def test_service_override():
    override = SplunkOverride(index={"prod": "api_prod"},
                              query="search index={index} app={service_id} earliest={earliest}")
    assert params(link(settings(), "api", "prod", override=override))["q"] == \
        'search index="api_prod" app="api" earliest="-1h"'
    assert params(link(settings(), "api", "test", override=override))["q"] == \
        'search index="app_all" app="api" earliest="-1h"'
    single = SplunkOverride(index="api_all", query=override.query)
    assert 'index="api_all"' in params(link(settings(), "api", "prod", override=single))["q"]


def test_api_links(configured):
    client = configured()
    assert client.get("/api/logs/splunk?service_id=api").status_code == 404
    service = {"id": "api", "name": "API", "logs": {"splunk": {"index": "api_idx"}}}
    assert client.put("/api/services/api", json=service).status_code == 200
    value = snapshot()
    value["instances"][0]["environment"] = "prod"
    assert post(client, value).status_code == 200
    response = client.get("/api/logs/splunk?service_id=api&environment=prod&range=1h")
    body = response.json()
    assert body["enabled"] and body["error"] is None
    assert params(body["service"])["q"] == 'search index="api_idx" gman_service="api" gman_environment="prod"'
    assert params(body["instances"]["node-1/container-1"])["q"] == (
        'search index="api_idx" gman_service="api" gman_environment="prod" host="host" container_id="container-1"')
    assert client.get("/api/logs/splunk?service_id=api&environment=test").json()["instances"] == {}
    assert client.get("/api/logs/splunk?service_id=api&range=1y").status_code == 422
    # Default period is one hour, as on the "Ресурсы" tab and in the README.
    default = client.get("/api/logs/splunk?service_id=api").json()
    assert params(default["service"])["earliest"] == "-1h"


def test_api_rejects_bad_override_and_reports_bad_config(configured, make_client):
    client = configured()
    for logs in ({"splunk": {"index": 'x" | delete'}}, {"splunk": {"query": "search {nope}"}}, {"splunk": {"bogus": 1}}):
        response = client.put("/api/services/api", json={"id": "api", "name": "API", "logs": logs})
        assert response.status_code == 422
    assert client.put("/api/services/api", json={"id": "api", "name": "API"}).status_code == 200
    misconfigured = make_client(splunk_url=BASE, splunk_indexes="{bad")
    body = misconfigured.get("/api/logs/splunk?service_id=api").json()
    assert body["enabled"] is False and body["service"] is None and "misconfigured" in body["error"]
    assert make_client().get("/api/logs/splunk?service_id=api").json()["enabled"] is False
