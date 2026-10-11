"""SSO against a mock IdP: discovery, JWKS, token endpoint with PKCE, userinfo; roles, owners, CSRF, logout."""
import base64
import hashlib
import importlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app import auth
from backend.app import db as store
from backend.tests.database import database_url
from backend.tests.test_api import auth as bearer, client  # noqa: F401  (fixture)

ISSUER = "https://idp.example/realms/corp"
CLIENT_ID = "gman-portal"
SECRET = "client-secret-of-the-portal-at-least-32-bytes"


class IdP:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        self.userinfo = None
        self.issuer = ISSUER

    def jwks(self):
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", use="sig", alg="RS256")
        return {"keys": [jwk]}

    def sign(self, claims, key=None, kid="k1"):
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid})

    def handler(self, request):
        path = request.url.path
        if path == "/realms/corp/.well-known/openid-configuration":
            base = "https://idp.example/realms/corp/protocol/openid-connect"
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": base + "/auth", "token_endpoint": base + "/token",
                "jwks_uri": base + "/certs", "userinfo_endpoint": base + "/userinfo",
                "end_session_endpoint": base + "/logout"})
        if path.endswith("/certs"):
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            entry = self.codes.pop(form.get("code"), None)
            if entry is None or form.get("grant_type") != "authorization_code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if challenge != entry["challenge"] or form.get("redirect_uri") != "http://testserver/api/auth/callback":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": entry["token"], "access_token": "access", "token_type": "Bearer"})
        if path.endswith("/userinfo") and self.userinfo is not None:
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


@pytest.fixture
def idp():
    return IdP()


@pytest.fixture
def sso(tmp_path, monkeypatch, idp):
    monkeypatch.setenv("DATABASE_URL", database_url(tmp_path))
    for key, value in {"GMAN_READ_TOKEN": "read", "GMAN_ADMIN_TOKEN": "admin", "GMAN_CI_TOKEN": "ci",
                       "GMAN_AGENT_TOKENS": '{"node-1":"agent"}',
                       "GMAN_OIDC_ISSUER": ISSUER, "GMAN_OIDC_CLIENT_ID": CLIENT_ID, "GMAN_OIDC_CLIENT_SECRET": SECRET,
                       "GMAN_OIDC_REDIRECT_URI": "http://testserver/api/auth/callback",
                       "GMAN_OIDC_ADMIN_GROUPS": "gman-admins", "GMAN_OIDC_OWNER_GROUPS": "dev-leads, gman-owners",
                       "GMAN_OIDC_VIEWER_GROUPS": "gman-viewers"}.items():
        monkeypatch.setenv(key, value)
    auth.client = httpx.Client(transport=httpx.MockTransport(idp.handler))
    from backend.app import main
    importlib.reload(main)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c
    main.engine.dispose()
    auth.client = httpx.Client(timeout=10)


def start(c, next_path="/"):
    response = c.get("/api/auth/login", params={"next": next_path}, follow_redirects=False)
    assert response.status_code == 302
    return response, {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}


def sign_in(c, idp, next_path="/", token=None, **claims):
    """Full browser round trip; claims set to None are left out of the ID token."""
    _, query = start(c, next_path)
    now = int(time.time())
    values = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "email": "alice@example.com", "name": "Alice",
              "iat": now, "exp": now + 300, "nonce": query["nonce"], "groups": ["gman-viewers"], **claims}
    code = secrets.token_hex(8)
    idp.codes[code] = {"token": token or idp.sign({k: v for k, v in values.items() if v is not None}),
                       "challenge": query["code_challenge"]}
    return c.get("/api/auth/callback", params={"code": code, "state": query["state"]}, follow_redirects=False)


def me(c):
    return c.get("/api/me").json()


def write(c, value, csrf=True):
    headers = {"X-CSRF-Token": me(c)["csrf_token"]} if csrf is True else ({"X-CSRF-Token": csrf} if csrf else {})
    return c.put("/api/services/" + value["id"], json=value, headers=headers)


