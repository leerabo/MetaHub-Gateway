"""Fenced terminal worker for PRD-147 backfill, cutover, and rollback jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from metahub_shared.db.schema import foreign_key_violation_count, table_columns, table_exists
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import PlatformJobContext

from .batches import sync_migration_batches
from .frontend_manifest import (
    FRONTEND_PRODUCTION_ASSET_SET_DIGEST,
    FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST,
)
from .manifest import DOMAIN_BY_CODE, LEGACY_ROUTE_MANIFEST, MANDATORY_RECONCILIATION_CHECKS

JOB_TYPE = "console.domain.migrate"
_RECONCILIATION_TTL = timedelta(minutes=15)
_SOURCE_EVIDENCE_TABLES = {
    "channel_attribution": "channel_migration_cursors",
    "customer_success": "platform_customer_success_migration_cursors",
    "growth_analytics": "platform_growth_migration_cursors",
    "brand_distribution": "brand_migration_cursors",
    "provider_engineering": "provider_engineering_scheduler_state",
    "commercial_accounting": "commercial_accounting_migration_cursors",
    "console_experience": "console_experience_migration_cursors",
    "workspace_navigation": "workspace_navigation_migration_cursors",
}


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    source_count: int
    target_count: int
    source_digest: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class _OwnerEvidenceTable:
    table: str
    workspace_column: str | None = "workspace_id"
    tenant_column: str | None = "tenant_id"
    owner_hash_column: str | None = None
    valid_column: str | None = None
    valid_values: frozenset[str] = frozenset()
    owner_table: str | None = None
    owner_key: str | None = None


_OWNER_EVIDENCE_TABLES = {
    "channel_attribution": (
        _OwnerEvidenceTable(
            "channel_legacy_object_mappings",
            workspace_column="owner_workspace_id",
            valid_column="result",
            valid_values=frozenset({"migrated", "sent_tombstone"}),
        ),
    ),
    "customer_success": (
        _OwnerEvidenceTable(
            "platform_customer_success_legacy_mappings",
            valid_column="status",
            valid_values=frozenset({"migrated", "sent_tombstone"}),
        ),
    ),
    "growth_analytics": (
        _OwnerEvidenceTable(
            "platform_growth_legacy_mappings",
            workspace_column="owner_workspace_id",
            valid_column="status",
            valid_values=frozenset({"mapped"}),
        ),
    ),
    "brand_distribution": (
        _OwnerEvidenceTable(
            "brand_legacy_object_mappings",
            workspace_column=None,
            valid_column="status",
            valid_values=frozenset(
                {
                    "default_collapsed",
                    "requires_assignment",
                    "historical_preserved",
                }
            ),
        ),
    ),
    "provider_engineering": (
        _OwnerEvidenceTable(
            "provider_engineering_legacy_mappings",
            workspace_column=None,
            tenant_column=None,
            owner_hash_column="legacy_owner_sha256",
            valid_column="result",
            valid_values=frozenset({"mapped"}),
        ),
    ),
    "commercial_accounting": (
        _OwnerEvidenceTable("workspace_subscription_projections"),
        _OwnerEvidenceTable("platform_invoice_workspace_bindings"),
        _OwnerEvidenceTable("billing_dispute_cases"),
        _OwnerEvidenceTable("workspace_internal_finops_disputes"),
        _OwnerEvidenceTable("workspace_quota_owner_bindings"),
        _OwnerEvidenceTable("billing_domain_reconciliation_source_snapshots"),
    ),
    "console_experience": (
        _OwnerEvidenceTable("workspace_dashboard_projection_heads"),
        _OwnerEvidenceTable("workspace_dashboard_projection_sources"),
        _OwnerEvidenceTable("workspace_domain_approval_source_cursors"),
        _OwnerEvidenceTable("workspace_assistant_query_projections"),
        _OwnerEvidenceTable(
            "account_preference_source_mappings",
            workspace_column=None,
            tenant_column="legacy_tenant_id",
        ),
        _OwnerEvidenceTable("workspace_deployment_diagnostic_heads"),
        _OwnerEvidenceTable("workspace_console_events"),
        _OwnerEvidenceTable(
            "workspace_console_event_outbox", owner_table="workspace_console_events",
            owner_key="workspace_console_event_id",
        ),
    ),
    "workspace_navigation": (
        _OwnerEvidenceTable("workspace_navigation_contract_snapshots"),
        _OwnerEvidenceTable("workspace_navigation_contract_items"),
        _OwnerEvidenceTable("workspace_navigation_projection_heads"),
        _OwnerEvidenceTable("workspace_navigation_projection_sources"),
        _OwnerEvidenceTable("workspace_role_task_home_heads"),
        _OwnerEvidenceTable(
            "workspace_role_task_home_sources", owner_table="workspace_role_task_home_versions",
            owner_key="role_task_home_version_id",
        ),
        _OwnerEvidenceTable("workspace_navigation_preferences"),
        _OwnerEvidenceTable("workspace_navigation_preference_source_mappings"),
        _OwnerEvidenceTable("workspace_advanced_tool_entitlement_heads"),
        _OwnerEvidenceTable(
            "workspace_advanced_tool_entitlement_sources",
            owner_table="workspace_advanced_tool_entitlement_versions",
            owner_key="advanced_tool_entitlement_id",
        ),
        _OwnerEvidenceTable("workspace_navigation_events"),
        _OwnerEvidenceTable(
            "workspace_navigation_event_outbox", owner_table="workspace_navigation_events",
            owner_key="workspace_navigation_event_id",
        ),
    ),
}


class ConsoleDomainMigrationJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    migration_id: str = Field(pattern=r"^cdm_[0-9a-f]{32}$")
    operation_generation: int = Field(ge=1)
    result_revision: int = Field(ge=1)
    action: Literal["backfill", "cutover", "rollback"]
    target_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_target(self) -> ConsoleDomainMigrationJobInput:
        if (self.action == "rollback") != (self.target_generation is not None):
            raise ValueError("rollback requires exactly one target generation")
        return self


def _run_console_domain_migration(
    context: PlatformJobContext,
    payload: ConsoleDomainMigrationJobInput,
) -> str:
    effect_key = (
        f"console-migration.{payload.action}.{payload.migration_id}."
        f"r{payload.result_revision}"
    )
    outcome = f"console_migration.{payload.action}_completed"

    def apply(connection: sqlite3.Connection) -> None:
        migration = connection.execute(
            "SELECT * FROM console_domain_migrations WHERE console_domain_migration_id=?",
            (payload.migration_id,),
        ).fetchone()
        if migration is None or int(migration["operation_generation"]) != (
            payload.operation_generation
        ):
            raise ApiError("console_migration.conflict")
        now = _database_now(connection)
        if payload.action == "backfill":
            _finish_backfill(connection, context.config, migration, now)
        elif payload.action == "cutover":
            _finish_cutover(connection, context.config, migration, now)
        else:
            _finish_rollback(
                connection,
                context.config,
                migration,
                int(payload.target_generation or 0),
                now,
            )

    context.commit_effect(
        effect_key,
        payload.model_dump(mode="json"),
        apply=apply,
    )
    return outcome


def _finish_backfill(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    now: datetime,
) -> None:
    state = str(migration["state"])
    if state not in {"backfill_queued", "backfilling"}:
        raise ApiError("console_migration.conflict")
    if state == "backfill_queued":
        _advance_state(connection, migration, "backfilling", now, progress=2500)
        migration = _reload(connection, migration)
    cursor = _complete_governance_cursor(connection, config, migration, now)
    _advance_state(connection, migration, "shadowing", now, progress=7500)
    migration = _reload(connection, migration)
    _refresh_reconciliation(connection, config, migration, cursor, now)
    reconciliation = _matched_reconciliation(connection, migration, now)
    if reconciliation is None:
        _advance_state(
            connection,
            migration,
            "blocked",
            now,
            progress=7500,
            error_code="console_migration.reconciliation_failed",
        )
        return
    _advance_state(
        connection,
        migration,
        "ready",
        now,
        progress=10000,
        reconciliation=reconciliation,
    )
    migration = _reload(connection, migration)
    _emit_event(
        connection,
        config,
        migration,
        "console_domain.backfilled",
        reconciliation,
        now,
    )


def _finish_cutover(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    now: datetime,
) -> None:
    if migration["state"] != "cutting_over":
        raise ApiError("console_migration.conflict")
    reconciliation = _matched_reconciliation(connection, migration, now)
    if reconciliation is None:
        _advance_state(
            connection,
            migration,
            "blocked",
            now,
            progress=int(migration["progress_basis_points"]),
            error_code="console_migration.reconciliation_failed",
        )
        return
    _advance_state(connection, migration, "validating", now, progress=9500)
    migration = _reload(connection, migration)
    target_generation = max(
        int(migration["target_generation"]) + 1,
        int(migration["operation_generation"]),
    )
    _advance_state(
        connection,
        migration,
        "active",
        now,
        progress=10000,
        target_generation=target_generation,
        completed=True,
    )
    migration = _reload(connection, migration)
    _verify_route_tombstones(connection, migration, reconciliation, now)
    _emit_event(
        connection,
        config,
        migration,
        "console_domain.cutover",
        reconciliation,
        now,
    )
    _emit_event(
        connection,
        config,
        migration,
        "legacy_route.retired",
        reconciliation,
        now,
    )


def _finish_rollback(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    target_generation: int,
    now: datetime,
) -> None:
    if migration["state"] != "rolling_back":
        raise ApiError("console_migration.conflict")
    if target_generation < 1 or target_generation >= int(migration["target_generation"]):
        raise ApiError("console_migration.rollback_unavailable")
    reconciliation = _matched_reconciliation(connection, migration, now)
    if reconciliation is None:
        _advance_state(
            connection,
            migration,
            "blocked",
            now,
            progress=int(migration["progress_basis_points"]),
            error_code="console_migration.reconciliation_failed",
        )
        return
    _advance_state(
        connection,
        migration,
        "rolled_back",
        now,
        progress=10000,
        target_generation=target_generation,
        completed=True,
    )
    migration = _reload(connection, migration)
    _emit_event(
        connection,
        config,
        migration,
        "console_domain.rolled_back",
        reconciliation,
        now,
    )


def _complete_governance_cursor(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row:
    existing = connection.execute(
        """SELECT * FROM console_backfill_cursors
           WHERE console_domain_migration_id=? ORDER BY created_at LIMIT 1""",
        (migration["console_domain_migration_id"],),
    ).fetchone()
    if existing is not None and existing["status"] == "completed":
        return existing
    table = _SOURCE_EVIDENCE_TABLES[str(migration["domain_code"])]
    evidence = _source_evidence(connection, config, table, migration)
    stamp = _db_time(now)
    workspace_id = str(migration["workspace_id"])
    tenant_id = str(migration["tenant_id"])
    migration_id = str(migration["console_domain_migration_id"])
    if existing is None:
        cursor_id = _id("cbf_")
        connection.execute(
            """INSERT INTO console_backfill_cursors(
               console_backfill_cursor_id,console_domain_migration_id,workspace_id,
               tenant_id,domain_code,mapping_code,source_table_code,
               source_schema_version,target_table_code,target_schema_version,
               source_upper_watermark_hmac,last_source_key_hmac,source_row_count,
               target_row_count,source_digest_hmac,target_digest_hmac,digest_key_version,
               owner_coverage_basis_points,foreign_key_coverage_basis_points,
               event_watermark,outbox_watermark,audit_watermark,scheduler_watermark,
               attempt_count,status,fence_generation,lease_token_hmac,lease_expires_at,
               revision,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,1,10000,10000,0,0,0,
                 (SELECT generation FROM scheduler_registry_heads WHERE head_key='production'),
                 1,'completed',1,NULL,NULL,1,?,?)""",
            (
                cursor_id,
                migration_id,
                workspace_id,
                tenant_id,
                migration["domain_code"],
                f"{migration['domain_code']}.owner-evidence-v2",
                f"{table}+owner-facts-v2",
                str(migration["source_migration_version"]),
                DOMAIN_BY_CODE[str(migration["domain_code"])].target_code,
                str(migration["target_migration_version"]),
                _secret_hmac(
                    config,
                    "source-watermark",
                    f"{table}\x1fowner-facts-v2\x1f{evidence.source_digest}",
                ),
                evidence.source_count,
                evidence.target_count,
                evidence.source_digest,
                evidence.target_digest,
                stamp,
                stamp,
            ),
        )
    else:
        cursor_id = str(existing["console_backfill_cursor_id"])
        connection.execute(
            """UPDATE console_backfill_cursors SET source_row_count=?,target_row_count=?,
                 source_digest_hmac=?,target_digest_hmac=?,attempt_count=attempt_count+1,
                 status='completed',fence_generation=fence_generation+1,
                 lease_token_hmac=NULL,lease_expires_at=NULL,revision=revision+1,
                 updated_at=? WHERE console_backfill_cursor_id=?""",
            (
                evidence.source_count,
                evidence.target_count,
                evidence.source_digest,
                evidence.target_digest,
                stamp,
                cursor_id,
            ),
        )
    attempt_number = int(
        connection.execute(
            "SELECT attempt_count FROM console_backfill_cursors WHERE console_backfill_cursor_id=?",
            (cursor_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO console_backfill_attempts(
           console_backfill_attempt_id,console_backfill_cursor_id,workspace_id,tenant_id,
           attempt_number,outcome_code,started_at,completed_at)
           VALUES(?,?,?,?,?,'console_migration.backfill_completed',?,?)""",
        (_id("cba_"), cursor_id, workspace_id, tenant_id, attempt_number, stamp, stamp),
    )
    sequence = int(
        connection.execute(
            """SELECT coalesce(max(sequence_number),0)+1 FROM console_backfill_checkpoints
               WHERE console_backfill_cursor_id=?""",
            (cursor_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO console_backfill_checkpoints(
           console_backfill_checkpoint_id,console_backfill_cursor_id,workspace_id,tenant_id,
           sequence_number,source_key_hmac,row_count,byte_count,fence_generation,committed_at)
           SELECT ?,console_backfill_cursor_id,workspace_id,tenant_id,?,?,?,0,
             fence_generation,? FROM console_backfill_cursors
           WHERE console_backfill_cursor_id=?""",
        (
            _id("cbc_"),
            sequence,
            evidence.target_digest,
            evidence.target_count,
            stamp,
            cursor_id,
        ),
    )
    return connection.execute(
        "SELECT * FROM console_backfill_cursors WHERE console_backfill_cursor_id=?",
        (cursor_id,),
    ).fetchone()


def _source_evidence(
    connection: sqlite3.Connection,
    config: AppConfig,
    table: str,
    migration: sqlite3.Row,
) -> _SourceEvidence:
    if not table_exists(connection, table):
        raise ApiError("console_migration.contract_not_ready")
    columns = table_columns(connection, table)
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
    batch_rows = [
        (table, tuple((column, _evidence_cell(row[column])) for column in columns))
        for row in rows
    ]
    owner_rows, valid_owner_rows = _owner_evidence_rows(
        connection,
        str(migration["domain_code"]),
        str(migration["workspace_id"]),
        str(migration["tenant_id"]),
    )
    source_payload = {
        "batch_cursor": batch_rows,
        "domain": str(migration["domain_code"]),
        "owner": (str(migration["workspace_id"]), str(migration["tenant_id"])),
        "owner_facts": owner_rows,
        "version": 2,
    }
    target_payload = {
        **source_payload,
        "owner_facts": valid_owner_rows,
    }
    return _SourceEvidence(
        source_count=len(owner_rows),
        target_count=len(valid_owner_rows),
        source_digest=_secret_hmac(config, "domain-evidence", _json(source_payload)),
        target_digest=_secret_hmac(config, "domain-evidence", _json(target_payload)),
    )


def _owner_evidence_rows(
    connection: sqlite3.Connection,
    domain_code: str,
    workspace_id: str,
    tenant_id: str,
) -> tuple[list[object], list[object]]:
    specs = _OWNER_EVIDENCE_TABLES.get(domain_code)
    if specs is None:
        raise ApiError("console_migration.contract_not_ready")
    owner_binding = connection.execute(
        """SELECT workspace_id,tenant_id,status FROM workspace_tenant_bindings
           WHERE workspace_id=? AND tenant_id=? AND status='active'""",
        (workspace_id, tenant_id),
    ).fetchone()
    if owner_binding is None:
        raise ApiError("console_migration.owner_mismatch")
    source_rows: list[object] = [
        ("workspace_tenant_bindings", workspace_id, tenant_id, "active")
    ]
    target_rows = list(source_rows)
    for spec in specs:
        if not table_exists(connection, spec.table):
            raise ApiError("console_migration.contract_not_ready")
        columns = table_columns(connection, spec.table)
        owner_columns = columns
        owner_alias = "evidence"
        source = f'"{spec.table}" evidence'
        if spec.owner_table is not None:
            if spec.owner_key not in columns or not table_exists(connection, spec.owner_table):
                raise ApiError("console_migration.contract_not_ready")
            owner_columns = table_columns(connection, spec.owner_table)
            if spec.owner_key not in owner_columns:
                raise ApiError("console_migration.contract_not_ready")
            source += (
                f' JOIN "{spec.owner_table}" owner'
                f' ON owner."{spec.owner_key}"=evidence."{spec.owner_key}"'
            )
            owner_alias = "owner"
        required = (spec.workspace_column, spec.tenant_column, spec.owner_hash_column)
        if any(column is not None and column not in owner_columns for column in required):
            raise ApiError("console_migration.contract_not_ready")
        if spec.valid_column is not None and spec.valid_column not in columns:
            raise ApiError("console_migration.contract_not_ready")
        predicates: list[str] = []
        parameters: list[object] = []
        if spec.workspace_column is not None:
            predicates.append(f'{owner_alias}."{spec.workspace_column}"=?')
            parameters.append(workspace_id)
        if spec.tenant_column is not None:
            predicates.append(f'{owner_alias}."{spec.tenant_column}"=?')
            parameters.append(tenant_id)
        if spec.owner_hash_column is not None:
            predicates.append(f'{owner_alias}."{spec.owner_hash_column}"=metahub_sha256(?)')
            parameters.append(tenant_id)
        if not predicates:
            raise ApiError("console_migration.contract_not_ready")
        ordering = ",".join(f'evidence."{column}"' for column in columns)
        rows = connection.execute(
            f'SELECT evidence.* FROM {source} WHERE {" AND ".join(predicates)} ORDER BY {ordering}',
            parameters,
        ).fetchall()
        for row in rows:
            fact = (
                spec.table,
                tuple((column, _evidence_cell(row[column])) for column in columns),
            )
            source_rows.append(fact)
            if spec.valid_column is None or str(row[spec.valid_column]) in spec.valid_values:
                target_rows.append(fact)
    return source_rows, target_rows


def _evidence_cell(value: object) -> object:
    if isinstance(value, bytes):
        return ("blob", len(value), hashlib.sha256(value).hexdigest())
    return value


def _refresh_reconciliation(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    cursor: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row:
    """Measure and persist the complete cutover profile for one migration generation."""

    facts = _reconciliation_facts(connection, cursor)
    static_ready = _static_contract_ready(connection)
    source_matches = facts["source_count"] == facts["target_count"]
    digest_matches = hmac.compare_digest(facts["source_digest"], facts["target_digest"])
    checks: dict[str, tuple[int, int]] = {
        "source_target_rows": (facts["source_count"], facts["target_count"]),
        "canonical_digest": (1, int(digest_matches)),
        "owner_coverage": (1, facts["owner_binding_count"]),
        "foreign_keys": (0, facts["foreign_key_violation_count"]),
        "event_outbox": (facts["event_watermark"], facts["outbox_watermark"]),
        "audit_watermark": (facts["audit_watermark"], facts["audit_watermark"]),
        "scheduler_watermark": (
            facts["registry_generation"],
            facts["scheduler_watermark"],
        ),
        "legacy_write_fence": (1, facts["ingress_contract_count"]),
        "route_registration_zero": (1, int(static_ready)),
        "openapi_zero": (1, int(static_ready)),
        "frontend_runtime_zero": (1, int(static_ready)),
        "prefetch_zero": (1, int(static_ready)),
        "permission_retirement": (1, int(static_ready)),
        "navigation_64_plus_4": (1, facts["navigation_contract_count"]),
        "cross_domain_negative": (1, int(static_ready)),
        "target_smoke": (1, int(source_matches and digest_matches)),
        "n_minus_one_fenced": (1, facts["route_deny_count"]),
        "late_writer_fenced": (1, facts["scheduler_guard_count"]),
        "static_sensitive_zero": (1, int(static_ready)),
    }
    if set(checks) != set(MANDATORY_RECONCILIATION_CHECKS):
        raise ApiError("console_migration.contract_not_ready")
    matched = all(expected == observed for expected, observed in checks.values())
    reconciliation_id = "drc_" + hashlib.sha256(
        (
            f"prd147/reconciliation/{migration['console_domain_migration_id']}/"
            f"{migration['operation_generation']}"
        ).encode("ascii")
    ).hexdigest()[:32]
    expected_digest = _secret_hmac(
        config,
        "reconciliation-expected",
        _json([(code, expected) for code, (expected, _observed) in sorted(checks.items())]),
    )
    observed_digest = (
        expected_digest
        if matched
        else _secret_hmac(
            config,
            "reconciliation-observed",
            _json(
                [(code, observed) for code, (_expected, observed) in sorted(checks.items())]
            ),
        )
    )
    stamp = _db_time(now)
    existing = connection.execute(
        """SELECT domain_reconciliation_id,revision
           FROM workspace_domain_reconciliations
           WHERE workspace_id=? AND tenant_id=? AND domain_code=?
             AND operation_generation=?""",
        (
            migration["workspace_id"],
            migration["tenant_id"],
            migration["domain_code"],
            migration["operation_generation"],
        ),
    ).fetchone()
    revision = 1 if existing is None else int(existing["revision"]) + 1
    if existing is None:
        connection.execute(
            """INSERT INTO workspace_domain_reconciliations(
               domain_reconciliation_id,console_domain_migration_id,workspace_id,tenant_id,
               domain_code,operation_generation,source_watermark_hmac,target_watermark_hmac,
               contract_release_generation,check_profile,check_profile_version,status,
               reason_code,expected_count,observed_count,expected_digest_hmac,
               observed_digest_hmac,coverage_basis_points,revision,checked_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,'console-domain-cutover-v1',1,?,?,?,?,?,?,?,?,?,?)""",
            (
                reconciliation_id,
                migration["console_domain_migration_id"],
                migration["workspace_id"],
                migration["tenant_id"],
                migration["domain_code"],
                migration["operation_generation"],
                cursor["source_upper_watermark_hmac"],
                facts["target_digest"],
                facts["contract_release_generation"],
                "matched" if matched else "mismatched",
                "all_checks_matched" if matched else "mandatory_check_mismatch",
                facts["source_count"],
                facts["target_count"],
                expected_digest,
                observed_digest,
                10000 if matched else 0,
                revision,
                stamp,
                stamp,
            ),
        )
    else:
        reconciliation_id = str(existing["domain_reconciliation_id"])
        connection.execute(
            """UPDATE workspace_domain_reconciliations SET
               source_watermark_hmac=?,target_watermark_hmac=?,
               contract_release_generation=?,status=?,reason_code=?,expected_count=?,
               observed_count=?,expected_digest_hmac=?,observed_digest_hmac=?,
               coverage_basis_points=?,revision=?,checked_at=?
               WHERE domain_reconciliation_id=?""",
            (
                cursor["source_upper_watermark_hmac"],
                facts["target_digest"],
                facts["contract_release_generation"],
                "matched" if matched else "mismatched",
                "all_checks_matched" if matched else "mandatory_check_mismatch",
                facts["source_count"],
                facts["target_count"],
                expected_digest,
                observed_digest,
                10000 if matched else 0,
                revision,
                stamp,
                reconciliation_id,
            ),
        )
        connection.execute(
            "DELETE FROM workspace_domain_reconciliation_checks WHERE domain_reconciliation_id=?",
            (reconciliation_id,),
        )
    for check_type, (expected, observed) in sorted(checks.items()):
        status = "matched" if expected == observed else "mismatched"
        artifact_digest = _secret_hmac(
            config,
            "reconciliation-artifact",
            _json(
                {
                    "check_type": check_type,
                    "expected": expected,
                    "frontend_manifest": FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST,
                    "observed": observed,
                    "route_manifest": facts["route_manifest_digest"],
                    "source_digest": facts["source_digest"],
                    "target_digest": facts["target_digest"],
                }
            ),
        )
        retention = (
            "security"
            if check_type
            in {
                "cross_domain_negative",
                "n_minus_one_fenced",
                "late_writer_fenced",
                "static_sensitive_zero",
            }
            else "operations"
            if check_type in {"event_outbox", "audit_watermark", "scheduler_watermark"}
            else "release"
        )
        connection.execute(
            """INSERT INTO workspace_domain_reconciliation_checks(
               domain_reconciliation_check_id,domain_reconciliation_id,workspace_id,tenant_id,
               operation_generation,check_type,status,reason_code,expected_count,
               observed_count,artifact_kind,artifact_digest,tool_version,retention_class,
               checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _id("dck_"),
                reconciliation_id,
                migration["workspace_id"],
                migration["tenant_id"],
                migration["operation_generation"],
                check_type,
                status,
                "evidence_matched" if status == "matched" else "evidence_mismatched",
                expected,
                observed,
                "structured-proof",
                artifact_digest,
                "prd147-reconciler-v1",
                retention,
                stamp,
            ),
        )
    row = connection.execute(
        "SELECT * FROM workspace_domain_reconciliations WHERE domain_reconciliation_id=?",
        (reconciliation_id,),
    ).fetchone()
    if row is None:
        raise ApiError("console_migration.reconciliation_failed")
    return row


def _reconciliation_facts(
    connection: sqlite3.Connection, cursor: sqlite3.Row
) -> dict[str, int | str]:
    migration_id = str(cursor["console_domain_migration_id"])
    contract = connection.execute(
        """SELECT route_deny_epoch,contract_generation,route_manifest_digest
           FROM console_ingress_contract_heads WHERE head_key='production'"""
    ).fetchone()
    registry = connection.execute(
        "SELECT generation FROM scheduler_registry_heads WHERE head_key='production'"
    ).fetchone()
    release = connection.execute(
        "SELECT generation FROM console_contract_release_heads WHERE head_key='production'"
    ).fetchone()
    foreign_key_violations = foreign_key_violation_count(connection)
    owner_binding_count = int(
        connection.execute(
            """SELECT count(*) FROM workspace_tenant_bindings
               WHERE workspace_id=? AND tenant_id=?""",
            (cursor["workspace_id"], cursor["tenant_id"]),
        ).fetchone()[0]
    )
    navigation_contract_count = _navigation_contract_count(
        connection, str(cursor["workspace_id"]), str(cursor["tenant_id"])
    )
    audit_watermark = int(
        connection.execute(
            """SELECT count(*) FROM platform_console_migration_audits
               WHERE owner_ref IS NULL OR length(owner_ref)=64"""
        ).fetchone()[0]
    )
    scheduler_generation = 0 if registry is None else int(registry["generation"])
    return {
        "source_count": int(cursor["source_row_count"]),
        "target_count": int(cursor["target_row_count"]),
        "source_digest": str(cursor["source_digest_hmac"]),
        "target_digest": str(cursor["target_digest_hmac"]),
        "owner_binding_count": owner_binding_count,
        "foreign_key_violation_count": foreign_key_violations,
        "event_watermark": int(cursor["event_watermark"]),
        "outbox_watermark": int(cursor["outbox_watermark"]),
        "audit_watermark": max(int(cursor["audit_watermark"]), audit_watermark),
        "registry_generation": scheduler_generation,
        "scheduler_watermark": int(cursor["scheduler_watermark"]),
        "ingress_contract_count": int(contract is not None),
        "navigation_contract_count": navigation_contract_count,
        "route_deny_count": int(contract is not None and int(contract["route_deny_epoch"]) >= 1),
        "scheduler_guard_count": int(registry is not None),
        "contract_release_generation": 0 if release is None else int(release["generation"]),
        "route_manifest_digest": "" if contract is None else str(contract["route_manifest_digest"]),
        "migration_id": migration_id,
    }


def _navigation_contract_count(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> int:
    row = connection.execute(
        """SELECT snapshot.item_count,
                  count(item.navigation_contract_item_id) AS actual_count,
                  count(DISTINCT item.source_navigation_id) AS unique_count,
                  sum(CASE WHEN item.source_kind='excel' THEN 1 ELSE 0 END) AS excel_count,
                  sum(CASE WHEN item.source_kind='extra' THEN 1 ELSE 0 END) AS extra_count
           FROM workspace_navigation_contract_heads head
           JOIN workspace_navigation_contract_snapshots snapshot
             ON snapshot.navigation_contract_snapshot_id=head.navigation_contract_snapshot_id
            AND snapshot.workspace_id=head.workspace_id
            AND snapshot.tenant_id=head.tenant_id
            AND snapshot.generation=head.generation
           LEFT JOIN workspace_navigation_contract_items item
             ON item.navigation_contract_snapshot_id=snapshot.navigation_contract_snapshot_id
            AND item.workspace_id=snapshot.workspace_id
            AND item.tenant_id=snapshot.tenant_id
            AND item.contract_generation=snapshot.generation
           WHERE head.workspace_id=? AND head.tenant_id=? AND snapshot.status='active'
           GROUP BY snapshot.navigation_contract_snapshot_id,snapshot.item_count""",
        (workspace_id, tenant_id),
    ).fetchone()
    return int(
        row is not None
        and int(row["item_count"]) == 68
        and int(row["actual_count"]) == 68
        and int(row["unique_count"]) == 68
        and int(row["excel_count"]) == 64
        and int(row["extra_count"]) == 4
    )


def _static_contract_ready(connection: sqlite3.Connection) -> bool:
    catalog = {
        (str(row["method"]), str(row["normalized_path"]))
        for row in connection.execute(
            "SELECT method,normalized_path FROM legacy_route_catalog"
        ).fetchall()
    }
    roles = {
        (str(row["role_code"]), str(row["permission_code"]))
        for row in connection.execute(
            "SELECT role_code,permission_code FROM platform_console_migration_role_permissions"
        ).fetchall()
    }
    expected_roles = {
        ("platform_owner", "platform.console_migration.read"),
        ("platform_owner", "platform.console_migration.approve"),
        ("platform_ops", "platform.console_migration.read"),
        ("platform_ops", "platform.console_migration.backfill"),
        ("platform_ops", "platform.console_migration.execute"),
        ("platform_security", "platform.console_migration.read"),
        ("platform_security", "platform.console_migration.security_review"),
    }
    return (
        catalog == set(LEGACY_ROUTE_MANIFEST)
        and roles == expected_roles
        and len(FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST) == 64
        and len(FRONTEND_PRODUCTION_ASSET_SET_DIGEST) == 64
    )


def _matched_reconciliation(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row | None:
    row = connection.execute(
        """SELECT * FROM workspace_domain_reconciliations
           WHERE console_domain_migration_id=? AND operation_generation=?
             AND check_profile='console-domain-cutover-v1' AND check_profile_version=1
             AND status='matched' AND expected_count=observed_count
             AND coverage_basis_points=10000 AND checked_at>?
           ORDER BY revision DESC LIMIT 1""",
        (
            migration["console_domain_migration_id"],
            migration["operation_generation"],
            _db_time(now - _RECONCILIATION_TTL),
        ),
    ).fetchone()
    if row is None or not hmac.compare_digest(
        str(row["expected_digest_hmac"]), str(row["observed_digest_hmac"])
    ):
        return None
    checks = connection.execute(
        """SELECT check_type,status,expected_count,observed_count,checked_at
           FROM workspace_domain_reconciliation_checks
           WHERE domain_reconciliation_id=?""",
        (row["domain_reconciliation_id"],),
    ).fetchall()
    return row if (
        {str(check["check_type"]) for check in checks}
        == set(MANDATORY_RECONCILIATION_CHECKS)
        and all(
            check["status"] == "matched"
            and int(check["expected_count"]) == int(check["observed_count"])
            and _parse_time(check["checked_at"]) > now - _RECONCILIATION_TTL
            for check in checks
        )
    ) else None


def _verify_route_tombstones(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    reconciliation: sqlite3.Row,
    now: datetime,
) -> None:
    release = connection.execute(
        "SELECT generation FROM console_contract_release_heads WHERE head_key='production'"
    ).fetchone()
    if release is None:
        raise ApiError("console_migration.contract_not_ready")
    generation = int(release["generation"])
    digest = str(reconciliation["expected_digest_hmac"])
    stamp = _db_time(now)
    for route in connection.execute(
        "SELECT legacy_route_id,owner_domain FROM legacy_route_catalog ORDER BY legacy_route_id"
    ).fetchall():
        route_id = str(route["legacy_route_id"])
        connection.execute(
            """INSERT INTO workspace_legacy_route_tombstones(
               workspace_legacy_route_tombstone_id,workspace_id,tenant_id,legacy_route_id,
               contract_generation,status,zero_check_digest,revision,created_at,updated_at)
               VALUES(?,?,?,?,?,'planned',NULL,1,?,?) ON CONFLICT DO NOTHING""",
            (
                _id("wlt_"),
                migration["workspace_id"],
                migration["tenant_id"],
                route_id,
                generation,
                stamp,
                stamp,
            ),
        )
        _advance_tombstone(
            connection,
            "workspace_legacy_route_tombstones",
            "workspace_id=? AND tenant_id=? AND legacy_route_id=? AND contract_generation=?",
            (migration["workspace_id"], migration["tenant_id"], route_id, generation),
            digest,
            stamp,
        )
        if route["owner_domain"] == "platform":
            connection.execute(
                """INSERT INTO platform_legacy_route_retirements(
                   platform_legacy_route_retirement_id,legacy_route_id,contract_generation,
                   status,zero_check_digest,revision,created_at,updated_at)
                   VALUES(?,?,?,'planned',NULL,1,?,?) ON CONFLICT DO NOTHING""",
                (_id("plt_"), route_id, generation, stamp, stamp),
            )
            _advance_tombstone(
                connection,
                "platform_legacy_route_retirements",
                "legacy_route_id=? AND contract_generation=?",
                (route_id, generation),
                digest,
                stamp,
            )


def _advance_tombstone(
    connection: sqlite3.Connection,
    table: str,
    predicate: str,
    parameters: tuple[object, ...],
    digest: str,
    stamp: str,
) -> None:
    current = connection.execute(
        f"SELECT status FROM {table} WHERE {predicate}", parameters
    ).fetchone()
    if current is None:
        raise ApiError("console_migration.contract_not_ready")
    states = ("planned", "write_fenced", "router_removed", "verified")
    index = states.index(str(current["status"]))
    for target in states[index + 1 :]:
        connection.execute(
            f"""UPDATE {table} SET status=?,zero_check_digest=?,revision=revision+1,
                 updated_at=? WHERE {predicate}""",
            (target, digest, stamp, *parameters),
        )


def _emit_event(
    connection: sqlite3.Connection,
    config: AppConfig,
    migration: sqlite3.Row,
    event_type: str,
    reconciliation: sqlite3.Row,
    now: datetime,
) -> None:
    aggregate_ref = _secret_hmac(
        config,
        "workspace",
        f"{migration['workspace_id']}\x1f{migration['tenant_id']}",
    )
    contract = connection.execute(
        """SELECT contract_generation FROM console_ingress_contract_heads
           WHERE head_key='production'"""
    ).fetchone()
    if contract is None:
        raise ApiError("console_migration.contract_not_ready")
    event_id = _id("cme_")
    payload = {
        "aggregate_ref": aggregate_ref,
        "contract_generation": int(contract["contract_generation"]),
        "domain_code": str(migration["domain_code"]),
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": _db_time(now),
        "operation_generation": int(migration["operation_generation"]),
        "reconciliation_digest": str(reconciliation["expected_digest_hmac"]),
        "schema_version": 1,
    }
    inserted = connection.execute(
        """INSERT INTO platform_console_migration_events(
           platform_console_migration_event_id,workspace_id,tenant_id,aggregate_ref,
           event_type,schema_version,domain_code,operation_generation,contract_generation,
           reconciliation_digest,payload_json,occurred_at)
           VALUES(?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
        (
            event_id,
            migration["workspace_id"],
            migration["tenant_id"],
            aggregate_ref,
            event_type,
            migration["domain_code"],
            migration["operation_generation"],
            contract["contract_generation"],
            reconciliation["expected_digest_hmac"],
            _json(payload),
            _db_time(now),
        ),
    ).rowcount
    if inserted == 1:
        connection.execute(
            """INSERT INTO platform_console_migration_event_outbox(
               platform_console_migration_event_outbox_id,
               platform_console_migration_event_id,status,attempt_count,next_attempt_at,
               lease_owner_id,lease_generation,lease_token_hmac,lease_expires_at,
               dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
            (_id("cmo_"), event_id, _db_time(now), _db_time(now), _db_time(now)),
        )


def _advance_state(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    target: str,
    now: datetime,
    *,
    progress: int,
    error_code: str | None = None,
    reconciliation: sqlite3.Row | None = None,
    target_generation: int | None = None,
    completed: bool = False,
) -> None:
    updated = connection.execute(
        """UPDATE console_domain_migrations SET state=?,progress_basis_points=?,
             reconciliation_id=coalesce(?,reconciliation_id),
             reconciliation_digest=coalesce(?,reconciliation_digest),
             target_generation=coalesce(?,target_generation),last_error_code=?,
             revision=revision+1,updated_at=?,completed_at=?
           WHERE console_domain_migration_id=? AND revision=?""",
        (
            target,
            progress,
            None if reconciliation is None else reconciliation["domain_reconciliation_id"],
            None if reconciliation is None else reconciliation["expected_digest_hmac"],
            target_generation,
            error_code,
            _db_time(now),
            _db_time(now) if completed else None,
            migration["console_domain_migration_id"],
            migration["revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("console_migration.conflict")
    sync_migration_batches(
        connection,
        str(migration["console_domain_migration_id"]),
        target,
        now,
    )


def _reload(connection: sqlite3.Connection, migration: sqlite3.Row) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM console_domain_migrations WHERE console_domain_migration_id=?",
        (migration["console_domain_migration_id"],),
    ).fetchone()
    if row is None:
        raise ApiError("console_migration.not_found")
    return row


def _database_now(connection: sqlite3.Connection) -> datetime:
    value = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()[0]
    return _parse_time(value)


def _secret_hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode(),
        f"prd147/{purpose}/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


register_job_handler(
    JOB_TYPE,
    input_model=ConsoleDomainMigrationJobInput,
    handler=_run_console_domain_migration,
)


__all__ = ["ConsoleDomainMigrationJobInput", "JOB_TYPE"]
