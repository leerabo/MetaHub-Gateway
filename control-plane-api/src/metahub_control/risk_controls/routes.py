"""Strict browser and PlatformSession HTTP boundaries for PRD-110."""

from __future__ import annotations

import hmac
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import CSRF_COOKIE, SESSION_COOKIE
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
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .models import (
    RiskAppealRequest,
    RiskAppealResponse,
    RiskCaseDecisionRequest,
    RiskCaseDecisionResponse,
    RiskCaseListResponse,
    RiskChallengeRequest,
    RiskChallengeResponse,
)
from .service import complete_challenge, create_appeal, decide_case, list_cases

router = APIRouter(tags=["risk-controls"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_risk(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    required = (
        {"platform.risk.read", "platform.risk.support_read"}
        if request.method == "GET"
        else {"platform.risk.decide"}
    )
    if required.isdisjoint(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.post("/risk/challenges", response_model=RiskChallengeResponse)
async def post_risk_challenge(request: Request, response: Response) -> RiskChallengeResponse:
    _reject_query(request)
    _require_origin(request, "risk.origin_invalid")
    global_token, workspace_token, csrf = _challenge_credentials(request)
    payload = await _model(request, RiskChallengeRequest)
    result = complete_challenge(
        request.app.state.config,
        global_session_token=global_token,
        workspace_session_token=workspace_token,
        csrf_token=csrf,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/risk/cases", response_model=RiskCaseListResponse)
async def get_platform_risk_cases(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_risk)],
) -> RiskCaseListResponse:
    status, limit, cursor = _case_query(request)
    result = list_cases(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        status=status,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/risk/cases/{case_id}/decisions",
    response_model=RiskCaseDecisionResponse,
    status_code=201,
)
async def post_platform_risk_case_decision(
    case_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_risk)],
) -> RiskCaseDecisionResponse:
    _reject_query(request)
    payload = await _model(request, RiskCaseDecisionRequest)
    result = decide_case(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        case_id=case_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{result.case.revision}"'
    _private(response)
    return result


@router.post("/risk/appeals", response_model=RiskAppealResponse, status_code=201)
async def post_risk_appeal(request: Request, response: Response) -> RiskAppealResponse:
    _reject_query(request)
    _require_origin(request, "risk.origin_invalid")
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("risk.credential_conflict")
    session_token = _required_cookie(
        request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
    )
    csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    payload = await _model(request, RiskAppealRequest)
    result = create_appeal(
        request.app.state.config,
        session_token=session_token,
        csrf_token=csrf,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


async def _model(request: Request, model_type):
    try:
        return model_type.model_validate(await _strict_json(request, "risk.request_invalid"))
    except ValueError:
        raise ApiError("risk.request_invalid") from None


def _case_query(request: Request) -> tuple[str | None, int, str | None]:
    values: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        values.setdefault(key, []).append(value)
    if not set(values) <= {"status", "limit", "cursor"} or any(
        len(items) != 1 for items in values.values()
    ):
        raise ApiError("risk.request_invalid")
    status = request.query_params.get("status")
    if status not in {None, "open", "under_review", "restricted", "cleared", "closed"}:
        raise ApiError("risk.request_invalid")
    raw_limit = request.query_params.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None or int(raw_limit) > 100:
        raise ApiError("risk.request_invalid")
    cursor = request.query_params.get("cursor")
    if cursor is not None and not 20 <= len(cursor) <= 2048:
        raise ApiError("risk.cursor_invalid")
    return status, int(raw_limit), cursor


def _reject_query(request: Request) -> None:
    if list(request.query_params.multi_items()):
        raise ApiError("risk.request_invalid")


def _challenge_credentials(request: Request) -> tuple[str | None, str | None, str]:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("risk.credential_conflict")
    global_tokens = _cookie_values(request, SESSION_COOKIE)
    workspace_tokens = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    if (
        len(global_tokens) > 1
        or len(workspace_tokens) > 1
        or not (global_tokens or workspace_tokens)
    ):
        raise ApiError("risk.credential_conflict")
    if global_tokens and not workspace_tokens:
        return (
            _required_cookie(request, SESSION_COOKIE, "identity.session_required"),
            None,
            _required_csrf(request, CSRF_COOKIE, "identity.csrf_invalid"),
        )
    if workspace_tokens and not global_tokens:
        return (
            None,
            _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
            _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"),
        )
    csrf_headers = _raw_headers(request, b"x-csrf-token")
    global_csrf = _cookie_values(request, CSRF_COOKIE)
    workspace_csrf = _cookie_values(request, WORKSPACE_CSRF_COOKIE)
    if len(csrf_headers) != 1 or len(global_csrf) != 1 or len(workspace_csrf) != 1:
        raise ApiError("risk.credential_conflict")
    selects_global = hmac.compare_digest(csrf_headers[0], global_csrf[0])
    selects_workspace = hmac.compare_digest(csrf_headers[0], workspace_csrf[0])
    if selects_global == selects_workspace:
        raise ApiError("risk.credential_conflict")
    if selects_global:
        return global_tokens[0], None, csrf_headers[0]
    return None, workspace_tokens[0], csrf_headers[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]{0,8}"', values[0]) is None:
        raise ApiError("risk.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("risk.request_invalid")
    return values[0]


__all__ = ["require_platform_risk", "router"]
