"""Single sign-on through Cloudflare Access.

When Odysseus sits behind a Cloudflare Tunnel protected by an Access
application, every request that reaches the app carries a JWT signed by the
Access team (``Cf-Access-Jwt-Assertion``). If that JWT verifies and its email
is mapped to an existing Odysseus user, the auth middleware issues a normal
session for that user instead of sending the browser to the login form.

Nothing here trusts the plain ``Cf-Access-Authenticated-User-Email`` header:
anything that can reach the app port directly could forge it. Only the
signature, audience and issuer of the JWT are trusted.

Configuration (all required to enable it):

    CF_ACCESS_TEAM_DOMAIN   e.g. "myteam.cloudflareaccess.com"
    CF_ACCESS_AUD           Application Audience (AUD) tag; comma-separated
                            to accept several Access applications
    CF_ACCESS_USERS         "alice@example.com=alice,bob@example.com=bob"

Optional:

    CF_ACCESS_ALLOW_ADMINS  "false" keeps admin accounts on password login
                            (default "true")
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional

import jwt

logger = logging.getLogger(__name__)

JWT_HEADER = "cf-access-jwt-assertion"
# GET /login?local=1 always shows the password form, so a user can still sign
# in as a different (or unmapped) account while SSO is enabled.
LOCAL_LOGIN_PARAM = "local"

_jwks_lock = threading.Lock()
_jwks_clients: Dict[str, jwt.PyJWKClient] = {}


def _team_domain() -> str:
    raw = (os.getenv("CF_ACCESS_TEAM_DOMAIN") or "").strip().lower()
    for prefix in ("https://", "http://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw.strip("/")


def _audiences() -> List[str]:
    return [a.strip() for a in (os.getenv("CF_ACCESS_AUD") or "").split(",") if a.strip()]


def _user_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for entry in (os.getenv("CF_ACCESS_USERS") or "").split(","):
        email, sep, username = entry.partition("=")
        email, username = email.strip().lower(), username.strip().lower()
        if sep and email and username:
            mapping[email] = username
    return mapping


def _allow_admins() -> bool:
    return (os.getenv("CF_ACCESS_ALLOW_ADMINS") or "true").strip().lower() not in ("0", "false", "no", "off")


def enabled() -> bool:
    return bool(_team_domain() and _audiences() and _user_map())


def _jwks_client(team: str) -> jwt.PyJWKClient:
    with _jwks_lock:
        client = _jwks_clients.get(team)
        if client is None:
            client = jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True, timeout=10)
            _jwks_clients[team] = client
        return client


def verified_email(token: Optional[str]) -> Optional[str]:
    """Return the email of a valid Access JWT, or None. Blocking (may fetch keys)."""
    if not token or not enabled():
        return None
    team = _team_domain()
    try:
        signing_key = _jwks_client(team).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_audiences(),
            issuer=f"https://{team}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except Exception as exc:
        logger.warning("Rejected Cloudflare Access JWT: %s", exc)
        return None
    email = claims.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def username_for_request(request, auth_manager) -> Optional[str]:
    """Map a request's verified Access identity to an Odysseus username."""
    if not enabled():
        return None
    email = verified_email(request.headers.get(JWT_HEADER))
    if not email:
        return None
    username = _user_map().get(email)
    if not username:
        logger.info("Cloudflare Access user %s is not mapped to an Odysseus account", email)
        return None
    if username not in auth_manager.users:
        logger.warning("CF_ACCESS_USERS maps %s to missing user '%s'", email, username)
        return None
    if not _allow_admins() and auth_manager.is_admin(username):
        return None
    return username


def wants_local_login(request) -> bool:
    return LOCAL_LOGIN_PARAM in request.query_params


def set_session_cookie(response, request, token: str) -> None:
    from core.auth import TOKEN_TTL
    from routes.auth_routes import SESSION_COOKIE, _secure_cookie

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_secure_cookie(request),
        path="/",
        max_age=TOKEN_TTL,
    )