def service(service_id, **fields):
    return {"id": service_id, "name": service_id.upper(), **fields}


def test_authorization_request_uses_pkce_state_and_nonce(sso):
    response, query = start(sso)
    assert response.headers["location"].startswith(ISSUER + "/protocol/openid-connect/auth?")
    assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["state"] and query["nonce"] and "openid" in query["scope"].split()
    cookie = response.headers["set-cookie"].lower()
    assert "gman_login=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/api/auth" in cookie
    assert "verifier" not in response.headers["location"]


def test_callback_creates_session(sso, idp):
    response = sign_in(sso, idp, next_path="/services?x=1", groups=["gman-admins", "other"])
    assert response.status_code == 302 and response.headers["location"] == "/services?x=1"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("gman_session="))
    assert "HttpOnly" in session_cookie and "samesite=lax" in session_cookie.lower() and "Secure" not in session_cookie
    user = me(sso)
    assert user["auth"] == "oidc" and user["role"] == "admin" and user["email"] == "alice@example.com"
    assert user["name"] == "Alice" and user["csrf_token"]
    assert sso.get("/api/overview").status_code == 200
    # Only a hash of the cookie value is stored.
    value = sso.cookies.get("gman_session")
    with Session(engine()) as session:
        ids = session.scalars(select(store.UserSession.id)).all()
    assert value not in ids and hashlib.sha256(value.encode()).hexdigest() in ids


def engine():
    from backend.app import main
    return main.engine


def test_role_mapping(sso, idp):
    assert auth.role_for(["gman-viewers", "dev-leads"]) == "owner"
    assert auth.role_for(["gman-admins", "gman-viewers"]) == "admin"
    assert auth.role_for(["gman-viewers"]) == "viewer"
    assert auth.role_for(["unrelated"]) is None
    sign_in(sso, idp, groups=["dev-leads"])
    assert me(sso)["role"] == "owner"


def test_user_without_mapped_group_is_refused(sso, idp):
    response = sign_in(sso, idp, groups=["contractors"])
    assert response.headers["location"] == "/?auth_error=forbidden"
    assert sso.get("/api/me").status_code == 401
    with Session(engine()) as session:
        assert session.scalars(select(store.UserSession)).all() == []


def test_wildcard_viewer_and_dotted_groups_claim(sso, idp, monkeypatch):
    monkeypatch.setenv("GMAN_OIDC_VIEWER_GROUPS", "*")
    monkeypatch.setenv("GMAN_OIDC_GROUPS_CLAIM", "realm_access.roles")
    sign_in(sso, idp, groups=None, realm_access={"roles": ["gman-admins"]})
    assert me(sso)["role"] == "admin"
    sso.cookies.clear()
    sign_in(sso, idp, groups=None, realm_access={"roles": []})
    assert me(sso)["role"] == "viewer"


def test_groups_from_userinfo_when_not_in_id_token(sso, idp):
    idp.userinfo = {"sub": "user-1", "groups": ["gman-owners"]}
    sign_in(sso, idp, groups=None)
    assert me(sso)["role"] == "owner"
    sso.cookies.clear()
    idp.userinfo = {"sub": "somebody-else", "groups": ["gman-admins"]}
    assert sign_in(sso, idp, groups=None).headers["location"] == "/?auth_error=forbidden"


