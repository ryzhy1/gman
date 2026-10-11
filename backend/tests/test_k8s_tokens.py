"""Per-node agent identity from Kubernetes bound service account tokens, verified by TokenReview."""
import base64
import json
import time

import httpx
import pytest

from backend.app import k8s_tokens
from backend.tests.test_api import auth, client  # noqa: F401  (fixture)
from backend.tests.test_gpu_attribution import node_snapshot

API = "https://10.0.0.1:6443"
ISSUER = "https://kubernetes.default.svc.cluster.local"
SA = "system:serviceaccount:gman-gpu:gman-gpu-agent"
POD_UID = "0b0c4f8e-7a51-4f43-a3a4-0d1c7f6a0001"


def jwt(name, iss=ISSUER, exp=None):
    """A JWT-shaped token: the API never verifies it locally, only the (mocked) TokenReview does."""
    payload = {"iss": iss, "aud": ["gman"], "sub": SA, "exp": exp or 4102444800, "name": name}
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{part}.signature-{name}"


def user(node=None, pod=None, uid=None, username=SA):
    extra = {"authentication.kubernetes.io/credential-id": ["JTI=1"]}
    if node:
        extra[k8s_tokens.NODE_EXTRA] = [node]
    if pod:
        extra.update({k8s_tokens.POD_NAME_EXTRA: [pod], k8s_tokens.POD_UID_EXTRA: [uid]})
    return {"username": username, "uid": "sa-uid", "groups": ["system:serviceaccounts"], "extra": extra}


# What the mocked API server answers for each token (status of the TokenReview).
STATUS = {
    jwt("gpu-1"): {"authenticated": True, "audiences": ["gman"], "user": user(node="gpu-1")},
    # Kubernetes < 1.30: no node-name extra, the node comes from the bound pod.
    jwt("old"): {"authenticated": True, "audiences": ["gman"], "user": user(pod="gman-gpu-agent-x1", uid=POD_UID)},
    jwt("recreated"): {"authenticated": True, "audiences": ["gman"],
                       "user": user(pod="gman-gpu-agent-x1", uid="another-uid")},
    jwt("expired"): {"authenticated": False, "error": "service account token has expired"},
    jwt("audience"): {"authenticated": False, "error": "token audiences [\"https://kubernetes.default.svc\"] is "
                                                       "invalid for the target audiences [\"gman\"]"},
    # An API server that ignored spec.audiences: authenticated, but the audience is not confirmed.
    jwt("unconfirmed"): {"authenticated": True, "audiences": ["https://kubernetes.default.svc"],
                         "user": user(node="gpu-1")},
    jwt("other-sa"): {"authenticated": True, "audiences": ["gman"],
                      "user": user(node="gpu-1", username="system:serviceaccount:ml:trainer")},
    jwt("unbound"): {"authenticated": True, "audiences": ["gman"], "user": user()},
}


@pytest.fixture
def k8s(monkeypatch, tmp_path, request):
    (tmp_path / "reviewer.token").write_text("reviewer-token\n")
    monkeypatch.setenv("GMAN_K8S_TOKEN_REVIEW", json.dumps({"cluster-01": {
        "api_url": API, "token_file": str(tmp_path / "reviewer.token"), "issuer": ISSUER}}))
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer reviewer-token"
        if request.url.path == "/apis/authentication.k8s.io/v1/tokenreviews":
            body = json.loads(request.content)
            assert body["kind"] == "TokenReview" and body["spec"]["audiences"] == ["gman"]
            status = STATUS.get(body["spec"]["token"], {"authenticated": False, "error": "invalid bearer token"})
            return httpx.Response(201, json={**body, "status": status})
        if request.url.path == "/api/v1/namespaces/gman-gpu/pods/gman-gpu-agent-x1":
            return httpx.Response(200, json={"metadata": {"name": "gman-gpu-agent-x1", "uid": POD_UID},
                                             "spec": {"nodeName": "gpu-2"}})
        return httpx.Response(404, json={})

    monkeypatch.setattr(k8s_tokens, "transport", httpx.MockTransport(handler))
    k8s_tokens.reset()
    api = request.getfixturevalue("client")  # sets its own GMAN_AGENT_TOKENS; read per request
    monkeypatch.setenv("GMAN_AGENT_TOKENS", '{"cluster-01":"agent","cluster-01.gpu-3":"static-gpu-3"}')
    return api, seen


