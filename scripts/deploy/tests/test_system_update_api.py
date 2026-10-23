"""Synthetic platform contexts exercise update permissions and queue boundaries."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "control-plane-api/src"), str(ROOT / "shared-sdk/python")]
from metahub_control import system_updates as updates
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.config import load_app_config


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("METAHUB_VERSION", "2.0.0-trial.1")
    (tmp_path / "requests").mkdir()
    (tmp_path / "status").mkdir()
    (tmp_path / "catalog.json").write_text(json.dumps({"releases": [
        {"version": "2.0.0-trial.1", "summary": "current"},
        {"version": "2.0.0-trial.2", "summary": "next"},
    ]}))
    monkeypatch.setenv("METAHUB_UPDATE_QUEUE_DIR", str(tmp_path))
    monkeypatch.setattr(updates, "_raw_headers", lambda *args: [])
    monkeypatch.setattr(updates, "_cookie_values", lambda *args: [])
    monkeypatch.setattr(updates, "_required_cookie", lambda *args: "synthetic")
    monkeypatch.setattr(updates, "_require_origin", lambda *args: None)
    monkeypatch.setattr(updates, "_required_csrf", lambda *args: "csrf")
    monkeypatch.setattr(updates, "authenticate_platform_session_context", lambda *args, **kwargs: SimpleNamespace(roles=("platform_owner",), account_id="synthetic-owner"))
    app = FastAPI()
    app.state.config = load_app_config("control-plane-api")
    app.include_router(updates.router)
    @app.exception_handler(ApiError)
    async def handler(request, error):
        return JSONResponse({"code": error.code}, status_code=403)
    return TestClient(app), tmp_path


def test_platform_owner_sees_newer_versions_only(client):
    browser, root = client
    assert [item["version"] for item in browser.get("/platform/system-updates").json()["releases"]] == ["2.0.0-trial.2"]


def test_current_version_uses_image_metadata_with_real_app_config(client, monkeypatch):
    browser, root = client
    assert not hasattr(browser.app.state.config, "version")
    monkeypatch.setenv("METAHUB_VERSION", "2.0.0-trial.2")
    result = browser.get("/platform/system-updates")
    assert result.status_code == 200
    assert result.json()["current_version"] == "2.0.0-trial.2"
    assert result.json()["releases"] == []
    assert browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True}).json()["code"] == "platform.request_invalid"
    assert not list((root / "requests").iterdir())


def test_invalid_image_version_cannot_queue_upgrade(client, monkeypatch):
    browser, root = client
    monkeypatch.setenv("METAHUB_VERSION", "unknown")
    assert browser.get("/platform/system-updates").json()["code"] == "platform.unavailable"
    assert browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True}).json()["code"] == "platform.unavailable"
    assert not list((root / "requests").iterdir())


def test_non_owner_cannot_queue_host_upgrade(client, monkeypatch):
    browser, root = client
    monkeypatch.setattr(updates, "authenticate_platform_session_context", lambda *args, **kwargs: SimpleNamespace(roles=("platform_finance",), account_id="synthetic"))
    result = browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True})
    assert result.json()["code"] == "platform.permission_denied"
    assert list((root / "requests").iterdir()) == []


def test_csrf_failure_cannot_create_request(client, monkeypatch):
    browser, root = client
    def reject(*args):
        raise ApiError("platform.csrf_invalid")
    monkeypatch.setattr(updates, "_required_csrf", reject)
    assert browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True}).json()["code"] == "platform.csrf_invalid"
    assert not list((root / "requests").iterdir())


@pytest.mark.parametrize("payload", [
    {"version": "2.0.0-trial.1", "confirmed": True},
    {"version": "2.0.0-trial.9", "confirmed": True},
    {"version": "../../escape", "confirmed": True},
    {"version": "2.0.0-trial.2", "confirmed": False},
    {"version": "2.0.0-trial.2", "confirmed": True, "command": "arbitrary"},
])
def test_only_confirmed_catalog_versions_can_be_queued(client, payload):
    browser, root = client
    assert browser.post("/platform/system-updates", json=payload).json()["code"] == "platform.request_invalid"
    assert not list((root / "requests").iterdir())


def test_queued_job_contains_only_identity_and_version_metadata(client):
    browser, root = client
    result = browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True})
    assert result.status_code == 202
    path = root / "requests" / (result.json()["job_id"] + ".json")
    job = json.loads(path.read_text())
    assert set(job) == {"job_id", "version", "account_id", "requested_at"}
    assert not list((root / "requests").glob("*.pending"))


def test_failed_upgrade_blocks_further_requests(client):
    browser, root = client
    (root / "status/failed.json").write_text(json.dumps({"status": "failed"}))
    assert browser.get("/platform/system-updates").json()["upgrade_blocked"] is True
    assert browser.post("/platform/system-updates", json={"version": "2.0.0-trial.2", "confirmed": True}).json()["code"] == "platform.unavailable"
    assert not list((root / "requests").iterdir())
