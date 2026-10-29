"""Browser cookie coexistence must not change the account preference principal."""

from types import SimpleNamespace

import pytest
from metahub_control.console_experience import routes
from metahub_shared.runtime.errors import ApiError
from starlette.requests import Request

GLOBAL_TOKEN = "g" * 43
GLOBAL_CSRF = "c" * 43
WORKSPACE_TOKEN = "w" * 43


def request_for(method="GET", *, cookies=None, extra=()):
    if cookies is None:
        cookies = [
            (routes.IDENTITY_SESSION_COOKIE, GLOBAL_TOKEN),
            (routes.IDENTITY_CSRF_COOKIE, GLOBAL_CSRF),
            (routes.WORKSPACE_SESSION_COOKIE, WORKSPACE_TOKEN),
            (routes.WORKSPACE_CSRF_COOKIE, "x" * 43),
        ]
    return Request({
        "type": "http", "method": method, "path": "/identity/preferences",
        "headers": [(b"cookie", "; ".join(f"{key}={value}" for key, value in cookies).encode())]
        + list(extra),
        "app": SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(allowed_origins=("http://localhost:5173",)),
        )),
    })


@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_coexisting_workspace_cookie_never_supplies_account_authority(monkeypatch, method):
    calls = []
    identity = object()

    def authenticate(config, token):
        calls.append(token)
        return identity

    monkeypatch.setattr(routes, "authenticate_global_session", authenticate)
    request = request_for(method, extra=[
        (b"origin", b"http://localhost:5173"),
        (b"x-csrf-token", GLOBAL_CSRF.encode()),
    ])
    assert routes.require_identity_preferences(request) is identity
    assert calls == [GLOBAL_TOKEN]


@pytest.mark.parametrize("kind,code", [
    ("workspace-only", "identity.session_required"),
    ("duplicate-global", "identity.session_required"),
    ("platform", "identity.credential_invalid"),
    ("support", "identity.credential_invalid"),
    ("authorization", "identity.credential_invalid"),
    ("workspace-csrf", "identity.csrf_invalid"),
    ("foreign-origin", "identity.origin_invalid"),
])
def test_browser_coexistence_keeps_authentication_and_csrf_fail_closed(monkeypatch, kind, code):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid credentials reached account authentication")

    monkeypatch.setattr(routes, "authenticate_global_session", unexpected)
    cookies = [(routes.IDENTITY_SESSION_COOKIE, GLOBAL_TOKEN),
               (routes.IDENTITY_CSRF_COOKIE, GLOBAL_CSRF),
               (routes.WORKSPACE_SESSION_COOKIE, WORKSPACE_TOKEN),
               (routes.WORKSPACE_CSRF_COOKIE, "x" * 43)]
    extra = [(b"origin", b"http://localhost:5173"), (b"x-csrf-token", GLOBAL_CSRF.encode())]
    if kind == "workspace-only":
        cookies = cookies[2:]
    elif kind == "duplicate-global":
        cookies.append(cookies[0])
    elif kind == "platform":
        cookies.append((routes.PLATFORM_SESSION_COOKIE, "p" * 43))
    elif kind == "support":
        cookies.append((routes.SUPPORT_SESSION_COOKIE, "s" * 43))
    elif kind == "authorization":
        extra.append((b"authorization", b"Bearer test-only"))
    elif kind == "workspace-csrf":
        extra[1] = (b"x-csrf-token", ("x" * 43).encode())
    elif kind == "foreign-origin":
        extra[0] = (b"origin", b"https://outside.example")
    with pytest.raises(ApiError) as error:
        routes.require_identity_preferences(request_for(
            "PATCH" if kind in {"workspace-csrf", "foreign-origin"} else "GET",
            cookies=cookies, extra=extra,
        ))
    assert error.value.code == code