def node(name, **extra):
    return {**node_snapshot(), "source_id": f"cluster-01.{name}", "cluster": "cluster-01", "hostname": name, **extra}


def send(api, token, value):
    return api.post("/api/inventory", headers=auth(token), json=value)


def test_bound_token_sends_only_its_own_node(k8s):
    api, seen = k8s
    token = jwt("gpu-1")
    assert send(api, token, node("gpu-1")).status_code == 200
    # Another GPU node of the same cluster, or the cluster source itself: refused.
    assert send(api, token, node("gpu-2")).status_code == 403
    cluster = {"source_id": "cluster-01", "source_type": "kubernetes-cluster", "hostname": "cluster-01",
               "observed_at": node_snapshot()["observed_at"], "instances": []}
    assert send(api, token, cluster).status_code == 403
    # The same source_id as another source type (would carry instances and history): refused too.
    docker = {"source_id": "cluster-01.gpu-1", "source_type": "docker-host", "hostname": "gpu-1",
              "observed_at": node_snapshot()["observed_at"], "instances": []}
    assert send(api, token, docker).status_code == 403
    # One TokenReview: the result is cached for a minute.
    assert len(seen) == 1
    source = next(s for s in api.get("/api/overview", headers=auth("read")).json()["sources"]
                  if s["source_id"] == "cluster-01.gpu-1")
    assert source["cluster"] == "cluster-01" and source["gpus"]


def test_pod_lookup_before_kubernetes_1_30(k8s):
    api, seen = k8s
    assert send(api, jwt("old"), node("gpu-2")).status_code == 200
    assert send(api, jwt("old"), node("gpu-1")).status_code == 403
    assert [r.url.path for r in seen][-1] == "/api/v1/namespaces/gman-gpu/pods/gman-gpu-agent-x1"
    # The pod of that name was recreated (different UID): the old token no longer names a node.
    assert send(api, jwt("recreated"), node("gpu-2")).status_code == 401


@pytest.mark.parametrize("name", ["expired", "audience", "unconfirmed", "other-sa", "unbound", "unknown"])
def test_rejected_tokens(k8s, name):
    api, _ = k8s
    assert send(api, jwt(name), node("gpu-1")).status_code == 401


def test_non_jwt_and_foreign_issuer_are_not_sent_to_the_cluster(k8s):
    api, seen = k8s
    assert send(api, "not-a-jwt", node("gpu-1")).status_code == 401
    assert send(api, jwt("gpu-1", iss="https://other-cluster"), node("gpu-1")).status_code == 401
    assert seen == []


def test_static_tokens_still_work(k8s):
    api, seen = k8s
    assert send(api, "static-gpu-3", node("gpu-3")).status_code == 200
    assert send(api, "static-gpu-3", node("gpu-1")).status_code == 403
    assert send(api, "wrong", node("gpu-3")).status_code == 401
    assert seen == []  # "wrong" is not JWT-shaped


def test_cluster_unreachable_is_retryable(k8s, monkeypatch):
    api, _ = k8s

    def down(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(k8s_tokens, "transport", httpx.MockTransport(down))
    # 503: the agent keeps the snapshot and retries; a rejection would be a 401.
    assert send(api, jwt("gpu-1"), node("gpu-1")).status_code == 503


def test_disabled_without_configuration(client, monkeypatch):
    def fail(request):
        raise AssertionError("no TokenReview without GMAN_K8S_TOKEN_REVIEW")

    monkeypatch.setattr(k8s_tokens, "transport", httpx.MockTransport(fail))
    monkeypatch.delenv("GMAN_K8S_TOKEN_REVIEW", raising=False)
    assert send(client, jwt("gpu-1"), node("gpu-1")).status_code == 401


def test_cache_never_outlives_the_token(k8s, monkeypatch):
    api, seen = k8s
    stale = jwt("gpu-1", exp=int(time.time()) - 10)
    monkeypatch.setitem(STATUS, stale, STATUS[jwt("gpu-1")])  # (a real API server would reject it)
    assert send(api, stale, node("gpu-1")).status_code == 200
    assert send(api, stale, node("gpu-1")).status_code == 200
    assert len(seen) == 2


def test_malformed_configuration_fails_at_startup(monkeypatch):
    monkeypatch.setenv("GMAN_K8S_TOKEN_REVIEW", '{"cluster-01": {"token": "x"}}')
    with pytest.raises(ValueError, match="api_url"):
        k8s_tokens.config()
