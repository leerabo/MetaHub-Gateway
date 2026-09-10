"""The S1 browser runner keeps source selection inside the workspace."""

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / "scripts/acceptance/s1-organization-browser.py"


@pytest.fixture
def isolated_import(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("METAHUB_S1_SOURCE_ROOT", raising=False)
    monkeypatch.delenv("METAHUB_S1_PRODUCTION", raising=False)
    monkeypatch.delenv("METAHUB_S1_FLOW", raising=False)


def test_default_browser_source_is_unchanged(isolated_import):
    config = runpy.run_path(str(HARNESS))
    assert config["SOURCE"] == ROOT
    assert config["FRONTEND"] == ROOT / "console-web"
    assert config["PRODUCTION"] is False
    assert config["FLOW"] == "organization"


def test_production_mode_serves_the_selected_dist(monkeypatch, isolated_import):
    monkeypatch.setenv("METAHUB_S1_SOURCE_ROOT", str(ROOT))
    monkeypatch.setenv("METAHUB_S1_PRODUCTION", "true")
    config = runpy.run_path(str(HARNESS))
    assert config["SOURCE"] == ROOT
    assert config["FRONTEND"] == ROOT / "console-web/dist"
    assert config["PRODUCTION"] is True


def test_source_outside_workspace_is_rejected(monkeypatch, isolated_import):
    monkeypatch.setenv("METAHUB_S1_SOURCE_ROOT", str(ROOT.parent))
    with pytest.raises(ValueError, match="isolated export"):
        runpy.run_path(str(HARNESS))


def test_malformed_mode_cannot_silently_test_source(monkeypatch, isolated_import):
    monkeypatch.setenv("METAHUB_S1_PRODUCTION", "tru")
    with pytest.raises(ValueError, match="true or false"):
        runpy.run_path(str(HARNESS))


def test_unknown_flow_cannot_silently_test_a_different_business(monkeypatch, isolated_import):
    monkeypatch.setenv("METAHUB_S1_FLOW", "finances")
    with pytest.raises(ValueError, match="organization or finance"):
        runpy.run_path(str(HARNESS))


def test_public_assets_do_not_scan_api_routes(isolated_import):
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    visited = []

    async def control(scope, receive, send):
        visited.append(scope["path"])
        await JSONResponse({"code": "test.unauthorized"}, status_code=401)(scope, receive, send)

    config = runpy.run_path(str(HARNESS))
    with TestClient(config["create_browser_app"](control)) as client:
        for url, content_type in (
            ("/", "text/html"),
            ("/identity.html", "text/html"),
            ("/console.html", "text/html"),
            ("/src/app/main.js", "javascript"),
            ("/src/styles/main.css", "text/css"),
        ):
            response = client.get(url)
            assert response.status_code == 200
            assert content_type in response.headers["content-type"]
        assert visited == []
        assert client.get("/src/missing.js").status_code == 404
        assert visited == []


def test_private_and_unknown_routes_still_reach_control(isolated_import):
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    visited = []

    async def control(scope, receive, send):
        visited.append((scope["method"], scope["path"]))
        await JSONResponse({"code": "test.unauthorized"}, status_code=401)(scope, receive, send)

    config = runpy.run_path(str(HARNESS))
    with TestClient(config["create_browser_app"](control)) as client:
        for method, url in (
            ("GET", "/workspace/session"),
            ("GET", "/consumers"),
            ("POST", "/consumers"),
            ("GET", "/platform/registration-policy"),
            ("GET", "/unknown.html"),
            ("GET", "/.env"),
            ("POST", "/__acceptance/usage"),
        ):
            response = client.request(method, url)
            assert response.status_code == 401
            assert response.json() == {"code": "test.unauthorized"}
            assert visited[-1] == (method, url)
        assert len(visited) == 7


def test_navigation_diagnostics_restore_trace_and_omit_error_details(isolated_import):
    import sqlite3

    config = runpy.run_path(str(HARNESS))
    failures = []
    previous = sys.gettrace()

    def operation():
        raise sqlite3.OperationalError("Private SQL and credential canary")

    wrapped = config["navigation_diagnostics"](operation, failures)
    with pytest.raises(sqlite3.OperationalError):
        wrapped()
    assert sys.gettrace() is previous
    assert failures == [{"function": "operation", "type": "OperationalError", "sqlite_code": None}]