@pytest.mark.parametrize("claims", [
    {"nonce": "replayed"}, {"aud": "another-client"}, {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, {"sub": None},
    {"aud": [CLIENT_ID, "other"]},  # several audiences require azp = this client
])
def test_invalid_id_tokens_are_rejected(sso, idp, claims):
    response = sign_in(sso, idp, groups=["gman-admins"], **claims)
    assert response.headers["location"] == "/?auth_error=invalid_token"
    assert sso.get("/api/me").status_code == 401


def test_signature_must_match_idp_keys(sso, idp):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forgeries = [lambda c: idp.sign(c, key=other), lambda c: idp.sign(c, kid="unknown"),
                 lambda c: jwt.encode(c, SECRET, algorithm="HS256", headers={"kid": "k1"})]
    for forge in forgeries:
        _, query = start(sso)
        now = int(time.time())
        token = forge({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "iat": now, "exp": now + 60,
                       "nonce": query["nonce"], "groups": ["gman-admins"]})
        code = secrets.token_hex(8)
        idp.codes[code] = {"token": token, "challenge": query["code_challenge"]}
        response = sso.get("/api/auth/callback", params={"code": code, "state": query["state"]}, follow_redirects=False)
        assert response.headers["location"] == "/?auth_error=invalid_token"
    assert sso.get("/api/me").status_code == 401


def test_state_is_checked(sso, idp):
    _, query = start(sso)
    assert sso.get("/api/auth/callback", params={"code": "c", "state": "forged"}).status_code == 400
    sso.cookies.clear()
    assert sso.get("/api/auth/callback", params={"code": "c", "state": query["state"]}).status_code == 400
    # An IdP error is shown only for the sign-in this browser started.
    _, query = start(sso)
    response = sso.get("/api/auth/callback", params={"error": "access_denied", "state": query["state"]},
                       follow_redirects=False)
    assert response.headers["location"] == "/?auth_error=idp_error"


def test_wrong_pkce_verifier_fails_exchange(sso, idp):
    _, query = start(sso)
    code = secrets.token_hex(8)
    idp.codes[code] = {"token": "unused", "challenge": "not-the-challenge"}
    response = sso.get("/api/auth/callback", params={"code": code, "state": query["state"]}, follow_redirects=False)
    assert response.headers["location"] == "/?auth_error=token_exchange"


def test_next_cannot_redirect_off_site(sso, idp):
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example"):
        sso.cookies.clear()
        assert sign_in(sso, idp, next_path=target).headers["location"] == "/"


def test_owner_edits_only_own_services(sso, idp):
    admin = bearer("admin")
    assert sso.put("/api/services/mine", headers=admin,
                   json=service("mine", owners=["Alice@Example.com"])).status_code == 200
    assert sso.put("/api/services/team", headers=admin, json=service("team", owners=["group:dev-leads"])).status_code == 200
    assert sso.put("/api/services/other", headers=admin, json=service("other", owners=["bob@example.com"])).status_code == 200
    sign_in(sso, idp, groups=["dev-leads"])
    assert write(sso, service("mine", description="updated", owners=["Alice@Example.com"])).status_code == 200
    assert write(sso, service("team", description="by group", owners=["group:dev-leads"])).status_code == 200
    assert write(sso, service("other", owners=["bob@example.com"])).status_code == 403
    assert write(sso, service("new-one", owners=["alice@example.com"])).status_code == 403
    response = write(sso, service("mine", owners=["Alice@Example.com", "mallory@example.com"]))
    assert response.status_code == 403 and "owners" in response.text
    events = sso.get("/api/events", params={"service_id": "mine"}).json()["events"]
    assert [e["type"] for e in events] == ["service-updated", "service-created"]
    assert events[0]["actor"] == "alice@example.com" and events[0]["actor_kind"] == "human"
    assert events[0]["actor_subject"] == "user-1" and events[0]["actor_source"] == "OIDC session"
    assert events[0]["changes"] == {"description": ["", "updated"]}
    assert events[1]["actor"] == "GMAN_ADMIN_TOKEN" and events[1]["actor_kind"] == "unknown"


def test_owner_listing_does_not_help_viewer(sso, idp):
    assert sso.put("/api/services/mine", headers=bearer("admin"),
                   json=service("mine", owners=["alice@example.com"])).status_code == 200
    sign_in(sso, idp, groups=["gman-viewers"])
    assert write(sso, service("mine", owners=["alice@example.com"])).status_code == 403


def test_admin_session_edits_everything(sso, idp):
    sign_in(sso, idp, groups=["gman-admins"])
    assert write(sso, service("new", owners=["bob@example.com"])).status_code == 200
    assert write(sso, service("new", owners=[])).status_code == 200
    events = sso.get("/api/events", params={"service_id": "new"}).json()["events"]
    assert events[0]["changes"] == {"owners": [["bob@example.com"], []]}
    assert events[0]["actor"] == "alice@example.com" and events[0]["actor_role"] == "admin"


def test_csrf_required_for_cookie_writes(sso, idp):
    sign_in(sso, idp, groups=["gman-admins"])
    assert write(sso, service("api"), csrf=False).status_code == 403
    assert write(sso, service("api"), csrf="wrong").status_code == 403
    assert write(sso, service("api"), csrf=b"\xfc").status_code == 403
    assert write(sso, service("api")).status_code == 200
    # Bearer automation is not exposed to CSRF and needs no token.
    assert sso.put("/api/services/api", headers=bearer("admin"), json=service("api")).status_code == 200


def test_logout_revokes_session(sso, idp, monkeypatch):
    sign_in(sso, idp, groups=["gman-viewers"])
    stolen = sso.cookies.get("gman_session")
    csrf = me(sso)["csrf_token"]
    assert sso.post("/api/auth/logout").status_code == 403
    response = sso.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200 and response.json() == {"redirect": "/?logged_out=1"}
    assert 'gman_session=""' in response.headers["set-cookie"] and "Max-Age=0" in response.headers["set-cookie"]
    assert sso.get("/api/me").status_code == 401
    sso.cookies.set("gman_session", stolen)
    assert sso.get("/api/me").status_code == 401
    # With a post-logout URI the browser is sent to the IdP's end-session endpoint.
    monkeypatch.setenv("GMAN_OIDC_POST_LOGOUT_URI", "http://testserver/?logged_out=1")
    sso.cookies.clear()
    sign_in(sso, idp)
    redirect = sso.post("/api/auth/logout", headers={"X-CSRF-Token": me(sso)["csrf_token"]}).json()["redirect"]
    assert redirect.startswith(ISSUER + "/protocol/openid-connect/logout?client_id=" + CLIENT_ID)


def test_expired_session_is_rejected(sso, idp):
    sign_in(sso, idp)
    with Session(engine()) as session:
        session.execute(update(store.UserSession).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        session.commit()
    assert sso.get("/api/me").status_code == 401


def test_bearer_tokens_still_work_with_sso(sso):
    assert sso.get("/api/auth/info").json() == {"oidc": True}
    assert sso.get("/api/overview", headers=bearer("read")).status_code == 200
    assert sso.get("/api/me", headers=bearer("read")).json()["role"] == "viewer"
    assert sso.put("/api/services/api", headers=bearer("read"), json=service("api")).status_code == 401
    assert sso.get("/api/overview", headers=bearer("wrong")).status_code == 401


def test_secure_cookie_over_https(sso, monkeypatch):
    monkeypatch.setenv("GMAN_OIDC_REDIRECT_URI", "https://gman.example/api/auth/callback")
    assert auth.session_cookie() == "__Host-gman_session"
    cookie = sso.get("/api/auth/login", follow_redirects=False).headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie


def test_half_configured_oidc_fails_at_startup(monkeypatch):
    monkeypatch.setenv("GMAN_OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("GMAN_OIDC_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        auth.check_config()


def test_token_mode_without_oidc(client):
    assert client.get("/api/auth/info").json() == {"oidc": False}
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/api/me").status_code == 401
    user = client.get("/api/me", headers=bearer("read")).json()
    assert user["auth"] == "token" and user["role"] == "viewer" and user["csrf_token"] is None
    assert client.get("/api/me", headers=bearer("admin")).json()["role"] == "admin"
    # A cookie means nothing while SSO is off.
    client.cookies.set("gman_session", "anything")
    assert client.get("/api/overview").status_code == 401
