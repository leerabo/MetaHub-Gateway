"""Strict PlatformSession HTTP boundary for PRD-147 migration governance."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .manifest import DOMAIN_BY_CODE
from .models import (
    BACKFILL_COMMAND_ADAPTER,
    CUTOVER_COMMAND_ADAPTER,
    ROLLBACK_COMMAND_ADAPTER,
    ConsoleMigrationCommandResponse,
    ConsoleMigrationStatusResponse,
)
from .service import (
    execute_backfill_command,
    execute_cutover_command,
    execute_rollback_command,
    get_console_migration_status,
)

router = APIRouter(prefix="/platform/console-migration", tags=["console-migration"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"(?:0|[1-9][0-9]*)"$')
_WORKSPACE_REF = re.compile(r"^[0-9a-f]{64}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{16,900}\.[0-9a-f]{64}$")
_STATES = frozenset(
    {
        "inventoried",
        "backfill_queued",
        "backfilling",
        "shadowing",
        "ready",
        "quiescing",
        "cutting_over",
        "validating",
        "active",
        "rollback_queued",
        "rolling_back",
        "rolled_back",
        "blocked",
    }
)


def require_console_migration(request: Request) -> PlatformContext:
    """Authenticate only PlatformSession and authorize before parsing a request body."""

    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _platform_csrf(request)
    actor = authenticate_platform_session_context(
        request.app.state.config,
        _platform_token(request),
        csrf_token=csrf,
    )
    if request.method == "GET":
        permitted = "platform.console_migration.read" in actor.permissions
    elif request.url.path.endswith("/backfills"):
        permitted = "platform.console_migration.backfill" in actor.permissions
    else:
        permitted = bool(
            {
                "platform.console_migration.backfill",
                "platform.console_migration.approve",
                "platform.console_migration.security_review",
                "platform.console_migration.execute",
            }
            & set(actor.permissions)
        )
    if not permitted:
        raise ApiError("console_migration.permission_revoked")
    return actor


@router.get("/status", response_model=ConsoleMigrationStatusResponse)
async def console_migration_status(
    request: Request,
    response: Response,
    _actor: Annotated[PlatformContext, Depends(require_console_migration)],
) -> ConsoleMigrationStatusResponse:
    _reject_body(request)
    domain_code, migration_state, workspace_ref, cursor, limit = _status_query(request)
    result = get_console_migration_status(
        request.app.state.config,
        session_token=_platform_token(request),
        domain_code=domain_code,
        state=migration_state,
        workspace_ref=workspace_ref,
        cursor=cursor,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/backfills",
    response_model=ConsoleMigrationCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def console_migration_backfills(
    request: Request,
    response: Response,
    _actor: Annotated[PlatformContext, Depends(require_console_migration)],
) -> ConsoleMigrationCommandResponse:
    _reject_query(request)
    payload = _parse(
        BACKFILL_COMMAND_ADAPTER,
        await _strict_json(request, "console_migration.invalid"),
    )
    _match_revision(request, payload.expected_revision)
    result = execute_backfill_command(
        request.app.state.config,
        payload,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _command_headers(response, result)
    return result


@router.post(
    "/cutovers",
    response_model=ConsoleMigrationCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def console_migration_cutovers(
    request: Request,
    response: Response,
    _actor: Annotated[PlatformContext, Depends(require_console_migration)],
) -> ConsoleMigrationCommandResponse:
    _reject_query(request)
    payload = _parse(
        CUTOVER_COMMAND_ADAPTER,
        await _strict_json(request, "console_migration.invalid"),
    )
    _match_revision(request, payload.expected_revision)
    result = execute_cutover_command(
        request.app.state.config,
        payload,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _command_headers(response, result)
    return result


@router.post(
    "/rollbacks",
    response_model=ConsoleMigrationCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def console_migration_rollbacks(
    request: Request,
    response: Response,
    _actor: Annotated[PlatformContext, Depends(require_console_migration)],
) -> ConsoleMigrationCommandResponse:
    _reject_query(request)
    payload = _parse(
        ROLLBACK_COMMAND_ADAPTER,
        await _strict_json(request, "console_migration.invalid"),
    )
    _match_revision(request, payload.expected_revision)
    result = execute_rollback_command(
        request.app.state.config,
        payload,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _command_headers(response, result)
    return result


def _parse(adapter, payload: object):
    try:
        return adapter.validate_python(payload)
    except ValueError:
        raise ApiError("console_migration.invalid") from None


def _status_query(
    request: Request,
) -> tuple[str | None, str | None, str | None, str | None, int]:
    items = request.query_params.multi_items()
    keys = [key for key, _value in items]
    if (
        len(keys) != len(set(keys))
        or any(
            key not in {"domain_code", "state", "workspace_ref", "cursor", "limit"}
            for key in keys
        )
        or any(value == "" for _key, value in items)
    ):
        raise ApiError("console_migration.invalid")
    values = dict(items)
    domain = values.get("domain_code")
    migration_state = values.get("state")
    workspace_ref = values.get("workspace_ref")
    cursor = values.get("cursor")
    if domain is not None and domain not in DOMAIN_BY_CODE:
        raise ApiError("console_migration.invalid")
    if migration_state is not None and migration_state not in _STATES:
        raise ApiError("console_migration.invalid")
    if workspace_ref is not None and _WORKSPACE_REF.fullmatch(workspace_ref) is None:
        raise ApiError("console_migration.invalid")
    if cursor is not None and _CURSOR.fullmatch(cursor) is None:
        raise ApiError("console_migration.invalid")
    raw_limit = values.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None:
        raise ApiError("console_migration.invalid")
    limit = int(raw_limit)
    if limit > 100:
        raise ApiError("console_migration.invalid")
    return domain, migration_state, workspace_ref, cursor, limit


def _match_revision(request: Request, expected: int) -> None:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _IF_MATCH.fullmatch(values[0]) is None:
        raise ApiError("console_migration.invalid")
    if int(values[0][1:-1]) != expected:
        raise ApiError("console_migration.conflict")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("console_migration.invalid")
    return values[0]


def _step_up(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("console_migration.permission_revoked")
    return values[0]


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("console_migration.invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("console_migration.invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("console_migration.invalid")


def _private(response: Response, fingerprint: str) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Permission-Fingerprint"] = fingerprint


def _command_headers(response: Response, result: ConsoleMigrationCommandResponse) -> None:
    response.headers["ETag"] = f'"{result.revision}"'
    response.headers["X-Polling-Location-Code"] = result.polling_location_code
    _private(response, result.permission_fingerprint)


__all__ = ["require_console_migration", "router"]
