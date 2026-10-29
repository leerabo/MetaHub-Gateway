"""Loopback or explicitly bound local secret management API."""

from __future__ import annotations

import re
from collections.abc import Callable
from secrets import token_hex, token_urlsafe

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from metahub_shared.private_gateway import (
    LOCAL_SECRET_MAX_BYTES,
    SUPPORT_BUNDLE_COLLECTOR_CODES,
    canonical_edge_json_bytes,
    strict_json_object,
)

from .auth import (
    LocalAdminAuthError,
    LocalAdminClaims,
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalManagementPolicy,
    csrf_sha256,
)
from .backups import LocalBackupError, LocalBackupStore, LocalDrillContext
from .store import LocalSecretStore, LocalSecretStoreError, ProviderTestResult
from .support_bundles import LocalSupportBundleError, LocalSupportBundleStore

router = APIRouter(prefix="/local", tags=["local-secret-custody"])

_SESSION_COOKIE = "__Host-metahub-local-session"
_CSRF_COOKIE = "__Host-metahub-local-csrf"
_ID = re.compile(r"^[a-z][a-z0-9_:-]{2,79}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$")
_KINDS = frozenset({"api_key", "bearer_token", "basic_password", "oauth_client_secret"})
_SUPPORT_BUNDLE_BODY_BYTES = 4_096
_BACKUP_BODY_BYTES = 4_096
_FORWARDED_HEADERS = frozenset(
    {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"x-original-url",
        b"x-rewrite-url",
    }
)
_SAFE_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@router.post("/admin/session-handoffs")
async def create_local_admin_session(request: Request) -> JSONResponse:
    _authorize_boundary(request)
    store = _store(request)
    deployment_id = _header(request, "x-local-deployment-id", pattern=_DEPLOYMENT_ID)
    account_id = _header(request, "x-local-account-id", pattern=_ID)
    tenant_id = _header(request, "x-local-tenant-id", pattern=_ID)
    workspace_id = _header(request, "x-local-workspace-id", pattern=_ID)
    credential = await _handoff_body(request)
    try:
        if deployment_id != store.deployment_id or request.client is None:
            raise _api_error("local_admin.authentication_failed", 401)
        try:
            _handoff_verifier(request).verify(credential, client_id=request.client.host)
        except LocalAdminAuthError as exc:
            status = 429 if exc.code == "local_admin.rate_limited" else 401
            raise _api_error(exc.code, status) from None

        csrf = token_urlsafe(32)
        now = _signer(request).now()
        if type(now) is not int:
            raise _api_error("local_secret.unavailable", 503)
        expires_at = now + 600
        claims = LocalAdminClaims.model_validate(
            {
                "schema_version": "1",
                "session_id": "las_" + token_hex(16),
                "account_id": account_id,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "deployment_id": deployment_id,
                "role": "tenant_security_admin",
                "csrf_sha256": csrf_sha256(csrf),
                "issued_at": now,
                "expires_at": expires_at,
                "step_up_expires_at": expires_at,
            }
        )
        response = _json({"deployment_id": deployment_id, "expires_at": expires_at}, 201)
        response.headers["X-CSRF-Token"] = csrf
        response.set_cookie(
            _SESSION_COOKIE,
            _signer(request).issue(claims),
            max_age=600,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.set_cookie(
            _CSRF_COOKIE,
            csrf,
            max_age=600,
            path="/",
            secure=True,
            httponly=False,
            samesite="strict",
        )
        return response
    finally:
        _zero(credential)


@router.delete("/admin/session")
async def delete_local_admin_session(request: Request) -> Response:
    _authorize_boundary(request)
    response = Response(status_code=204, headers=_SAFE_HEADERS)
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        _CSRF_COOKIE,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    return response


@router.post("/secrets")
async def create_local_secret(request: Request) -> JSONResponse:
    claims = _authorize(request)
    store = _store(request)
    provider_connection_id = _header(request, "x-provider-connection-id", pattern=_ID)
    label = _header(request, "x-local-secret-label", pattern=_LABEL)
    kind_value = _header(request, "x-local-secret-kind")
    if kind_value not in _KINDS:
        raise _api_error("local_secret.payload_invalid", 422)
    plaintext = await _secret_body(request)
    try:
        reference = store.create(
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            provider_connection_id=provider_connection_id,
            label=label,
            kind=kind_value,  # type: ignore[arg-type]
            plaintext=plaintext,
        )
        return _json(reference.model_dump(mode="json"), 201)
    except LocalSecretStoreError as exc:
        raise _store_api_error(exc) from None
    finally:
        _zero(plaintext)


@router.post("/secrets/{reference_id}/rotate")
async def rotate_local_secret(reference_id: str, request: Request) -> JSONResponse:
    claims = _authorize(request)
    store = _store(request)
    provider_connection_id = _provider_connection_id(store, claims, reference_id)
    revision = _revision(request)
    plaintext = await _secret_body(request)
    try:
        reference = store.rotate(
            reference_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            plaintext=plaintext,
            tester=_tester(request, provider_connection_id),
            expected_revision=revision,
        )
        return _json(reference.model_dump(mode="json"), 200)
    except LocalSecretStoreError as exc:
        raise _store_api_error(exc) from None
    finally:
        _zero(plaintext)


@router.delete("/secrets/{reference_id}")
async def delete_local_secret(reference_id: str, request: Request) -> Response:
    claims = _authorize(request)
    try:
        _store(request).delete(
            reference_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            expected_revision=_revision(request),
        )
        return Response(status_code=204, headers=_SAFE_HEADERS)
    except LocalSecretStoreError as exc:
        raise _store_api_error(exc) from None


@router.post("/provider-connections/{provider_connection_id}/test")
async def test_local_provider_connection(
    provider_connection_id: str, request: Request
) -> JSONResponse:
    claims = _authorize(request)
    references = _store(request).list_references(
        tenant_id=claims.tenant_id, workspace_id=claims.workspace_id
    )
    reference = next(
        (
            item
            for item in references
            if item.provider_connection_id == provider_connection_id and item.status != "disabled"
        ),
        None,
    )
    if reference is None:
        raise _api_error("local_secret.not_found", 404)
    try:
        result = _store(request).test_connection(
            reference.reference_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            tester=_tester(request, reference.provider_connection_id),
        )
        return _json(
            {
                "test_id": result.test_id,
                "reference_id": result.reference_id,
                "version": result.version,
                "outcome": result.outcome,
                "capabilities": list(result.capabilities),
                "latency_bucket": result.latency_bucket,
                "created_at": result.created_at,
            },
            200,
        )
    except LocalSecretStoreError as exc:
        raise _store_api_error(exc) from None


@router.post("/support-bundles")
async def preview_local_support_bundle(request: Request) -> JSONResponse:
    claims = _authorize(request)
    collector_codes = await _support_bundle_body(request)
    try:
        summary = _support_bundle_store(request).preview(
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            account_id=claims.account_id,
            collector_codes=collector_codes,
        )
        return _json(summary.model_dump(mode="json"), 201)
    except LocalSupportBundleError as exc:
        raise _support_bundle_api_error(exc) from None


@router.post("/support-bundles/{bundle_id}/approve")
async def approve_local_support_bundle(bundle_id: str, request: Request) -> JSONResponse:
    claims = _authorize(request)
    try:
        summary = _support_bundle_store(request).approve(
            bundle_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            account_id=claims.account_id,
            expected_revision=_revision(
                request, error_code="local_support_bundle.revision_invalid"
            ),
        )
        return _json(summary.model_dump(mode="json"), 200)
    except LocalSupportBundleError as exc:
        raise _support_bundle_api_error(exc) from None


@router.delete("/support-bundles/{bundle_id}")
async def cancel_local_support_bundle(bundle_id: str, request: Request) -> JSONResponse:
    claims = _authorize(request)
    try:
        summary = _support_bundle_store(request).cancel(
            bundle_id,
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            expected_revision=_revision(
                request, error_code="local_support_bundle.revision_invalid"
            ),
        )
        return _json(summary.model_dump(mode="json"), 200)
    except LocalSupportBundleError as exc:
        raise _support_bundle_api_error(exc) from None


@router.post("/backups")
async def create_local_backup(request: Request) -> JSONResponse:
    claims = _authorize(request)
    payload = await _backup_body(request, operation="create")
    try:
        summary = _backup_store(request).create(
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            account_id=claims.account_id,
            source_generation=payload["source_generation"],  # type: ignore[arg-type]
            retention_seconds=payload["retention_seconds"],  # type: ignore[arg-type]
            storage_kind=payload["storage_kind"],  # type: ignore[arg-type]
        )
        return _json(summary.model_dump(mode="json"), 201)
    except LocalBackupError as exc:
        raise _backup_api_error(exc) from None


@router.post("/restores")
async def restore_local_backup(request: Request) -> JSONResponse:
    claims = _authorize(request)
    payload = await _backup_body(request, operation="restore")
    try:
        drill_payload = payload.get("drill_context")
        drill_context = (
            None
            if drill_payload is None
            else LocalDrillContext.model_validate_json(
                canonical_edge_json_bytes(drill_payload, maximum=_BACKUP_BODY_BYTES)
            )
        )
        summary = _backup_store(request).restore(
            payload["backup_id"],  # type: ignore[arg-type]
            tenant_id=claims.tenant_id,
            workspace_id=claims.workspace_id,
            account_id=claims.account_id,
            target_schema_head=payload["target_schema_head"],  # type: ignore[arg-type]
            drill_context=drill_context,
        )
        return _json(summary.model_dump(mode="json"), 200)
    except (TypeError, ValueError):
        raise _api_error("local_restore.request_invalid", 422) from None
    except LocalBackupError as exc:
        raise _backup_api_error(exc) from None


def _authorize(request: Request) -> LocalAdminClaims:
    _authorize_boundary(request)
    if len(_raw_headers(request, b"cookie")) != 1:
        raise _api_error("local_admin.authentication_failed", 401)
    token = request.cookies.get(_SESSION_COOKIE)
    csrf_cookie = request.cookies.get(_CSRF_COOKIE)
    csrf_header = _header(request, "x-csrf-token")
    if csrf_cookie is None or csrf_header != csrf_cookie:
        raise _api_error("local_admin.csrf_invalid", 403)
    try:
        return _signer(request).verify(
            token,
            csrf_token=csrf_header,
            deployment_id=_store(request).deployment_id,
            require_step_up=True,
        )
    except LocalAdminAuthError:
        raise _api_error("local_admin.authentication_failed", 401) from None


def _authorize_boundary(request: Request) -> None:
    forwarded = any(name.lower() in _FORWARDED_HEADERS for name, _ in request.scope["headers"])
    if request.url.query or forwarded:
        raise _api_error("local_admin.request_invalid", 400)
    if request.client is None or not _policy(request).permits_client(request.client.host):
        raise _api_error("local_admin.network_denied", 403)
    origin = _header(request, "origin")
    if origin not in _policy(request).allowed_origins:
        raise _api_error("local_admin.origin_invalid", 403)
    if _header(request, "sec-fetch-site") not in {"same-origin", "same-site"}:
        raise _api_error("local_admin.origin_invalid", 403)


async def _handoff_body(request: Request) -> bytearray:
    if request.headers.get("content-type", "").partition(";")[0].lower() != (
        "application/octet-stream"
    ):
        raise _api_error("local_admin.content_type_invalid", 415)
    if request.headers.get("content-encoding") is not None:
        raise _api_error("local_admin.authentication_failed", 401)
    length_header = request.headers.get("content-length")
    if length_header is not None:
        try:
            length = int(length_header)
        except ValueError:
            raise _api_error("local_admin.authentication_failed", 401) from None
        if not 1 <= length <= 512:
            raise _api_error("local_admin.authentication_failed", 401)
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 512:
                raise _api_error("local_admin.authentication_failed", 401)
        if not body:
            raise _api_error("local_admin.authentication_failed", 401)
        return body
    except Exception:
        _zero(body)
        raise


async def _secret_body(request: Request) -> bytearray:
    if request.headers.get("content-type", "").partition(";")[0].lower() != (
        "application/octet-stream"
    ):
        raise _api_error("local_secret.content_type_invalid", 415)
    if request.headers.get("content-encoding") is not None:
        raise _api_error("local_secret.payload_invalid", 422)
    length_header = request.headers.get("content-length")
    if length_header is not None:
        try:
            length = int(length_header)
        except ValueError:
            raise _api_error("local_secret.payload_invalid", 422) from None
        if not 1 <= length <= LOCAL_SECRET_MAX_BYTES:
            raise _api_error("local_secret.payload_invalid", 422)
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > LOCAL_SECRET_MAX_BYTES:
                raise _api_error("local_secret.payload_invalid", 422)
        if not body:
            raise _api_error("local_secret.payload_invalid", 422)
        return body
    except Exception:
        _zero(body)
        raise


async def _support_bundle_body(request: Request) -> tuple[str, ...]:
    if request.headers.get("content-type", "").partition(";")[0].lower() != ("application/json"):
        raise _api_error("local_support_bundle.content_type_invalid", 415)
    if request.headers.get("content-encoding") is not None:
        raise _api_error("local_support_bundle.request_invalid", 422)
    content_lengths = _raw_headers(request, b"content-length")
    if len(content_lengths) > 1:
        raise _api_error("local_support_bundle.request_invalid", 422)
    if content_lengths:
        try:
            length_text = content_lengths[0].decode("ascii")
            length = int(length_text)
        except (UnicodeDecodeError, ValueError):
            raise _api_error("local_support_bundle.request_invalid", 422) from None
        if str(length) != length_text or not 1 <= length <= _SUPPORT_BUNDLE_BODY_BYTES:
            raise _api_error("local_support_bundle.request_invalid", 422)
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _SUPPORT_BUNDLE_BODY_BYTES:
                raise _api_error("local_support_bundle.request_invalid", 422)
        try:
            payload = strict_json_object(
                bytes(body), canonical=False, maximum=_SUPPORT_BUNDLE_BODY_BYTES
            )
        except ValueError:
            raise _api_error("local_support_bundle.request_invalid", 422) from None
    finally:
        _zero(body)
    if frozenset(payload) != {"collector_codes"}:
        raise _api_error("local_support_bundle.request_invalid", 422)
    codes = payload["collector_codes"]
    if (
        type(codes) is not list
        or not 1 <= len(codes) <= len(SUPPORT_BUNDLE_COLLECTOR_CODES)
        or any(type(code) is not str for code in codes)
        or len(set(codes)) != len(codes)
        or any(code not in SUPPORT_BUNDLE_COLLECTOR_CODES for code in codes)
    ):
        raise _api_error("local_support_bundle.request_invalid", 422)
    return tuple(codes)


async def _backup_body(
    request: Request, *, operation: str
) -> dict[str, object]:
    error_prefix = "local_backup" if operation == "create" else "local_restore"
    if request.headers.get("content-type", "").partition(";")[0].lower() != (
        "application/json"
    ):
        raise _api_error("local_backup.content_type_invalid", 415)
    if request.headers.get("content-encoding") is not None:
        raise _api_error(f"{error_prefix}.request_invalid", 422)
    content_lengths = _raw_headers(request, b"content-length")
    if len(content_lengths) > 1:
        raise _api_error(f"{error_prefix}.request_invalid", 422)
    if content_lengths:
        try:
            length_text = content_lengths[0].decode("ascii")
            length = int(length_text)
        except (UnicodeDecodeError, ValueError):
            raise _api_error(f"{error_prefix}.request_invalid", 422) from None
        if str(length) != length_text or not 1 <= length <= _BACKUP_BODY_BYTES:
            raise _api_error(f"{error_prefix}.request_invalid", 422)
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _BACKUP_BODY_BYTES:
                raise _api_error(f"{error_prefix}.request_invalid", 422)
        try:
            payload = strict_json_object(
                bytes(body), canonical=False, maximum=_BACKUP_BODY_BYTES
            )
        except ValueError:
            raise _api_error(f"{error_prefix}.request_invalid", 422) from None
    finally:
        _zero(body)
    if operation == "create":
        if set(payload) != {"source_generation", "retention_seconds", "storage_kind"}:
            raise _api_error("local_backup.request_invalid", 422)
        if (
            type(payload["source_generation"]) is not int
            or not 1 <= payload["source_generation"] <= 2_147_483_647
            or type(payload["retention_seconds"]) is not int
            or not 3600 <= payload["retention_seconds"] <= 31_536_000
            or payload["storage_kind"] not in {"local", "customer_object_store"}
        ):
            raise _api_error("local_backup.request_invalid", 422)
    elif operation == "restore":
        if set(payload) not in (
            {"backup_id", "target_schema_head"},
            {"backup_id", "target_schema_head", "drill_context"},
        ) or (
            type(payload["backup_id"]) is not str
            or re.fullmatch(r"ebk_[0-9a-f]{32}", payload["backup_id"]) is None
            or type(payload["target_schema_head"]) is not str
            or re.fullmatch(r"[0-9]{4}", payload["target_schema_head"]) is None
            or (
                "drill_context" in payload
                and type(payload["drill_context"]) is not dict
            )
        ):
            raise _api_error("local_restore.request_invalid", 422)
    else:
        raise _api_error("local_backup.request_invalid", 422)
    return payload


def _tester(
    request: Request, provider_connection_id: str
) -> Callable[[memoryview], ProviderTestResult]:
    callback = getattr(request.app.state, "local_provider_tester", None)
    if callback is None or not callable(callback):
        return lambda _secret: ProviderTestResult("provider_unavailable", (), "lt_100ms")
    return lambda secret: callback(provider_connection_id, secret)


def _provider_connection_id(
    store: LocalSecretStore, claims: LocalAdminClaims, reference_id: str
) -> str:
    try:
        reference = next(
            (
                item
                for item in store.list_references(
                    tenant_id=claims.tenant_id, workspace_id=claims.workspace_id
                )
                if item.reference_id == reference_id and item.status != "disabled"
            ),
            None,
        )
    except LocalSecretStoreError as exc:
        raise _store_api_error(exc) from None
    if reference is None:
        raise _api_error("local_secret.not_found", 404)
    return reference.provider_connection_id


def _revision(request: Request, *, error_code: str = "local_secret.revision_invalid") -> int:
    raw = _header(request, "if-match")
    if re.fullmatch(r"[1-9][0-9]{0,9}", raw) is None:
        raise _api_error(error_code, 422)
    value = int(raw)
    if value > 2_147_483_647:
        raise _api_error(error_code, 422)
    return value


def _header(request: Request, name: str, *, pattern: re.Pattern[str] | None = None) -> str:
    values = _raw_headers(request, name.encode("ascii"))
    if len(values) != 1:
        raise _api_error("local_admin.request_invalid", 400)
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise _api_error("local_admin.request_invalid", 400) from None
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _api_error("local_secret.payload_invalid", 422)
    return value


def _raw_headers(request: Request, name: bytes) -> tuple[bytes, ...]:
    lowered = name.lower()
    return tuple(value for key, value in request.scope["headers"] if key.lower() == lowered)


def _store(request: Request) -> LocalSecretStore:
    value = getattr(request.app.state, "local_secret_store", None)
    if type(value) is not LocalSecretStore:
        raise _api_error("local_secret.unavailable", 503)
    return value


def _support_bundle_store(request: Request) -> LocalSupportBundleStore:
    value = getattr(request.app.state, "local_support_bundle_store", None)
    if type(value) is not LocalSupportBundleStore:
        raise _api_error("local_support_bundle.unavailable", 503)
    return value


def _backup_store(request: Request) -> LocalBackupStore:
    value = getattr(request.app.state, "local_backup_store", None)
    if type(value) is not LocalBackupStore:
        raise _api_error("local_backup.unavailable", 503)
    return value


def _signer(request: Request) -> LocalAdminSessionSigner:
    value = getattr(request.app.state, "local_admin_session_signer", None)
    if type(value) is not LocalAdminSessionSigner:
        raise _api_error("local_secret.unavailable", 503)
    return value


def _handoff_verifier(request: Request) -> LocalAdminHandoffVerifier:
    value = getattr(request.app.state, "local_admin_handoff_verifier", None)
    if type(value) is not LocalAdminHandoffVerifier:
        raise _api_error("local_secret.unavailable", 503)
    return value


def _policy(request: Request) -> LocalManagementPolicy:
    value = getattr(request.app.state, "local_management_policy", None)
    if type(value) is not LocalManagementPolicy:
        raise _api_error("local_secret.unavailable", 503)
    return value


def _json(body: object, status_code: int) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers=_SAFE_HEADERS)


