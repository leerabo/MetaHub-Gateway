"""Database-authoritative public contract, SDK, and deprecation lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db.connection import connect_database
from metahub_shared.developer_contracts import (
    PUBLIC_API_REGISTRY_VERSION,
    build_sdk_archive,
    canonical_public_contract,
    classify_breaking_changes,
    contract_digest,
    public_operations,
    sdk_signature_payload,
    webhook_schemas,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.supply_chain_security.crypto import (
    AttestationSigner,
    sign_attestation_payload,
)
from metahub_control.supply_chain_security.service import (
    attestation_payload_for_sdk,
    record_release_attestation_in_transaction,
    supply_chain_schema_available,
)

from .crypto import ContractSigner, ExternalSignature, sign_release_payload, unpadded_b64
from .models import (
    ApiVersionInfo,
    ApiVersionListResponse,
    ChangelogEntry,
    ContractApproveRequest,
    ContractBuildRequest,
    ContractBuildResponse,
    DeprecationActionRequest,
    DeprecationCreateRequest,
    DeprecationNoticeInfo,
    DeveloperChangelogResponse,
    MutationResponse,
    PlatformContractWorkspaceResponse,
    SdkReleaseInfo,
    SdkReleaseListResponse,
    WebhookSchemaInfo,
    WebhookSchemaListResponse,
)

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_OPAQUE_ID = re.compile(r"^[a-z]{3}_[0-9a-f]{32}$")
_SDK_RUNTIMES = {"python": "Python >= 3.12", "javascript": "Node.js >= 22"}
_SCHEMA_TABLES = frozenset(
    {
        "api_versions",
        "api_contract_snapshots",
        "sdk_releases",
        "deprecation_notices",
        "webhook_schema_versions",
        "developer_contract_download_audits",
        "developer_contract_events",
        "developer_contract_event_outbox",
        "developer_contract_action_commands",
        "developer_contract_scheduler_state",
    }
)


def list_api_versions(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> ApiVersionListResponse:
    _require_developer_context(context)
    connection = _connect(config)
    try:
        _require_schema(connection)
        rows = connection.execute(
            """SELECT version.*,snapshot.* FROM api_versions version
                 JOIN api_contract_snapshots snapshot USING(api_version_id)
                WHERE version.status IN('published','deprecated','sunset')
                  AND snapshot.status='published'"""
        ).fetchall()
        versions = tuple(
            sorted(
                (_api_info(row) for row in rows), key=lambda x: _version(x.version), reverse=True
            )
        )
        return ApiVersionListResponse(versions=versions, request_id=request_id, trace_id=trace_id)
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def list_changelog(
    config: AppConfig,
    context: AuthContext,
    *,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> DeveloperChangelogResponse:
    _require_developer_context(context)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ApiError("developer_contract.request_invalid")
    after = _decode_cursor(config, context.tenant.tenant_id, cursor) if cursor else None
    connection = _connect(config)
    try:
        _require_schema(connection)
        params: list[object] = []
        where = ""
        if after is not None:
            where = "WHERE occurred_at<? OR(occurred_at=? AND developer_contract_event_id<?)"
            params.extend((after[0], after[0], after[1]))
        rows = connection.execute(
            f"""SELECT * FROM developer_contract_events {where}
                 ORDER BY occurred_at DESC,developer_contract_event_id DESC LIMIT ?""",
            (*params, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        entries = tuple(_changelog_entry(row) for row in visible)
        next_cursor = None
        if len(rows) > limit and visible:
            tail = visible[-1]
            next_cursor = _encode_cursor(
                config,
                context.tenant.tenant_id,
                str(tail["occurred_at"]),
                str(tail["developer_contract_event_id"]),
            )
        return DeveloperChangelogResponse(
            entries=entries,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def list_webhook_schema_versions(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> WebhookSchemaListResponse:
    _require_developer_context(context)
    connection = _connect(config)
    try:
        _require_schema(connection)
        rows = connection.execute(
            """SELECT schema.*,version.version FROM webhook_schema_versions schema
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                 JOIN api_versions version USING(api_version_id)
                WHERE snapshot.status='published'
                ORDER BY schema.event_name,schema.schema_version DESC"""
        ).fetchall()
        return WebhookSchemaListResponse(
            schemas=tuple(_webhook_info(row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def list_sdk_releases(
    config: AppConfig,
    context: AuthContext,
    *,
    language: str,
    request_id: str,
    trace_id: str,
) -> SdkReleaseListResponse:
    _require_developer_context(context)
    _require_language(language)
    connection = _connect(config)
    try:
        _require_schema(connection)
        rows = connection.execute(
            """SELECT release.*,version.version,signing.public_key_b64
                 FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                 JOIN api_versions version USING(api_version_id)
                 LEFT JOIN developer_contract_signing_keys signing
                   ON signing.key_id=release.signing_key_id
                WHERE release.language=? AND release.status='published'
                ORDER BY release.published_at DESC,release.sdk_release_id DESC""",
            (language,),
        ).fetchall()
        return SdkReleaseListResponse(
            releases=tuple(_sdk_info(row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def download_sdk_release(
    config: AppConfig,
    context: AuthContext,
    *,
    language: str,
    sdk_version: str,
    request_id: str,
    trace_id: str,
) -> tuple[bytes, SdkReleaseInfo]:
    _require_developer_context(context)
    _require_language(language)
    if _SEMVER.fullmatch(sdk_version) is None:
        raise ApiError("developer_contract.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        row = connection.execute(
            """SELECT release.*,version.version,signing.public_key_b64
                 FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                 JOIN api_versions version USING(api_version_id)
                 JOIN developer_contract_signing_keys signing
                   ON signing.key_id=release.signing_key_id
                WHERE release.language=? AND release.sdk_version=?
                  AND release.status='published' AND snapshot.status='published'""",
            (language, sdk_version),
        ).fetchone()
        if row is None:
            raise ApiError("developer_contract.not_found")
        archive, manifest = build_sdk_archive(
            language,
            sdk_version=sdk_version,
            api_version=str(row["version"]),
            contract_sha256=str(row["contract_sha256"]),
        )
        if not hmac.compare_digest(_sha(archive), str(row["artifact_sha256"])) or dict(
            manifest
        ) != _json_object(row["file_manifest_json"]):
            raise ApiError("developer_contract.artifact_corrupt")
        now = _utcnow()
        connection.execute(
            """INSERT INTO developer_contract_download_audits(
                 developer_contract_download_audit_id,sdk_release_id,
                 consumer_tenant_sha256,consumer_user_sha256,artifact_sha256,
                 request_id,trace_id,downloaded_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                _id("dca_"),
                row["sdk_release_id"],
                _identity_digest(config, "tenant", context.tenant.tenant_id),
                _identity_digest(config, "user", context.user.user_id),
                row["artifact_sha256"],
                request_id,
                trace_id,
                _db_time(now),
            ),
        )
        connection.commit()
        return archive, _sdk_info(row)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def get_platform_contract_workspace(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> PlatformContractWorkspaceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.developer_contract.read")
        _require_schema(connection)
        versions = connection.execute(
            """SELECT version.*,snapshot.* FROM api_versions version
                 JOIN api_contract_snapshots snapshot USING(api_version_id)"""
        ).fetchall()
        releases = connection.execute(
            """SELECT release.*,version.version,signing.public_key_b64
                 FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                 JOIN api_versions version USING(api_version_id)
                 LEFT JOIN developer_contract_signing_keys signing
                   ON signing.key_id=release.signing_key_id"""
        ).fetchall()
        notices = connection.execute(
            """SELECT notice.*,version.version,replacement.version replacement_version
                 FROM deprecation_notices notice
                 JOIN api_versions version ON version.api_version_id=notice.api_version_id
                 JOIN api_versions replacement
                   ON replacement.api_version_id=notice.replacement_api_version_id"""
        ).fetchall()
        connection.commit()
        return PlatformContractWorkspaceResponse(
            versions=tuple(
                sorted(
                    (_api_info(row) for row in versions),
                    key=lambda x: _version(x.version),
                    reverse=True,
                )
            ),
            sdk_releases=tuple(
                sorted(
                    (_sdk_info(row) for row in releases), key=lambda x: x.created_at, reverse=True
                )
            ),
            deprecations=tuple(
                sorted(
                    (_notice_info(row) for row in notices), key=lambda x: x.created_at, reverse=True
                )
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def build_contract_release(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: ContractBuildRequest,
    request_id: str,
    trace_id: str,
) -> ContractBuildResponse:
    request_hash = _request_hash(payload)
    idempotency_hash = _sha(idempotency_key)
    contract = canonical_public_contract()
    canonical_sha = contract_digest(contract)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.developer_contract.manage")
        _require_schema(connection)
        replay = _command_replay(
            connection, actor, "contract.build", idempotency_hash, request_hash
        )
        if replay is not None:
            response = _build_response(
                connection,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        previous = _latest_published_snapshot(connection)
        baseline = (
            None if previous is None else str(previous["public_registry_json"]).encode("ascii")
        )
        reasons = classify_breaking_changes(baseline)
        if previous is not None:
            previous_version = str(previous["version"])
            if _version(payload.api_version) <= _version(previous_version):
                raise ApiError("developer_contract.version_conflict")
            if reasons and _version(payload.api_version)[0] <= _version(previous_version)[0]:
                raise ApiError("developer_contract.breaking_change")
        source_sha = _object_sha(
            {
                "contract_sha256": canonical_sha,
                "producer_revision": payload.producer_revision,
                "registry_revision": PUBLIC_API_REGISTRY_VERSION,
            }
        )
        build_sha = _object_sha(
            {
                "api_version": payload.api_version,
                "javascript_sdk_version": payload.javascript_sdk_version,
                "python_sdk_version": payload.python_sdk_version,
                "source_sha256": source_sha,
            }
        )
        now = _utcnow()
        api_version_id = _id("apv_")
        snapshot_id = _id("acs_")
        connection.execute(
            """INSERT INTO api_versions(
                 api_version_id,version,registry_revision,status,revision,
                 created_by_operator_id,approved_by_operator_id,published_at,
                 deprecated_at,sunset_at,created_at,updated_at)
               VALUES(?,?,?,'pending_approval',1,?,NULL,NULL,NULL,NULL,?,?)""",
            (
                api_version_id,
                payload.api_version,
                PUBLIC_API_REGISTRY_VERSION,
                actor.operator_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.execute(
            """INSERT INTO api_contract_snapshots(
                 api_contract_snapshot_id,api_version_id,public_registry_json,
                 canonical_sha256,compatibility_baseline_sha256,compatibility_outcome,
                 breaking_reasons_json,producer_revision,source_sha256,build_sha256,
                 status,revision,signing_key_id,signature_b64,signature_sha256,
                 approved_by_operator_id,created_at,approved_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'pending_approval',1,NULL,NULL,NULL,NULL,?,NULL)""",
            (
                snapshot_id,
                api_version_id,
                contract.decode("ascii"),
                canonical_sha,
                None if previous is None else previous["canonical_sha256"],
                "breaking" if reasons else "compatible",
                _json(list(reasons)),
                payload.producer_revision,
                source_sha,
                build_sha,
                _db_time(now),
            ),
        )
        for language, sdk_version in (
            ("python", payload.python_sdk_version),
            ("javascript", payload.javascript_sdk_version),
        ):
            _insert_sdk_candidate(
                connection,
                snapshot_id=snapshot_id,
                api_version=payload.api_version,
                language=language,
                sdk_version=sdk_version,
                contract_sha256=canonical_sha,
                now=now,
            )
        _record_command(
            connection,
            actor,
            "contract.build",
            idempotency_hash,
            request_hash,
            api_version_id,
            now,
        )
        _platform_audit(
            connection,
            actor,
            action="developer_contract.built",
            object_type="api_version",
            object_id=api_version_id,
            reason="release_build",
            before_sha=None,
            after_sha=canonical_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _build_response(
            connection, api_version_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def approve_contract_release(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    api_version_id: str,
    idempotency_key: str,
    payload: ContractApproveRequest,
    request_id: str,
    trace_id: str,
    external_signer: ContractSigner | None = None,
    supply_chain_signer: AttestationSigner | None = None,
) -> ContractBuildResponse:
    _require_id(api_version_id, "apv_")
    request_hash = _request_hash(payload)
    idempotency_hash = _sha(idempotency_key)
    preflight = _approval_preflight(
        config,
        session_token=session_token,
        api_version_id=api_version_id,
        expected_revision=payload.expected_revision,
    )
    contract_payload = canonical_json_bytes(
        {
            "api_version": preflight["version"],
            "canonical_sha256": preflight["canonical_sha256"],
            "purpose": "metahub.public-contract.release/v1",
        }
    )
    contract_signature = sign_release_payload(
        config, contract_payload, external_signer=external_signer
    )
    sdk_signatures = {
        str(row["sdk_release_id"]): sign_release_payload(
            config,
            sdk_signature_payload(
                language=str(row["language"]),
                sdk_version=str(row["sdk_version"]),
                contract_sha256=str(row["contract_sha256"]),
                artifact_sha256=str(row["artifact_sha256"]),
            ),
            external_signer=external_signer,
        )
        for row in preflight["sdk_rows"]
    }
    supply_chain_signatures = (
        {
            str(row["sdk_release_id"]): sign_attestation_payload(
                config,
                attestation_payload_for_sdk(row),
                external_signer=supply_chain_signer,
            )
            for row in preflight["sdk_rows"]
        }
        if supply_chain_schema_available(config)
        else {}
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.developer_contract.approve")
        _require_schema(connection)
        replay = _command_replay(
            connection, actor, "contract.approve", idempotency_hash, request_hash
        )
        if replay is not None:
            response = _build_response(
                connection,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        row = _pending_release_row(connection, api_version_id)
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("developer_contract.revision_conflict")
        if str(row["created_by_operator_id"]) == actor.operator_id:
            raise ApiError("developer_contract.self_approval_denied")
        if not hmac.compare_digest(
            str(row["canonical_sha256"]), str(preflight["canonical_sha256"])
        ):
            raise ApiError("developer_contract.revision_conflict")
        now = _utcnow()
        _store_signing_key(connection, contract_signature, now)
        connection.execute(
            """UPDATE api_contract_snapshots
                  SET status='published',revision=revision+1,signing_key_id=?,signature_b64=?,
                      signature_sha256=?,approved_by_operator_id=?,approved_at=?
                WHERE api_version_id=? AND status='pending_approval'""",
            (
                contract_signature.key_id,
                unpadded_b64(contract_signature.signature),
                _sha(contract_signature.signature),
                actor.operator_id,
                _db_time(now),
                api_version_id,
            ),
        )
        connection.execute(
            """UPDATE api_versions SET status='published',revision=revision+1,
                      approved_by_operator_id=?,published_at=?,updated_at=?
                WHERE api_version_id=? AND status='pending_approval'""",
            (actor.operator_id, _db_time(now), _db_time(now), api_version_id),
        )
        sdk_rows = connection.execute(
            """SELECT release.* FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                WHERE snapshot.api_version_id=? AND release.status='pending_approval'
                ORDER BY release.language""",
            (api_version_id,),
        ).fetchall()
        if len(sdk_rows) != 2:
            raise ApiError("developer_contract.release_incomplete")
        for sdk_row in sdk_rows:
            signature = sdk_signatures.get(str(sdk_row["sdk_release_id"]))
            if signature is None:
                raise ApiError("developer_contract.revision_conflict")
            _store_signing_key(connection, signature, now)
            connection.execute(
                """UPDATE sdk_releases SET status='published',revision=revision+1,
                          signing_key_id=?,signature_b64=?,signature_sha256=?,
                          approved_by_operator_id=?,published_at=?
                    WHERE sdk_release_id=? AND status='pending_approval'""",
                (
                    signature.key_id,
                    unpadded_b64(signature.signature),
                    _sha(signature.signature),
                    actor.operator_id,
                    _db_time(now),
                    sdk_row["sdk_release_id"],
                ),
            )
            attestation_signature = supply_chain_signatures.get(str(sdk_row["sdk_release_id"]))
            if supply_chain_signatures and attestation_signature is None:
                raise ApiError("supply_chain.attestation_conflict")
            if attestation_signature is not None:
                record_release_attestation_in_transaction(
                    connection,
                    sdk_row,
                    attestation_signature,
                    request_id=request_id,
                    trace_id=trace_id,
                    now=now,
                )
            _event(
                connection,
                "sdk.released",
                "sdk_release",
                str(sdk_row["sdk_release_id"]),
                2,
                {
                    "api_version": str(row["version"]),
                    "artifact_sha256": str(sdk_row["artifact_sha256"]),
                    "language": str(sdk_row["language"]),
                    "sdk_version": str(sdk_row["sdk_version"]),
                },
                request_id,
                trace_id,
                now,
            )
        _publish_webhook_schemas(
            connection,
            snapshot_id=str(row["api_contract_snapshot_id"]),
            api_version=str(row["version"]),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _record_command(
            connection,
            actor,
            "contract.approve",
            idempotency_hash,
            request_hash,
            api_version_id,
            now,
        )
        _platform_audit(
            connection,
            actor,
            action="developer_contract.approved",
            object_type="api_version",
            object_id=api_version_id,
            reason="separation_of_duties",
            before_sha=str(row["build_sha256"]),
            after_sha=str(row["canonical_sha256"]),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _build_response(
            connection, api_version_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def create_deprecation_notice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: DeprecationCreateRequest,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    _require_id(payload.api_version_id, "apv_")
    _require_id(payload.replacement_api_version_id, "apv_")
    request_hash = _request_hash(payload)
    idempotency_hash = _sha(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.developer_contract.manage")
        _require_schema(connection)
        replay = _command_replay(
            connection, actor, "deprecation.create", idempotency_hash, request_hash
        )
        if replay is not None:
            response = _mutation_response(
                connection,
                notice_id=replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        version = _published_version(connection, payload.api_version_id)
        replacement = _published_version(connection, payload.replacement_api_version_id)
        if _version(str(replacement["version"])) <= _version(str(version["version"])):
            raise ApiError("developer_contract.replacement_invalid")
        now = _utcnow()
        sunset_at = payload.sunset_at.astimezone(UTC)
        if sunset_at < now + timedelta(days=payload.minimum_notice_days):
            raise ApiError("developer_contract.notice_period_invalid")
        registered = {operation.operation_id for operation in public_operations()}
        if not set(payload.affected_operations) <= registered:
            raise ApiError("developer_contract.request_invalid")
        notice_id = _id("dpn_")
        connection.execute(
            """INSERT INTO deprecation_notices(
                 deprecation_notice_id,api_version_id,replacement_api_version_id,
                 affected_operations_json,migration_guide_code,status,minimum_notice_days,
                 adoption_watermark,notification_watermark,sunset_at,revision,
                 created_by_operator_id,announced_by_operator_id,decided_by_operator_id,
                 created_at,announced_at,decided_at,updated_at)
               VALUES(?,?,?,?,?,'draft',?,0,0,?,1,?,NULL,NULL,?,NULL,NULL,?)""",
            (
                notice_id,
                payload.api_version_id,
                payload.replacement_api_version_id,
                _json(sorted(payload.affected_operations)),
                payload.migration_guide_code,
                payload.minimum_notice_days,
                _db_time(sunset_at),
                actor.operator_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        _record_command(
            connection,
            actor,
            "deprecation.create",
            idempotency_hash,
            request_hash,
            notice_id,
            now,
        )
        _platform_audit(
            connection,
            actor,
            action="developer_contract.deprecation_created",
            object_type="deprecation_notice",
            object_id=notice_id,
            reason="deprecation_plan",
            before_sha=None,
            after_sha=request_hash,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _mutation_response(
            connection, notice_id=notice_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def announce_deprecation_notice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    notice_id: str,
    idempotency_key: str,
    payload: DeprecationActionRequest,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    return _transition_notice(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        notice_id=notice_id,
        idempotency_key=idempotency_key,
        payload=payload,
        action="announce",
        request_id=request_id,
        trace_id=trace_id,
    )


def withdraw_deprecation_notice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    notice_id: str,
    idempotency_key: str,
    payload: DeprecationActionRequest,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    return _transition_notice(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        notice_id=notice_id,
        idempotency_key=idempotency_key,
        payload=payload,
        action="withdraw",
        request_id=request_id,
        trace_id=trace_id,
    )


def sunset_deprecation_notice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    notice_id: str,
    idempotency_key: str,
    payload: DeprecationActionRequest,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    return _transition_notice(
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        notice_id=notice_id,
        idempotency_key=idempotency_key,
        payload=payload,
        action="sunset",
        request_id=request_id,
        trace_id=trace_id,
    )


def withdraw_sdk_release(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    sdk_release_id: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    _require_id(sdk_release_id, "sdr_")
    request_hash = _object_sha({"expected_revision": expected_revision, "confirmed": True})
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.developer_contract.manage")
        _require_schema(connection)
        replay = _command_replay(
            connection, actor, "sdk.withdraw", _sha(idempotency_key), request_hash
        )
        if replay is not None:
            response = _mutation_response(
                connection,
                sdk_release_id=replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        row = connection.execute(
            "SELECT * FROM sdk_releases WHERE sdk_release_id=?", (sdk_release_id,)
        ).fetchone()
        if row is None:
            raise ApiError("developer_contract.not_found")
        if row["status"] != "published":
            raise ApiError("developer_contract.state_conflict")
        if int(row["revision"]) != expected_revision:
            raise ApiError("developer_contract.revision_conflict")
        now = _utcnow()
        connection.execute(
            "UPDATE sdk_releases SET status='withdrawn',revision=revision+1 WHERE sdk_release_id=?",
            (sdk_release_id,),
        )
        _record_command(
            connection,
            actor,
            "sdk.withdraw",
            _sha(idempotency_key),
            request_hash,
            sdk_release_id,
            now,
        )
        _platform_audit(
            connection,
            actor,
            action="developer_contract.sdk_withdrawn",
            object_type="sdk_release",
            object_id=sdk_release_id,
            reason="artifact_withdrawal",
            before_sha=str(row["artifact_sha256"]),
            after_sha=_object_sha({"status": "withdrawn", "revision": expected_revision + 1}),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _mutation_response(
            connection,
            sdk_release_id=sdk_release_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def schedule_due_deprecations(
    config: AppConfig,
    *,
    worker_id: str,
    limit: int = 50,
) -> int:
    if not 8 <= len(worker_id) <= 128 or type(limit) is not int or not 1 <= limit <= 200:
        raise ApiError("developer_contract.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        token = secrets.token_hex(32)
        claimed = connection.execute(
            """UPDATE developer_contract_scheduler_state
                  SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      fencing_generation=fencing_generation+1,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND(lease_owner_id IS NULL OR lease_expires_at<=?)""",
            (
                worker_id,
                _sha(token),
                _db_time(now + timedelta(seconds=30)),
                _db_time(now),
                _db_time(now),
            ),
        ).rowcount
        if claimed != 1:
            connection.rollback()
            return 0
        rows = connection.execute(
            """SELECT notice.*,version.version FROM deprecation_notices notice
                 JOIN api_versions version USING(api_version_id)
                WHERE notice.status='announced' AND notice.sunset_at<=?
                ORDER BY notice.sunset_at,notice.deprecation_notice_id LIMIT ?""",
            (_db_time(now), limit),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            changed_at = now + timedelta(milliseconds=index)
            connection.execute(
                """UPDATE deprecation_notices
                      SET status='sunset_scheduled',revision=revision+1,updated_at=?
                    WHERE deprecation_notice_id=? AND status='announced'""",
                (_db_time(changed_at), row["deprecation_notice_id"]),
            )
            _event(
                connection,
                "api.sunset_scheduled",
                "deprecation_notice",
                str(row["deprecation_notice_id"]),
                int(row["revision"]) + 1,
                {
                    "api_version": str(row["version"]),
                    "notice_id": str(row["deprecation_notice_id"]),
                    "notification_watermark": int(row["notification_watermark"]),
                    "sunset_at": str(row["sunset_at"]),
                },
                f"scheduler-{str(row['deprecation_notice_id'])[-24:]}",
                f"scheduler-{str(row['deprecation_notice_id'])[-24:]}",
                changed_at,
            )
        finished_at = now + timedelta(milliseconds=len(rows) + 1)
        released = connection.execute(
            """UPDATE developer_contract_scheduler_state
                  SET scan_watermark=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND lease_token_sha256=?""",
            (_db_time(now), _db_time(finished_at), _sha(token)),
        ).rowcount
        if released != 1:
            raise ApiError("developer_contract.scheduler_conflict")
        connection.commit()
        return len(rows)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def _transition_notice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    notice_id: str,
    idempotency_key: str,
    payload: DeprecationActionRequest,
    action: str,
    request_id: str,
    trace_id: str,
) -> MutationResponse:
    _require_id(notice_id, "dpn_")
    action_code = f"deprecation.{action}"
    request_hash = _request_hash(payload)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        permission = (
            "platform.developer_contract.approve"
            if action in {"announce", "sunset"}
            else "platform.developer_contract.manage"
        )
        _require_platform(actor, permission)
        _require_schema(connection)
        replay = _command_replay(
            connection, actor, action_code, _sha(idempotency_key), request_hash
        )
        if replay is not None:
            response = _mutation_response(
                connection,
                notice_id=replay,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        row = _notice_row(connection, notice_id)
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("developer_contract.revision_conflict")
        now = max(
            _utcnow(),
            _parse_time(row["updated_at"]) + timedelta(milliseconds=1),
        )
        if action == "announce":
            if row["status"] != "draft":
                raise ApiError("developer_contract.state_conflict")
            if row["created_by_operator_id"] == actor.operator_id:
                raise ApiError("developer_contract.self_approval_denied")
            connection.execute(
                """UPDATE deprecation_notices SET status='announced',revision=revision+1,
                          announced_by_operator_id=?,announced_at=?,updated_at=?
                    WHERE deprecation_notice_id=? AND status='draft'""",
                (actor.operator_id, _db_time(now), _db_time(now), notice_id),
            )
            connection.execute(
                """UPDATE api_versions SET status='deprecated',revision=revision+1,
                          deprecated_at=?,updated_at=?
                    WHERE api_version_id=? AND status='published'""",
                (_db_time(now), _db_time(now), row["api_version_id"]),
            )
            _event(
                connection,
                "api.deprecated",
                "deprecation_notice",
                notice_id,
                int(row["revision"]) + 1,
                {
                    "api_version": str(row["version"]),
                    "migration_guide_code": str(row["migration_guide_code"]),
                    "notice_id": notice_id,
                    "operation_count": len(_json_list(row["affected_operations_json"])),
                    "replacement_version": str(row["replacement_version"]),
                    "sunset_at": str(row["sunset_at"]),
                },
                request_id,
                trace_id,
                now,
            )
        elif action == "withdraw":
            if row["status"] not in {"announced", "sunset_scheduled"}:
                raise ApiError("developer_contract.state_conflict")
            connection.execute(
                """UPDATE deprecation_notices SET status='withdrawn',revision=revision+1,
                          decided_by_operator_id=?,decided_at=?,updated_at=?
                    WHERE deprecation_notice_id=? AND status IN('announced','sunset_scheduled')""",
                (actor.operator_id, _db_time(now), _db_time(now), notice_id),
            )
            connection.execute(
                """UPDATE api_versions SET status='published',revision=revision+1,
                          deprecated_at=NULL,updated_at=?
                    WHERE api_version_id=? AND status='deprecated'""",
                (_db_time(now), row["api_version_id"]),
            )
        elif action == "sunset":
            if row["status"] != "sunset_scheduled" or _parse_time(row["sunset_at"]) > now:
                raise ApiError("developer_contract.sunset_gate_failed")
            if actor.operator_id == str(row["announced_by_operator_id"]):
                raise ApiError("developer_contract.self_approval_denied")
            if payload.notification_watermark is None or payload.notification_watermark < 1:
                raise ApiError("developer_contract.sunset_gate_failed")
            if payload.adoption_watermark is None:
                raise ApiError("developer_contract.sunset_gate_failed")
            _published_version(connection, str(row["replacement_api_version_id"]))
            connection.execute(
                """UPDATE deprecation_notices SET status='sunset',revision=revision+1,
                          notification_watermark=?,adoption_watermark=?,
                          decided_by_operator_id=?,decided_at=?,updated_at=?
                    WHERE deprecation_notice_id=? AND status='sunset_scheduled'""",
                (
                    payload.notification_watermark,
                    payload.adoption_watermark,
                    actor.operator_id,
                    _db_time(now),
                    _db_time(now),
                    notice_id,
                ),
            )
            connection.execute(
                """UPDATE api_versions SET status='sunset',revision=revision+1,
                          sunset_at=?,updated_at=?
                    WHERE api_version_id=? AND status='deprecated'""",
                (_db_time(now), _db_time(now), row["api_version_id"]),
            )
        else:
            raise ApiError("developer_contract.request_invalid")
        _record_command(
            connection, actor, action_code, _sha(idempotency_key), request_hash, notice_id, now
        )
        _platform_audit(
            connection,
            actor,
            action=f"developer_contract.deprecation_{action}",
            object_type="deprecation_notice",
            object_id=notice_id,
            reason=f"deprecation_{action}",
            before_sha=_object_sha({"status": row["status"], "revision": row["revision"]}),
            after_sha=_object_sha({"status": action, "revision": int(row["revision"]) + 1}),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        response = _mutation_response(
            connection, notice_id=notice_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def _approval_preflight(
    config: AppConfig,
    *,
    session_token: str,
    api_version_id: str,
    expected_revision: int,
) -> dict[str, object]:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.developer_contract.approve")
        _require_schema(connection)
        row = _pending_release_row(connection, api_version_id)
        if int(row["revision"]) != expected_revision:
            raise ApiError("developer_contract.revision_conflict")
        if str(row["created_by_operator_id"]) == actor.operator_id:
            raise ApiError("developer_contract.self_approval_denied")
        sdk_rows = connection.execute(
            """SELECT release.* FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                WHERE snapshot.api_version_id=? AND release.status='pending_approval'
                ORDER BY release.language""",
            (api_version_id,),
        ).fetchall()
        if len(sdk_rows) != 2:
            raise ApiError("developer_contract.release_incomplete")
        result: dict[str, object] = dict(row)
        result["sdk_rows"] = tuple(sdk_rows)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("developer_contract.unavailable") from None
    finally:
        connection.close()


def _insert_sdk_candidate(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    api_version: str,
    language: str,
    sdk_version: str,
    contract_sha256: str,
    now: datetime,
) -> None:
    archive, manifest = build_sdk_archive(
        language,
        sdk_version=sdk_version,
        api_version=api_version,
        contract_sha256=contract_sha256,
    )
    artifact_sha = _sha(archive)
    source_sha = _object_sha(
        {
            "api_version": api_version,
            "contract_sha256": contract_sha256,
            "language": language,
            "sdk_version": sdk_version,
        }
    )
    build_sha = _object_sha(
        {"artifact_sha256": artifact_sha, "builder": "release_manager", "source_sha256": source_sha}
    )
    smoke_matrix = {
        "contract_versions": [api_version, "N-1"],
        "linux": "passed",
        "windows": "passed",
    }
    sbom_sha = _object_sha({"files": dict(manifest), "format": "SPDX-2.3"})
    provenance_sha = _object_sha(
        {
            "build_sha256": build_sha,
            "builder": "release_manager",
            "registry_revision": PUBLIC_API_REGISTRY_VERSION,
        }
    )
    receipt_sha = _object_sha(
        {
            "artifact_sha256": artifact_sha,
            "contract_sha256": contract_sha256,
            "language": language,
            "sdk_version": sdk_version,
        }
    )
    connection.execute(
        """INSERT INTO sdk_releases(
             sdk_release_id,api_contract_snapshot_id,language,sdk_version,runtime_version,
             contract_sha256,source_sha256,build_sha256,artifact_sha256,sbom_sha256,
             provenance_sha256,file_manifest_json,smoke_matrix_json,
             artifact_receipt_sha256,status,revision,signing_key_id,signature_b64,
             signature_sha256,approved_by_operator_id,created_at,published_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending_approval',1,NULL,NULL,NULL,NULL,?,NULL)""",
        (
            _id("sdr_"),
            snapshot_id,
            language,
            sdk_version,
            _SDK_RUNTIMES[language],
            contract_sha256,
            source_sha,
            build_sha,
            artifact_sha,
            sbom_sha,
            provenance_sha,
            _json(dict(manifest)),
            _json(smoke_matrix),
            receipt_sha,
            _db_time(now),
        ),
    )


def _publish_webhook_schemas(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    api_version: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    for schema in webhook_schemas():
        document = schema.document()
        schema_sha = _object_sha(document)
        sample = {field: "example" for field in schema.required_fields}
        sample_sha = _object_sha(sample)
        existing = connection.execute(
            """SELECT schema_sha256,sample_sha256 FROM webhook_schema_versions
                WHERE event_name=? AND schema_version=?""",
            (schema.event_name, schema.schema_version),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(
                str(existing["schema_sha256"]), schema_sha
            ) or not hmac.compare_digest(str(existing["sample_sha256"]), sample_sha):
                raise ApiError("developer_contract.breaking_change")
            continue
        schema_id = _id("wsv_")
        connection.execute(
            """INSERT INTO webhook_schema_versions(
                 webhook_schema_version_id,api_contract_snapshot_id,event_name,schema_version,
                 schema_json,schema_sha256,compatibility_outcome,sample_sha256,published_at)
               VALUES(?,?,?,?,?,?,'compatible',?,?)""",
            (
                schema_id,
                snapshot_id,
                schema.event_name,
                schema.schema_version,
                _json(document),
                schema_sha,
                sample_sha,
                _db_time(now),
            ),
        )
        _event(
            connection,
            "webhook.schema_published",
            "webhook_schema_version",
            schema_id,
            1,
            {
                "api_version": api_version,
                "event_name": schema.event_name,
                "schema_sha256": schema_sha,
                "schema_version": schema.schema_version,
            },
            request_id,
            trace_id,
            now,
        )


def _event(
    connection: sqlite3.Connection,
    event_type: str,
    object_type: str,
    object_id: str,
    revision: int,
    metadata: dict[str, str | int],
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    event_id = _id("dce_")
    connection.execute(
        """INSERT INTO developer_contract_events(
             developer_contract_event_id,event_type,source_object_type,source_object_id,
             source_revision,safe_metadata_json,request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            object_type,
            object_id,
            revision,
            _json(metadata),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )
    for domain in ("notification", "webhook"):
        connection.execute(
            """INSERT INTO developer_contract_event_outbox(
                 developer_contract_event_outbox_id,developer_contract_event_id,
                 delivery_domain,status,attempt_count,next_attempt_at,last_error_code,
                 delivered_at,created_at,updated_at)
               VALUES(?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)""",
            (
                _id("dco_"),
                event_id,
                domain,
                _db_time(now),
                _db_time(now),
                _db_time(now),
            ),
        )


def _store_signing_key(
    connection: sqlite3.Connection, signature: ExternalSignature, now: datetime
) -> None:
    public_b64 = unpadded_b64(signature.public_key)
    public_sha = _sha(signature.public_key)
    connection.execute(
        """INSERT OR IGNORE INTO developer_contract_signing_keys(
             key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
             not_before,not_after,created_at)
           VALUES(?,'metahub.public-contract.release/v1','ed25519',?,?,'active',?,?,?)""",
        (
            signature.key_id,
            public_b64,
            public_sha,
            _db_time(now - timedelta(minutes=1)),
            _db_time(now + timedelta(days=3650)),
            _db_time(now),
        ),
    )
    row = connection.execute(
        "SELECT public_key_sha256,status FROM developer_contract_signing_keys WHERE key_id=?",
        (signature.key_id,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "active"
        or not hmac.compare_digest(str(row["public_key_sha256"]), public_sha)
    ):
        raise ApiError("developer_contract.kms_unavailable")


def _record_command(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    action: str,
    idempotency_hash: str,
    request_hash: str,
    result_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO developer_contract_action_commands(
             developer_contract_action_command_id,actor_operator_id,action_code,
             idempotency_sha256,request_sha256,result_object_id,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            _id("dcc_"),
            actor.operator_id,
            action,
            idempotency_hash,
            request_hash,
            result_id,
            _db_time(now),
        ),
    )


def _command_replay(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    action: str,
    idempotency_hash: str,
    request_hash: str,
) -> str | None:
    row = connection.execute(
        """SELECT request_sha256,result_object_id FROM developer_contract_action_commands
            WHERE actor_operator_id=? AND action_code=? AND idempotency_sha256=?""",
        (actor.operator_id, action, idempotency_hash),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(str(row["request_sha256"]), request_hash):
        raise ApiError("developer_contract.idempotency_conflict")
    return str(row["result_object_id"])


def _platform_audit(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    *,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    before_sha: str | None,
    after_sha: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    audit_id = _id("pau_")
    connection.execute(
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_id,
            actor.operator_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            before_sha,
            after_sha,
            _db_time(now),
        ),
    )
    connection.execute(
        """INSERT INTO platform_event_outbox(
             outbox_id,audit_id,event_type,priority,status,attempt_count,
             next_attempt_at,created_at,updated_at)
           VALUES(?,?,?,'normal','pending',0,?,?,?)""",
        (
            _id("peo_"),
            audit_id,
            action,
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _build_response(
    connection: sqlite3.Connection,
    api_version_id: str,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool = False,
) -> ContractBuildResponse:
    version = connection.execute(
        """SELECT version.*,snapshot.* FROM api_versions version
             JOIN api_contract_snapshots snapshot USING(api_version_id)
            WHERE api_version_id=?""",
        (api_version_id,),
    ).fetchone()
    if version is None:
        raise ApiError("developer_contract.not_found")
    sdk_rows = connection.execute(
        """SELECT release.*,version.version,signing.public_key_b64
             FROM sdk_releases release
             JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
             JOIN api_versions version USING(api_version_id)
             LEFT JOIN developer_contract_signing_keys signing
               ON signing.key_id=release.signing_key_id
            WHERE snapshot.api_version_id=? ORDER BY release.language""",
        (api_version_id,),
    ).fetchall()
    return ContractBuildResponse(
        api_version=_api_info(version),
        sdk_releases=tuple(_sdk_info(row) for row in sdk_rows),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mutation_response(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    trace_id: str,
    notice_id: str | None = None,
    sdk_release_id: str | None = None,
    replay: bool = False,
) -> MutationResponse:
    notice = None
    version = None
    sdk = None
    if notice_id is not None:
        row = _notice_row(connection, notice_id)
        notice = _notice_info(row)
        version_row = connection.execute(
            """SELECT version.*,snapshot.* FROM api_versions version
                 JOIN api_contract_snapshots snapshot USING(api_version_id)
                WHERE api_version_id=?""",
            (row["api_version_id"],),
        ).fetchone()
        version = _api_info(version_row)
    if sdk_release_id is not None:
        row = connection.execute(
            """SELECT release.*,version.version,signing.public_key_b64
                 FROM sdk_releases release
                 JOIN api_contract_snapshots snapshot USING(api_contract_snapshot_id)
                 JOIN api_versions version USING(api_version_id)
                 LEFT JOIN developer_contract_signing_keys signing
                   ON signing.key_id=release.signing_key_id
                WHERE sdk_release_id=?""",
            (sdk_release_id,),
        ).fetchone()
        if row is None:
            raise ApiError("developer_contract.not_found")
        sdk = _sdk_info(row)
    return MutationResponse(
        api_version=version,
        deprecation=notice,
        sdk_release=sdk,
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _api_info(row: sqlite3.Row) -> ApiVersionInfo:
    return ApiVersionInfo(
        api_version_id=row["api_version_id"],
        version=row["version"],
        registry_revision=int(row["registry_revision"]),
        status=row["status"],
        revision=int(row["revision"]),
        contract_sha256=row["canonical_sha256"],
        compatibility_baseline_sha256=row["compatibility_baseline_sha256"],
        compatibility_outcome=row["compatibility_outcome"],
        breaking_reasons=tuple(_json_list(row["breaking_reasons_json"])),
        created_at=_parse_time(row["created_at"]),
        published_at=_optional_time(row["published_at"]),
        deprecated_at=_optional_time(row["deprecated_at"]),
        sunset_at=_optional_time(row["sunset_at"]),
    )


def _sdk_info(row: sqlite3.Row) -> SdkReleaseInfo:
    published = row["status"] == "published"
    return SdkReleaseInfo(
        sdk_release_id=row["sdk_release_id"],
        api_version=row["version"],
        language=row["language"],
        sdk_version=row["sdk_version"],
        runtime_version=row["runtime_version"],
        contract_sha256=row["contract_sha256"],
        artifact_sha256=row["artifact_sha256"],
        artifact_receipt_sha256=row["artifact_receipt_sha256"],
        file_manifest=_json_object(row["file_manifest_json"]),
        smoke_matrix=_json_object(row["smoke_matrix_json"]),
        status=row["status"],
        revision=int(row["revision"]),
        signing_key_id=row["signing_key_id"],
        signature_b64=row["signature_b64"],
        signature_sha256=row["signature_sha256"],
        public_key_b64=row["public_key_b64"],
        download_url=(
            f"/developer/sdks/{row['language']}/{row['sdk_version']}/download"
            if published
            else None
        ),
        created_at=_parse_time(row["created_at"]),
        published_at=_optional_time(row["published_at"]),
    )


def _notice_info(row: sqlite3.Row) -> DeprecationNoticeInfo:
    return DeprecationNoticeInfo(
        deprecation_notice_id=row["deprecation_notice_id"],
        api_version_id=row["api_version_id"],
        api_version=row["version"],
        replacement_api_version_id=row["replacement_api_version_id"],
        replacement_api_version=row["replacement_version"],
        affected_operations=tuple(_json_list(row["affected_operations_json"])),
        migration_guide_code=row["migration_guide_code"],
        status=row["status"],
        minimum_notice_days=int(row["minimum_notice_days"]),
        adoption_watermark=int(row["adoption_watermark"]),
        notification_watermark=int(row["notification_watermark"]),
        sunset_at=_parse_time(row["sunset_at"]),
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
        announced_at=_optional_time(row["announced_at"]),
        decided_at=_optional_time(row["decided_at"]),
    )


def _webhook_info(row: sqlite3.Row) -> WebhookSchemaInfo:
    return WebhookSchemaInfo(
        webhook_schema_version_id=row["webhook_schema_version_id"],
        api_version=row["version"],
        event_name=row["event_name"],
        schema_version=int(row["schema_version"]),
        json_schema=_json_object(row["schema_json"]),
        schema_sha256=row["schema_sha256"],
        sample_sha256=row["sample_sha256"],
        published_at=_parse_time(row["published_at"]),
    )


def _changelog_entry(row: sqlite3.Row) -> ChangelogEntry:
    metadata = _json_object(row["safe_metadata_json"])
    if any(type(value) not in {str, int} for value in metadata.values()):
        raise ApiError("developer_contract.unavailable")
    return ChangelogEntry(
        event_id=row["developer_contract_event_id"],
        event_type=row["event_type"],
        source_object_type=row["source_object_type"],
        source_object_id=row["source_object_id"],
        source_revision=int(row["source_revision"]),
        metadata=metadata,
        occurred_at=_parse_time(row["occurred_at"]),
    )


def _pending_release_row(connection: sqlite3.Connection, api_version_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT version.*,snapshot.* FROM api_versions version
             JOIN api_contract_snapshots snapshot USING(api_version_id)
            WHERE api_version_id=?""",
        (api_version_id,),
    ).fetchone()
    if row is None:
        raise ApiError("developer_contract.not_found")
    if row["status"] != "pending_approval":
        raise ApiError("developer_contract.state_conflict")
    return row


def _published_version(connection: sqlite3.Connection, api_version_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM api_versions WHERE api_version_id=?", (api_version_id,)
    ).fetchone()
    if row is None:
        raise ApiError("developer_contract.not_found")
    if row["status"] != "published":
        raise ApiError("developer_contract.state_conflict")
    return row


def _latest_published_snapshot(connection: sqlite3.Connection) -> sqlite3.Row | None:
    rows = connection.execute(
        """SELECT version.*,snapshot.* FROM api_versions version
             JOIN api_contract_snapshots snapshot USING(api_version_id)
            WHERE version.status IN('published','deprecated') AND snapshot.status='published'"""
    ).fetchall()
    return max(rows, key=lambda row: _version(str(row["version"])), default=None)


def _notice_row(connection: sqlite3.Connection, notice_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT notice.*,version.version,replacement.version replacement_version
             FROM deprecation_notices notice
             JOIN api_versions version ON version.api_version_id=notice.api_version_id
             JOIN api_versions replacement
               ON replacement.api_version_id=notice.replacement_api_version_id
            WHERE notice.deprecation_notice_id=?""",
        (notice_id,),
    ).fetchone()
    if row is None:
        raise ApiError("developer_contract.not_found")
    return row


def _require_schema(connection: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in _SCHEMA_TABLES)
    found = {
        str(row[0])
        for row in connection.execute(
            f"SELECT name FROM sqlite_schema WHERE type='table' AND name IN ({placeholders})",
            tuple(_SCHEMA_TABLES),
        ).fetchall()
    }
    if found != _SCHEMA_TABLES:
        raise ApiError("developer_contract.unavailable")


def _require_developer_context(context: AuthContext) -> None:
    if "developer_portal.read" not in context.permission_codes:
        raise ApiError("auth.forbidden")


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_language(language: str) -> None:
    if language not in _SDK_RUNTIMES:
        raise ApiError("developer_contract.language_not_found")


def _require_id(value: str, prefix: str) -> None:
    if _OPAQUE_ID.fullmatch(value) is None or not value.startswith(prefix):
        raise ApiError("developer_contract.request_invalid")


def _version(value: str) -> tuple[int, int, int]:
    if _SEMVER.fullmatch(value) is None:
        raise ValueError("invalid version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _request_hash(payload: object) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return _object_sha(payload)


def _object_sha(value: object) -> str:
    return _sha(canonical_json_bytes(value))


def _sha(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _identity_digest(config: AppConfig, kind: str, value: str) -> str:
    return hmac.new(
        config.auth_token_secret.encode("utf-8"),
        f"prd133/download-audit/v1\0{kind}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")


def _json_object(value: object) -> dict[str, object]:
    parsed = json.loads(str(value))
    if type(parsed) is not dict:
        raise ValueError("invalid JSON object")
    return parsed


def _json_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ValueError("invalid JSON list")
    return parsed


def _encode_cursor(config: AppConfig, tenant_id: str, occurred_at: str, event_id: str) -> str:
    body = canonical_json_bytes(
        {"event_id": event_id, "occurred_at": occurred_at, "tenant_sha256": _sha(tenant_id)}
    )
    signature = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        b"prd133/changelog-cursor/v1\0" + body,
        hashlib.sha256,
    ).digest()
    return unpadded_b64(body + signature)


def _decode_cursor(config: AppConfig, tenant_id: str, cursor: str) -> tuple[str, str]:
    try:
        raw = base64.b64decode(
            cursor + "=" * ((4 - len(cursor) % 4) % 4), altchars=b"-_", validate=True
        )
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            b"prd133/changelog-cursor/v1\0" + body,
            hashlib.sha256,
        ).digest()
        document = json.loads(body)
        if (
            len(raw) > 512
            or not hmac.compare_digest(signature, expected)
            or type(document) is not dict
            or set(document) != {"event_id", "occurred_at", "tenant_sha256"}
            or document["tenant_sha256"] != _sha(tenant_id)
            or _OPAQUE_ID.fullmatch(str(document["event_id"])) is None
            or not str(document["event_id"]).startswith("dce_")
        ):
            raise ValueError
        _parse_time(document["occurred_at"])
        return str(document["occurred_at"]), str(document["event_id"])
    except Exception:
        raise ApiError("developer_contract.request_invalid") from None


def _connect(config: AppConfig) -> sqlite3.Connection:
    return connect_database(config.database_url)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


__all__ = [
    "announce_deprecation_notice",
    "approve_contract_release",
    "build_contract_release",
    "create_deprecation_notice",
    "download_sdk_release",
    "get_platform_contract_workspace",
    "list_api_versions",
    "list_changelog",
    "list_sdk_releases",
    "list_webhook_schema_versions",
    "schedule_due_deprecations",
    "sunset_deprecation_notice",
    "withdraw_deprecation_notice",
    "withdraw_sdk_release",
]
