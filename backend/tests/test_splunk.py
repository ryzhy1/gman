from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app import splunk
from backend.tests.test_api import auth, client, post, snapshot  # noqa: F401 (fixture)

BASE = "https://splunk.example.com/en-US/app/search/search"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GMAN_SPLUNK_URL", BASE)
    monkeypatch.setenv("GMAN_SPLUNK_INDEXES", '{"prod": "app_prod", "*": "app_all"}')
    monkeypatch.delenv("GMAN_SPLUNK_SEARCH", raising=False)


def params(url):
    parts = urlsplit(url)
    assert parts.scheme + "://" + parts.netloc + parts.path == BASE
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_quote_escapes_backslash_quote_and_control_characters():
    assert splunk.quote('a"b') == '"a\\"b"'
    assert splunk.quote("a\\") == '"a\\\\"'
    assert splunk.quote('x" | delete') == '"x\\" | delete"'
    assert splunk.quote("a\nb") == '"a b"'


def test_injection_stays_inside_quoted_value(configured):
    service = {"id": 'api" OR index=* | delete "'}
    q = params(splunk.link(splunk.config(), service, "prod"))["q"]
    assert q == ('search index="app_prod" gman_service="api\\" OR index=* | delete \\"" '
                 'gman_environment="prod"')


def test_missing_config_disables_links(monkeypatch):
    monkeypatch.delenv("GMAN_SPLUNK_URL", raising=False)
    assert splunk.config() is None
    assert splunk.links({"id": "api"}, []) == {"enabled": False, "service": None, "instances": {}}


@pytest.mark.parametrize("env,value", [
    ("GMAN_SPLUNK_URL", "javascript:alert(1)"), ("GMAN_SPLUNK_URL", "https://user:pw@splunk"),
    ("GMAN_SPLUNK_INDEXES", "{bad"), ("GMAN_SPLUNK_INDEXES", '{"prod": "a b"}'),
    ("GMAN_SPLUNK_SEARCH", "search index={idx}")])
def test_invalid_config_is_rejected(configured, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(splunk.ConfigError):
        splunk.config()


def test_index_per_environment_and_fallback(configured):
    settings = splunk.config()
    assert 'index="app_prod"' in params(splunk.link(settings, {"id": "api"}, "prod"))["q"]
    assert 'index="app_all"' in params(splunk.link(settings, {"id": "api"}, "test"))["q"]
    settings["indexes"] = {"prod": "app_prod"}
    assert params(splunk.link(settings, {"id": "api"}, "test"))["q"] == 'search gman_service="api" gman_environment="test"'


def test_instance_placeholders_time_range_and_existing_query(configured, monkeypatch):
    monkeypatch.setenv("GMAN_SPLUNK_URL", BASE + "?display.page.search.mode=fast&q=old")
    monkeypatch.setenv("GMAN_SPLUNK_SEARCH", "search {index} sourcetype=docker host={host} pod={pod} "
                       "ns={namespace} cid={container_id} name={instance}")
    settings = splunk.config()
    docker = {"id": "abc", "name": "api-1", "runtime": "docker"}
    result = parse_qs(urlsplit(splunk.link(settings, {"id": "api"}, "prod", docker, "host-1", "7d")).query)
    assert result["q"] == ['search "app_prod" sourcetype=docker host="host-1" cid="abc" name="api-1"']
    assert result["earliest"] == ["-7d"] and result["latest"] == ["now"]
    assert result["display.page.search.mode"] == ["fast"]
    pod = {"id": "uid:app", "name": "api-7f/app", "runtime": "kubernetes", "namespace": "team"}
    q = params(splunk.link(settings, {"id": "api"}, "prod", pod, "node-1"))["q"]
    assert q == 'search "app_prod" sourcetype=docker host="node-1" pod="api-7f" ns="team" name="api-7f/app"'


def test_service_override(configured):
    service = {"id": "api", "logs": {"splunk": {"index": {"prod": "api_prod"},
                                                "query": "search index={index} app={service_id} earliest={earliest}"}}}
    settings = splunk.config()
    assert params(splunk.link(settings, service, "prod", range_="1h"))["q"] == \
        'search index="api_prod" app="api" earliest="-1h"'
    assert params(splunk.link(settings, service, "test"))["q"] == 'search index="app_all" app="api" earliest="-24h"'
    service["logs"]["splunk"]["index"] = "api_all"
    assert 'index="api_all"' in params(splunk.link(settings, service, "prod"))["q"]


def test_api_links(client, configured):  # noqa: F811
    assert client.get("/api/logs/splunk?service_id=api", headers=auth("read")).status_code == 404
    service = {"id": "api", "name": "API", "logs": {"splunk": {"index": "api_idx"}}}
    assert client.put("/api/services/api", headers=auth("admin"), json=service).status_code == 200
    value = snapshot()
    value["instances"][0]["environment"] = "prod"
    assert post(client, value).status_code == 200
    response = client.get("/api/logs/splunk?service_id=api&environment=prod&range=1h", headers=auth("read"))
    body = response.json()
    assert body["enabled"] and body["error"] is None
    assert params(body["service"])["q"] == 'search index="api_idx" gman_service="api" gman_environment="prod"'
    assert params(body["instances"]["node-1/container-1"])["q"] == (
        'search index="api_idx" gman_service="api" gman_environment="prod" host="host" container_id="container-1"')
    assert client.get("/api/logs/splunk?service_id=api&environment=test", headers=auth("read")).json()["instances"] == {}
    assert client.get("/api/logs/splunk?service_id=api&range=1y", headers=auth("read")).status_code == 422
    assert client.get("/api/logs/splunk?service_id=api").status_code == 401


def test_api_rejects_bad_override_and_reports_bad_config(client, configured, monkeypatch):  # noqa: F811
    for logs in ({"splunk": {"index": 'x" | delete'}}, {"splunk": {"query": "search {nope}"}}, {"splunk": {"bogus": 1}}):
        response = client.put("/api/services/api", headers=auth("admin"), json={"id": "api", "name": "API", "logs": logs})
        assert response.status_code == 422
    assert client.put("/api/services/api", headers=auth("admin"), json={"id": "api", "name": "API"}).status_code == 200
    monkeypatch.setenv("GMAN_SPLUNK_INDEXES", "{bad")
    body = client.get("/api/logs/splunk?service_id=api", headers=auth("read")).json()
    assert body["enabled"] is False and body["service"] is None and "misconfigured" in body["error"]
    monkeypatch.delenv("GMAN_SPLUNK_URL")
    assert client.get("/api/logs/splunk?service_id=api", headers=auth("read")).json()["enabled"] is False
