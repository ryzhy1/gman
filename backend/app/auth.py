"""Human sign-in: OIDC Authorization Code + PKCE with a server-side session and roles from IdP groups.

The backend is a confidential client. The browser only ever holds an opaque HttpOnly session cookie;
mutating requests made with it must carry the session's CSRF token in X-CSRF-Token.
Machine credentials (agent and CI tokens) and the Bearer read/admin tokens are handled in main.py.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from sqlalchemy import delete

from . import db as store

logger = logging.getLogger("gman.auth")

ROLES = ("viewer", "owner", "admin")  # ascending privilege
# Asymmetric algorithms only: "none" and HMAC (keyed with the client secret) are never accepted.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
LOGIN_COOKIE = "gman_login"
LOGIN_TTL = 600
CSRF_HEADER = "X-CSRF-Token"

client = httpx.Client(timeout=10)  # replaced in tests with a MockTransport client


class Failure(Exception):
    """Sign-in failed; `code` is shown to the user, the message is only logged."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def env(name, default=""):
    return os.getenv(name, default).strip()


def names(value):
    return {item.strip() for item in value.split(",") if item.strip()}


def enabled():
    return bool(env("GMAN_OIDC_ISSUER"))


def check_config():
    """Fail at startup instead of at the first login when OIDC is half-configured."""
    if not enabled():
        return
    missing = [n for n in ("GMAN_OIDC_CLIENT_ID", "GMAN_OIDC_CLIENT_SECRET", "GMAN_OIDC_REDIRECT_URI") if not env(n)]
    if missing:
        raise RuntimeError("OIDC is enabled by GMAN_OIDC_ISSUER but not configured: " + ", ".join(missing))
    if urlsplit(env("GMAN_OIDC_REDIRECT_URI")).scheme not in ("http", "https"):
        raise RuntimeError("GMAN_OIDC_REDIRECT_URI must be an absolute HTTP(S) URL")
    if not any(env(n) for n in ("GMAN_OIDC_ADMIN_GROUPS", "GMAN_OIDC_OWNER_GROUPS", "GMAN_OIDC_VIEWER_GROUPS")):
        logger.warning("No GMAN_OIDC_*_GROUPS configured: every SSO sign-in will be refused")


def secure():
    """Secure cookies whenever the portal is served over HTTPS (production); plain HTTP only for local runs."""
    return urlsplit(env("GMAN_OIDC_REDIRECT_URI")).scheme == "https"


def session_cookie():
    # The __Host- prefix makes the browser refuse the cookie unless Secure, Path=/ and no Domain.
    return "__Host-gman_session" if secure() else "gman_session"


def session_hours():
    return float(env("GMAN_SESSION_HOURS", "8") or 8)


# --- Provider metadata and keys -------------------------------------------------------------------

class Provider:
    """Discovery document and JWKS, cached; keys are refetched on an unknown kid (key rotation)."""

    def __init__(self, issuer):
        self.issuer = issuer
        self.meta, self.meta_at = None, 0.0
        self.keys, self.keys_at = {}, float("-inf")

    def get(self, url, **kwargs):
        try:
            response = client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise Failure("idp_unavailable", f"GET {url}: {error}") from None

    def metadata(self):
        if self.meta is None or time.monotonic() - self.meta_at > 3600:
            meta = self.get(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
            if meta.get("issuer") != self.issuer:
                raise Failure("idp_unavailable", "Discovery issuer does not match GMAN_OIDC_ISSUER")
            self.meta, self.meta_at = meta, time.monotonic()
        return self.meta

    def key(self, kid):
        if (kid not in self.keys or not self.keys) and time.monotonic() - self.keys_at > 30:
            keys = {}
            for item in self.get(self.metadata()["jwks_uri"]).get("keys", []):
                if item.get("use", "sig") != "sig":
                    continue
                try:
                    keys[item.get("kid")] = jwt.PyJWK(item)
                except jwt.PyJWTError:
                    continue  # key types this server cannot use
            self.keys, self.keys_at = keys, time.monotonic()
        if kid is None and len(self.keys) == 1:
            return next(iter(self.keys.values()))
        if kid not in self.keys:
            raise Failure("invalid_token", f"Unknown signing key {kid!r}")
        return self.keys[kid]


providers = {}


def provider():
    issuer = env("GMAN_OIDC_ISSUER")
    if issuer not in providers:
        providers[issuer] = Provider(issuer)
    return providers[issuer]


def reset():
    providers.clear()


# --- Login state (state, nonce, PKCE verifier) in a short-lived signed cookie ------------------------

def state_key():
    # Derived from the client secret so that replicas agree without one more secret to distribute.
    return hmac.new(env("GMAN_OIDC_CLIENT_SECRET").encode(), b"gman login state v1", hashlib.sha256).digest()


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload):
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + b64(hmac.new(state_key(), body.encode(), hashlib.sha256).digest())


