"""Cloudflare Access SSO: JWT verification and email → user mapping."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src import cf_access

ROOT = Path(__file__).resolve().parents[1]
TEAM = "team.cloudflareaccess.com"
AUD = "aud-odysseus"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
OTHER_KEY = _key()


class _FakeJWKS:
    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, _token):
        return jwt.PyJWK.from_dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key())))


def _token(key=SIGNING_KEY, **overrides):
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "email": "Mat@Example.com",
        "iat": now,
        "exp": now + 600,
        "type": "app",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


class _AuthManager:
    def __init__(self, users, admins=()):
        self.users = {u: {} for u in users}
        self._admins = set(admins)

    def is_admin(self, username):
        return username in self._admins


class _Request:
    def __init__(self, token=None, extra=None):
        self.headers = dict(extra or {})
        if token:
            self.headers[cf_access.JWT_HEADER] = token


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", f"https://{TEAM}/")
    monkeypatch.setenv("CF_ACCESS_AUD", f"other-app, {AUD}")
    monkeypatch.setenv("CF_ACCESS_USERS", "mat@example.com=Mat, ana@example.com=ana")
    monkeypatch.delenv("CF_ACCESS_ALLOW_ADMINS", raising=False)
    monkeypatch.setattr(cf_access, "_jwks_client", lambda team: _FakeJWKS(SIGNING_KEY))


def test_disabled_without_full_config(monkeypatch):
    assert cf_access.enabled()
    monkeypatch.delenv("CF_ACCESS_USERS")
    assert not cf_access.enabled()
    assert cf_access.verified_email(_token()) is None


def test_valid_token_maps_to_user():
    manager = _AuthManager(["mat", "ana"])
    assert cf_access.verified_email(_token()) == "mat@example.com"
    assert cf_access.username_for_request(_Request(_token()), manager) == "mat"


@pytest.mark.parametrize(
    "token",
    [
        _token(key=OTHER_KEY),
        _token(aud=["someone-else"]),
        _token(iss="https://evil.cloudflareaccess.com"),
        _token(exp=int(time.time()) - 60),
        _token(exp=None),
        "not-a-jwt",
    ],
    ids=["forged-signature", "wrong-aud", "wrong-issuer", "expired", "no-exp", "garbage"],
)
def test_rejected_tokens(token):
    assert cf_access.verified_email(token) is None
    assert cf_access.username_for_request(_Request(token), _AuthManager(["mat"])) is None


def test_plain_email_header_is_not_trusted():
    request = _Request(extra={"cf-access-authenticated-user-email": "mat@example.com"})
    assert cf_access.username_for_request(request, _AuthManager(["mat"])) is None


def test_unmapped_email_is_rejected():
    request = _Request(_token(email="stranger@example.com"))
    assert cf_access.username_for_request(request, _AuthManager(["mat"])) is None


def test_mapping_to_missing_user_is_rejected():
    assert cf_access.username_for_request(_Request(_token()), _AuthManager(["ana"])) is None


def test_admins_allowed_by_default_and_can_be_excluded(monkeypatch):
    manager = _AuthManager(["mat"], admins=["mat"])
    assert cf_access.username_for_request(_Request(_token()), manager) == "mat"
    monkeypatch.setenv("CF_ACCESS_ALLOW_ADMINS", "false")
    assert cf_access.username_for_request(_Request(_token()), manager) is None


def test_auth_middleware_signs_in_from_access_jwt():
    """Drive the real AuthMiddleware with a stubbed identity check."""
    env = dict(os.environ)
    env.update({
        "AUTH_ENABLED": "true",
        "CF_ACCESS_TEAM_DOMAIN": TEAM,
        "CF_ACCESS_AUD": AUD,
        "CF_ACCESS_USERS": "mat@example.com=mat",
    })
    probe = textwrap.dedent(
        """
        import asyncio
        import json

        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        import app as app_module
        from src import cf_access


        class _AuthManager:
            is_configured = True
            users = {"mat": {}}

            def __init__(self):
                self.sessions = []

            def validate_token(self, token):
                return token in self.sessions

            def get_username_for_token(self, token):
                return "mat" if token in self.sessions else None

            def create_session_trusted(self, username):
                self.sessions.append("tok-%d" % len(self.sessions))
                return self.sessions[-1]


        cf_access.username_for_request = (
            lambda request, manager: "mat" if request.headers.get(cf_access.JWT_HEADER) == "good" else None
        )


        async def _case(route_path, *, jwt_header=None, query=b""):
            manager = _AuthManager()
            app_module.auth_manager = manager
            seen = []

            async def endpoint(request):
                seen.append(getattr(request.state, "current_user", None))
                return JSONResponse({"reached": True})

            downstream = Starlette(routes=[Route(route_path, endpoint)])
            middleware = app_module.AuthMiddleware(downstream)
            headers = [(b"cf-access-jwt-assertion", jwt_header.encode())] if jwt_header else []
            scope = {
                "type": "http", "http_version": "1.1", "method": "GET", "scheme": "https",
                "path": route_path, "raw_path": route_path.encode(), "root_path": "",
                "query_string": query, "headers": headers,
                "client": ("192.0.2.10", 4321), "server": ("testserver", 443), "app": downstream,
            }
            sent = []
            done = False

            async def receive():
                nonlocal done
                if done:
                    return {"type": "http.disconnect"}
                done = True
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            hdrs = [(k.decode().lower(), v.decode()) for k, v in start["headers"]]
            cookies = [v for k, v in hdrs if k == "set-cookie"]
            return {
                "status": start["status"],
                "location": dict(hdrs).get("location"),
                "user": seen[0] if seen else None,
                "cookie": cookies[0].split(";")[0] if cookies else None,
                "secure": bool(cookies) and "secure" in cookies[0].lower(),
            }


        async def main():
            print("RESULT=" + json.dumps({
                "page_sso": await _case("/notes", jwt_header="good"),
                "page_no_jwt": await _case("/notes"),
                "page_bad_jwt": await _case("/notes", jwt_header="bad"),
                "api_sso": await _case("/api/models", jwt_header="good"),
                "api_no_jwt": await _case("/api/models"),
                "login_sso": await _case("/login", jwt_header="good"),
                "login_local": await _case("/login", jwt_header="good", query=b"local=1"),
            }))


        asyncio.run(main())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    line = next(l for l in result.stdout.splitlines() if l.startswith("RESULT="))
    r = json.loads(line[len("RESULT="):])

    assert r["page_sso"] == {"status": 200, "location": None, "user": "mat",
                             "cookie": "odysseus_session=tok-0", "secure": True}
    assert r["page_no_jwt"]["status"] == 302 and r["page_no_jwt"]["location"] == "/login"
    assert r["page_bad_jwt"]["status"] == 302 and r["page_bad_jwt"]["cookie"] is None
    assert r["api_sso"]["status"] == 200 and r["api_sso"]["cookie"] == "odysseus_session=tok-0"
    assert r["api_no_jwt"]["status"] == 401
    assert r["login_sso"]["status"] == 302 and r["login_sso"]["location"] == "/"
    assert r["login_sso"]["cookie"] == "odysseus_session=tok-0"
    assert r["login_local"] == {"status": 200, "location": None, "user": None,
                                "cookie": None, "secure": False}
