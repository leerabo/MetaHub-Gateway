"""Signed provider catalog publication and tenant instantiation for PRD-119."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlunsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from metahub_shared.config_distribution import canonical_json_bytes
from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import provider_adapter_spec

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.platform_access.service import PlatformContext
from metahub_control.provider_health import service as provider_health

from .models import (
    CatalogInstanceSummary,
    CatalogProviderCreateRequest,
    CatalogProviderCreateResponse,
    CompatibilityResponse,
    PlatformCatalogPublishRequest,
    PlatformCatalogPublishResponse,
    PlatformCatalogResponse,
    PlatformCatalogVersionSummary,
    ProviderCatalogEntry,
    ProviderCatalogManifest,
    ProviderCatalogResponse,
    ProviderPreflightRequest,
    ProviderPreflightResponse,
    ProviderPreflightRun,
)
from .seed import REQUIRED_ENTRY_MODES, SEED_KEY_ID

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_HOST_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")
_BLOCKED_HOSTS = frozenset(
    {
        "instance-data",
        "instance-data.ec2.internal",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)
_OFFICIAL_ENTRY_CODES = frozenset(
    {"zhipu", "dashscope", "moonshot", "wenxin", "volcengine", "hunyuan", "deepseek"}
)


@dataclass(frozen=True, slots=True)
class _WorkspaceOwner:
    workspace_id: str
    tenant_id: str
    account_id: str


def list_provider_catalog(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    vendor_code: str | None,
    capability_code: str | None,
    region_code: str | None,
    request_id: str,
    trace_id: str,
) -> ProviderCatalogResponse:
    """Return a verified tenant projection of the current immutable catalog."""

    _require_workspace_permission(context, "provider_catalog.read")
    checked_status = _optional_code(status, {"active", "deprecated", "retired"})
    checked_vendor = _optional_code(vendor_code)
    checked_capability = _optional_code(capability_code)
    checked_region = _optional_region(region_code)
    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "provider_catalog.read")
    try:
        transaction.begin()
        _workspace_owner(connection, transaction.context)
        head = _head(connection)
        manifest, version = _verified_manifest(connection, str(head["version"]))
        entries = tuple(
            entry
            for entry in manifest.entries
            if (checked_status is None or entry.status == checked_status)
            and (checked_vendor is None or entry.vendor_code == checked_vendor)
            and (
                checked_capability is None
                or checked_capability in {item.code for item in entry.capabilities}
            )
            and (
                checked_region is None
                or checked_region in entry.data_regions
                or checked_region in {item.region_code for item in entry.endpoints}
            )
        )
        result = ProviderCatalogResponse(
            version=manifest.version,
            generation=int(version["generation"]),
            manifest_sha256=str(version["manifest_sha256"]),
            published_at=_datetime(version["published_at"]),
            entries=entries,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        transaction.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()


def create_catalog_provider(
    config: AppConfig,
    context: AuthContext,
    payload: CatalogProviderCreateRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> CatalogProviderCreateResponse:
    """Create one Provider from a server-reconstructed official endpoint."""

    _require_workspace_permission(context, "provider.write")
    _valid_idempotency(idempotency_key)
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "provider.write")
    try:
        transaction.begin()
        context = transaction.context
        owner = _workspace_owner(connection, context)
        manifest, _version = _verified_manifest(connection, payload.version)
        entry = _entry(manifest, payload.entry_code)
        if entry.instantiation_mode != "provider" or entry.status != "active":
            raise ApiError("provider_catalog.entry_not_instantiable")
        endpoint = _endpoint(entry, payload.region_code)
        selected = tuple(payload.capability_codes)
        available = {item.code for item in entry.capabilities}
        if not set(selected) <= available:
            raise ApiError("provider_catalog.capability_invalid")
        idempotency_sha256 = _workspace_idempotency(
            config, owner, context.user.user_id, "provider.instantiate", idempotency_key
        )
        replay = _workspace_command(
            connection,
            owner=owner,
            actor_account_id=owner.account_id,
            action="provider.instantiate",
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            if replay["request_sha256"] != request_sha256:
                raise ApiError("provider_catalog.idempotency_conflict")
            result = CatalogProviderCreateResponse(
                instance=_instance_summary(
                    connection, owner, str(replay["result_object_id"])
                ),
                idempotent_replay=True,
                next_action="configure_provider_key",
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            transaction.commit()
            return result

        now = _db_time(_utcnow())
        provider_id = str(uuid4())
        instance_id = _id("pci_")
        base_url = _endpoint_url(endpoint)
        auth_type = "bearer" if "bearer" in entry.auth_modes else "none"
        connection.execute(
            """
            INSERT INTO providers(
              provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
              provider_type,auth_type,description,health_status,created_by,next_probe_at)
            VALUES(?,?,?,?,?,'active',?,?,?,?,?,'unknown',?,?)
            """,
            (
                provider_id,
                owner.tenant_id,
                payload.provider_code,
                payload.name,
                base_url,
                now,
                now,
                entry.provider_type,
                auth_type,
                f"Official catalog {entry.entry_code}@{manifest.version}/{endpoint.region_code}",
                context.user.user_id,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO provider_catalog_instances(
              provider_catalog_instance_id,workspace_id,tenant_id,provider_id,entry_code,
              catalog_version,region_code,capability_codes_json,endpoint_sha256,revision,
              created_by_account_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,1,?,?)
            """,
            (
                instance_id,
                owner.workspace_id,
                owner.tenant_id,
                provider_id,
                entry.entry_code,
                manifest.version,
                endpoint.region_code,
                _canonical_json(list(selected)),
                _digest_text(base_url),
                owner.account_id,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO workspace_provider_catalog_commands(
              provider_catalog_command_id,workspace_id,tenant_id,actor_account_id,
              action_code,idempotency_sha256,request_sha256,result_object_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("pcc_"),
                owner.workspace_id,
                owner.tenant_id,
                owner.account_id,
                "provider.instantiate",
                idempotency_sha256,
                request_sha256,
                instance_id,
                now,
            ),
        )
        result = CatalogProviderCreateResponse(
            instance=_instance_summary(connection, owner, instance_id),
            next_action="configure_provider_key",
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        transaction.commit()
        return result
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "providers.tenant_id, providers.provider_code" in str(exc) or "UNIQUE" in str(exc):
            raise ApiError("provider_catalog.provider_code_exists") from None
        raise ApiError("provider_catalog.create_invalid") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()


def preflight_catalog_provider(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    payload: ProviderPreflightRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ProviderPreflightResponse:
    """Probe a catalog instance through the existing pinned Provider Health transport."""

    _require_workspace_permission(context, "provider.write")
    _valid_idempotency(idempotency_key)
    if not provider_id or len(provider_id) > 160:
        raise ApiError("provider_catalog.request_invalid")
    request_sha256 = _digest(
        {"provider_id": provider_id, "timeout_seconds": payload.timeout_seconds}
    )
    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "provider.write")
    try:
        transaction.begin()
        context = transaction.context
        owner = _workspace_owner(connection, context)
        claim = _preflight_claim(connection, owner, provider_id)
        idempotency_sha256 = _workspace_idempotency(
            config, owner, context.user.user_id, "provider.preflight", idempotency_key
        )
        replay = _workspace_command(
            connection,
            owner=owner,
            actor_account_id=owner.account_id,
            action="provider.preflight",
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            if replay["request_sha256"] != request_sha256:
                raise ApiError("provider_catalog.idempotency_conflict")
            result = ProviderPreflightResponse(
                run=_preflight_summary(
                    connection, owner, str(replay["result_object_id"])
                ),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            transaction.commit()
            return result
        health_claim, credentials = provider_health.claim_catalog_provider_in_transaction(
            config.model_copy(update={
                "provider_health_lease_seconds": max(config.provider_health_lease_seconds, 30),
            }),
            connection, owner.tenant_id, provider_id,
            expected_base_url=str(claim["base_url"]),
        )
        transaction.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()

    try:
        probe = provider_health.perform_catalog_provider_probe(
            config.model_copy(
                update={"provider_health_timeout_seconds": payload.timeout_seconds}
            ),
            health_claim,
            credentials,
        )
        return _complete_catalog_preflight(
            config, context, owner, provider_id, claim, health_claim, probe,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            request_id=request_id,
            trace_id=trace_id,
        )
    except BaseException:
        provider_health.release_catalog_provider_probe(config, health_claim)
        raise
    finally:
        credentials.clear()


def _complete_catalog_preflight(
    config: AppConfig,
    context: AuthContext,
    owner: _WorkspaceOwner,
    provider_id: str,
    claim,
    health_claim,
    probe: provider_health.CatalogProbeResult,
    *,
    idempotency_sha256: str,
    request_sha256: str,
    request_id: str,
    trace_id: str,
) -> ProviderPreflightResponse:
    status, outcome, latency, http_status, checked_at = _preflight_outcome(probe)

    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "provider.write")
    try:
        transaction.begin()
        current_owner = _workspace_owner(connection, transaction.context)
        current_claim = _preflight_claim(connection, current_owner, provider_id)
        if current_owner != owner or any(
            current_claim[field] != claim[field]
            for field in (
                "provider_catalog_instance_id", "revision", "endpoint_sha256",
                "capability_sha256", "catalog_version", "region_code", "entry_code",
            )
        ):
            raise ApiError("provider_catalog.instance_changed")
        replay = _workspace_command(
            connection,
            owner=current_owner,
            actor_account_id=current_owner.account_id,
            action="provider.preflight",
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            if replay["request_sha256"] != request_sha256:
                raise ApiError("provider_catalog.idempotency_conflict")
            result = ProviderPreflightResponse(
                run=_preflight_summary(
                    connection, current_owner, str(replay["result_object_id"])
                ),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.execute(
                """
                UPDATE providers SET probe_lease_token=NULL,probe_lease_until=NULL
                WHERE tenant_id=? AND provider_id=? AND health_generation=? AND probe_lease_token=?
                """,
                (
                    owner.tenant_id, provider_id,
                    health_claim.health_generation, health_claim.lease_token,
                ),
            )
            transaction.commit()
            return result
        health_response, transitioned = provider_health.complete_catalog_provider_in_transaction(
            config, connection, health_claim, probe, request_id=request_id, trace_id=trace_id,
        )
        run_id = _id("pfr_")
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO provider_preflight_runs(
              provider_preflight_run_id,workspace_id,tenant_id,provider_id,
              provider_catalog_instance_id,instance_revision,status,outcome_code,latency_ms,
              http_status,capability_sha256,request_id,checked_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                current_owner.workspace_id,
                current_owner.tenant_id,
                provider_id,
                current_claim["provider_catalog_instance_id"],
                current_claim["revision"],
                status,
                outcome,
                latency,
                http_status,
                current_claim["capability_sha256"],
                _identifier(request_id),
                _db_time(checked_at),
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO workspace_provider_catalog_commands(
              provider_catalog_command_id,workspace_id,tenant_id,actor_account_id,
              action_code,idempotency_sha256,request_sha256,result_object_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("pcc_"),
                current_owner.workspace_id,
                current_owner.tenant_id,
                current_owner.account_id,
                "provider.preflight",
                idempotency_sha256,
                request_sha256,
                run_id,
                now,
            ),
        )
        event_id = _catalog_event(
            connection,
            event_type="provider.preflight_completed",
            catalog_version=str(current_claim["catalog_version"]),
            outcome_code=outcome,
            revision=int(current_claim["revision"]),
            payload={
                "provider_id": provider_id,
                "run_id": run_id,
                "status": status,
                "outcome_code": outcome,
            },
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now,
            workspace_id=current_owner.workspace_id,
            tenant_id=current_owner.tenant_id,
            provider_id=provider_id,
            preflight_run_id=run_id,
            entry_code=str(current_claim["entry_code"]),
        )
        _catalog_outbox(connection, event_id, "workspace", now)
        result = ProviderPreflightResponse(
            run=_preflight_summary(connection, current_owner, run_id),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        def commit_result(connection):
            if (
                health_claim.lease_until is None
                or health_claim.lease_until <= provider_health._utcnow()
            ):
                raise ApiError("provider_health.probe_superseded")
            connection.commit()

        transaction.commit(commit_result)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()
    if transitioned:
        provider_health._dispatch_unhealthy_event(
            config, health_claim, health_response.provider_health,
            list(probe.attempts), request_id, trace_id,
        )
    return result


def get_catalog_compatibility(
    config: AppConfig,
    context: AuthContext,
    entry_code: str,
    *,
    request_id: str,
    trace_id: str,
) -> CompatibilityResponse:
    """Return only the public compatibility projection for one current entry."""

    _require_workspace_permission(context, "provider_catalog.read")
    if _CODE_RE.fullmatch(entry_code) is None:
        raise ApiError("provider_catalog.request_invalid")
    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "provider_catalog.read")
    try:
        transaction.begin()
        owner = _workspace_owner(connection, transaction.context)
        head = _head(connection)
        manifest, _version = _verified_manifest(connection, str(head["version"]))
        entry = _entry(manifest, entry_code)
        count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM provider_catalog_instances
                WHERE workspace_id=? AND tenant_id=? AND entry_code=?
                """,
                (owner.workspace_id, owner.tenant_id, entry.entry_code),
            ).fetchone()[0]
        )
        result = CompatibilityResponse(
            entry_code=entry.entry_code,
            version=manifest.version,
            status=entry.status,
            compatibility=entry.compatibility,
            current_instances=count,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        transaction.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()


def get_platform_catalog(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlatformCatalogResponse:
    """Return signature and release metadata without tenant or endpoint details."""

    _require_platform_read(context)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        head = _head(connection)
        _verified_manifest(connection, str(head["version"]))
        rows = connection.execute(
            "SELECT * FROM provider_catalog_versions ORDER BY generation DESC LIMIT 10"
        ).fetchall()
        summaries = tuple(_version_summary(row) for row in rows)
        result = PlatformCatalogResponse(
            current=summaries[0],
            recent_versions=summaries,
            security_projection="platform.provider_catalog.manage" not in context.permissions,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()


def publish_platform_catalog(
    config: AppConfig,
    context: PlatformContext,
    payload: PlatformCatalogPublishRequest,
    *,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PlatformCatalogPublishResponse:
    """Verify and atomically publish one immutable Ed25519 catalog version."""

    _require_platform_permission(context, "platform.provider_catalog.manage")
    _valid_idempotency(idempotency_key)
    _validate_manifest(payload.manifest)
    raw = canonical_json_bytes(payload.manifest.model_dump(mode="json"))
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    request_sha256 = _digest(
        {
            "manifest_sha256": manifest_sha256,
            "key_id": payload.key_id,
            "signature_sha256": _digest_text(payload.signature),
            "reason_code": payload.reason_code,
        }
    )
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        head = _head(connection)
        current_manifest, current_version = _verified_manifest(
            connection, str(head["version"])
        )
        replay = connection.execute(
            """
            SELECT * FROM platform_provider_catalog_commands
            WHERE operator_id=? AND idempotency_sha256=?
            """,
            (context.operator_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if replay["request_sha256"] != request_sha256:
                raise ApiError("provider_catalog.idempotency_conflict")
            row = connection.execute(
                "SELECT * FROM provider_catalog_versions WHERE version=?",
                (replay["result_version"],),
            ).fetchone()
            if row is None:
                raise ApiError("provider_catalog.unavailable")
            result = PlatformCatalogPublishResponse(
                catalog=_version_summary(row),
                idempotent_replay=True,
                propagated=False,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result
        if int(head["revision"]) != expected_revision:
            raise ApiError("provider_catalog.revision_conflict")
        if _version_tuple(payload.manifest.version) <= _version_tuple(current_manifest.version):
            raise ApiError("provider_catalog.version_conflict")
        key = connection.execute(
            """
            SELECT * FROM provider_catalog_trusted_keys
            WHERE key_id=? AND algorithm='ed25519' AND status='active'
            """,
            (payload.key_id,),
        ).fetchone()
        if key is None:
            raise ApiError("provider_catalog.signature_invalid")
        _verify_signature(
            str(key["public_key_b64"]), payload.signature, raw
        )
        generation = int(current_version["generation"]) + 1
        current_profiles = {
            item.entry_code: item.compatibility.model_dump(mode="json")
            for item in current_manifest.entries
        }
        changed_entries = tuple(
            entry.entry_code
            for entry in payload.manifest.entries
            if current_profiles.get(entry.entry_code)
            != entry.compatibility.model_dump(mode="json")
        )
        now = _db_time(
            max(
                _utcnow(),
                _datetime(head["updated_at"]) + timedelta(milliseconds=1),
            )
        )
        connection.execute(
            """
            INSERT INTO provider_catalog_versions(
              version,generation,schema_version,manifest_sha256,key_id,signature_algorithm,
              signature_b64,entry_count,compatibility_change_count,reason_code,
              published_by_operator_id,published_at)
            VALUES(?,?,?,?,?,'ed25519',?,?,?,?,?,?)
            """,
            (
                payload.manifest.version,
                generation,
                payload.manifest.schema_version,
                manifest_sha256,
                payload.key_id,
                payload.signature,
                len(payload.manifest.entries),
                len(changed_entries),
                payload.reason_code,
                context.operator_id,
                now,
            ),
        )
        _insert_manifest(connection, payload.manifest)
        changed = connection.execute(
            """
            UPDATE provider_catalog_head
            SET version=?,generation=?,revision=revision+1,updated_at=?
            WHERE singleton=1 AND revision=?
            """,
            (payload.manifest.version, generation, now, expected_revision),
        ).rowcount
        if changed != 1:
            raise ApiError("provider_catalog.revision_conflict")
        connection.execute(
            """
            INSERT INTO platform_provider_catalog_commands(
              platform_provider_catalog_command_id,operator_id,idempotency_sha256,
              request_sha256,result_version,result_generation,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                _id("ppc_"),
                context.operator_id,
                idempotency_sha256,
                request_sha256,
                payload.manifest.version,
                generation,
                now,
            ),
        )
        event_id = _catalog_event(
            connection,
            event_type="provider_catalog.updated",
            catalog_version=payload.manifest.version,
            outcome_code=payload.reason_code,
            revision=generation,
            payload={
                "version": payload.manifest.version,
                "generation": generation,
                "manifest_sha256": manifest_sha256,
            },
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now,
        )
        _catalog_outbox(connection, event_id, "platform", now)
        for entry_code in changed_entries:
            compatibility_event = _catalog_event(
                connection,
                event_type="provider.compatibility_changed",
                catalog_version=payload.manifest.version,
                entry_code=entry_code,
                outcome_code="compatibility_changed",
                revision=generation,
                payload={
                    "version": payload.manifest.version,
                    "entry_code": entry_code,
                    "generation": generation,
                },
                request_id=request_id,
                trace_id=trace_id,
                occurred_at=now,
            )
            _catalog_outbox(connection, compatibility_event, "platform", now)
        row = connection.execute(
            "SELECT * FROM provider_catalog_versions WHERE version=?",
            (payload.manifest.version,),
        ).fetchone()
        result = PlatformCatalogPublishResponse(
            catalog=_version_summary(row),
            propagated=False,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("provider_catalog.publish_invalid") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("provider_catalog.unavailable") from None
    finally:
        connection.close()


def _verified_manifest(
    connection: sqlite3.Connection, version_value: str
) -> tuple[ProviderCatalogManifest, sqlite3.Row]:
    version = connection.execute(
        "SELECT * FROM provider_catalog_versions WHERE version=?", (version_value,)
    ).fetchone()
    if version is None:
        raise ApiError("provider_catalog.version_not_found")
    entry_rows = connection.execute(
        """
        SELECT * FROM provider_catalog_entries
        WHERE version=? ORDER BY entry_code
        """,
        (version_value,),
    ).fetchall()
    entry_codes = {str(row["entry_code"]) for row in entry_rows}
    capabilities_by_entry: dict[str, list[dict[str, object]]] = {
        entry_code: [] for entry_code in entry_codes
    }
    for item in connection.execute(
        """
        SELECT * FROM provider_capabilities
        WHERE version=? ORDER BY entry_code,capability_code
        """,
        (version_value,),
    ).fetchall():
        entry_code = str(item["entry_code"])
        if entry_code not in capabilities_by_entry:
            raise ApiError("provider_catalog.integrity_failed")
        capabilities_by_entry[entry_code].append(
            {
                "code": item["capability_code"],
                "max_context_tokens": int(item["max_context_tokens"]),
                "supports_stream": bool(item["supports_stream"]),
                "supports_tools": bool(item["supports_tools"]),
                "supports_files": bool(item["supports_files"]),
            }
        )
    endpoints_by_entry: dict[str, list[dict[str, object]]] = {
        entry_code: [] for entry_code in entry_codes
    }
    for item in connection.execute(
        """
        SELECT * FROM regional_endpoint_templates
        WHERE version=? ORDER BY entry_code,region_code
        """,
        (version_value,),
    ).fetchall():
        entry_code = str(item["entry_code"])
        if entry_code not in endpoints_by_entry:
            raise ApiError("provider_catalog.integrity_failed")
        endpoints_by_entry[entry_code].append(
            {
                "region_code": item["region_code"],
                "cloud_service_code": item["cloud_service_code"],
                "scheme": item["scheme"],
                "host": item["host"],
                "path_template": item["path_template"],
                "residency_tags": _json_list(item["residency_tags_json"]),
            }
        )
    profiles = {
        str(item["entry_code"]): item
        for item in connection.execute(
            """
            SELECT * FROM compatibility_profiles
            WHERE version=? ORDER BY entry_code
            """,
            (version_value,),
        ).fetchall()
    }
    if set(profiles) != entry_codes:
        raise ApiError("provider_catalog.integrity_failed")
    entries: list[dict[str, object]] = []
    for row in entry_rows:
        entry_code = str(row["entry_code"])
        profile = profiles.get(entry_code)
        if profile is None:
            raise ApiError("provider_catalog.integrity_failed")
        entries.append(
            {
                "entry_code": entry_code,
                "vendor_code": row["vendor_code"],
                "display_name": row["display_name"],
                "provider_type": row["provider_type"],
                "service_class": row["service_class"],
                "auth_modes": _json_list(row["auth_modes_json"]),
                "data_regions": _json_list(row["data_regions_json"]),
                "compliance_tags": _json_list(row["compliance_tags_json"]),
                "status": row["status"],
                "instantiation_mode": row["instantiation_mode"],
                "capabilities": capabilities_by_entry[entry_code],
                "endpoints": endpoints_by_entry[entry_code],
                "compatibility": {
                    "level": profile["compatibility_level"],
                    "doc_code": profile["doc_code"],
                    "migration_source_code": profile["migration_source_code"],
                    "request_difference_codes": _json_list(
                        profile["request_difference_codes_json"]
                    ),
                },
            }
        )
    try:
        manifest = ProviderCatalogManifest.model_validate(
            {
                "schema_version": int(version["schema_version"]),
                "version": version["version"],
                "entries": entries,
            }
        )
    except ValueError:
        raise ApiError("provider_catalog.integrity_failed") from None
    _validate_manifest(manifest)
    raw = canonical_json_bytes(manifest.model_dump(mode="json"))
    if (
        len(entries) != int(version["entry_count"])
        or hashlib.sha256(raw).hexdigest() != version["manifest_sha256"]
    ):
        raise ApiError("provider_catalog.integrity_failed")
    key = connection.execute(
        "SELECT * FROM provider_catalog_trusted_keys WHERE key_id=?",
        (version["key_id"],),
    ).fetchone()
    expected_key_status = "seed_only" if int(version["generation"]) == 1 else "active"
    if (
        key is None
        or key["algorithm"] != "ed25519"
        or key["status"] != expected_key_status
        or (int(version["generation"]) == 1 and key["key_id"] != SEED_KEY_ID)
    ):
        raise ApiError("provider_catalog.integrity_failed")
    try:
        _verify_signature(
            str(key["public_key_b64"]), str(version["signature_b64"]), raw
        )
    except ApiError:
        raise ApiError("provider_catalog.integrity_failed") from None
    return manifest, version


def _validate_manifest(manifest: ProviderCatalogManifest) -> None:
    modes = {entry.entry_code: entry.instantiation_mode for entry in manifest.entries}
    if modes != REQUIRED_ENTRY_MODES:
        raise ApiError("provider_catalog.manifest_invalid")
    official = {
        entry.entry_code
        for entry in manifest.entries
        if entry.service_class == "official_cloud"
    }
    if official != _OFFICIAL_ENTRY_CODES:
        raise ApiError("provider_catalog.manifest_invalid")
    for entry in manifest.entries:
        try:
            spec = provider_adapter_spec(entry.provider_type)
        except Exception:
            raise ApiError("provider_catalog.manifest_invalid") from None
        if not set(entry.auth_modes) <= set(spec.allowed_auth_types):
            raise ApiError("provider_catalog.manifest_invalid")
        if entry.instantiation_mode == "provider":
            for endpoint in entry.endpoints:
                _validate_endpoint(endpoint.host, endpoint.path_template)
        elif entry.endpoints:
            raise ApiError("provider_catalog.manifest_invalid")


def _insert_manifest(connection: sqlite3.Connection, manifest: ProviderCatalogManifest) -> None:
    for entry in manifest.entries:
        connection.execute(
            """
            INSERT INTO provider_catalog_entries(
              version,entry_code,vendor_code,display_name,provider_type,service_class,
              auth_modes_json,data_regions_json,compliance_tags_json,status,instantiation_mode)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                manifest.version,
                entry.entry_code,
                entry.vendor_code,
                entry.display_name,
                entry.provider_type,
                entry.service_class,
                _canonical_json(entry.auth_modes),
                _canonical_json(entry.data_regions),
                _canonical_json(entry.compliance_tags),
                entry.status,
                entry.instantiation_mode,
            ),
        )
        for capability in entry.capabilities:
            connection.execute(
                "INSERT INTO provider_capabilities VALUES(?,?,?,?,?,?,?)",
                (
                    manifest.version,
                    entry.entry_code,
                    capability.code,
                    capability.max_context_tokens,
                    int(capability.supports_stream),
                    int(capability.supports_tools),
                    int(capability.supports_files),
                ),
            )
        for endpoint in entry.endpoints:
            connection.execute(
                "INSERT INTO regional_endpoint_templates VALUES(?,?,?,?,?,?,?,?)",
                (
                    manifest.version,
                    entry.entry_code,
                    endpoint.region_code,
                    endpoint.cloud_service_code,
                    endpoint.scheme,
                    endpoint.host,
                    endpoint.path_template,
                    _canonical_json(endpoint.residency_tags),
                ),
            )
        compatibility = entry.compatibility
        connection.execute(
            "INSERT INTO compatibility_profiles VALUES(?,?,?,?,?,?)",
            (
                manifest.version,
                entry.entry_code,
                compatibility.level,
                compatibility.doc_code,
                compatibility.migration_source_code,
                _canonical_json(compatibility.request_difference_codes),
            ),
        )


def _preflight_claim(
    connection: sqlite3.Connection, owner: _WorkspaceOwner, provider_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT instance.*,provider.base_url,provider.status provider_status
        FROM provider_catalog_instances instance
        JOIN providers provider
          ON provider.tenant_id=instance.tenant_id AND provider.provider_id=instance.provider_id
        WHERE instance.workspace_id=? AND instance.tenant_id=? AND instance.provider_id=?
        """,
        (owner.workspace_id, owner.tenant_id, provider_id),
    ).fetchone()
    if row is None:
        raise ApiError("provider_catalog.instance_not_found")
    if row["provider_status"] != "active":
        raise ApiError("provider_catalog.provider_inactive")
    manifest, _version = _verified_manifest(connection, str(row["catalog_version"]))
    entry = _entry(manifest, str(row["entry_code"]))
    endpoint = _endpoint(entry, str(row["region_code"]))
    reconstructed = _endpoint_url(endpoint)
    if (
        _digest_text(reconstructed) != row["endpoint_sha256"]
        or _digest_text(str(row["base_url"])) != row["endpoint_sha256"]
    ):
        raise ApiError("provider_catalog.instance_changed")
    selected = _json_list(row["capability_codes_json"])
    capability_sha256 = _digest(
        {
            "entry_code": entry.entry_code,
            "version": manifest.version,
            "capability_codes": selected,
        }
    )
    values = dict(row)
    values["capability_sha256"] = capability_sha256
    return _mapping_row(values)


def _preflight_outcome(probe) -> tuple[str, str, int, int | None, datetime]:
    now = _utcnow()
    if probe is None or not probe.attempts:
        return "inconclusive", "probe_unavailable", 0, None, now
    attempts = probe.attempts
    latency = min(30_000, sum(max(0, int(item.latency_ms)) for item in attempts))
    last = attempts[-1]
    checked_at = last.checked_at.astimezone(UTC)
    if probe.result_status == "success":
        return "succeeded", "probe_succeeded", latency, last.http_status, checked_at
    error_codes = {item.error_code for item in attempts}
    if "timeout" in error_codes:
        return "timed_out", "probe_timed_out", latency, last.http_status, checked_at
    if probe.result_status == "inconclusive":
        return (
            "inconclusive",
            _outcome_code(last.error_code, "probe_inconclusive"),
            latency,
            last.http_status,
            checked_at,
        )
    return (
        "failed",
        _outcome_code(last.error_code, "probe_failed"),
        latency,
        last.http_status,
        checked_at,
    )


def _catalog_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    catalog_version: str,
    outcome_code: str,
    revision: int,
    payload: object,
    request_id: str,
    trace_id: str,
    occurred_at: str,
    workspace_id: str | None = None,
    tenant_id: str | None = None,
    provider_id: str | None = None,
    preflight_run_id: str | None = None,
    entry_code: str | None = None,
) -> str:
    event_id = _id("pce_")
    connection.execute(
        """
        INSERT INTO provider_catalog_events(
          provider_catalog_event_id,event_type,workspace_id,tenant_id,provider_id,
          provider_preflight_run_id,catalog_version,entry_code,outcome_code,revision,
          payload_sha256,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            event_type,
            workspace_id,
            tenant_id,
            provider_id,
            preflight_run_id,
            catalog_version,
            entry_code,
            outcome_code,
            revision,
            _digest(payload),
            _identifier(request_id),
            _identifier(trace_id),
            occurred_at,
        ),
    )
    return event_id


def _catalog_outbox(
    connection: sqlite3.Connection, event_id: str, domain: str, now: str
) -> None:
    connection.execute(
        """
        INSERT INTO provider_catalog_event_outbox(
          provider_catalog_outbox_id,provider_catalog_event_id,delivery_domain,status,
          attempt_count,available_at,delivered_at,last_error_code,created_at,updated_at)
        VALUES(?,?,?,'pending',0,?,NULL,NULL,?,?)
        """,
        (_id("pco_"), event_id, domain, now, now, now),
    )


def _workspace_owner(
    connection: sqlite3.Connection, context: AuthContext
) -> _WorkspaceOwner:
    row = connection.execute(
        """
        SELECT session.workspace_id,session.tenant_id,session.account_id
        FROM workspace_sessions session
        JOIN workspace_memberships member
          ON member.workspace_membership_id=session.membership_id
         AND member.workspace_id=session.workspace_id AND member.tenant_id=session.tenant_id
         AND member.account_id=session.account_id
        JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=session.workspace_id AND binding.tenant_id=session.tenant_id
        WHERE session.workspace_session_id=? AND session.tenant_id=?
          AND member.legacy_user_id=?
          AND session.status='active' AND member.status='active' AND binding.status='active'
        """,
        (context.session.session_id, context.user.tenant_id, context.user.user_id),
    ).fetchone()
    if row is None:
        if context.session.session_id.startswith("wss_"):
            raise ApiError("provider_catalog.workspace_required")
        rows = connection.execute(
            """
            SELECT member.workspace_id,member.tenant_id,member.account_id
            FROM workspace_memberships member
            JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
            JOIN global_accounts account ON account.account_id=member.account_id
            JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
            WHERE member.legacy_user_id=? AND member.tenant_id=?
              AND member.status='active' AND binding.status='active'
              AND workspace.status='active' AND account.status='active' AND account.login_enabled=1
            ORDER BY member.workspace_id
            """,
            (context.user.user_id, context.user.tenant_id),
        ).fetchall()
        if len(rows) != 1:
            raise ApiError("provider_catalog.workspace_required")
        row = rows[0]
    return _WorkspaceOwner(
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        account_id=str(row["account_id"]),
    )


def _workspace_command(
    connection: sqlite3.Connection,
    *,
    owner: _WorkspaceOwner,
    actor_account_id: str,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM workspace_provider_catalog_commands
        WHERE workspace_id=? AND tenant_id=? AND actor_account_id=?
          AND action_code=? AND idempotency_sha256=?
        """,
        (
            owner.workspace_id,
            owner.tenant_id,
            actor_account_id,
            action,
            idempotency_sha256,
        ),
    ).fetchone()


def _instance_summary(
    connection: sqlite3.Connection, owner: _WorkspaceOwner, instance_id: str
) -> CatalogInstanceSummary:
    row = connection.execute(
        """
        SELECT instance.*,provider.provider_code
        FROM provider_catalog_instances instance
        JOIN providers provider
          ON provider.tenant_id=instance.tenant_id AND provider.provider_id=instance.provider_id
        WHERE instance.workspace_id=? AND instance.tenant_id=?
          AND instance.provider_catalog_instance_id=?
        """,
        (owner.workspace_id, owner.tenant_id, instance_id),
    ).fetchone()
    if row is None:
        raise ApiError("provider_catalog.instance_not_found")
    return CatalogInstanceSummary(
        provider_catalog_instance_id=row["provider_catalog_instance_id"],
        workspace_id=row["workspace_id"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        entry_code=row["entry_code"],
        catalog_version=row["catalog_version"],
        region_code=row["region_code"],
        capability_codes=tuple(_json_list(row["capability_codes_json"])),
        revision=int(row["revision"]),
        created_at=_datetime(row["created_at"]),
    )


def _preflight_summary(
    connection: sqlite3.Connection, owner: _WorkspaceOwner, run_id: str
) -> ProviderPreflightRun:
    row = connection.execute(
        """
        SELECT run.*,instance.entry_code,instance.catalog_version,instance.region_code
        FROM provider_preflight_runs run
        JOIN provider_catalog_instances instance
          ON instance.provider_catalog_instance_id=run.provider_catalog_instance_id
         AND instance.workspace_id=run.workspace_id AND instance.tenant_id=run.tenant_id
        WHERE run.workspace_id=? AND run.tenant_id=?
          AND run.provider_preflight_run_id=?
        """,
        (owner.workspace_id, owner.tenant_id, run_id),
    ).fetchone()
    if row is None:
        raise ApiError("provider_catalog.preflight_not_found")
    return ProviderPreflightRun(
        provider_preflight_run_id=row["provider_preflight_run_id"],
        provider_id=row["provider_id"],
        entry_code=row["entry_code"],
        catalog_version=row["catalog_version"],
        region_code=row["region_code"],
        status=row["status"],
        outcome_code=row["outcome_code"],
        latency_ms=int(row["latency_ms"]),
        http_status=row["http_status"],
        capability_sha256=row["capability_sha256"],
        checked_at=_datetime(row["checked_at"]),
    )


def _version_summary(row: sqlite3.Row) -> PlatformCatalogVersionSummary:
    if row is None:
        raise ApiError("provider_catalog.unavailable")
    return PlatformCatalogVersionSummary(
        version=row["version"],
        generation=int(row["generation"]),
        manifest_sha256=row["manifest_sha256"],
        key_id=row["key_id"],
        signature_algorithm=row["signature_algorithm"],
        signature_sha256=_digest_text(str(row["signature_b64"])),
        entry_count=int(row["entry_count"]),
        compatibility_change_count=int(row["compatibility_change_count"]),
        published_by_operator_id=row["published_by_operator_id"],
        published_at=_datetime(row["published_at"]),
    )


def _head(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM provider_catalog_head WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise ApiError("provider_catalog.unavailable")
    version = connection.execute(
        "SELECT generation FROM provider_catalog_versions WHERE version=?",
        (row["version"],),
    ).fetchone()
    if version is None or int(version["generation"]) != int(row["generation"]):
        raise ApiError("provider_catalog.integrity_failed")
    return row


def _entry(manifest: ProviderCatalogManifest, entry_code: str) -> ProviderCatalogEntry:
    for entry in manifest.entries:
        if entry.entry_code == entry_code:
            return entry
    raise ApiError("provider_catalog.entry_not_found")


def _endpoint(entry: ProviderCatalogEntry, region_code: str):
    for endpoint in entry.endpoints:
        if endpoint.region_code == region_code:
            return endpoint
    raise ApiError("provider_catalog.region_not_found")


def _endpoint_url(endpoint) -> str:
    _validate_endpoint(endpoint.host, endpoint.path_template)
    return urlunsplit(("https", endpoint.host, endpoint.path_template.rstrip("/"), "", ""))


def _validate_endpoint(host: str, path: str) -> None:
    if (
        host != host.lower()
        or host.endswith(".")
        or _HOST_RE.fullmatch(host) is None
        or host in _BLOCKED_HOSTS
        or host.endswith((".localhost", ".local", ".internal"))
        or "metadata" in host.split(".")
        or _PATH_RE.fullmatch(path) is None
        or "\\" in path
        or any(segment == ".." for segment in path.split("/"))
    ):
        raise ApiError("provider_catalog.endpoint_invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    raise ApiError("provider_catalog.endpoint_invalid")


def _verify_signature(public_key_b64: str, signature_b64: str, raw: bytes) -> None:
    try:
        public = _unb64(public_key_b64)
        signature = _unb64(signature_b64)
        if len(public) != 32 or len(signature) != 64:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(signature, raw)
    except (ValueError, binascii.Error, InvalidSignature):
        raise ApiError("provider_catalog.signature_invalid") from None


def _unb64(value: str) -> bytes:
    if "=" in value:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )


def _require_workspace_permission(context: AuthContext, permission: str) -> None:
    granted = (
        permission in context.extension_permission_codes
        if permission == "provider_catalog.read"
        else permission in context.permission_codes
    )
    if not granted:
        raise ApiError("auth.permission_denied")


def _require_platform_read(context: PlatformContext) -> None:
    if not {
        "platform.provider_catalog.manage",
        "platform.provider_catalog.security_read",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")


def _require_platform_permission(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _workspace_idempotency(
    config: AppConfig,
    owner: _WorkspaceOwner,
    actor_user_id: str,
    action: str,
    value: str,
) -> str:
    return hmac.new(
        config.auth_token_secret.encode("utf-8"),
        (
            "prd119/workspace-idempotency\0"
            f"{owner.workspace_id}\0{actor_user_id}\0{action}\0{value}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


def _platform_idempotency(config: AppConfig, operator_id: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd119/platform-idempotency\0{operator_id}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _valid_idempotency(value: str) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("provider_catalog.request_invalid")


def _optional_code(value: str | None, allowed: set[str] | None = None) -> str | None:
    if value is None:
        return None
    if _CODE_RE.fullmatch(value) is None or (allowed is not None and value not in allowed):
        raise ApiError("provider_catalog.request_invalid")
    return value


def _optional_region(value: str | None) -> str | None:
    if value is not None and _REGION_RE.fullmatch(value) is None:
        raise ApiError("provider_catalog.request_invalid")
    return value


def _outcome_code(value: str | None, fallback: str) -> str:
    return value if value is not None and _CODE_RE.fullmatch(value) is not None else fallback


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(item) for item in value.split("."))
    except ValueError:
        raise ApiError("provider_catalog.manifest_invalid") from None
    if len(parts) != 4:
        raise ApiError("provider_catalog.manifest_invalid")
    return parts  # type: ignore[return-value]


def _json_list(value: object) -> list[str]:
    parsed = json.loads(str(value))
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ValueError("invalid catalog list")
    return parsed


def _canonical_json(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 160:
        raise ApiError("provider_catalog.unavailable")
    return value


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC)


def _mapping_row(values: dict[str, object]):
    class MappingRow(dict):
        pass

    return MappingRow(values)


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("provider_catalog.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


__all__ = [
    "create_catalog_provider",
    "get_catalog_compatibility",
    "get_platform_catalog",
    "list_provider_catalog",
    "preflight_catalog_provider",
    "publish_platform_catalog",
]