def unsign(value):
    try:
        body, mac = value.split(".", 1)
        expected = b64(hmac.new(state_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac.encode(), expected.encode()):
            return None
        payload = json.loads(unb64(body))
    except (ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("exp", 0) > time.time() else None


def local_path(value):
    """Only same-origin paths are accepted as the post-login target (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


def start_login(next_path):
    """Returns (authorization URL, login cookie value)."""
    meta = provider().metadata()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = b64(hashlib.sha256(verifier.encode()).digest())
    query = {"response_type": "code", "client_id": env("GMAN_OIDC_CLIENT_ID"),
             "redirect_uri": env("GMAN_OIDC_REDIRECT_URI"), "scope": env("GMAN_OIDC_SCOPES", "openid profile email"),
             "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    cookie = sign({"state": state, "nonce": nonce, "verifier": verifier, "next": local_path(next_path),
                   "exp": int(time.time()) + LOGIN_TTL})
    endpoint = meta["authorization_endpoint"]
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(query), cookie


# --- Callback: code exchange and ID token validation ---------------------------------------------

def exchange(code, verifier):
    meta = provider().metadata()
    client_id, secret = env("GMAN_OIDC_CLIENT_ID"), env("GMAN_OIDC_CLIENT_SECRET")
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": env("GMAN_OIDC_REDIRECT_URI"),
            "code_verifier": verifier}
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    auth = None
    if "client_secret_basic" in methods:
        auth = (quote(client_id, safe=""), quote(secret, safe=""))  # RFC 6749 2.3.1: form-encode, then Basic
    else:
        data.update(client_id=client_id, client_secret=secret)
    try:
        response = client.post(meta["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
        tokens = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise Failure("idp_unavailable", f"Token endpoint: {error}") from None
    if response.status_code != 200 or not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise Failure("token_exchange", f"Token endpoint returned {response.status_code}: {str(tokens)[:300]}")
    return tokens


def verify_id_token(token, nonce):
    client_id = env("GMAN_OIDC_CLIENT_ID")
    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in ALGORITHMS:
            raise Failure("invalid_token", f"Algorithm {algorithm!r} is not accepted")
        key = provider().key(header.get("kid"))
        claims = jwt.decode(token, key=key.key, algorithms=[algorithm], audience=client_id,
                            issuer=env("GMAN_OIDC_ISSUER"), leeway=60,
                            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
    except jwt.PyJWTError as error:
        raise Failure("invalid_token", f"ID token rejected: {error}") from None
    audience = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(audience) > 1 and claims.get("azp") != client_id:
        raise Failure("invalid_token", "azp does not name this client")
    if not hmac.compare_digest(str(claims["nonce"]).encode(), nonce.encode()):
        raise Failure("invalid_token", "Nonce mismatch")
    return claims


def claim(claims, path):
    """Dotted path, e.g. `groups` or Keycloak's `realm_access.roles`."""
    value = claims
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value] if isinstance(value, list) else None


def groups_of(claims, tokens):
    path = env("GMAN_OIDC_GROUPS_CLAIM", "groups")
    found = claim(claims, path)
    if found is not None:
        return found
    # Some IdPs only release groups from userinfo; it must describe the same subject.
    endpoint = provider().metadata().get("userinfo_endpoint")
    if endpoint and tokens.get("access_token"):
        info = provider().get(endpoint, headers={"Authorization": "Bearer " + tokens["access_token"]})
        if info.get("sub") == claims["sub"]:
            return claim(info, path) or []
    return []


def role_for(groups):
    """Highest role whose configured groups intersect the user's; `*` in viewer groups admits everyone."""
    groups = set(groups)
    for role, variable in (("admin", "GMAN_OIDC_ADMIN_GROUPS"), ("owner", "GMAN_OIDC_OWNER_GROUPS"),
                           ("viewer", "GMAN_OIDC_VIEWER_GROUPS")):
        configured = names(env(variable))
        if groups & configured or (role == "viewer" and "*" in configured):
            return role
    return None


def complete_login(session, code, state, cookie):
    """Validate the callback and create a session. Returns (cookie value, next path)."""
    pending = unsign(cookie or "")
    if not pending or not state or not hmac.compare_digest(pending["state"].encode(), state.encode()):
        raise Failure("state", "Login state missing, expired or mismatched")
    tokens = exchange(code, pending["verifier"])
    claims = verify_id_token(tokens["id_token"], pending["nonce"])
    groups = groups_of(claims, tokens)
    role = role_for(groups)
    if role is None:
        raise Failure("forbidden", f"No GMan role for {claims['sub']} (groups: {sorted(groups)[:20]})")
    value = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session.execute(delete(store.UserSession).where(store.UserSession.expires_at < now))
    session.add(store.UserSession(
        id=digest(value), subject=str(claims["sub"]), role=role, csrf_token=secrets.token_urlsafe(32),
        data={"email": str(claims.get("email") or ""), "name": str(claims.get("name") or ""),
              "username": str(claims.get("preferred_username") or ""), "groups": groups[:500]},
        created_at=now, expires_at=now + timedelta(hours=session_hours())))
    session.commit()
    logger.info("SSO sign-in: %s as %s", claims.get("email") or claims["sub"], role)
    return value, pending["next"]


def logout_url():
    """IdP end-session URL when configured, otherwise the portal itself in signed-out state."""
    target = env("GMAN_OIDC_POST_LOGOUT_URI")
    try:
        endpoint = provider().metadata().get("end_session_endpoint") if target else None
    except Failure:
        endpoint = None
    if not endpoint:
        return "/?logged_out=1"
    query = urlencode({"client_id": env("GMAN_OIDC_CLIENT_ID"), "post_logout_redirect_uri": target})
    return endpoint + ("&" if "?" in endpoint else "?") + query


# --- Principals -----------------------------------------------------------------------------------

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    kind: str  # "session" (SSO user) or "token" (shared Bearer token)
    role: str
    subject: str
    email: str = ""
    name: str = ""
    username: str = ""
    groups: tuple = ()
    csrf_token: str = ""
    session_id: str = field(default="", repr=False)

    def at_least(self, role):
        return ROLES.index(self.role) >= ROLES.index(role)

    @property
    def actor(self):
        return self.email or self.username or self.subject

    def identities(self):
        """What a service's `owners` entries are matched against, case-insensitively."""
        ids = {self.subject, self.email, self.username} | {"group:" + g for g in self.groups}
        return sorted({i.casefold() for i in ids if i and i != "group:"})

    def owns(self, service):
        mine = set(self.identities())
        return any(str(o).strip().casefold() in mine for o in service.get("owners") or [])

    def can_edit(self, service):
        return self.role == "admin" or (self.role == "owner" and self.kind == "session" and self.owns(service))


def from_session(session, value):
    if not value:
        return None
    row = session.get(store.UserSession, digest(value))
    if row is None or row.expires_at <= datetime.now(timezone.utc):
        return None
    data = row.data or {}
    return Principal(kind="session", role=row.role, subject=row.subject, email=data.get("email", ""),
                     name=data.get("name", ""), username=data.get("username", ""),
                     groups=tuple(data.get("groups", [])), csrf_token=row.csrf_token, session_id=row.id)


def csrf_ok(principal, header):
    return bool(header) and hmac.compare_digest(header.encode("utf-8", "replace"), principal.csrf_token.encode())


def end_session(session, principal):
    session.execute(delete(store.UserSession).where(store.UserSession.id == principal.session_id))
    session.commit()