class _LocalApiError(Exception):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _api_error(code: str, status_code: int) -> _LocalApiError:
    return _LocalApiError(code, status_code)


def _store_api_error(error: LocalSecretStoreError) -> _LocalApiError:
    status = {
        "local_secret.not_found": 404,
        "local_secret.conflict": 409,
        "local_secret.revision_conflict": 409,
        "local_secret.rotation_conflict": 409,
        "local_secret.rotation_in_progress": 409,
        "local_secret.in_use": 409,
        "local_secret.payload_invalid": 422,
    }.get(error.code, 503)
    return _api_error(error.code, status)


def _support_bundle_api_error(error: LocalSupportBundleError) -> _LocalApiError:
    status = {
        "local_support_bundle.not_found": 404,
        "local_support_bundle.expired": 410,
        "local_support_bundle.state_conflict": 409,
        "local_support_bundle.revision_conflict": 409,
        "local_support_bundle.revision_invalid": 422,
        "local_support_bundle.request_invalid": 422,
        "local_support_bundle.sensitive_detected": 422,
        "local_support_bundle.size_exceeded": 422,
    }.get(error.code, 503)
    return _api_error(error.code, status)


def _backup_api_error(error: LocalBackupError) -> _LocalApiError:
    status = {
        "local_backup.not_found": 404,
        "local_restore.not_found": 404,
        "local_restore.backup_unavailable": 409,
        "local_backup.request_invalid": 422,
        "local_restore.request_invalid": 422,
        "local_restore.owner_mismatch": 403,
        "local_restore.schema_incompatible": 409,
        "local_restore.expired": 410,
        "local_restore.state_conflict": 409,
        "local_backup.storage_conflict": 409,
    }.get(error.code, 503)
    return _api_error(error.code, status)


def _zero(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)


def install_local_api_error_handler(app: object) -> None:
    async def handler(_request: Request, error: _LocalApiError) -> JSONResponse:
        return _json({"code": error.code}, error.status_code)

    app.add_exception_handler(_LocalApiError, handler)  # type: ignore[attr-defined]


__all__ = ("install_local_api_error_handler", "router")
