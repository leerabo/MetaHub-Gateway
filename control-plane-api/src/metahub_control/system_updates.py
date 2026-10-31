"""Owner-only requests for locally staged releases; no Docker or shell access."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.config import build_service_info
from metahub_control.platform_access.constants import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE, SUPPORT_SESSION_COOKIE
from metahub_control.platform_access.service import authenticate_platform_session_context
from metahub_control.platform_access.service import platform_session_matches_global_cookie
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
from metahub_control.saas_billing.routes import _cookie_values, _private, _raw_headers, _require_origin, _required_cookie, _required_csrf, _strict_json
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

router = APIRouter(tags=["system-updates"])
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")
OPERATIONS = {"upgrade", "restart"}


def version_order(value):
    base, separator, prerelease = value.partition("-")
    identifiers = tuple((0, int(part)) if part.isdigit() else (1, part)
                        for part in prerelease.split(".")) if separator else ()
    return (*(int(part) for part in base.split(".")), not bool(separator), identifiers)


def _owner(request):
    if _raw_headers(request, b"authorization") or _cookie_values(request, WORKSPACE_SESSION_COOKIE) or _cookie_values(request, SUPPORT_SESSION_COOKIE):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method == "POST":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(request.app.state.config, _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"), csrf_token=csrf)
    global_values = _cookie_values(request, GLOBAL_SESSION_COOKIE)
    if global_values and (len(global_values) != 1 or not platform_session_matches_global_cookie(request.app.state.config, context, global_values[0])):
        raise ApiError("platform.credential_conflict")
    if "platform_owner" not in context.roles:
        raise ApiError("platform.permission_denied")
    return context


def read_catalog(root: Path):
    value = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("releases"), list):
        raise ValueError("Invalid release catalog")
    releases = []
    for item in value["releases"]:
        if not isinstance(item, dict) or not VERSION.fullmatch(str(item.get("version", ""))):
            raise ValueError("Invalid release version")
        releases.append({"version": item["version"], "summary": str(item.get("summary", ""))[:300]})
    return releases


def _root():
    configured = os.environ.get("METAHUB_UPDATE_QUEUE_DIR", "")
    return Path(configured) if configured else None


def _current_version(request):
    # AppConfig contains business configuration, not image version metadata.
    # Use the same source as /version so catalog filtering and downgrade checks
    # describe the installed image even when the retained state file is older.
    value = build_service_info(request.app.state.config, "", "").version
    if not VERSION.fullmatch(value):
        raise ApiError("platform.unavailable")
    return value


@router.get("/platform/system-updates")
async def status(request: Request, response: Response):
    await run_in_threadpool(_owner, request)
    _private(response)
    root = _root()
    current = _current_version(request)
    if root is None:
        return {"enabled": False, "current_version": current, "releases": [], "jobs": []}
    try:
        releases = [item for item in read_catalog(root) if version_order(item["version"]) > version_order(current)]
        jobs = []
        for path in sorted((root / "status").glob("*.json"), reverse=True)[:20]:
            value = json.loads(path.read_text(encoding="utf-8"))
            jobs.append({key: value.get(key) for key in ("job_id", "operation", "version", "status", "message", "updated_at")})
        blocked = any(job["status"] in {"running", "failed"} for job in jobs) or any((root / "requests").glob("*.json"))
        return {"enabled": True, "current_version": current, "releases": releases, "jobs": jobs, "upgrade_blocked": blocked}
    except (OSError, ValueError, TypeError):
        raise ApiError("platform.unavailable") from None


@router.post("/platform/system-updates", status_code=202)
async def request_update(request: Request, response: Response):
    context = await run_in_threadpool(_owner, request)
    if request.url.query:
        raise ApiError("platform.request_invalid")
    payload = await _strict_json(request, "platform.request_invalid")
    if not isinstance(payload, dict) or set(payload) != {"version", "confirmed"} or payload["confirmed"] is not True or not isinstance(payload["version"], str) or not VERSION.fullmatch(payload["version"]):
        raise ApiError("platform.request_invalid")
    root = _root()
    if root is None:
        raise ApiError("platform.unavailable")
    try:
        if payload["version"] not in {item["version"] for item in read_catalog(root)}:
            raise ApiError("platform.request_invalid")
        if version_order(payload["version"]) <= version_order(_current_version(request)):
            raise ApiError("platform.request_invalid")
        if any((root / "requests").glob("*.json")) or any(json.loads(path.read_text(encoding="utf-8")).get("status") in {"running", "failed"} for path in (root / "status").glob("*.json")):
            raise ApiError("platform.unavailable")
        job_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex
        job = {"job_id": job_id, "operation": "upgrade", "version": payload["version"], "account_id": context.account_id, "requested_at": datetime.now(UTC).isoformat()}
        path = root / "requests" / (job_id + ".json")
        # The host sees only fully written files. Hard-link publishes without
        # overwriting an existing request; the request never contains commands.
        temporary = path.with_suffix(".pending")
        with temporary.open("x", encoding="utf-8") as output:
            temporary.chmod(0o600)
            json.dump(job, output)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        temporary.unlink()
        _private(response)
        return {"job_id": job_id, "status": "queued", "version": payload["version"]}
    except ApiError:
        raise
    except (OSError, ValueError, TypeError):
        raise ApiError("platform.unavailable") from None


@router.post("/platform/system-restart", status_code=202)
async def request_restart(request: Request, response: Response):
    """Queue a controlled restart of the current production services."""
    context = await run_in_threadpool(_owner, request)
    if request.url.query:
        raise ApiError("platform.request_invalid")
    payload = await _strict_json(request, "platform.request_invalid")
    if not isinstance(payload, dict) or set(payload) != {"confirmed"} or payload["confirmed"] is not True:
        raise ApiError("platform.request_invalid")
    root = _root()
    if root is None:
        raise ApiError("platform.unavailable")
    try:
        current = _current_version(request)
        if any((root / "requests").glob("*.json")) or any(
            json.loads(path.read_text(encoding="utf-8")).get("status") in {"running", "failed"}
            for path in (root / "status").glob("*.json")
        ):
            raise ApiError("platform.unavailable")
        job_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex
        job = {
            "job_id": job_id,
            "operation": "restart",
            "version": current,
            "account_id": context.account_id,
            "requested_at": datetime.now(UTC).isoformat(),
        }
        path = root / "requests" / (job_id + ".json")
        temporary = path.with_suffix(".pending")
        with temporary.open("x", encoding="utf-8") as output:
            temporary.chmod(0o600)
            json.dump(job, output)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        temporary.unlink()
        _private(response)
        return {"job_id": job_id, "status": "queued", "operation": "restart", "version": current}
    except ApiError:
        raise
    except (OSError, ValueError, TypeError):
        raise ApiError("platform.unavailable") from None
