"""Dual-session browser boundary for PRD-134 privacy operations."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    DataSubjectRequestCancel,
    DataSubjectRequestCreate,
    DataSubjectRequestListResponse,
    DataSubjectRequestResponse,
    ExportReceiptResponse,
    PlatformPrivacyQueueResponse,
    PrivacyPreferencePatchRequest,
    PrivacyPreferenceResponse,
)
from .service import (
    cancel_data_subject_request,
    create_data_subject_request,
    download_identity_export,
    get_data_subject_request,
    get_privacy_preferences,
    issue_export_receipt,
    list_data_subject_requests,
    list_platform_privacy_queue,
    update_privacy_preferences,
)

router = APIRouter(tags=["privacy"])
_DSR_ID = re.compile(r"^dsr_[0-9a-f]{32}$")
_EXPORT_ID = re.compile(r"^pex_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_RECEIPT = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


@dataclass(frozen=True, slots=True, repr=False)
class _IdentityCredentials:
    global_token: str
    workspace_token: str
    global_csrf: str | None = None


@router.get("/identity/privacy-preferences", response_model=PrivacyPreferenceResponse)
async def get_preferences(request: Request, response: Response) -> PrivacyPreferenceResponse:
    credentials = _identity_credentials(request, write=False)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        get_privacy_preferences,
        request.app.state.config,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.preference.revision}"'
    _private(response)
    return result


@router.patch("/identity/privacy-preferences", response_model=PrivacyPreferenceResponse)
async def patch_preferences(request: Request, response: Response) -> PrivacyPreferenceResponse:
    credentials = _identity_credentials(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "privacy.request_invalid"), PrivacyPreferencePatchRequest
    )
    result = await run_in_threadpool(
        update_privacy_preferences,
        request.app.state.config,
        payload,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        global_csrf_token=credentials.global_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.preference.revision}"'
    _private(response)
    return result


@router.get("/identity/data-subject-requests", response_model=DataSubjectRequestListResponse)
async def get_requests(request: Request, response: Response) -> DataSubjectRequestListResponse:
    credentials = _identity_credentials(request, write=False)
    _reject_body(request)
    limit = _limit(request, maximum=50)
    result = await run_in_threadpool(
        list_data_subject_requests,
        request.app.state.config,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/identity/data-subject-requests",
    response_model=DataSubjectRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_request(request: Request, response: Response) -> DataSubjectRequestResponse:
    credentials = _identity_credentials(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "privacy.request_invalid"), DataSubjectRequestCreate
    )
    result = await run_in_threadpool(
        create_data_subject_request,
        request.app.state.config,
        payload,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        global_csrf_token=credentials.global_csrf,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.request.revision}"'
    _private(response)
    return result


@router.get(
    "/identity/data-subject-requests/{request_object_id}",
    response_model=DataSubjectRequestResponse,
)
async def get_request(
    request_object_id: str, request: Request, response: Response
) -> DataSubjectRequestResponse:
    checked_id = _dsr_id(request_object_id)
    credentials = _identity_credentials(request, write=False)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        get_data_subject_request,
        request.app.state.config,
        checked_id,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.request.revision}"'
    _private(response)
    return result


@router.delete(
    "/identity/data-subject-requests/{request_object_id}",
    response_model=DataSubjectRequestResponse,
)
async def delete_request(
    request_object_id: str, request: Request, response: Response
) -> DataSubjectRequestResponse:
    checked_id = _dsr_id(request_object_id)
    credentials = _identity_credentials(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "privacy.request_invalid"), DataSubjectRequestCancel
    )
    result = await run_in_threadpool(
        cancel_data_subject_request,
        request.app.state.config,
        checked_id,
        payload,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        global_csrf_token=credentials.global_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.request.revision}"'
    _private(response)
    return result


@router.post(
    "/identity/data-subject-requests/{request_object_id}/export-receipts",
    response_model=ExportReceiptResponse,
)
async def post_export_receipt(
    request_object_id: str, request: Request, response: Response
) -> ExportReceiptResponse:
    checked_id = _dsr_id(request_object_id)
    credentials = _identity_credentials(request, write=True)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        issue_export_receipt,
        request.app.state.config,
        checked_id,
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        global_csrf_token=credentials.global_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/identity/exports/{export_id}/download", response_class=Response)
async def get_export_download(export_id: str, request: Request) -> Response:
    checked_id = _export_id(export_id)
    credentials = _identity_credentials(request, write=False)
    _reject_query_and_body(request)
    receipts = _raw_headers(request, b"x-privacy-export-receipt")
    if len(receipts) != 1 or _RECEIPT.fullmatch(receipts[0]) is None:
        raise ApiError("privacy.export_receipt_invalid")
    result = await run_in_threadpool(
        download_identity_export,
        request.app.state.config,
        checked_id,
        receipt=receipts[0],
        global_session_token=credentials.global_token,
        workspace_session_token=credentials.workspace_token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    digest = base64.b64encode(bytes.fromhex(result.content_sha256)).decode("ascii")
    return Response(
        content=result.content,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Digest": f"sha-256={digest}",
            "X-Artifact-Receipt-SHA256": result.receipt_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/platform/privacy/queue", response_model=PlatformPrivacyQueueResponse)
async def get_platform_queue(request: Request, response: Response) -> PlatformPrivacyQueueResponse:
    token = _platform_credentials(request)
    _reject_body(request)
    limit = _limit(request, maximum=100)
    result = await run_in_threadpool(
        list_platform_privacy_queue,
        request.app.state.config,
        platform_session_token=token,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _identity_credentials(request: Request, *, write: bool) -> _IdentityCredentials:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("privacy.credential_conflict")
    global_values = _cookie_values(request, GLOBAL_SESSION_COOKIE)
    workspace_values = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    if len(global_values) != 1 or len(workspace_values) != 1:
        raise ApiError("privacy.session_required")
    if not valid_opaque_value(global_values[0]) or not valid_opaque_value(workspace_values[0]):
        raise ApiError("privacy.session_invalid")
    if not write:
        return _IdentityCredentials(global_values[0], workspace_values[0])
    _require_origin(request, "privacy.origin_invalid")
    csrf = _required_csrf(request, GLOBAL_CSRF_COOKIE, "identity.csrf_invalid")
    return _IdentityCredentials(global_values[0], workspace_values[0], csrf)


def _platform_credentials(request: Request) -> str:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, GLOBAL_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("privacy.credential_conflict")
    values = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    if len(values) != 1:
        raise ApiError("platform.session_required")
    if not valid_opaque_value(values[0]):
        raise ApiError("platform.session_invalid")
    return values[0]


def _model(payload: dict[str, object], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("privacy.request_invalid") from None


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("privacy.revision_required")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("privacy.idempotency_invalid")
    return values[0]


def _limit(request: Request, *, maximum: int) -> int:
    items = request.query_params.multi_items()
    if any(key != "limit" for key, _ in items) or len(items) > 1:
        raise ApiError("privacy.request_invalid")
    raw = request.query_params.get("limit", "20")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw) is None or int(raw) > maximum:
        raise ApiError("privacy.request_invalid")
    return int(raw)


def _dsr_id(value: str) -> str:
    if _DSR_ID.fullmatch(value) is None:
        raise ApiError("privacy.request_not_found")
    return value


def _export_id(value: str) -> str:
    if _EXPORT_ID.fullmatch(value) is None:
        raise ApiError("privacy.export_not_found")
    return value


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("privacy.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("privacy.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("privacy.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    _reject_body(request)


__all__ = ["router"]
