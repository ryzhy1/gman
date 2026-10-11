"""Per-node agent identity from Kubernetes bound service account tokens (TokenReview).

The GPU DaemonSet (deploy/gpu-attribution-k8s.yaml) mounts a projected service account token with the
audience "gman". The API sends it to the cluster's TokenReview API, which checks the signature, expiry,
audience and that the pod it is bound to still exists, and returns the pod (and, on Kubernetes >= 1.30,
the node) the token belongs to. The agent is then the source "<cluster>.<node>" and may only send the
kubernetes-node snapshot of exactly that node: a pod on one GPU node cannot report another node.

Configuration, GMAN_K8S_TOKEN_REVIEW (JSON) or GMAN_K8S_TOKEN_REVIEW_FILE (path to the same JSON); keys are
cluster source IDs (the cluster inventory agent's --source-id, GMAN_CLUSTER of the DaemonSet):

    {"cluster-01": {"api_url": "https://10.0.0.1:6443", "ca_file": "/etc/gman/k8s/cluster-01-ca.pem",
                    "token_file": "/etc/gman/k8s/cluster-01-reviewer.token",
                    "audience": "gman", "issuer": "https://kubernetes.default.svc.cluster.local",
                    "service_accounts": ["gman-gpu:gman-gpu-agent"]}}

audience defaults to "gman", service_accounts to ["gman-gpu:gman-gpu-agent"]; token may be given inline
instead of token_file (re-read on every review, so a rotated token file is picked up), and the CA as
certificate_authority_data (base64 PEM, as in a kubeconfig) instead of ca_file; without either the system
trust store is used. Inline values let the whole configuration live in .env (Docker Compose). issuer is optional and needed only to tell several clusters apart without sending a
token to every configured cluster: a token is reviewed only by clusters whose issuer matches its (unverified)
"iss" claim or that set no issuer. Static GMAN_AGENT_TOKENS keep working and are checked first.
"""
import base64
import hashlib
import json
import logging
import os
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import HTTPException

logger = logging.getLogger("gman.k8s_tokens")

NODE_EXTRA = "authentication.kubernetes.io/node-name"  # Kubernetes >= 1.30 (ServiceAccountTokenPodNodeInfo)
POD_NAME_EXTRA = "authentication.kubernetes.io/pod-name"
POD_UID_EXTRA = "authentication.kubernetes.io/pod-uid"
SA_PREFIX = "system:serviceaccount:"
CACHE_SECONDS = 60
transport = None  # httpx transport; replaced in tests with a MockTransport

_cache = {}
_cache_lock = threading.Lock()


class NodeIdentity(str):
    """The source_id "<cluster>.<node>" of a reviewed token, with the parts it was built from."""

    def __new__(cls, cluster, node):
        value = super().__new__(cls, f"{cluster}.{node}")
        value.cluster, value.node = cluster, node
        return value


def config():
    raw = os.getenv("GMAN_K8S_TOKEN_REVIEW", "")
    path = os.getenv("GMAN_K8S_TOKEN_REVIEW_FILE", "")
    if path:
        with open(path) as file:
            raw = file.read()
    clusters = json.loads(raw) if raw.strip() else {}
    if not isinstance(clusters, dict) or not all(isinstance(c, dict) and c.get("api_url") for c in clusters.values()):
        raise ValueError("GMAN_K8S_TOKEN_REVIEW: an object of clusters, each with api_url")
    return clusters


def enabled():
    return bool(os.getenv("GMAN_K8S_TOKEN_REVIEW") or os.getenv("GMAN_K8S_TOKEN_REVIEW_FILE"))


def reset():
    with _cache_lock:
        _cache.clear()


