"""Strict Developer Bearer and Platform cookie boundaries for PRD-133."""

from __future__ import annotations

import base64
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_developer_portal_reader
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
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    ApiVersionListResponse,
    ContractApproveRequest,
    ContractBuildRequest,
    ContractBuildResponse,
    DeprecationActionRequest,
    DeprecationCreateRequest,
    DeveloperChangelogResponse,
    MutationResponse,
    PlatformContractWorkspaceResponse,
    SdkReleaseListResponse,
    SdkWithdrawRequest,
    WebhookSchemaListResponse,
)
from .service import (
    announce_deprecation_notice,
    approve_contract_release,
    build_contract_release,
    create_deprecation_notice,
    download_sdk_release,
    get_platform_contract_workspace,
    list_api_versions,
    list_changelog,
    list_sdk_releases,
    list_webhook_schema_versions,
    sunset_deprecation_notice,
    withdraw_deprecation_notice,
    withdraw_sdk_release,
)

router = APIRouter(tags=["developer-contracts"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_contract(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
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
    if not {
        "platform.developer_contract.read",
        "platform.developer_contract.manage",
        "platform.developer_contract.approve",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/developer/api-versions", response_model=ApiVersionListResponse)
async def developer_api_versions(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
) -> ApiVersionListResponse:
    _exact_query(request, set())
    _private(response)
    return list_api_versions(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/developer/changelog", response_model=DeveloperChangelogResponse)
async def developer_changelog(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> DeveloperChangelogResponse:
    _exact_query(request, {"limit", "cursor"})
    _private(response)
    return list_changelog(
        request.app.state.config,
        context,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/developer/webhook-schemas", response_model=WebhookSchemaListResponse)
async def developer_webhook_schemas(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
) -> WebhookSchemaListResponse:
    _exact_query(request, set())
    _private(response)
    return list_webhook_schema_versions(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/developer/sdks/{language}", response_model=SdkReleaseListResponse)
async def developer_sdks(
    language: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
) -> SdkReleaseListResponse:
    _exact_query(request, set())
    _private(response)
    return list_sdk_releases(
        request.app.state.config,
        context,
        language=language,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/developer/sdks/{language}/{sdk_version}/download", response_class=Response)
async def developer_sdk_download(
    language: str,
    sdk_version: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_developer_portal_reader)],
) -> Response:
    _exact_query(request, set())
    archive, release = download_sdk_release(
        request.app.state.config,
        context,
        language=language,
        sdk_version=sdk_version,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store, private",
            "Content-Disposition": (
                f'attachment; filename="metahub-{release.language}-sdk-{release.sdk_version}.zip"'
            ),
            "Digest": (
                "sha-256="
                + base64.b64encode(bytes.fromhex(release.artifact_sha256)).decode("ascii")
            ),
            "X-Artifact-Receipt-SHA256": release.artifact_receipt_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/platform/developer-contracts", response_model=PlatformContractWorkspaceResponse)
async def platform_contracts(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> PlatformContractWorkspaceResponse:
    _exact_query(request, set())
    result = get_platform_contract_workspace(
        request.app.state.config,
        session_token=_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/developer-contracts/build",
    response_model=ContractBuildResponse,
    status_code=201,
)
async def platform_contract_build(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> ContractBuildResponse:
    payload = _model(ContractBuildRequest, request, await _body(request))
    result = build_contract_release(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/developer-contracts/{api_version_id}/approve",
    response_model=ContractBuildResponse,
)
async def platform_contract_approve(
    api_version_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> ContractBuildResponse:
    payload = _model(ContractApproveRequest, request, await _body(request))
    result = approve_contract_release(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        api_version_id=api_version_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        external_signer=getattr(request.app.state, "developer_contract_signer", None),
    )
    _private(response)
    return result


@router.post(
    "/platform/developer-contracts/deprecations",
    response_model=MutationResponse,
    status_code=201,
)
async def platform_deprecation_create(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> MutationResponse:
    payload = _model(DeprecationCreateRequest, request, await _body(request))
    result = create_deprecation_notice(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/developer-contracts/deprecations/{notice_id}/{action}",
    response_model=MutationResponse,
)
async def platform_deprecation_action(
    notice_id: str,
    action: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> MutationResponse:
    payload = _model(DeprecationActionRequest, request, await _body(request))
    functions = {
        "announce": announce_deprecation_notice,
        "withdraw": withdraw_deprecation_notice,
        "sunset": sunset_deprecation_notice,
    }
    function = functions.get(action)
    if function is None:
        raise ApiError("developer_contract.not_found")
    result = function(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        notice_id=notice_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/developer-contracts/sdks/{sdk_release_id}/withdraw",
    response_model=MutationResponse,
)
async def platform_sdk_withdraw(
    sdk_release_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_contract)],
) -> MutationResponse:
    payload = _model(SdkWithdrawRequest, request, await _body(request))
    result = withdraw_sdk_release(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        sdk_release_id=sdk_release_id,
        expected_revision=payload.expected_revision,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _body(request: Request) -> dict[str, object]:
    _exact_query(request, set())
    return await _strict_json(request, "developer_contract.request_invalid")


def _model(model, request: Request, value: dict[str, object]):
    del request
    try:
        return model.model_validate(value)
    except ValueError:
        raise ApiError("developer_contract.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("developer_contract.request_invalid")
    return values[0]


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _exact_query(request: Request, allowed: set[str]) -> None:
    keys = [key for key, _value in request.query_params.multi_items()]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise ApiError("developer_contract.request_invalid")


__all__ = ["router"]
