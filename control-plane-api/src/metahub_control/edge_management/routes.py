"""Credential-separated HTTP boundaries for PRD-120 Edge identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import zlib
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse
from metahub_shared.private_gateway import (
    MAX_SUPPORT_BUNDLE_BYTES,
    DeploymentValidationReport,
    EdgeSecretSyncBatch,
    EdgeSecretSyncReceipt,
    EdgeTelemetryBatch,
    EdgeTelemetryError,
    EdgeTelemetryReceiptWire,
    SupportBundleUploadMetadata,
    SupportBundleUploadReceipt,
    canonical_edge_json_bytes,
    reject_forbidden_telemetry_fields,
    strict_json_object,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.background import BackgroundTask

from metahub_control.identity.service import valid_opaque_value
from metahub_control.machine_identity.transport import verified_mtls_fingerprint
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

from .backup_models import (
    AgentBackupReportRequest,
    AgentDrillReportRequest,
    BackupReportResponse,
    BackupStatusResponse,
    PlatformBackupStatusListResponse,
    RestoreDrillMutationRequest,
    RestoreDrillResponse,
)
from .backup_service import (
    get_workspace_backup_status,
    list_platform_backup_statuses,
    mutate_workspace_restore_drill,
    report_agent_backup,
    report_agent_restore_drill,
)
from .desired_state_service import (
    acknowledge_agent_desired_state,
    get_workspace_desired_state,
    pull_agent_desired_state,
    rollback_workspace_desired_state,
    write_workspace_desired_state,
)
from .fleet_diagnostic_service import (
    advance_agent_diagnostic,
    decide_workspace_diagnostic_grant,
    get_workspace_edge_health,
    list_platform_fleet,
    mutate_platform_diagnostic_command,
    pull_agent_diagnostic,
    record_agent_diagnostic_result,
    request_platform_diagnostic_grant,
)
from .installer_service import (
    decide_platform_installer_release,
    list_platform_installer_releases,
    list_workspace_installers,
    record_workspace_preflight,
    resolve_workspace_installer_artifact,
    start_workspace_installation,
    submit_platform_installer_release,
    update_workspace_installation,
)
from .license_models import (
    AgentLicenseAcknowledgementRequest,
    AgentLicenseAcknowledgementResponse,
    AgentLicenseResponse,
    LicenseListResponse,
    LicenseResponse,
    PlatformLicenseIssueRequest,
    PlatformLicenseRevokeRequest,
    WorkspaceLicenseRefreshRequest,
)
from .license_service import (
    acknowledge_agent_license,
    get_workspace_license,
    issue_platform_license,
    list_platform_licenses,
    pull_agent_license,
    refresh_workspace_license,
    revoke_platform_license,
)
from .models import (
    EdgeAckCreateRequest,
    EdgeAckResponse,
    EdgeAgentRegistrationRequest,
    EdgeAgentTelemetryWatermarkResponse,
    EdgeCertificateResponse,
    EdgeCertificateRotationRequest,
    EdgeDeliveryAcceptanceRequest,
    EdgeDeliveryAcceptanceResponse,
    EdgeDeploymentListResponse,
    EdgeDeploymentRevokeRequest,
    EdgeDeploymentRevokeResponse,
    EdgeDesiredStateApprovalRequest,
    EdgeDesiredStatePublishRequest,
    EdgeDesiredStatePullResponse,
    EdgeDesiredStateResponse,
    EdgeDesiredStateRollbackRequest,
    EdgeDiagnosticCommandMutationRequest,
    EdgeDiagnosticCommandResponse,
    EdgeDiagnosticGrantMutationRequest,
    EdgeDiagnosticGrantResponse,
    EdgeDiagnosticLeaseRequest,
    EdgeDiagnosticLeaseResponse,
    EdgeDiagnosticPendingResponse,
    EdgeDiagnosticReportRequest,
    EdgeDiagnosticResultResponse,
    EdgeHealthResponse,
    EdgeInstallationResponse,
    EdgeInstallationStartRequest,
    EdgeInstallationUpdateRequest,
    EdgeInstallerCatalogResponse,
    EdgePreflightRecordRequest,
    EdgePreflightRecordResponse,
    EdgeSecretReferenceListResponse,
    EdgeSessionCreateRequest,
    EdgeSessionResponse,
    EdgeSupportBundleResponse,
    EdgeTelemetryPolicyPatchRequest,
    EdgeTelemetryPolicyResponse,
    EdgeTelemetryWatermarkResponse,
    EdgeValidationResponse,
    EdgeValidationStartRequest,
    PlatformEdgeFleetResponse,
    PlatformFleetPageResponse,
    PlatformInstallerReleaseDecisionRequest,
    PlatformInstallerReleaseListResponse,
    PlatformInstallerReleaseResponse,
    PlatformInstallerReleaseSubmitRequest,
    WorkspaceRegistrationCreateRequest,
    WorkspaceRegistrationResponse,
)
from .offline_bundle_models import (
    OfflineBundleStateResponse,
    PlatformOfflineBundleListResponse,
    PlatformOfflineDecisionRequest,
    PlatformOfflineDecisionResponse,
    WorkspaceOfflineBundleRequest,
    WorkspaceOfflineResolutionRequest,
)
from .offline_bundle_service import (
    decide_platform_offline_request,
    get_workspace_offline_state,
    list_platform_offline_states,
    request_workspace_offline_bundle,
    request_workspace_offline_resolution,
)
from .secret_reference_service import (
    list_workspace_secret_references,
    synchronize_agent_secret_references,
)
from .service import (
    assert_workspace_private_gateway_enabled,
    create_edge_session,
    create_workspace_registration,
    list_platform_edge_fleet,
    list_workspace_deployments,
    register_edge_agent,
    revoke_edge_deployment,
    rotate_edge_certificate,
)
from .support_bundle_service import (
    authorize_agent_support_bundle_upload,
    get_workspace_support_bundle,
    prepare_support_bundle_staging,
    resolve_support_bundle_download,
    store_agent_support_bundle,
)
from .telemetry_service import (
    get_agent_telemetry_watermark,
    get_workspace_telemetry_watermark,
    patch_workspace_telemetry_policy,
    receive_agent_telemetry,
)
from .upgrade_models import (
    AgentUpgradeCommandResponse,
    AgentUpgradeMutationRequest,
    AgentUpgradeReceiptResponse,
    AgentUpgradeReportRequest,
    FleetCompatibilityResponse,
    FleetRolloutMutationRequest,
    GatewayReleaseListResponse,
    GatewayReleaseResponse,
    MaintenanceWindowUpdateRequest,
    PlatformGatewayReleaseMutationRequest,
    PlatformRolloutCreateRequest,
    RollbackCreateRequest,
    RollbackResponse,
    RolloutListResponse,
    RolloutResponse,
    WorkspaceUpgradeResponse,
)
from .upgrade_service import (
    create_platform_rollout,
    get_fleet_compatibility,
    get_workspace_upgrade,
    list_platform_gateway_releases,
    list_platform_rollouts,
    mutate_agent_upgrade,
    mutate_platform_gateway_release,
    mutate_platform_rollout,
    pull_agent_upgrade,
    record_agent_upgrade_report,
    request_workspace_rollback,
    update_workspace_maintenance,
)
from .validation_service import (
    accept_workspace_delivery,
    get_agent_validation,
    get_workspace_validation,
    record_agent_validation,
    start_workspace_validation,
)

router = APIRouter(tags=["edge-management"])
_WORK_LIMITER = anyio.CapacityLimiter(4)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_MAX_TELEMETRY_COMPRESSED_BYTES = 262_144
_MAX_TELEMETRY_BYTES = 1_048_576
_MAX_SUPPORT_BUNDLE_METADATA_BYTES = 65_536
_MAX_SUPPORT_BUNDLE_WIRE_BYTES = (
    4 + _MAX_SUPPORT_BUNDLE_METADATA_BYTES + MAX_SUPPORT_BUNDLE_BYTES + 64
)
_SUPPORT_BUNDLE_MEDIA_TYPE = "application/vnd.metahub.edge-support-bundle"
_UNTRUSTED_CERT_HEADERS = frozenset(
    {
        b"ssl-client-cert",
        b"x-client-cert",
        b"x-client-cert-sha256",
        b"x-forwarded-client-cert",
        b"x-mtls-client-cert",
        b"x-ssl-client-cert",
    }
)


@router.get("/edge/deployments", response_model=EdgeDeploymentListResponse)
async def get_edge_deployments(request: Request, response: Response) -> EdgeDeploymentListResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        list_workspace_deployments,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/health",
    response_model=EdgeHealthResponse,
)
async def get_edge_health(
    deployment_id: str, request: Request, response: Response
) -> EdgeHealthResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        get_workspace_edge_health,
        request.app.state.config,
        deployment_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.health.generation}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/diagnostic-grants",
    response_model=EdgeDiagnosticGrantResponse,
)
async def post_edge_diagnostic_grant(
    deployment_id: str, request: Request, response: Response
) -> EdgeDiagnosticGrantResponse:
    payload = _model(
        await _strict_json(request, "edge.diagnostic_request_invalid"),
        EdgeDiagnosticGrantMutationRequest,
        error_code="edge.diagnostic_request_invalid",
    )
    if payload.action == "request":
        context = require_platform_edge_fleet(request)
        result = await _run(
            request_platform_diagnostic_grant,
            request.app.state.config,
            context,
            deployment_id,
            payload,
            idempotency_key=_platform_idempotency_key(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        if not result.idempotent_replay:
            response.status_code = status.HTTP_201_CREATED
    else:
        _prepare_workspace(request, write=True)
        result = await _run(
            decide_workspace_diagnostic_grant,
            request.app.state.config,
            deployment_id,
            payload,
            session_token=_workspace_cookie(request),
            csrf_token=_workspace_csrf(request),
            step_up_grant=_required_step_up_grant(request),
            idempotency_key=_idempotency_key(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    response.headers["ETag"] = f'"{result.grant.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/diagnostics",
    response_model=EdgeDiagnosticCommandResponse,
)
async def post_edge_diagnostic(
    deployment_id: str, request: Request, response: Response
) -> EdgeDiagnosticCommandResponse:
    context = require_platform_edge_fleet(request)
    payload = _model(
        await _strict_json(request, "edge.diagnostic_request_invalid"),
        EdgeDiagnosticCommandMutationRequest,
        error_code="edge.diagnostic_request_invalid",
    )
    result = await _run(
        mutate_platform_diagnostic_command,
        request.app.state.config,
        context,
        deployment_id,
        payload,
        step_up_grant=(
            _required_step_up_grant(request)
            if payload.action == "run"
            else _optional_step_up_grant(request)
        ),
        idempotency_key=_platform_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if payload.action == "run" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.command.revision}"'
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/diagnostics/pending",
    response_model=EdgeDiagnosticPendingResponse,
)
async def get_agent_diagnostic(
    deployment_id: str, request: Request, response: Response
) -> EdgeDiagnosticPendingResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    result = await _run(
        pull_agent_diagnostic,
        request.app.state.config,
        deployment_id,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.command_revision or 0}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/diagnostics/{command_id}/lease",
    response_model=EdgeDiagnosticLeaseResponse,
)
async def post_agent_diagnostic_lease(
    deployment_id: str, command_id: str, request: Request, response: Response
) -> EdgeDiagnosticLeaseResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge.diagnostic_request_invalid"),
        EdgeDiagnosticLeaseRequest,
        error_code="edge.diagnostic_request_invalid",
    )
    result = await _run(
        advance_agent_diagnostic,
        request.app.state.config,
        deployment_id,
        command_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.command.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/diagnostics/{command_id}/result",
    response_model=EdgeDiagnosticResultResponse,
)
async def post_agent_diagnostic_result(
    deployment_id: str, command_id: str, request: Request, response: Response
) -> EdgeDiagnosticResultResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge.diagnostic_result_invalid"),
        EdgeDiagnosticReportRequest,
        error_code="edge.diagnostic_result_invalid",
    )
    result = await _run(
        record_agent_diagnostic_result,
        request.app.state.config,
        deployment_id,
        command_id,
        payload.report,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/validations",
    response_model=EdgeValidationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_validation(
    deployment_id: str, request: Request, response: Response
) -> EdgeValidationResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeValidationStartRequest
    )
    result = await _run(
        start_workspace_validation,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/validations/{validation_run_id}",
    response_model=EdgeValidationResponse,
)
async def get_edge_validation(
    deployment_id: str,
    validation_run_id: str,
    request: Request,
    response: Response,
) -> EdgeValidationResponse:
    _reject_query(request)
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=False)
        result = await _run(
            get_workspace_validation,
            request.app.state.config,
            deployment_id,
            validation_run_id,
            session_token=_workspace_cookie(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    else:
        fingerprint = _prepare_agent(request, require_mtls=True)
        result = await _run(
            get_agent_validation,
            request.app.state.config,
            deployment_id,
            validation_run_id,
            verified_fingerprint=fingerprint,
            edge_session_token=_edge_session(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/validations/{validation_run_id}/report",
    response_model=EdgeValidationResponse,
)
async def post_edge_validation_report(
    deployment_id: str,
    validation_run_id: str,
    request: Request,
    response: Response,
) -> EdgeValidationResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    report = _model(await _strict_json(request, "edge.request_invalid"), DeploymentValidationReport)
    result = await _run(
        record_agent_validation,
        request.app.state.config,
        deployment_id,
        validation_run_id,
        report,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/delivery-acceptances",
    response_model=EdgeDeliveryAcceptanceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_delivery_acceptance(
    deployment_id: str, request: Request, response: Response
) -> EdgeDeliveryAcceptanceResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeDeliveryAcceptanceRequest
    )
    result = await _run(
        accept_workspace_delivery,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_required_step_up_grant(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.post(
    "/edge/support-bundles",
    response_model=SupportBundleUploadReceipt,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_support_bundle(
    request: Request, response: Response
) -> SupportBundleUploadReceipt:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    deployment_id = _edge_deployment(request)
    edge_session = _edge_session(request)
    authorization = await _run(
        authorize_agent_support_bundle_upload,
        request.app.state.config,
        deployment_id,
        verified_fingerprint=fingerprint,
        edge_session_token=edge_session,
    )
    staged_path = await _run(prepare_support_bundle_staging, request.app.state.config)
    try:
        metadata = await _read_support_bundle_upload(request, staged_path)
        result = await _run(
            store_agent_support_bundle,
            request.app.state.config,
            authorization,
            metadata,
            staged_path,
            verified_fingerprint=fingerprint,
            edge_session_token=edge_session,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    finally:
        try:
            staged_path.unlink(missing_ok=True)
        except OSError:
            pass
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.get("/edge/support-bundles/{bundle_id}", response_model=None)
async def get_edge_support_bundle(
    bundle_id: str, request: Request, response: Response
) -> Response | EdgeSupportBundleResponse:
    _reject_query(request)
    support_credentials = _cookie_values(request, SUPPORT_SESSION_COOKIE)
    if support_credentials:
        _prepare_support(request)
        download = await _run(
            resolve_support_bundle_download,
            request.app.state.config,
            bundle_id,
            support_session_token=support_credentials[0],
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return FileResponse(
            download.path,
            media_type="application/vnd.metahub.edge-support-bundle-encrypted",
            filename=f"{bundle_id}.enc",
            headers={
                "Cache-Control": "no-store, private",
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(download.cleanup),
        )
    _prepare_workspace(request, write=False)
    result: EdgeSupportBundleResponse = await _run(
        get_workspace_support_bundle,
        request.app.state.config,
        bundle_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.get("/edge/installers", response_model=EdgeInstallerCatalogResponse)
async def get_edge_installers(request: Request, response: Response) -> EdgeInstallerCatalogResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        list_workspace_installers,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.get("/edge/installers/{release_id}/artifacts/{artifact_id}")
async def get_edge_installer_artifact(
    release_id: str, artifact_id: str, request: Request
) -> Response:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    artifact = await _run(
        resolve_workspace_installer_artifact,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        release_id=release_id,
        artifact_id=artifact_id,
    )
    return FileResponse(
        artifact.path,
        media_type=artifact.media_type,
        filename=artifact.filename,
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{artifact.sha256}"',
        },
    )


@router.post(
    "/edge/preflights",
    response_model=EdgePreflightRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_preflight(request: Request, response: Response) -> EdgePreflightRecordResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgePreflightRecordRequest
    )
    result = await _run(
        record_workspace_preflight,
        request.app.state.config,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.post(
    "/edge/installations",
    response_model=EdgeInstallationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_installation(request: Request, response: Response) -> EdgeInstallationResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeInstallationStartRequest
    )
    result = await _run(
        start_workspace_installation,
        request.app.state.config,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.installation.revision}"'
    _edge_private(response)
    return result


@router.patch("/edge/installations/{installation_id}", response_model=EdgeInstallationResponse)
async def patch_edge_installation(
    installation_id: str, request: Request, response: Response
) -> EdgeInstallationResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeInstallationUpdateRequest
    )
    result = await _run(
        update_workspace_installation,
        request.app.state.config,
        installation_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_optional_step_up_grant(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.installation.revision}"'
    _edge_private(response)
    return result


@router.get("/edge/secret-references", response_model=EdgeSecretReferenceListResponse)
async def get_edge_secret_references(
    request: Request, response: Response
) -> EdgeSecretReferenceListResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        list_workspace_secret_references,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/internal/edge/secret-references/sync",
    response_model=EdgeSecretSyncReceipt,
)
async def post_edge_secret_reference_sync(
    request: Request, response: Response
) -> EdgeSecretSyncReceipt:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_json(request, "edge.request_invalid"), EdgeSecretSyncBatch)
    result = await _run(
        synchronize_agent_secret_references,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/registrations",
    response_model=WorkspaceRegistrationResponse | EdgeCertificateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_registration(
    request: Request, response: Response
) -> WorkspaceRegistrationResponse | EdgeCertificateResponse:
    _reject_query(request)
    raw = await _strict_json(request, "edge.request_invalid")
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=True)
        payload = _model(raw, WorkspaceRegistrationCreateRequest)
        result = await _run(
            create_workspace_registration,
            request.app.state.config,
            session_token=_workspace_cookie(request),
            csrf_token=_workspace_csrf(request),
            idempotency_key=_idempotency_key(request),
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response.headers["ETag"] = f'"{result.deployment.revision}"'
    else:
        _prepare_agent(request, require_mtls=False)
        payload = _model(raw, EdgeAgentRegistrationRequest)
        result = await _run(
            register_edge_agent,
            request.app.state.config,
            payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    _edge_private(response)
    return result


@router.post("/edge/certificates", response_model=EdgeCertificateResponse)
async def post_edge_certificate(request: Request, response: Response) -> EdgeCertificateResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"),
        EdgeCertificateRotationRequest,
    )
    result = await _run(
        rotate_edge_certificate,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/sessions",
    response_model=EdgeSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_session(request: Request, response: Response) -> EdgeSessionResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_json(request, "edge.request_invalid"), EdgeSessionCreateRequest)
    result = await _run(
        create_edge_session,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/revoke",
    response_model=EdgeDeploymentRevokeResponse,
)
async def post_edge_deployment_revoke(
    deployment_id: str, request: Request, response: Response
) -> EdgeDeploymentRevokeResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeDeploymentRevokeRequest
    )
    result = await _run(
        revoke_edge_deployment,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.deployment.revision}"'
    _edge_private(response)
    return result


@router.get(
    "/edge/licenses/{deployment_id}",
    response_model=LicenseResponse,
)
async def get_edge_license(
    deployment_id: str, request: Request, response: Response
) -> LicenseResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        get_workspace_license,
        request.app.state.config,
        deployment_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.license.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/licenses/{license_id}/refresh",
    response_model=LicenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_license_refresh(
    license_id: str, request: Request, response: Response
) -> LicenseResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge_license.request_invalid"),
        WorkspaceLicenseRefreshRequest,
        error_code="edge_license.request_invalid",
    )
    result = await _run(
        refresh_workspace_license,
        request.app.state.config,
        license_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.license.revision}"'
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/license",
    response_model=AgentLicenseResponse | None,
)
async def get_agent_license(
    deployment_id: str, request: Request, response: Response
) -> AgentLicenseResponse | None:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    result = await _run(
        pull_agent_license,
        request.app.state.config,
        deployment_id,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        _edge_private(response)
        return None
    response.headers["ETag"] = f'"{result.entitlement.manifest_sha256}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/license/acks",
    response_model=AgentLicenseAcknowledgementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_agent_license_acknowledgement(
    deployment_id: str, request: Request, response: Response
) -> AgentLicenseAcknowledgementResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _json_model(
        await _strict_json(request, "edge_license.ack_invalid"),
        AgentLicenseAcknowledgementRequest,
        error_code="edge_license.ack_invalid",
    )
    result = await _run(
        acknowledge_agent_license,
        request.app.state.config,
        deployment_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/upgrade",
    response_model=WorkspaceUpgradeResponse,
)
async def get_edge_upgrade(
    deployment_id: str, request: Request, response: Response
) -> WorkspaceUpgradeResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        get_workspace_upgrade,
        request.app.state.config,
        deployment_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.head.revision}"'
    _edge_private(response)
    return result


@router.patch(
    "/edge/deployments/{deployment_id}/maintenance-window",
    response_model=WorkspaceUpgradeResponse,
)
async def patch_edge_maintenance_window(
    deployment_id: str, request: Request, response: Response
) -> WorkspaceUpgradeResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge_upgrade.maintenance_invalid"),
        MaintenanceWindowUpdateRequest,
        error_code="edge_upgrade.maintenance_invalid",
    )
    result = await _run(
        update_workspace_maintenance,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.maintenance.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/rollback",
    response_model=RollbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_upgrade_rollback(
    deployment_id: str, request: Request, response: Response
) -> RollbackResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge_upgrade.request_invalid"),
        RollbackCreateRequest,
        error_code="edge_upgrade.request_invalid",
    )
    result = await _run(
        request_workspace_rollback,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_required_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.rollback.revision}"'
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/upgrades/pending",
    response_model=AgentUpgradeCommandResponse | None,
)
async def get_agent_upgrade(
    deployment_id: str, request: Request, response: Response
) -> AgentUpgradeCommandResponse | None:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    result = await _run(
        pull_agent_upgrade,
        request.app.state.config,
        deployment_id,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        _edge_private(response)
        return None
    response.headers["ETag"] = f'"{result.command_revision if result else 0}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/upgrades/{command_id}",
    response_model=AgentUpgradeCommandResponse,
)
async def post_agent_upgrade(
    deployment_id: str, command_id: str, request: Request, response: Response
) -> AgentUpgradeCommandResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge_upgrade.request_invalid"),
        AgentUpgradeMutationRequest,
        error_code="edge_upgrade.request_invalid",
    )
    result = await _run(
        mutate_agent_upgrade,
        request.app.state.config,
        deployment_id,
        command_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.command_revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/upgrades/{command_id}/report",
    response_model=AgentUpgradeReceiptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_agent_upgrade_report(
    deployment_id: str, command_id: str, request: Request, response: Response
) -> AgentUpgradeReceiptResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _json_model(
        await _strict_json(request, "edge_upgrade.receipt_invalid"),
        AgentUpgradeReportRequest,
        error_code="edge_upgrade.receipt_invalid",
    )
    if payload.report.upgrade_command_id != command_id:
        raise ApiError("edge_upgrade.receipt_invalid")
    result = await _run(
        record_agent_upgrade_report,
        request.app.state.config,
        deployment_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/desired-state",
    response_model=EdgeDesiredStateResponse | EdgeDesiredStatePullResponse,
)
async def get_edge_desired_state(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse | EdgeDesiredStatePullResponse | Response:
    _reject_query(request)
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=False)
        result = await _run(
            get_workspace_desired_state,
            request.app.state.config,
            deployment_id,
            session_token=_workspace_cookie(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response.headers["ETag"] = _desired_etag(result)
    else:
        fingerprint = _prepare_agent(request, require_mtls=True)
        result = await _run(
            pull_agent_desired_state,
            request.app.state.config,
            deployment_id,
            verified_fingerprint=fingerprint,
            edge_session_token=_edge_session(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response = Response(
            content=canonical_edge_json_bytes(result.model_dump(mode="json"), maximum=786_432),
            media_type="application/json",
        )
        response.headers["ETag"] = f'"{result.envelope_sha256}"'
        _edge_private(response)
        return response
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/desired-state",
    response_model=EdgeDesiredStateResponse,
)
async def post_edge_desired_state(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    raw = await _strict_json(request, "edge.request_invalid")
    if raw.get("action") == "publish":
        payload = _model(raw, EdgeDesiredStatePublishRequest)
    elif raw.get("action") in {"approve", "reject"}:
        payload = _model(raw, EdgeDesiredStateApprovalRequest)
    else:
        raise ApiError("edge.request_invalid")
    result = await _run(
        write_workspace_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_desired_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _desired_etag(result)
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/acks",
    response_model=EdgeAckResponse,
)
async def post_edge_ack(
    deployment_id: str, request: Request, response: Response
) -> EdgeAckResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_json(request, "edge.request_invalid"), EdgeAckCreateRequest)
    result = await _run(
        acknowledge_agent_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/rollbacks",
    response_model=EdgeDesiredStateResponse,
)
async def post_edge_rollback(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"),
        EdgeDesiredStateRollbackRequest,
    )
    result = await _run(
        rollback_workspace_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_desired_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _desired_etag(result)
    _edge_private(response)
    return result


@router.post(
    "/edge/telemetry/batches",
    response_model=EdgeTelemetryReceiptWire,
)
async def post_edge_telemetry_batch(
    request: Request, response: Response
) -> EdgeTelemetryReceiptWire:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_telemetry_json(request), EdgeTelemetryBatch)
    result = await _run(
        receive_agent_telemetry,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/telemetry-watermark",
    response_model=EdgeTelemetryWatermarkResponse | EdgeAgentTelemetryWatermarkResponse,
)
async def get_edge_telemetry_watermark(
    deployment_id: str, request: Request, response: Response
) -> EdgeTelemetryWatermarkResponse | EdgeAgentTelemetryWatermarkResponse:
    _reject_query(request)
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=False)
        result = await _run(
            get_workspace_telemetry_watermark,
            request.app.state.config,
            deployment_id,
            session_token=_workspace_cookie(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    else:
        fingerprint = _prepare_agent(request, require_mtls=True)
        result = await _run(
            get_agent_telemetry_watermark,
            request.app.state.config,
            deployment_id,
            verified_fingerprint=fingerprint,
            edge_session_token=_edge_session(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    response.headers["ETag"] = f'"{result.policy.revision if result.policy else 0}"'
    _edge_private(response)
    return result


@router.patch(
    "/edge/deployments/{deployment_id}/telemetry-policy",
    response_model=EdgeTelemetryPolicyResponse,
)
async def patch_edge_telemetry_policy(
    deployment_id: str, request: Request, response: Response
) -> EdgeTelemetryPolicyResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeTelemetryPolicyPatchRequest
    )
    result = await _run(
        patch_workspace_telemetry_policy,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.policy.revision}"'
    _edge_private(response)
    return result


def require_platform_edge_fleet(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
        "platform.edge_fleet.read",
        "platform.edge_fleet.manage",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_platform_edge_installer(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
    allowed = (
        {"platform.edge_installer.read"}
        if request.method == "GET"
        else {
            "platform.edge_installer.manage",
            "platform.edge_installer.approve",
        }
    )
    if not allowed.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_platform_edge_upgrade(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
        "platform.edge_release.read",
        "platform.edge_release.manage",
        "platform.edge_release.approve",
        "platform.edge_rollout.read",
        "platform.edge_rollout.manage",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_platform_edge_license(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
        "platform.edge_license.read",
        "platform.edge_license.issue",
        "platform.edge_license.revoke",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_platform_edge_backup(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
    if not {"platform.edge_backup.read", "platform.edge_drill.manage"}.intersection(
        context.permissions
    ):
        raise ApiError("platform.permission_denied")
    return context


def require_platform_edge_offline(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
        "platform.edge_offline_bundle.read",
        "platform.edge_offline_bundle.manage",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.post(
    "/edge/offline-bundles/export",
    response_model=OfflineBundleStateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_offline_bundle_export(
    request: Request, response: Response
) -> OfflineBundleStateResponse:
    return await _post_workspace_offline_bundle(request, response, operation="export")


@router.post(
    "/edge/offline-bundles/import",
    response_model=OfflineBundleStateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_offline_bundle_import(
    request: Request, response: Response
) -> OfflineBundleStateResponse:
    return await _post_workspace_offline_bundle(request, response, operation="import")


async def _post_workspace_offline_bundle(
    request: Request, response: Response, *, operation: str
) -> OfflineBundleStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _json_model(
        await _strict_json(request, "edge_offline_bundle.request_invalid"),
        WorkspaceOfflineBundleRequest,
        error_code="edge_offline_bundle.request_invalid",
    )
    result = await _run(
        request_workspace_offline_bundle,
        request.app.state.config,
        operation,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_required_step_up_grant(request),
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_offline_bundle.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.post(
    "/edge/offline-bundles/conflicts/{conflict_id}/resolutions",
    response_model=OfflineBundleStateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_offline_conflict_resolution(
    conflict_id: str, request: Request, response: Response
) -> OfflineBundleStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _json_model(
        await _strict_json(request, "edge_offline_bundle.request_invalid"),
        WorkspaceOfflineResolutionRequest,
        error_code="edge_offline_bundle.request_invalid",
    )
    result = await _run(
        request_workspace_offline_resolution,
        request.app.state.config,
        conflict_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_required_step_up_grant(request),
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_offline_bundle.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/offline-bundles",
    response_model=OfflineBundleStateResponse,
)
async def get_edge_offline_bundles(
    deployment_id: str, request: Request, response: Response
) -> OfflineBundleStateResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        get_workspace_offline_state,
        request.app.state.config,
        deployment_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.get(
    "/platform/edge/offline-bundles",
    response_model=PlatformOfflineBundleListResponse,
)
async def get_platform_edge_offline_bundles(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_offline)],
) -> PlatformOfflineBundleListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_offline_states,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/edge/offline-bundles/{offline_request_id}/decisions",
    response_model=PlatformOfflineDecisionResponse,
)
async def post_platform_edge_offline_decision(
    offline_request_id: str,
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_offline)],
) -> PlatformOfflineDecisionResponse:
    _reject_query(request)
    payload = _json_model(
        await _strict_json(request, "edge_offline_bundle.request_invalid"),
        PlatformOfflineDecisionRequest,
        error_code="edge_offline_bundle.request_invalid",
    )
    result = await _run(
        decide_platform_offline_request,
        request.app.state.config,
        context,
        offline_request_id,
        payload,
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_offline_bundle.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/backup-status",
    response_model=BackupStatusResponse,
)
async def get_edge_backup_status(
    deployment_id: str, request: Request, response: Response
) -> BackupStatusResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        get_workspace_backup_status,
        request.app.state.config,
        deployment_id,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.status.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/restore-drills",
    response_model=RestoreDrillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_restore_drill(
    deployment_id: str, request: Request, response: Response
) -> RestoreDrillResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge_backup.request_invalid"),
        RestoreDrillMutationRequest,
        error_code="edge_backup.request_invalid",
    )
    result = await _run(
        mutate_workspace_restore_drill,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_required_step_up_grant(request),
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_backup.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if payload.action == "approve" or result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.drill.revision}"'
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/backups/reports",
    response_model=BackupReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_backup_report(
    deployment_id: str, request: Request, response: Response
) -> BackupReportResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _json_model(
        await _strict_json(request, "edge_backup.request_invalid"),
        AgentBackupReportRequest,
        error_code="edge_backup.request_invalid",
    )
    result = await _run(
        report_agent_backup,
        request.app.state.config,
        deployment_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/restore-drills/{drill_id}/reports",
    response_model=RestoreDrillResponse,
)
async def post_edge_restore_drill_report(
    deployment_id: str, drill_id: str, request: Request, response: Response
) -> RestoreDrillResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _json_model(
        await _strict_json(request, "edge_backup.request_invalid"),
        AgentDrillReportRequest,
        error_code="edge_backup.request_invalid",
    )
    result = await _run(
        report_agent_restore_drill,
        request.app.state.config,
        deployment_id,
        drill_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.drill.revision}"'
    _edge_private(response)
    return result


@router.get(
    "/platform/edge/backups",
    response_model=PlatformBackupStatusListResponse,
)
async def get_platform_edge_backups(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_backup)],
) -> PlatformBackupStatusListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_backup_statuses,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/edge/licenses",
    response_model=LicenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_platform_edge_license(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_license)],
) -> LicenseResponse:
    _reject_query(request)
    payload = _json_model(
        await _strict_json(request, "edge_license.request_invalid"),
        PlatformLicenseIssueRequest,
        error_code="edge_license.request_invalid",
    )
    result = await _run(
        issue_platform_license,
        request.app.state.config,
        context,
        payload,
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_license.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.license.revision}"'
    _private(response)
    return result


@router.get(
    "/platform/edge/licenses",
    response_model=LicenseListResponse,
)
async def get_platform_edge_licenses(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_license)],
) -> LicenseListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_licenses,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/edge/licenses/{license_id}/revoke",
    response_model=LicenseResponse,
)
async def post_platform_edge_license_revoke(
    license_id: str,
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_license)],
) -> LicenseResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge_license.request_invalid"),
        PlatformLicenseRevokeRequest,
        error_code="edge_license.request_invalid",
    )
    result = await _run(
        revoke_platform_license,
        request.app.state.config,
        context,
        license_id,
        payload,
        idempotency_key=_platform_idempotency_key(
            request, error_code="edge_license.request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.license.revision}"'
    _private(response)
    return result


@router.get("/platform/releases", response_model=GatewayReleaseListResponse)
async def get_platform_gateway_releases(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> GatewayReleaseListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_gateway_releases,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/releases", response_model=GatewayReleaseResponse)
async def post_platform_gateway_release(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> GatewayReleaseResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "fleet.release_request_invalid"),
        PlatformGatewayReleaseMutationRequest,
        error_code="fleet.release_request_invalid",
    )
    result = await _run(
        mutate_platform_gateway_release,
        request.app.state.config,
        context,
        payload,
        step_up_grant=(None if payload.action == "submit" else _required_step_up_grant(request)),
        idempotency_key=_platform_idempotency_key(
            request, error_code="fleet.release_request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if payload.action == "submit" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.release.revision}"'
    _private(response)
    return result


@router.get("/platform/rollouts", response_model=RolloutListResponse)
async def get_platform_rollouts(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> RolloutListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_rollouts,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/releases/{release_id}/rollouts",
    response_model=RolloutResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_platform_rollout(
    release_id: str,
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> RolloutResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "fleet.rollout_request_invalid"),
        PlatformRolloutCreateRequest,
        error_code="fleet.rollout_request_invalid",
    )
    result = await _run(
        create_platform_rollout,
        request.app.state.config,
        context,
        release_id,
        payload,
        idempotency_key=_platform_idempotency_key(
            request, error_code="fleet.rollout_request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.rollout.revision}"'
    _private(response)
    return result


@router.post("/fleet/rollouts/{rollout_id}/pause", response_model=RolloutResponse)
async def post_fleet_rollout_pause(
    rollout_id: str,
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> RolloutResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "fleet.rollout_request_invalid"),
        FleetRolloutMutationRequest,
        error_code="fleet.rollout_request_invalid",
    )
    result = await _run(
        mutate_platform_rollout,
        request.app.state.config,
        context,
        rollout_id,
        payload,
        idempotency_key=_platform_idempotency_key(
            request, error_code="fleet.rollout_request_invalid"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.rollout.revision}"'
    _private(response)
    return result


@router.get("/fleet/compatibility", response_model=FleetCompatibilityResponse)
async def get_platform_fleet_compatibility(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_upgrade)],
) -> FleetCompatibilityResponse:
    query = _compatibility_query(request)
    result = await _run(
        get_fleet_compatibility,
        request.app.state.config,
        context,
        release_id=query["release_id"],
        deployment_id=query["deployment_id"],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/edge/installers",
    response_model=PlatformInstallerReleaseListResponse,
)
async def get_platform_edge_installers(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_installer)],
) -> PlatformInstallerReleaseListResponse:
    _reject_query(request)
    result = await _run(
        list_platform_installer_releases,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/edge/installers",
    response_model=PlatformInstallerReleaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_platform_edge_installer(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_installer)],
) -> PlatformInstallerReleaseResponse:
    _reject_query(request)
    payload = _platform_model(
        await _strict_json(request, "platform.edge_installer_request_invalid"),
        PlatformInstallerReleaseSubmitRequest,
    )
    result = await _run(
        submit_platform_installer_release,
        request.app.state.config,
        context,
        payload,
        idempotency_key=_platform_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.release.revision}"'
    _private(response)
    return result


@router.patch(
    "/platform/edge/installers/{release_id}",
    response_model=PlatformInstallerReleaseResponse,
)
async def patch_platform_edge_installer(
    release_id: str,
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_installer)],
) -> PlatformInstallerReleaseResponse:
    _reject_query(request)
    payload = _platform_model(
        await _strict_json(request, "platform.edge_installer_request_invalid"),
        PlatformInstallerReleaseDecisionRequest,
    )
    result = await _run(
        decide_platform_installer_release,
        request.app.state.config,
        context,
        release_id,
        payload,
        step_up_grant=_required_step_up_grant(request),
        expected_revision=_platform_if_match(request),
        idempotency_key=_platform_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.release.revision}"'
    _private(response)
    return result


@router.get(
    "/platform/fleet",
    response_model=PlatformFleetPageResponse,
)
async def get_platform_fleet(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_fleet)],
) -> PlatformFleetPageResponse:
    query = _fleet_query(request)
    result = await _run(
        list_platform_fleet,
        request.app.state.config,
        context,
        status_filter=query["status"],
        region_filter=query["region"],
        health_filter=query["health"],
        cursor=query["cursor"],
        limit=query["limit"],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/fleet/edge-deployments",
    response_model=PlatformEdgeFleetResponse,
)
async def get_platform_edge_fleet(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_fleet)],
) -> PlatformEdgeFleetResponse:
    _reject_query(request)
    result = await _run(
        list_platform_edge_fleet,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _prepare_workspace(request: Request, *, write: bool) -> None:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    session_token = _workspace_cookie(request)
    assert_workspace_private_gateway_enabled(
        request.app.state.config, session_token=session_token
    )
    if write:
        _require_origin(request, "edge.request_invalid")
        _workspace_csrf(request)


def _prepare_agent(request: Request, *, require_mtls: bool) -> str:
    if _raw_headers(request, b"authorization") or _raw_headers(request, b"cookie"):
        raise ApiError("edge.authentication_failed")
    if any(
        name.lower() in _UNTRUSTED_CERT_HEADERS for name, _value in request.scope.get("headers", ())
    ):
        raise ApiError("edge.authentication_failed")
    fingerprint = verified_mtls_fingerprint(request)
    if require_mtls and fingerprint is None:
        raise ApiError("edge.authentication_failed")
    return fingerprint or ""


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("edge.request_invalid")
    return int(values[0][1:-1])


def _desired_if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"(?:0|[1-9][0-9]*)"', values[0]) is None:
        raise ApiError("edge.request_invalid")
    return int(values[0][1:-1])


def _edge_session(request: Request) -> str:
    values = _raw_headers(request, b"x-edge-session")
    if len(values) != 1:
        raise ApiError("edge.authentication_failed")
    return values[0]


def _edge_deployment(request: Request) -> str:
    values = _raw_headers(request, b"x-edge-deployment-id")
    if len(values) != 1 or re.fullmatch(r"edp_[0-9a-f]{32}", values[0]) is None:
        raise ApiError("edge.authentication_failed")
    return values[0]


def _prepare_support(request: Request) -> None:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    ):
        raise ApiError("support.session_invalid")
    values = _cookie_values(request, SUPPORT_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("support.session_invalid")


def _desired_etag(result: EdgeDesiredStateResponse) -> str:
    if result.state is None:
        return '"0"'
    revision = (
        result.state.revision
        if result.state.status == "approval_pending"
        else result.state.head_revision
    )
    return f'"{revision}"'


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("edge.request_invalid")
    return values[0]


def _platform_idempotency_key(
    request: Request, *, error_code: str = "platform.edge_installer_request_invalid"
) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError(error_code)
    return values[0]


def _platform_if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("platform.edge_installer_request_invalid")
    return int(values[0][1:-1])


def _optional_step_up_grant(request: Request) -> str | None:
    values = _raw_headers(request, b"x-step-up-grant")
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.required")
    return values[0]


def _required_step_up_grant(request: Request) -> str:
    grant = _optional_step_up_grant(request)
    if grant is None:
        raise ApiError("step_up.required")
    return grant


def _model(payload: dict[str, Any], model_type, *, error_code: str = "edge.request_invalid"):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError(error_code) from None


def _json_model(payload: dict[str, Any], model_type, *, error_code: str = "edge.request_invalid"):
    try:
        return model_type.model_validate_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    except ValueError:
        raise ApiError(error_code) from None


def _platform_model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("platform.edge_installer_request_invalid") from None


async def _strict_telemetry_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if (
        len(content_types) != 1
        or re.fullmatch(r"application/json(?:;\s*charset=utf-8)?", content_types[0], re.IGNORECASE)
        is None
    ):
        raise ApiError("edge.request_invalid")
    encodings = _raw_headers(request, b"content-encoding")
    if len(encodings) > 1:
        raise ApiError("edge.request_invalid")
    encoding = encodings[0].lower() if encodings else "identity"
    if encoding not in {"gzip", "identity"}:
        raise ApiError("edge.request_invalid")
    maximum_body = _MAX_TELEMETRY_COMPRESSED_BYTES if encoding == "gzip" else _MAX_TELEMETRY_BYTES
    body = await _bounded_body(request, maximum_body)
    try:
        if encoding == "gzip":
            if not body:
                raise ApiError("edge.request_invalid")
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(body, _MAX_TELEMETRY_BYTES + 1)
            if (
                len(decoded) > _MAX_TELEMETRY_BYTES
                or decoder.unconsumed_tail
                or decoder.unused_data
                or not decoder.eof
                or (len(decoded) > 65_536 and len(decoded) > len(body) * 64)
            ):
                raise ApiError("edge.request_invalid")
        elif encoding == "identity":
            if not body:
                raise ApiError("edge.request_invalid")
            decoded = body

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        def invalid_constant(_value: str) -> object:
            raise ValueError("constant")

        parsed = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
        if type(parsed) is not dict:
            raise ValueError("object")
        reject_forbidden_telemetry_fields(parsed)
        return parsed
    except ApiError:
        raise
    except (EdgeTelemetryError, RecursionError, UnicodeError, ValueError, zlib.error):
        raise ApiError("edge.request_invalid") from None


async def _bounded_body(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise ApiError("edge.request_invalid")
        body.extend(chunk)
    return bytes(body)


async def _read_support_bundle_upload(request: Request, staged_path) -> SupportBundleUploadMetadata:
    content_types = _raw_headers(request, b"content-type")
    lengths = _raw_headers(request, b"content-length")
    if (
        len(content_types) != 1
        or content_types[0].lower() != _SUPPORT_BUNDLE_MEDIA_TYPE
        or len(lengths) != 1
        or not lengths[0].isascii()
        or not lengths[0].isdecimal()
    ):
        raise ApiError("edge.support_bundle_invalid")
    declared_length = int(lengths[0])
    if not 38 <= declared_length <= _MAX_SUPPORT_BUNDLE_WIRE_BYTES:
        raise ApiError("edge.support_bundle_invalid")
    prefix = bytearray()
    metadata_bytes = bytearray()
    metadata_length: int | None = None
    metadata: SupportBundleUploadMetadata | None = None
    object_digest = hashlib.sha256()
    object_size = 0
    object_prefix = bytearray()
    received = 0
    try:
        with staged_path.open("wb") as writer:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > declared_length or received > _MAX_SUPPORT_BUNDLE_WIRE_BYTES:
                    raise ApiError("edge.support_bundle_invalid")
                offset = 0
                while offset < len(chunk):
                    if len(prefix) < 4:
                        count = min(4 - len(prefix), len(chunk) - offset)
                        prefix.extend(chunk[offset : offset + count])
                        offset += count
                        if len(prefix) == 4:
                            metadata_length = int.from_bytes(prefix, "big")
                            if not 1 <= metadata_length <= _MAX_SUPPORT_BUNDLE_METADATA_BYTES:
                                raise ApiError("edge.support_bundle_invalid")
                        continue
                    if metadata is None:
                        assert metadata_length is not None
                        count = min(metadata_length - len(metadata_bytes), len(chunk) - offset)
                        metadata_bytes.extend(chunk[offset : offset + count])
                        offset += count
                        if len(metadata_bytes) < metadata_length:
                            continue
                        try:
                            metadata = SupportBundleUploadMetadata.model_validate(
                                strict_json_object(
                                    bytes(metadata_bytes),
                                    canonical=True,
                                    maximum=_MAX_SUPPORT_BUNDLE_METADATA_BYTES,
                                )
                            )
                        except ValueError:
                            raise ApiError("edge.support_bundle_invalid") from None
                        if declared_length != 4 + metadata_length + metadata.object_size_bytes:
                            raise ApiError("edge.support_bundle_invalid")
                        continue
                    count = len(chunk) - offset
                    if object_size + count > metadata.object_size_bytes:
                        raise ApiError("edge.support_bundle_invalid")
                    part = chunk[offset:]
                    if len(object_prefix) < 5:
                        object_prefix.extend(part[: 5 - len(object_prefix)])
                    writer.write(part)
                    object_digest.update(part)
                    object_size += count
                    offset = len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if (
            received != declared_length
            or metadata is None
            or object_size != metadata.object_size_bytes
            or bytes(object_prefix) != b"MHSB1"
            or not hmac.compare_digest(object_digest.hexdigest(), metadata.object_sha256)
        ):
            raise ApiError("edge.support_bundle_invalid")
        return metadata
    except ApiError:
        raise
    except (OSError, RuntimeError):
        raise ApiError("edge.support_bundle_storage_unavailable") from None


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("edge.request_invalid")


def _fleet_query(request: Request) -> dict[str, str | int | None]:
    allowed = {"status", "region", "health", "cursor", "limit"}
    items = request.query_params.multi_items()
    names = [name for name, _value in items]
    if any(name not in allowed for name in names) or len(names) != len(set(names)):
        raise ApiError("platform.edge_fleet_request_invalid")
    values = {name: value for name, value in items}
    raw_limit = values.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None:
        raise ApiError("platform.edge_fleet_request_invalid")
    limit = int(raw_limit)
    if limit > 200:
        raise ApiError("platform.edge_fleet_request_invalid")
    return {
        "status": values.get("status"),
        "region": values.get("region"),
        "health": values.get("health"),
        "cursor": values.get("cursor"),
        "limit": limit,
    }


def _compatibility_query(request: Request) -> dict[str, str | None]:
    allowed = {"release_id", "deployment_id"}
    items = request.query_params.multi_items()
    names = [name for name, _value in items]
    if any(name not in allowed for name in names) or len(names) != len(set(names)):
        raise ApiError("fleet.compatibility_request_invalid")
    values = dict(items)
    release_id = values.get("release_id")
    deployment_id = values.get("deployment_id")
    if release_id is not None and re.fullmatch(r"ger_[0-9a-f]{32}", release_id) is None:
        raise ApiError("fleet.compatibility_request_invalid")
    if deployment_id is not None and re.fullmatch(r"edp_[0-9a-f]{32}", deployment_id) is None:
        raise ApiError("fleet.compatibility_request_invalid")
    return {"release_id": release_id, "deployment_id": deployment_id}


def _edge_private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _run(function, /, *args, **kwargs):
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=_WORK_LIMITER)


__all__ = ["router"]