def unverified_claims(token):
    """JWT payload without verification: used only to pick the cluster and bound the cache lifetime.
    Every security decision comes from the TokenReview response."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def http_client(cluster):
    verify = True
    if cluster.get("certificate_authority_data"):
        verify = ssl.create_default_context(cadata=base64.b64decode(cluster["certificate_authority_data"]).decode())
    elif cluster.get("ca_file"):
        verify = ssl.create_default_context(cafile=cluster["ca_file"])
    return httpx.Client(base_url=cluster["api_url"].rstrip("/"), verify=verify, timeout=5, transport=transport)


def reviewer_token(cluster):
    if cluster.get("token_file"):
        with open(cluster["token_file"]) as file:
            return file.read().strip()
    return cluster.get("token", "")


def review(name, cluster, token):
    """NodeIdentity for an authenticated, pod-bound token of an allowed service account; None otherwise.
    Raises httpx.HTTPError (and OSError for an unreadable reviewer token) when the cluster cannot answer."""
    audience = cluster.get("audience", "gman")
    allowed = set(cluster.get("service_accounts", ["gman-gpu:gman-gpu-agent"]))
    headers = {"Authorization": "Bearer " + reviewer_token(cluster)}
    body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
            "spec": {"token": token, "audiences": [audience]}}
    with http_client(cluster) as client:
        response = client.post("/apis/authentication.k8s.io/v1/tokenreviews", json=body, headers=headers)
        response.raise_for_status()
        status = response.json().get("status") or {}
        if not status.get("authenticated"):
            logger.info("TokenReview in %s: not authenticated (%s)", name, status.get("error", "")[:200])
            return None
        # An API server that ignores spec.audiences would accept its own default tokens: require the echo.
        if audience not in (status.get("audiences") or []):
            logger.warning("TokenReview in %s: audience %s not confirmed", name, audience)
            return None
        user = status.get("user") or {}
        username = user.get("username", "")
        account = username[len(SA_PREFIX):] if username.startswith(SA_PREFIX) else ""
        if account not in allowed:
            logger.warning("TokenReview in %s: service account %r is not allowed", name, username)
            return None
        namespace = account.split(":", 1)[0]
        extra = user.get("extra") or {}
        node = (extra.get(NODE_EXTRA) or [""])[0]
        if not node:
            # Before Kubernetes 1.30 the token names only its pod: look up the pod's node (get pods in the
            # agent's namespace) and require the same UID, so a recreated pod of the same name does not count.
            pod, uid = (extra.get(POD_NAME_EXTRA) or [""])[0], (extra.get(POD_UID_EXTRA) or [""])[0]
            if not (pod and uid):
                logger.warning("TokenReview in %s: token of %s is not bound to a pod", name, username)
                return None
            found = client.get(f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod, safe='')}",
                               headers=headers)
            if found.status_code == 404:
                return None
            found.raise_for_status()
            item = found.json()
            if (item.get("metadata") or {}).get("uid") != uid:
                return None
            node = (item.get("spec") or {}).get("nodeName", "")
        return NodeIdentity(name, node) if node else None


def authenticate(authorization):
    """Source identity of a Kubernetes bound token, None when it is not one (the caller answers 401).
    503 when a candidate cluster cannot be asked: the agent keeps the snapshot and retries."""
    if not (authorization or "").startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    claims = unverified_claims(token)
    if claims is None:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    try:
        clusters = config()
    except (OSError, ValueError) as error:
        logger.error("Kubernetes token review configuration: %s", error)
        raise HTTPException(503, "Kubernetes token review is misconfigured")
    issuer = claims.get("iss")
    identity, failed = None, False
    for name, cluster in clusters.items():
        if cluster.get("issuer") and cluster["issuer"] != issuer:
            continue
        try:
            identity = review(name, cluster, token)
        except (httpx.HTTPError, OSError, ValueError) as error:
            logger.warning("TokenReview in %s failed: %s", name, error)
            failed = True
            continue
        if identity:
            break
    if identity is None and failed:
        raise HTTPException(503, "Kubernetes token review unavailable")
    # Cache for at most a minute and never past the token's own expiry; rejections are cached too, so a
    # stream of bad tokens does not turn into a stream of TokenReviews.
    ttl = CACHE_SECONDS
    if isinstance(claims.get("exp"), (int, float)):
        ttl = max(0, min(ttl, claims["exp"] - time.time()))
    with _cache_lock:
        if len(_cache) > 10000:
            _cache.clear()
        _cache[key] = (now + ttl, identity)
    return identity


def check_snapshot(source_id, snapshot):
    """A reviewed token may send only the kubernetes-node snapshot of its own cluster and node."""
    if isinstance(source_id, NodeIdentity) and not (
            snapshot.source_type == "kubernetes-node" and snapshot.cluster == source_id.cluster
            and snapshot.hostname == source_id.node):
        raise HTTPException(403, "Kubernetes token is valid only for its own node's kubernetes-node snapshot")
