"""Browser GET export origin fallback does not weaken write or CSRF boundaries."""

from types import SimpleNamespace

import pytest
from metahub_control.auth.dependencies import _require_workspace_origin, _workspace_csrf
from metahub_control.workspace_access.constants import WORKSPACE_CSRF_COOKIE
from metahub_shared.runtime.errors import ApiError
from starlette.requests import Request

ORIGIN = "https://console.example.test"
BROWSER = [("sec-fetch-site", "same-origin"), ("referer", ORIGIN + "/console.html")]


def request(headers, method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/usage/export.csv",
            "headers": [(name.encode("ascii"), value.encode("latin-1")) for name, value in headers],
            "app": SimpleNamespace(
                state=SimpleNamespace(config=SimpleNamespace(allowed_origins=[ORIGIN]))
            ),
        }
    )


@pytest.mark.parametrize("headers", [BROWSER, [("origin", ORIGIN)], [*BROWSER, ("origin", ORIGIN)]])
def test_trusted_get_export_origin(headers):
    _require_workspace_origin(request(headers))


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("referer", ORIGIN + "/console.html")],
        [("sec-fetch-site", "same-origin")],
        [("sec-fetch-site", "cross-site"), ("referer", ORIGIN)],
        [("sec-fetch-site", "same-site"), ("referer", ORIGIN)],
        [*BROWSER, ("sec-fetch-site", "same-origin")],
        [*BROWSER, ("referer", ORIGIN)],
        [*BROWSER, ("origin", "null")],
        [*BROWSER, ("origin", "")],
        [*BROWSER, ("origin", "https://attacker.example")],
        [*BROWSER, ("origin", ORIGIN), ("origin", ORIGIN)],
        [("origin", ORIGIN + "," + ORIGIN)],
        [("origin", ORIGIN), ("sec-fetch-site", "cross-site")],
    ],
)
def test_ambiguous_or_cross_site_export_origin_is_rejected(headers):
    with pytest.raises(ApiError) as error:
        _require_workspace_origin(request(headers))
    assert error.value.code == "onboarding.origin_invalid"


@pytest.mark.parametrize(
    "referrer",
    [
        ORIGIN + ".attacker.example/",
        "https://attacker.example/" + ORIGIN,
        "https://user@console.example.test/",
        "https://console.example.test:443/",
        "/console.html",
        "javascript:alert(1)",
        "https://[invalid/",
        ORIGIN + "/#fragment",
        ORIGIN + "/\nprivate",
        " " + ORIGIN,
    ],
)
def test_referrer_is_an_exact_parsed_origin(referrer):
    with pytest.raises(ApiError) as error:
        _require_workspace_origin(
            request([("sec-fetch-site", "same-origin"), ("referer", referrer)])
        )
    assert error.value.code == "onboarding.origin_invalid"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_origin_fallback_is_get_only(method):
    with pytest.raises(ApiError):
        _require_workspace_origin(request(BROWSER, method))
    _require_workspace_origin(request([("origin", ORIGIN)], method))


def test_trusted_browser_origin_does_not_replace_csrf():
    for headers in [
        BROWSER,
        [*BROWSER, ("x-csrf-token", "a" * 43)],
        [*BROWSER, ("cookie", WORKSPACE_CSRF_COOKIE + "=" + "a" * 43)],
    ]:
        browser_request = request(headers)
        _require_workspace_origin(browser_request)
        with pytest.raises(ApiError) as error:
            _workspace_csrf(browser_request)
        assert error.value.code == "workspace_session.csrf_invalid"
