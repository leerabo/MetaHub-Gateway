"""PRD-124 signed installer catalog and tenant-owned installation facts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    InstallerContractError,
    PreflightReport,
    load_strict_json,
    verify_installer_artifacts,
    verify_installer_release,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.service import WorkspaceSessionContext

from .models import (
    EdgeInstallationResponse,
    EdgeInstallationStartRequest,
    EdgeInstallationSummary,
    EdgeInstallationUpdateRequest,
    EdgeInstallerCatalogResponse,
    EdgePreflightRecordRequest,
    EdgePreflightRecordResponse,
    EdgePreflightSummary,
    InstallerArtifactDownload,
    PlatformInstallerReleaseDecisionRequest,
    PlatformInstallerReleaseListResponse,
    PlatformInstallerReleaseResponse,
    PlatformInstallerReleaseSubmitRequest,
    PlatformInstallerReleaseSummary,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _digest,
    _id,
    _identifier,
    _owned_deployment,
    _rollback,
    _utcnow,
    _workspace_context,
    _workspace_idempotency,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_INSTALLER_ASSET_ROOT = _REPOSITORY_ROOT / "infra" / "installers"
_CURRENT_SCHEMA = 116
_CURRENT_AGENT_VERSION = "1.0.0"
_CURRENT_CONTROL_VERSION = "1.0.0"
_TERMINAL_STATUSES = frozenset({"active", "failed", "uninstalled"})
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@dataclass(frozen=True, slots=True)
class InstallerArtifactFile:
    path: Path
    filename: str
    media_type: str
    sha256: str


def list_platform_installer_releases(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlatformInstallerReleaseListResponse:
    _require_platform(context, "platform.edge_installer.read")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM installer_releases "
            "ORDER BY submitted_at DESC,installer_release_id DESC LIMIT 50"
        ).fetchall()
        result = PlatformInstallerReleaseListResponse(
            releases=tuple(_platform_release_summary(connection, row) for row in rows),
            security_projection="platform.edge_installer.manage" not in context.permissions,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except InstallerContractError:
        _rollback(connection)
        raise ApiError("platform.edge_installer_integrity_failed") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.edge_installer_unavailable") from None
    finally:
        connection.close()


def submit_platform_installer_release(
    config: AppConfig,
    context: PlatformContext,
    payload: PlatformInstallerReleaseSubmitRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PlatformInstallerReleaseResponse:
    _require_platform(context, "platform.edge_installer.manage")
    action = "platform.edge_installer.submit"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _platform_release_command(
            connection, context, action, idempotency_sha256
        )
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise ApiError("platform.edge_installer_idempotency_conflict")
            row = _platform_release_row(
                connection, str(replay["installer_release_id"])
            )
            result = _platform_release_response(
                connection, row, request_id, trace_id, replay=True
            )
            connection.commit()
            return result

        envelope = payload.release.model_dump(mode="json")
        trusted_keys = _trusted_release_keys(connection, active_only=True)
        verified = verify_installer_release(
            envelope,
            trusted_keys=trusted_keys,
            current_schema=_CURRENT_SCHEMA,
            agent_version=_CURRENT_AGENT_VERSION,
            control_version=_CURRENT_CONTROL_VERSION,
        )
        root = _release_asset_root(verified.release.manifest.version)
        stored_envelope = load_strict_json(root / "manifest.json")
        if stored_envelope != envelope:
            raise InstallerContractError("installer.manifest_digest_invalid")
        verify_installer_artifacts(verified, root)
        manifest = verified.release.manifest
        signature = verified.release.signature
        sbom = next(item for item in manifest.artifacts if item.kind == "sbom")
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO installer_releases(
              installer_release_id,version,channel,status,manifest_sha256,sbom_sha256,
              signature_algorithm,signature_key_id,signature_value,signature_purpose,
              supported_os_json,supported_arch_json,schema_min,schema_max,
              agent_min_version,agent_max_version,control_min_version,control_max_version,
              n_minus_one,revision,submitted_by_account_id,approved_by_account_id,
              submitted_at,approved_at,withdrawn_at)
            VALUES(?,?,?,'approval_pending',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,NULL,?,NULL,NULL)
            """,
            (
                manifest.release_id,
                manifest.version,
                manifest.channel,
                verified.release.manifest_sha256,
                sbom.sha256,
                signature.algorithm,
                signature.key_id,
                signature.value,
                manifest.signature_purpose,
                _canonical(manifest.supported_os),
                _canonical(manifest.supported_arch),
                manifest.schema_min,
                manifest.schema_max,
                manifest.agent_min_version,
                manifest.agent_max_version,
                manifest.control_min_version,
                manifest.control_max_version,
                int(manifest.n_minus_one),
                context.account_id,
                now,
            ),
        )
        for artifact in manifest.artifacts:
            connection.execute(
                """
                INSERT INTO installer_release_artifacts(
                  installer_artifact_id,installer_release_id,kind,filename,media_type,
                  content_sha256,sbom_sha256,size_bytes,executable,download_path,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id,
                    manifest.release_id,
                    artifact.kind,
                    artifact.filename,
                    artifact.media_type,
                    artifact.sha256,
                    artifact.sbom_sha256,
                    artifact.size_bytes,
                    int(artifact.executable),
                    f"/edge/installers/{manifest.release_id}/artifacts/{artifact.artifact_id}",
                    now,
                ),
            )
        for profile in manifest.profiles:
            connection.execute(
                """
                INSERT INTO installer_profiles(
                  install_profile_id,installer_release_id,profile_code,display_name,cpu_cores,
                  memory_mib,disk_mib,required_ports_json,dns_targets_json,runtime,
                  runtime_min_version,filesystem,proxy_modes_json,ca_mode,revision,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                """,
                (
                    profile.install_profile_id,
                    manifest.release_id,
                    profile.profile_code,
                    profile.display_name,
                    profile.cpu_cores,
                    profile.memory_mib,
                    profile.disk_mib,
                    _canonical(profile.required_ports),
                    _canonical(profile.dns_targets),
                    profile.runtime,
                    profile.runtime_min_version,
                    profile.filesystem,
                    _canonical(profile.proxy_modes),
                    profile.ca_mode,
                    now,
                ),
            )
        _insert_platform_release_command(
            connection,
            context,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            release_id=manifest.release_id,
            now=now,
        )
        _platform_audit(
            connection,
            context,
            action=action,
            release_id=manifest.release_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={
                "status": "approval_pending",
                "manifest_sha256": verified.release.manifest_sha256,
            },
            now=now,
        )
        row = _platform_release_row(connection, manifest.release_id)
        result = _platform_release_response(
            connection, row, request_id, trace_id, replay=False
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except InstallerContractError:
        _rollback(connection)
        raise ApiError("platform.edge_installer_release_invalid") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("platform.edge_installer_conflict") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.edge_installer_unavailable") from None
    finally:
        connection.close()


def decide_platform_installer_release(
    config: AppConfig,
    context: PlatformContext,
    release_id: str,
    payload: PlatformInstallerReleaseDecisionRequest,
    *,
    step_up_grant: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PlatformInstallerReleaseResponse:
    permission = (
        "platform.edge_installer.approve"
        if payload.action == "approve"
        else "platform.edge_installer.manage"
    )
    _require_platform(context, permission)
    action = f"platform.edge_installer.{payload.action}"
    _validate_release_decision(payload)
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _platform_release_command(
            connection, context, action, idempotency_sha256
        )
        if replay is not None:
            if (
                str(replay["request_sha256"]) != request_sha256
                or str(replay["installer_release_id"]) != release_id
            ):
                raise ApiError("platform.edge_installer_idempotency_conflict")
            row = _platform_release_row(connection, release_id)
            result = _platform_release_response(
                connection, row, request_id, trace_id, replay=True
            )
            connection.commit()
            return result

        row = _platform_release_row(connection, release_id)
        if int(row["revision"]) != expected_revision:
            raise ApiError("platform.edge_installer_revision_conflict")
        if payload.action == "approve":
            if str(row["status"]) != "approval_pending":
                raise ApiError("platform.edge_installer_state_conflict")
            if str(row["submitted_by_account_id"]) == context.account_id:
                raise ApiError("platform.edge_installer_self_approval_denied")
        elif str(row["status"]) not in {"approval_pending", "released"}:
            raise ApiError("platform.edge_installer_state_conflict")

        now_value = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=context.account_id,
            global_session_id=context.global_session_id,
            grant=step_up_grant,
            action_code=action,
            resource_type="installer_release",
            resource_id=release_id,
            now=now_value,
        )
        now = _db_time(now_value)
        before = {"status": str(row["status"]), "revision": int(row["revision"])}
        if payload.action == "approve":
            updated = connection.execute(
                """
                UPDATE installer_releases
                SET status='released',approved_by_account_id=?,approved_at=?,
                    withdrawn_at=NULL,revision=revision+1
                WHERE installer_release_id=? AND status='approval_pending' AND revision=?
                """,
                (context.account_id, now, release_id, expected_revision),
            ).rowcount
        else:
            updated = connection.execute(
                """
                UPDATE installer_releases
                SET status='withdrawn',withdrawn_at=?,revision=revision+1
                WHERE installer_release_id=? AND status IN('approval_pending','released')
                  AND revision=?
                """,
                (now, release_id, expected_revision),
            ).rowcount
        if updated != 1:
            raise ApiError("platform.edge_installer_revision_conflict")
        _insert_platform_release_command(
            connection,
            context,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            release_id=release_id,
            now=now,
        )
        current = _platform_release_row(connection, release_id)
        _platform_audit(
            connection,
            context,
            action=action,
            release_id=release_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            before=before,
            after={"status": str(current["status"]), "revision": int(current["revision"])},
            now=now,
        )
        result = _platform_release_response(
            connection, current, request_id, trace_id, replay=False
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except InstallerContractError:
        _rollback(connection)
        raise ApiError("platform.edge_installer_integrity_failed") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.edge_installer_unavailable") from None
    finally:
        connection.close()


def list_workspace_installers(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeInstallerCatalogResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.installer.read",
        )
        releases, downloads = _released_catalog(connection)
        preflight_rows = connection.execute(
            """
            SELECT * FROM edge_preflight_reports
            WHERE workspace_id=? AND tenant_id=?
            ORDER BY received_at DESC,preflight_report_id DESC
            """,
            (context.workspace_id, context.tenant_id),
        ).fetchall()
        rows = connection.execute(
            """
            SELECT * FROM edge_installations
            WHERE workspace_id=? AND tenant_id=?
            ORDER BY updated_at DESC,installation_id DESC
            """,
            (context.workspace_id, context.tenant_id),
        ).fetchall()
        response = EdgeInstallerCatalogResponse(
            releases=releases,
            downloads=downloads,
            preflights=tuple(_preflight_summary(row) for row in preflight_rows),
            installations=tuple(_installation_summary(row) for row in rows),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.rollback()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except InstallerContractError:
        _rollback(connection)
        raise ApiError("edge.installer_integrity_failed") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def resolve_workspace_installer_artifact(
    config: AppConfig,
    *,
    session_token: str,
    release_id: str,
    artifact_id: str,
) -> InstallerArtifactFile:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.installer.read",
        )
        row = connection.execute(
            """
            SELECT artifact.*,release.version
            FROM installer_release_artifacts artifact
            JOIN installer_releases release
              ON release.installer_release_id=artifact.installer_release_id
            WHERE artifact.installer_release_id=? AND artifact.installer_artifact_id=?
              AND release.status='released'
            """,
            (release_id, artifact_id),
        ).fetchone()
        if row is None:
            raise ApiError("edge.installer_not_found")
        root = _release_asset_root(str(row["version"]))
        path = (root / str(row["filename"])).resolve(strict=True)
        if (
            path.parent != root
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != int(row["size_bytes"])
            or _file_sha256(path) != str(row["content_sha256"])
        ):
            raise ApiError("edge.installer_integrity_failed")
        connection.rollback()
        return InstallerArtifactFile(
            path=path,
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            sha256=str(row["content_sha256"]),
        )
    except ApiError:
        _rollback(connection)
        raise
    except InstallerContractError:
        _rollback(connection)
        raise ApiError("edge.installer_integrity_failed") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def record_workspace_preflight(
    config: AppConfig,
    payload: EdgePreflightRecordRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgePreflightRecordResponse:
    request_sha256 = _digest(payload.model_dump(mode="json"))
    report_sha256 = _digest(payload.report.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.installation.manage",
        )
        _owned_deployment(connection, context, payload.deployment_id)
        idempotency_sha256 = _workspace_idempotency(
            config, context, "edge.preflight.record", idempotency_key
        )
        replay = _install_command(
            connection, context, "edge.preflight.record", idempotency_sha256
        )
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise ApiError("edge.idempotency_conflict")
            row = _owned_preflight(connection, context, str(replay["result_object_id"]))
            result = _preflight_response(row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        _validated_release_profile(
            connection,
            payload.report.release_id,
            payload.report.install_profile_id,
        )
        _validate_preflight_times(payload.report)
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO edge_preflight_reports(
              preflight_report_id,workspace_id,tenant_id,edge_deployment_id,
              installer_release_id,install_profile_id,generation,outcome,
              environment_sha256,checks_json,report_sha256,duration_ms,
              started_at,completed_at,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.report.preflight_report_id,
                context.workspace_id,
                context.tenant_id,
                payload.deployment_id,
                payload.report.release_id,
                payload.report.install_profile_id,
                payload.report.generation,
                payload.report.outcome,
                payload.report.environment_sha256,
                _canonical(payload.report.model_dump(mode="json")["checks"]),
                report_sha256,
                payload.report.duration_ms,
                payload.report.started_at,
                payload.report.completed_at,
                now,
            ),
        )
        _insert_install_command(
            connection,
            context,
            action="edge.preflight.record",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=payload.report.preflight_report_id,
            now=now,
        )
        row = _owned_preflight(connection, context, payload.report.preflight_report_id)
        result = _preflight_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "edge_deployment_id,generation" in str(exc):
            raise ApiError("edge.generation_conflict") from None
        raise ApiError("edge.unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def start_workspace_installation(
    config: AppConfig,
    payload: EdgeInstallationStartRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeInstallationResponse:
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.installation.manage",
        )
        deployment = _owned_deployment(connection, context, payload.deployment_id)
        if deployment["status"] in {"revoked", "retired"}:
            raise ApiError("edge.state_conflict")
        report = _owned_preflight(connection, context, payload.preflight_report_id)
        if (
            report["edge_deployment_id"] != payload.deployment_id
            or report["generation"] != payload.generation
            or report["outcome"] != "passed"
        ):
            raise ApiError("edge.preflight_failed")
        idempotency_sha256 = _workspace_idempotency(
            config, context, "edge.install.start", idempotency_key
        )
        replay = _install_command(connection, context, "edge.install.start", idempotency_sha256)
        if replay is not None:
            if str(replay["request_sha256"]) != request_sha256:
                raise ApiError("edge.idempotency_conflict")
            row = _owned_installation(connection, context, str(replay["result_object_id"]))
            result = _installation_response(row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO edge_installations(
              installation_id,workspace_id,tenant_id,edge_deployment_id,
              installer_release_id,install_profile_id,preflight_report_id,generation,
              status,current_step,revision,journal_sha256,last_receipt_sha256,
              compensation_status,retain_config,retain_audit,retain_secrets,
              failure_reason_code,idempotency_sha256,request_sha256,started_at,
              updated_at,completed_at,failed_at)
            VALUES(?,?,?,?,?,?,?,?,'preflight_passed',NULL,1,?,NULL,'not_required',
              1,1,1,NULL,?,?,?,?,NULL,NULL)
            """,
            (
                payload.installation_id,
                context.workspace_id,
                context.tenant_id,
                payload.deployment_id,
                report["installer_release_id"],
                report["install_profile_id"],
                payload.preflight_report_id,
                payload.generation,
                payload.journal_sha256,
                idempotency_sha256,
                request_sha256,
                now,
                now,
            ),
        )
        _insert_install_audit(
            connection,
            context,
            installation_id=payload.installation_id,
            deployment_id=payload.deployment_id,
            generation=payload.generation,
            event_type="edge.install_started",
            step=None,
            outcome="started",
            reason="install_started",
            receipt=None,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            outbox_domains=("workspace", "fleet"),
        )
        _insert_install_command(
            connection,
            context,
            action="edge.install.start",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=payload.installation_id,
            now=now,
        )
        row = _owned_installation(connection, context, payload.installation_id)
        result = _installation_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge.generation_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def update_workspace_installation(
    config: AppConfig,
    installation_id: str,
    payload: EdgeInstallationUpdateRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str | None,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeInstallationResponse:
    request_sha256 = _digest(payload.model_dump(mode="json"))
    action = (
        "edge.uninstall"
        if payload.status in {"uninstalling", "uninstalled"}
        else "edge.install.step"
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.installation.manage",
        )
        idempotency_sha256 = _workspace_idempotency(config, context, action, idempotency_key)
        replay = _install_command(connection, context, action, idempotency_sha256)
        if replay is not None:
            if (
                str(replay["request_sha256"]) != request_sha256
                or str(replay["result_object_id"]) != installation_id
            ):
                raise ApiError("edge.idempotency_conflict")
            row = _owned_installation(connection, context, installation_id)
            result = _installation_response(row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        current = _owned_installation(connection, context, installation_id)
        if int(current["revision"]) != expected_revision:
            raise ApiError("edge.revision_conflict")
        _validate_install_update(current, payload)
        now_value = _utcnow()
        now = _db_time(now_value)
        purging = not (payload.retain_config and payload.retain_audit and payload.retain_secrets)
        if purging:
            if not step_up_grant:
                raise ApiError("step_up.required")
            consume_step_up_grant_in_transaction(
                connection,
                config,
                context=context,
                grant=step_up_grant,
                action_code="edge.installation.purge",
                resource_type="edge_installation",
                resource_id=installation_id,
                now=now_value,
            )
        terminal_time = now if payload.status in {"active", "uninstalled"} else None
        failed_time = now if payload.status == "failed" else None
        failure_reason = payload.reason_code if payload.status == "failed" else None
        updated = connection.execute(
            """
            UPDATE edge_installations
            SET status=?,current_step=?,revision=revision+1,journal_sha256=?,
                last_receipt_sha256=?,compensation_status=?,retain_config=?,retain_audit=?,
                retain_secrets=?,failure_reason_code=?,updated_at=?,completed_at=?,failed_at=?
            WHERE workspace_id=? AND tenant_id=? AND installation_id=? AND revision=?
            """,
            (
                payload.status,
                payload.current_step,
                payload.journal_sha256,
                payload.receipt_sha256,
                payload.compensation_status,
                int(payload.retain_config),
                int(payload.retain_audit),
                int(payload.retain_secrets),
                failure_reason,
                now,
                terminal_time,
                failed_time,
                context.workspace_id,
                context.tenant_id,
                installation_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge.revision_conflict")
        if payload.status == "failed":
            event_type = "edge.install_failed"
        elif payload.status in {"active", "uninstalled"}:
            event_type = "edge.install_completed"
        else:
            event_type = "edge.install_started"
        outcome = "failed" if payload.status == "failed" else "succeeded"
        reason = payload.reason_code or payload.current_step or payload.status
        outboxes: tuple[str, ...] = ()
        if payload.status in _TERMINAL_STATUSES:
            outboxes = (
                ("workspace", "fleet", "security")
                if payload.status == "failed"
                else ("workspace", "fleet")
            )
        _insert_install_audit(
            connection,
            context,
            installation_id=installation_id,
            deployment_id=str(current["edge_deployment_id"]),
            generation=int(current["generation"]),
            event_type=event_type,
            step=payload.current_step,
            outcome=outcome,
            reason=reason,
            receipt=payload.receipt_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            outbox_domains=outboxes,
        )
        _insert_install_command(
            connection,
            context,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=installation_id,
            now=now,
        )
        row = _owned_installation(connection, context, installation_id)
        result = _installation_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "edge_installation_transition_invalid" in str(exc):
            raise ApiError("edge.state_conflict") from None
        raise ApiError("edge.unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def _require_platform(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _validate_release_decision(payload: PlatformInstallerReleaseDecisionRequest) -> None:
    if (
        payload.action == "approve"
        and payload.reason_code != "release_approved"
    ) or (
        payload.action == "withdraw"
        and payload.reason_code == "release_approved"
    ):
        raise ApiError("platform.edge_installer_request_invalid")


def _platform_idempotency(
    config: AppConfig, operator_id: str, action: str, value: str
) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise ApiError("platform.edge_installer_request_invalid")
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"metahub/prd124/platform-installer-idempotency/v1\0"
        + operator_id.encode("utf-8")
        + b"\0"
        + action.encode("ascii")
        + b"\0"
        + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _platform_release_command(
    connection: sqlite3.Connection,
    context: PlatformContext,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM platform_installer_release_commands
        WHERE operator_id=? AND action_code=? AND idempotency_sha256=?
        """,
        (context.operator_id, action, idempotency_sha256),
    ).fetchone()


def _insert_platform_release_command(
    connection: sqlite3.Connection,
    context: PlatformContext,
    *,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    release_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_installer_release_commands(
          release_command_id,operator_id,action_code,idempotency_sha256,
          request_sha256,installer_release_id,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            _id("irc_"),
            context.operator_id,
            action,
            idempotency_sha256,
            request_sha256,
            release_id,
            now,
        ),
    )


def _platform_release_row(
    connection: sqlite3.Connection, release_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM installer_releases WHERE installer_release_id=?",
        (release_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.edge_installer_not_found")
    return row


def _platform_release_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool,
) -> PlatformInstallerReleaseResponse:
    return PlatformInstallerReleaseResponse(
        release=_platform_release_summary(connection, row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _platform_release_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> PlatformInstallerReleaseSummary:
    verified, _artifacts = _verified_release_row(
        connection, row, active_keys_only=False
    )
    return PlatformInstallerReleaseSummary(
        release=verified.release,
        status=str(row["status"]),
        revision=int(row["revision"]),
        submitted_by_account_id=str(row["submitted_by_account_id"]),
        approved_by_account_id=(
            str(row["approved_by_account_id"])
            if row["approved_by_account_id"] is not None
            else None
        ),
        submitted_at=_datetime(row["submitted_at"]),
        approved_at=_datetime(row["approved_at"]) if row["approved_at"] else None,
        withdrawn_at=(
            _datetime(row["withdrawn_at"]) if row["withdrawn_at"] else None
        ),
    )


def _platform_audit(
    connection: sqlite3.Connection,
    context: PlatformContext,
    *,
    action: str,
    release_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: str,
    before: object | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("pau_"),
            context.operator_id,
            action,
            "installer_release",
            release_id,
            reason,
            _identifier(request_id),
            _identifier(trace_id),
            _digest(before) if before is not None else None,
            _digest(after),
            now,
        ),
    )


def _trusted_release_keys(
    connection: sqlite3.Connection, *, active_only: bool
) -> dict[str, bytes]:
    query = "SELECT * FROM installer_trusted_release_keys"
    if active_only:
        query += " WHERE status='active'"
    return {
        str(row["key_id"]): _decode_public_key(str(row["public_key_b64"]))
        for row in connection.execute(query).fetchall()
    }


def _release_asset_root(version: str) -> Path:
    candidate = _INSTALLER_ASSET_ROOT / f"v{version}"
    try:
        asset_root = _INSTALLER_ASSET_ROOT.resolve(strict=True)
        if candidate.is_symlink():
            raise OSError
        root = candidate.resolve(strict=True)
        if root.parent != asset_root or not root.is_dir():
            raise OSError
        return root
    except OSError:
        raise InstallerContractError("installer.artifact_missing") from None


def _verified_release_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    active_keys_only: bool,
):
    artifacts = connection.execute(
        "SELECT * FROM installer_release_artifacts WHERE installer_release_id=? "
        "ORDER BY installer_artifact_id",
        (row["installer_release_id"],),
    ).fetchall()
    profiles = connection.execute(
        "SELECT * FROM installer_profiles WHERE installer_release_id=? "
        "ORDER BY install_profile_id",
        (row["installer_release_id"],),
    ).fetchall()
    keys = _trusted_release_keys(connection, active_only=active_keys_only)
    manifest = {
        "schema_version": "metahub.installer/v1",
        "release_id": str(row["installer_release_id"]),
        "version": str(row["version"]),
        "channel": str(row["channel"]),
        "signature_purpose": str(row["signature_purpose"]),
        "supported_os": json.loads(row["supported_os_json"]),
        "supported_arch": json.loads(row["supported_arch_json"]),
        "schema_min": int(row["schema_min"]),
        "schema_max": int(row["schema_max"]),
        "agent_min_version": str(row["agent_min_version"]),
        "agent_max_version": str(row["agent_max_version"]),
        "control_min_version": str(row["control_min_version"]),
        "control_max_version": str(row["control_max_version"]),
        "n_minus_one": bool(row["n_minus_one"]),
        "artifacts": [_artifact_manifest(item) for item in artifacts],
        "profiles": [_profile_manifest(item) for item in profiles],
    }
    envelope = {
        "manifest": manifest,
        "manifest_sha256": str(row["manifest_sha256"]),
        "signature": {
            "algorithm": str(row["signature_algorithm"]),
            "key_id": str(row["signature_key_id"]),
            "public_key": _public_key_b64(keys.get(str(row["signature_key_id"]))),
            "value": str(row["signature_value"]),
        },
    }
    verified = verify_installer_release(
        envelope,
        trusted_keys=keys,
        current_schema=_CURRENT_SCHEMA,
        agent_version=_CURRENT_AGENT_VERSION,
        control_version=_CURRENT_CONTROL_VERSION,
    )
    return verified, artifacts


def _released_catalog(connection: sqlite3.Connection):
    releases = []
    downloads: list[InstallerArtifactDownload] = []
    rows = connection.execute(
        "SELECT * FROM installer_releases WHERE status='released' "
        "ORDER BY submitted_at DESC,installer_release_id DESC"
    ).fetchall()
    for row in rows:
        verified, artifacts = _verified_release_row(
            connection, row, active_keys_only=True
        )
        releases.append(verified.release)
        downloads.extend(
            InstallerArtifactDownload(
                artifact_id=str(item["installer_artifact_id"]),
                release_id=str(item["installer_release_id"]),
                filename=str(item["filename"]),
                download_path=str(item["download_path"]),
                sha256=str(item["content_sha256"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in artifacts
        )
    return tuple(releases), tuple(downloads)


def _artifact_manifest(row: sqlite3.Row) -> dict[str, object]:
    return {
        "artifact_id": str(row["installer_artifact_id"]),
        "kind": str(row["kind"]),
        "filename": str(row["filename"]),
        "media_type": str(row["media_type"]),
        "sha256": str(row["content_sha256"]),
        "sbom_sha256": str(row["sbom_sha256"]),
        "size_bytes": int(row["size_bytes"]),
        "executable": bool(row["executable"]),
    }


def _profile_manifest(row: sqlite3.Row) -> dict[str, object]:
    return {
        "install_profile_id": str(row["install_profile_id"]),
        "profile_code": str(row["profile_code"]),
        "display_name": str(row["display_name"]),
        "cpu_cores": int(row["cpu_cores"]),
        "memory_mib": int(row["memory_mib"]),
        "disk_mib": int(row["disk_mib"]),
        "required_ports": json.loads(row["required_ports_json"]),
        "dns_targets": json.loads(row["dns_targets_json"]),
        "runtime": str(row["runtime"]),
        "runtime_min_version": str(row["runtime_min_version"]),
        "filesystem": str(row["filesystem"]),
        "proxy_modes": json.loads(row["proxy_modes_json"]),
        "ca_mode": str(row["ca_mode"]),
    }


def _validated_release_profile(
    connection: sqlite3.Connection, release_id: str, profile_id: str
) -> None:
    releases, _downloads = _released_catalog(connection)
    release = next((item for item in releases if item.manifest.release_id == release_id), None)
    if release is None:
        raise ApiError("edge.installer_not_found")
    if profile_id not in {item.install_profile_id for item in release.manifest.profiles}:
        raise ApiError("edge.install_profile_not_found")


def _validate_preflight_times(report: PreflightReport) -> None:
    started = _datetime(report.started_at)
    completed = _datetime(report.completed_at)
    elapsed = int((completed - started).total_seconds() * 1000)
    if elapsed < 0 or abs(elapsed - report.duration_ms) > 2_000:
        raise ApiError("edge.preflight_invalid")


def _validate_install_update(
    current: sqlite3.Row, payload: EdgeInstallationUpdateRequest
) -> None:
    if payload.status == "failed":
        if payload.reason_code is None:
            raise ApiError("edge.request_invalid")
    elif payload.reason_code is not None:
        raise ApiError("edge.request_invalid")
    if payload.status in {"installing", "installed", "connecting", "active"} and (
        payload.current_step is None or payload.receipt_sha256 is None
    ):
        raise ApiError("edge.request_invalid")
    if payload.status in {"uninstalling", "uninstalled"} and payload.current_step is not None:
        raise ApiError("edge.request_invalid")
    if (
        not (payload.retain_config and payload.retain_audit and payload.retain_secrets)
        and payload.status not in {"uninstalling", "uninstalled"}
    ):
        raise ApiError("edge.state_conflict")
    if (
        (not bool(current["retain_config"]) and payload.retain_config)
        or (not bool(current["retain_audit"]) and payload.retain_audit)
        or (not bool(current["retain_secrets"]) and payload.retain_secrets)
    ):
        raise ApiError("edge.state_conflict")


def _install_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_install_commands
        WHERE workspace_id=? AND tenant_id=? AND actor_account_id=?
          AND action_code=? AND idempotency_sha256=?
        """,
        (
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
        ),
    ).fetchone()


def _insert_install_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_install_commands(
          install_command_id,workspace_id,tenant_id,actor_account_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("inc_"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _owned_preflight(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, report_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_preflight_reports WHERE workspace_id=? AND tenant_id=? "
        "AND preflight_report_id=?",
        (context.workspace_id, context.tenant_id, report_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.preflight_not_found")
    return row


def _owned_installation(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, installation_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_installations WHERE workspace_id=? AND tenant_id=? "
        "AND installation_id=?",
        (context.workspace_id, context.tenant_id, installation_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.installation_not_found")
    return row


def _insert_install_audit(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    installation_id: str,
    deployment_id: str,
    generation: int,
    event_type: str,
    step: str | None,
    outcome: str,
    reason: str,
    receipt: str | None,
    request_id: str,
    trace_id: str,
    now: str,
    outbox_domains: tuple[str, ...],
) -> None:
    prior = connection.execute(
        "SELECT entry_sha256 FROM edge_install_audits WHERE installation_id=? "
        "ORDER BY occurred_at DESC,install_audit_id DESC LIMIT 1",
        (installation_id,),
    ).fetchone()
    previous = str(prior["entry_sha256"]) if prior is not None else "0" * 64
    audit_id = _id("ina_")
    entry_sha256 = _digest(
        {
            "audit_id": audit_id,
            "installation_id": installation_id,
            "generation": generation,
            "event_type": event_type,
            "step": step,
            "outcome": outcome,
            "reason": reason,
            "receipt_sha256": receipt,
            "previous_sha256": previous,
            "request_id": _identifier(request_id),
            "trace_id": _identifier(trace_id),
            "occurred_at": now,
        }
    )
    connection.execute(
        """
        INSERT INTO edge_install_audits(
          install_audit_id,workspace_id,tenant_id,edge_deployment_id,installation_id,
          generation,actor_type,actor_account_id,event_type,step,outcome_code,reason_code,
          receipt_sha256,previous_sha256,entry_sha256,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,'human',?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            audit_id,
            context.workspace_id,
            context.tenant_id,
            deployment_id,
            installation_id,
            generation,
            context.account_id,
            event_type,
            step,
            outcome,
            reason,
            receipt,
            previous,
            entry_sha256,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for domain in outbox_domains:
        connection.execute(
            """
            INSERT INTO edge_install_outbox(
              install_outbox_id,workspace_id,tenant_id,edge_deployment_id,installation_id,
              install_audit_id,event_type,delivery_domain,status,attempt_count,available_at,
              delivered_at,last_error_code,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'pending',0,?,NULL,NULL,?,?)
            """,
            (
                _id("ino_"),
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                installation_id,
                audit_id,
                event_type,
                domain,
                now,
                now,
                now,
            ),
        )


def _preflight_response(
    row: sqlite3.Row, request_id: str, trace_id: str, *, replay: bool
) -> EdgePreflightRecordResponse:
    return EdgePreflightRecordResponse(
        preflight_report_id=str(row["preflight_report_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        release_id=str(row["installer_release_id"]),
        install_profile_id=str(row["install_profile_id"]),
        generation=int(row["generation"]),
        outcome=str(row["outcome"]),
        report_sha256=str(row["report_sha256"]),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _preflight_summary(row: sqlite3.Row) -> EdgePreflightSummary:
    return EdgePreflightSummary(
        preflight_report_id=str(row["preflight_report_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        release_id=str(row["installer_release_id"]),
        install_profile_id=str(row["install_profile_id"]),
        generation=int(row["generation"]),
        outcome=str(row["outcome"]),
        checks=tuple(json.loads(row["checks_json"])),
        duration_ms=int(row["duration_ms"]),
        started_at=_datetime(row["started_at"]),
        completed_at=_datetime(row["completed_at"]),
        received_at=_datetime(row["received_at"]),
    )


def _installation_response(
    row: sqlite3.Row, request_id: str, trace_id: str, *, replay: bool
) -> EdgeInstallationResponse:
    return EdgeInstallationResponse(
        installation=_installation_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _installation_summary(row: sqlite3.Row) -> EdgeInstallationSummary:
    return EdgeInstallationSummary(
        installation_id=str(row["installation_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        release_id=str(row["installer_release_id"]),
        install_profile_id=str(row["install_profile_id"]),
        preflight_report_id=str(row["preflight_report_id"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        current_step=str(row["current_step"]) if row["current_step"] is not None else None,
        revision=int(row["revision"]),
        journal_sha256=str(row["journal_sha256"]),
        last_receipt_sha256=(
            str(row["last_receipt_sha256"])
            if row["last_receipt_sha256"] is not None
            else None
        ),
        compensation_status=str(row["compensation_status"]),
        retain_config=bool(row["retain_config"]),
        retain_audit=bool(row["retain_audit"]),
        retain_secrets=bool(row["retain_secrets"]),
        failure_reason_code=(
            str(row["failure_reason_code"]) if row["failure_reason_code"] is not None else None
        ),
        started_at=_datetime(row["started_at"]),
        updated_at=_datetime(row["updated_at"]),
        completed_at=_datetime(row["completed_at"]) if row["completed_at"] else None,
        failed_at=_datetime(row["failed_at"]) if row["failed_at"] else None,
    )


def _decode_public_key(value: str) -> bytes:
    decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    if len(decoded) != 32:
        raise InstallerContractError("installer.signing_key_untrusted")
    return decoded


def _public_key_b64(value: bytes | None) -> str:
    if value is None or len(value) != 32:
        raise InstallerContractError("installer.signing_key_untrusted")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = (
    "InstallerArtifactFile",
    "decide_platform_installer_release",
    "list_workspace_installers",
    "list_platform_installer_releases",
    "record_workspace_preflight",
    "resolve_workspace_installer_artifact",
    "start_workspace_installation",
    "submit_platform_installer_release",
    "update_workspace_installation",
)
