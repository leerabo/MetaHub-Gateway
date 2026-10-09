"""Authoritative Workspace onboarding evaluator and append-only mutations."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import decrypt_provider_key
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    OnboardingStepId,
    OnboardingStepSource,
    OnboardingStepStatus,
    WorkspaceOnboardingResponse,
    WorkspaceOnboardingStep,
)

logger = logging.getLogger("metahub.control.workspace_onboarding")

_STEP_IDS: tuple[OnboardingStepId, ...] = (
    "workspace_profile",
    "environment",
    "provider",
    "model",
    "model_alias",
    "project_application",
    "virtual_key",
    "first_call",
)
_STEP_META = {
    "workspace_profile": ("确认空间资料", "confirm", "#workspace-profile"),
    "environment": ("准备开发环境", "confirm", "#workspace-environments"),
    "provider": ("接入模型服务", "configure", "#workspace-providers"),
    "model": ("添加可用模型", "configure", "#workspace-models"),
    "model_alias": ("配置模型别名", "configure", "#workspace-model-aliases"),
    "project_application": ("建立业务归属", "configure", "#workspace-projects"),
    "virtual_key": ("创建调用凭据", "configure", "#workspace-virtual-keys"),
    "first_call": ("完成首次调用", "recheck", "#workspace-first-call"),
}
_EVIDENCE_KIND = {
    "workspace_profile": "profile_confirmation",
    "environment": "environment_confirmation",
    "provider": "provider_probe",
    "model": "model_binding",
    "model_alias": "alias_binding",
    "project_application": "project_application_binding",
    "virtual_key": "virtual_key_binding",
    "first_call": "gateway_usage",
}
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ACTION_REASON_CODES = {
    "confirm": frozenset({"user_confirmed"}),
    "defer": frozenset({"do_later"}),
    "reopen": frozenset({"configuration_changed", "retry_required"}),
}


@dataclass(frozen=True, slots=True)
class _ResourceFact:
    complete: bool
    candidate_ready: bool
    source: OnboardingStepSource
    checked_value: str
    resource_ids: tuple[str, ...] = ()
    updated_at_by_id: tuple[tuple[str, datetime], ...] = ()


@dataclass(frozen=True, slots=True)
class _EvaluatedStep:
    step_id: OnboardingStepId
    status: OnboardingStepStatus
    source: OnboardingStepSource
    evidence_kind: str
    evidence_sha256: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class _Evaluation:
    workspace_kind: Literal["organization"]
    steps: tuple[_EvaluatedStep, ...]
    completed_steps: int
    bitset: int
    projection_sha256: str
    evaluated_at: datetime


def onboarding_schema_available_in_transaction(connection: sqlite3.Connection) -> bool:
    """Return whether the complete 0089 projection capability is installed."""

    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name IN("
            "'onboarding_progress','onboarding_step_facts','onboarding_acknowledgements',"
            "'activation_checkpoints','onboarding_events','onboarding_event_outbox')"
        )
    }
    return len(names) == 6


def get_workspace_onboarding(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceOnboardingResponse:
    """Evaluate one selected Workspace without changing projection or emitting events."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            touch=True,
        )
        _require_schema(connection)
        progress = _progress_row(connection, context.workspace_id)
        evaluation = _evaluate(
            connection, context.workspace_id, context.tenant_id,
            encryption_secret=config.provider_key_encryption_secret,
        )
        connection.commit()
        return _response(evaluation, progress, request_id=request_id, trace_id=trace_id)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.unavailable") from None
    finally:
        connection.close()


def get_legacy_workspace_onboarding(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> WorkspaceOnboardingResponse | None:
    """Evaluate the bound Workspace for the N/N-1 Legacy Bearer adapter."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        if not onboarding_schema_available_in_transaction(connection):
            connection.rollback()
            return None
        binding = connection.execute(
            """
            SELECT binding.workspace_id
            FROM workspace_tenant_bindings binding
            JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
            WHERE binding.tenant_id=? AND binding.status='active'
              AND workspace.status='active'
            """,
            (context.user.tenant_id,),
        ).fetchone()
        if binding is None:
            raise ApiError("onboarding.unavailable")
        progress = _progress_row(connection, binding["workspace_id"])
        evaluation = _evaluate(
            connection,
            binding["workspace_id"],
            context.user.tenant_id,
            encryption_secret=config.provider_key_encryption_secret,
        )
        connection.commit()
        return _response(evaluation, progress, request_id=request_id, trace_id=trace_id)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.unavailable") from None
    finally:
        connection.close()


def mutate_workspace_onboarding_step(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_id: str,
    action: str,
    reason_code: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceOnboardingResponse:
    """Append one acknowledgement, re-evaluate, and atomically persist the projection."""

    normalized_step = _step_id(step_id)
    if action not in _ACTION_REASON_CODES or reason_code not in _ACTION_REASON_CODES[action]:
        raise ApiError("onboarding.request_invalid")
    _validate_idempotency(idempotency_key)
    request_sha256 = _request_hash(
        {"action": action, "reason_code": reason_code, "step_id": normalized_step}
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_schema(connection)
        replay = _acknowledgement_replay(
            connection,
            config,
            context,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            evaluation = _evaluate(
                connection, context.workspace_id, context.tenant_id,
                encryption_secret=config.provider_key_encryption_secret,
            )
            progress = _progress_row(connection, context.workspace_id)
            connection.commit()
            return _response(evaluation, progress, request_id=request_id, trace_id=trace_id)

        progress = _progress_row(connection, context.workspace_id)
        if int(progress["revision"]) != expected_revision:
            raise ApiError("onboarding.revision_conflict")
        current = _evaluate(
            connection, context.workspace_id, context.tenant_id,
            encryption_secret=config.provider_key_encryption_secret,
        )
        _authorize_step_action(context, current, normalized_step, action)
        if action == "confirm" and not _confirmation_candidate_ready(
            connection,
            context.workspace_id,
            context.tenant_id,
            current.workspace_kind,
            normalized_step,
        ):
            raise ApiError("onboarding.action_invalid")
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO onboarding_acknowledgements(
              onboarding_acknowledgement_id,workspace_id,tenant_id,actor_membership_id,
              actor_account_id,membership_revision,step_id,action,reason_code,
              idempotency_key_hmac,request_sha256,result_revision,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("ona_"),
                context.workspace_id,
                context.tenant_id,
                context.membership_id,
                context.account_id,
                _membership_revision(connection, context),
                normalized_step,
                action,
                reason_code,
                _idempotency_hmac(config, idempotency_key),
                request_sha256,
                expected_revision + 1,
                _db_time(now),
            ),
        )
        evaluation = _evaluate(
            connection, context.workspace_id, context.tenant_id, now=now,
            encryption_secret=config.provider_key_encryption_secret,
        )
        updated = _persist_evaluation(
            connection,
            config,
            context,
            old_progress=progress,
            evaluation=evaluation,
            trigger_source="step_action",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        _log_projection(context, updated, normalized_step, action, request_id, trace_id)
        return _response(evaluation, updated, request_id=request_id, trace_id=trace_id)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.unavailable") from None
    finally:
        connection.close()


def recheck_workspace_onboarding(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceOnboardingResponse:
    """Persist a fresh eight-step checkpoint and activation transition."""

    _validate_idempotency(idempotency_key)
    request_sha256 = _request_hash({"action": "recheck"})
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_schema(connection)
        replay = _checkpoint_replay(
            connection,
            config,
            context,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            evaluation = _evaluate(
                connection, context.workspace_id, context.tenant_id,
                encryption_secret=config.provider_key_encryption_secret,
            )
            progress = _progress_row(connection, context.workspace_id)
            connection.commit()
            return _response(evaluation, progress, request_id=request_id, trace_id=trace_id)
        progress = _progress_row(connection, context.workspace_id)
        if int(progress["revision"]) != expected_revision:
            raise ApiError("onboarding.revision_conflict")
        evaluation = _evaluate(
            connection, context.workspace_id, context.tenant_id,
            encryption_secret=config.provider_key_encryption_secret,
        )
        updated = _persist_evaluation(
            connection,
            config,
            context,
            old_progress=progress,
            evaluation=evaluation,
            trigger_source="manual_recheck",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        _log_projection(context, updated, None, "recheck", request_id, trace_id)
        return _response(evaluation, updated, request_id=request_id, trace_id=trace_id)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("onboarding.unavailable") from None
    finally:
        connection.close()


def _evaluate(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    *,
    now: datetime | None = None,
    encryption_secret: str | None = None,
) -> _Evaluation:
    checked_at = now or _utcnow()
    workspace = connection.execute(
        "SELECT kind,created_at FROM workspaces WHERE workspace_id=? AND status='active'",
        (workspace_id,),
    ).fetchone()
    if workspace is None:
        raise ApiError("onboarding.unavailable")
    persisted = {
        str(row["step_id"]): row
        for row in connection.execute(
            "SELECT * FROM onboarding_step_facts WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
    }
    if set(persisted) != set(_STEP_IDS):
        raise ApiError("onboarding.unavailable")

    profile = _profile_fact(connection, workspace_id, tenant_id, str(workspace["kind"]))
    environment = _environment_fact(connection, workspace_id, tenant_id)
    provider = _provider_fact(
        connection, workspace_id, tenant_id, encryption_secret=encryption_secret,
    )
    model = _model_fact(connection, workspace_id, tenant_id, provider)
    alias = _alias_fact(connection, workspace_id, tenant_id, environment, model)
    project_application = _project_application_fact(connection, workspace_id, tenant_id)
    virtual_key = _virtual_key_fact(
        connection,
        workspace_id,
        tenant_id,
        environment,
        project_application,
    )
    first_call = _first_call_fact(
        connection,
        tenant_id,
        _parse_time(workspace["created_at"]),
        provider,
        model,
        alias,
        project_application,
        virtual_key,
    )
    resources = {
        "workspace_profile": profile,
        "environment": environment,
        "provider": provider,
        "model": model,
        "model_alias": alias,
        "project_application": project_application,
        "virtual_key": virtual_key,
        "first_call": first_call,
    }
    steps: list[_EvaluatedStep] = []
    prerequisites_complete = True
    bitset = 0
    for index, step_id in enumerate(_STEP_IDS):
        fact = resources[step_id]
        previous = persisted[step_id]
        latest_ack = _latest_ack_action(connection, workspace_id, step_id)
        if fact.complete:
            status: OnboardingStepStatus = "complete"
            bitset |= 1 << index
        elif latest_ack == "reopen":
            status = "current" if prerequisites_complete else "blocked"
        elif previous["status"] == "complete":
            status = "needs_attention"
        elif latest_ack == "defer" and prerequisites_complete:
            status = "deferred"
        elif prerequisites_complete:
            status = "current"
        else:
            status = "blocked"
        source = fact.source
        if fact.complete and step_id in {"workspace_profile", "environment"}:
            source = "user_confirmed"
        steps.append(
            _EvaluatedStep(
                step_id=step_id,
                status=status,
                source=source,
                evidence_kind=_EVIDENCE_KIND[step_id],
                evidence_sha256=_evidence_hash(step_id, fact, status),
                checked_at=checked_at,
            )
        )
        prerequisites_complete = prerequisites_complete and fact.complete
    completed = bitset.bit_count()
    projection = _projection_hash(str(workspace["kind"]), steps, bitset)
    return _Evaluation(
        workspace_kind=str(workspace["kind"]),  # type: ignore[arg-type]
        steps=tuple(steps),
        completed_steps=completed,
        bitset=bitset,
        projection_sha256=projection,
        evaluated_at=checked_at,
    )


def _profile_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    workspace_kind: str,
) -> _ResourceFact:
    ready = connection.execute(
        """
        SELECT EXISTS(
          SELECT 1 FROM organization_profiles profile
          WHERE profile.workspace_id=? AND profile.profile_state!='migration_conflict'
            AND length(trim(profile.name))>0 AND length(trim(profile.slug_normalized))>0
        )
        """,
        (workspace_id,),
    ).fetchone()[0]
    confirmed = ready and _valid_confirmation(
        connection, workspace_id, tenant_id, "workspace_profile"
    )
    return _ResourceFact(bool(confirmed), bool(ready), "system_created", f"ready={int(ready)}")


def _environment_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
) -> _ResourceFact:
    rows = connection.execute(
        """
        SELECT environment.environment_id,environment.updated_at,origin.origin
        FROM workspace_environment_origins origin
        JOIN environments environment
          ON environment.tenant_id=origin.tenant_id
         AND environment.environment_id=origin.environment_id
        WHERE origin.workspace_id=? AND origin.tenant_id=?
          AND environment.code='dev' AND environment.status='active'
        ORDER BY CASE origin.origin WHEN 'user_created' THEN 0
          WHEN 'system_created' THEN 1 ELSE 2 END,environment.created_at
        """,
        (workspace_id, tenant_id),
    ).fetchall()
    ready = bool(rows)
    confirmed = ready and _valid_confirmation(connection, workspace_id, tenant_id, "environment")
    source: OnboardingStepSource = (
        "legacy_seed"
        if rows and all(row["origin"] == "legacy_existing" for row in rows)
        else "system_created"
        if rows
        else "none"
    )
    return _ResourceFact(
        bool(confirmed),
        ready,
        source,
        f"ready={int(ready)}",
        tuple(str(row["environment_id"]) for row in rows),
        tuple(
            (str(row["environment_id"]), _parse_time(row["updated_at"])) for row in rows
        ),
    )


def _provider_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    *,
    encryption_secret: str | None = None,
) -> _ResourceFact:
    rows = connection.execute(
        """
        SELECT provider.provider_id,provider.provider_type,provider.auth_type,
          provider.health_status,provider.last_success_at,provider.updated_at
        FROM providers provider
        WHERE provider.tenant_id=? AND provider.status='active'
          AND provider.created_by IN(
            SELECT member.legacy_user_id FROM workspace_memberships member
            WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
              AND member.role_code!='service_account')
        ORDER BY provider.updated_at DESC LIMIT 20
        """,
        (tenant_id, workspace_id, tenant_id),
    ).fetchall()
    verified: list[sqlite3.Row] = []
    for row in rows:
        provider_updated = _parse_time(row["updated_at"])
        provider_success = _optional_time(row["last_success_at"])
        provider_healthy = (
            row["health_status"] == "healthy"
            and provider_success is not None
            and provider_success >= provider_updated
        )
        if (
            row["provider_type"] == "local_endpoint"
            and row["auth_type"] in {"none", "no_auth"}
            and provider_healthy
        ):
            verified.append(row)
            continue
        if not provider_healthy:
            continue
        credentials = connection.execute(
            """
            SELECT credential.encrypted_key,credential.probe_health_status,
              credential.probe_last_success_at,credential.updated_at
            FROM provider_credentials credential
            WHERE credential.tenant_id=? AND credential.provider_id=?
              AND credential.status='active'
              AND credential.created_by IN(
                SELECT member.legacy_user_id FROM workspace_memberships member
                WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
                  AND member.role_code!='service_account')
            ORDER BY credential.updated_at DESC LIMIT 20
            """,
            (tenant_id, row["provider_id"], workspace_id, tenant_id),
        ).fetchall()
        for credential in credentials:
            credential_success = _optional_time(credential["probe_last_success_at"])
            credential_updated = _parse_time(credential["updated_at"])
            if (
                credential["probe_health_status"] != "healthy"
                or credential_success is None
                or credential_success < max(provider_updated, credential_updated)
                or not str(credential["encrypted_key"]).startswith("aesgcm-v1:")
            ):
                continue
            try:
                plaintext = decrypt_provider_key(
                    str(credential["encrypted_key"]), encryption_secret=encryption_secret,
                )
            except ValueError:
                continue
            if plaintext:
                verified.append(row)
                break
    legacy = connection.execute(
        """
        SELECT EXISTS(
          SELECT 1 FROM providers provider
          LEFT JOIN provider_credentials credential
            ON credential.tenant_id=provider.tenant_id
           AND credential.provider_id=provider.provider_id
          WHERE provider.tenant_id=? AND provider.status='active'
            AND (provider.created_by IS NULL
              OR credential.encrypted_key LIKE 'local-placeholder:%')
        )
        """,
        (tenant_id,),
    ).fetchone()[0]
    source: OnboardingStepSource = "verified" if verified else "legacy_seed" if legacy else "none"
    return _ResourceFact(
        bool(verified),
        bool(rows),
        source,
        f"verified={int(bool(verified))}",
        tuple(str(row["provider_id"]) for row in verified),
        tuple(
            (str(row["provider_id"]), _parse_time(row["updated_at"])) for row in verified
        ),
    )


def _model_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    provider: _ResourceFact,
) -> _ResourceFact:
    if not provider.resource_ids:
        legacy = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM models WHERE tenant_id=? AND status='active' "
            "AND created_by IS NULL)",
            (tenant_id,),
        ).fetchone()[0]
        return _ResourceFact(False, False, "legacy_seed" if legacy else "none", "verified=0")
    placeholders = ",".join("?" for _ in provider.resource_ids)
    rows = connection.execute(
        f"""
        SELECT model.model_id,model.updated_at
        FROM models model
        WHERE model.tenant_id=? AND model.status='active'
          AND model.provider_id IN({placeholders})
          AND model.created_by IN(
            SELECT member.legacy_user_id FROM workspace_memberships member
            WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
              AND member.role_code!='service_account')
          AND model.provider_model_id IS NOT NULL
          AND length(trim(model.provider_model_id))>0
          AND json_valid(model.capabilities)=1
          AND json_type(model.capabilities)='object'
        ORDER BY model.updated_at DESC
        """,
        (tenant_id, *provider.resource_ids, workspace_id, tenant_id),
    ).fetchall()
    legacy = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM models WHERE tenant_id=? AND status='active' "
        "AND created_by IS NULL)",
        (tenant_id,),
    ).fetchone()[0]
    source: OnboardingStepSource = "verified" if rows else "legacy_seed" if legacy else "none"
    return _ResourceFact(
        bool(rows),
        bool(rows),
        source,
        f"verified={int(bool(rows))}",
        tuple(str(row["model_id"]) for row in rows),
        tuple((str(row["model_id"]), _parse_time(row["updated_at"])) for row in rows),
    )


def _alias_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    environment: _ResourceFact,
    model: _ResourceFact,
) -> _ResourceFact:
    rows: list[sqlite3.Row] = []
    if environment.resource_ids and model.resource_ids:
        env_slots = ",".join("?" for _ in environment.resource_ids)
        model_slots = ",".join("?" for _ in model.resource_ids)
        rows = connection.execute(
            f"""
            SELECT alias_row.model_alias_id,alias_row.updated_at
            FROM model_aliases alias_row
            WHERE alias_row.tenant_id=? AND alias_row.status='active'
              AND alias_row.environment='dev'
              AND alias_row.environment_id IN({env_slots})
              AND alias_row.default_model_id IN({model_slots})
              AND alias_row.created_by IN(
                SELECT member.legacy_user_id FROM workspace_memberships member
                WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
                  AND member.role_code!='service_account')
            ORDER BY alias_row.updated_at DESC
            """,
            (
                tenant_id,
                *environment.resource_ids,
                *model.resource_ids,
                workspace_id,
                tenant_id,
            ),
        ).fetchall()
    legacy = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM model_aliases WHERE tenant_id=? AND status='active' "
        "AND created_by IS NULL)",
        (tenant_id,),
    ).fetchone()[0]
    source: OnboardingStepSource = "verified" if rows else "legacy_seed" if legacy else "none"
    return _ResourceFact(
        bool(rows),
        bool(rows),
        source,
        f"verified={int(bool(rows))}",
        tuple(str(row["model_alias_id"]) for row in rows),
        tuple(
            (str(row["model_alias_id"]), _parse_time(row["updated_at"])) for row in rows
        ),
    )


def _project_application_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
) -> _ResourceFact:
    rows = connection.execute(
        """
        SELECT project.project_id,application.application_id,
          project.updated_at project_updated_at,application.updated_at application_updated_at
        FROM projects project
        JOIN applications application
          ON application.tenant_id=project.tenant_id
         AND application.project_id=project.project_id
        WHERE project.tenant_id=? AND project.status='active'
          AND application.status='active'
          AND project.created_by IN(
            SELECT member.legacy_user_id FROM workspace_memberships member
            WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
              AND member.role_code!='service_account')
          AND application.created_by IN(
            SELECT member.legacy_user_id FROM workspace_memberships member
            WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
              AND member.role_code!='service_account')
        ORDER BY application.updated_at DESC
        """,
        (tenant_id, workspace_id, tenant_id, workspace_id, tenant_id),
    ).fetchall()
    legacy = connection.execute(
        """
        SELECT EXISTS(
          SELECT 1 FROM projects project
          LEFT JOIN applications application
            ON application.tenant_id=project.tenant_id
           AND application.project_id=project.project_id
          WHERE project.tenant_id=?
            AND (project.created_by IS NULL OR application.created_by IS NULL))
        """,
        (tenant_id,),
    ).fetchone()[0]
    source: OnboardingStepSource = "verified" if rows else "legacy_seed" if legacy else "none"
    ids = tuple(f"{row['project_id']}\x1f{row['application_id']}" for row in rows)
    updated = tuple(
        (
            f"{row['project_id']}\x1f{row['application_id']}",
            max(_parse_time(row["project_updated_at"]), _parse_time(row["application_updated_at"])),
        )
        for row in rows
    )
    return _ResourceFact(
        bool(rows), bool(rows), source, f"verified={int(bool(rows))}", ids, updated
    )


def _virtual_key_fact(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    environment: _ResourceFact,
    project_application: _ResourceFact,
) -> _ResourceFact:
    rows: list[sqlite3.Row] = []
    pairs = {tuple(value.split("\x1f", 1)) for value in project_application.resource_ids}
    if environment.resource_ids and pairs:
        env_slots = ",".join("?" for _ in environment.resource_ids)
        rows = connection.execute(
            f"""
            SELECT virtual_key.virtual_key_id,virtual_key.project_id,
              virtual_key.application_id,virtual_key.updated_at
            FROM virtual_keys virtual_key
            WHERE virtual_key.tenant_id=? AND virtual_key.status='active'
              AND virtual_key.environment='dev'
              AND virtual_key.environment_id IN({env_slots})
              AND virtual_key.project_id IS NOT NULL
              AND virtual_key.application_id IS NOT NULL
              AND virtual_key.created_by IN(
                SELECT member.legacy_user_id FROM workspace_memberships member
                WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
                  AND member.role_code!='service_account')
            ORDER BY virtual_key.updated_at DESC
            """,
            (tenant_id, *environment.resource_ids, workspace_id, tenant_id),
        ).fetchall()
        rows = [
            row
            for row in rows
            if (str(row["project_id"]), str(row["application_id"])) in pairs
        ]
    legacy = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM virtual_keys WHERE tenant_id=? AND status='active' "
        "AND created_by IS NULL)",
        (tenant_id,),
    ).fetchone()[0]
    source: OnboardingStepSource = "verified" if rows else "legacy_seed" if legacy else "none"
    return _ResourceFact(
        bool(rows),
        bool(rows),
        source,
        f"verified={int(bool(rows))}",
        tuple(str(row["virtual_key_id"]) for row in rows),
        tuple(
            (str(row["virtual_key_id"]), _parse_time(row["updated_at"])) for row in rows
        ),
    )


def _first_call_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    workspace_created_at: datetime,
    provider: _ResourceFact,
    model: _ResourceFact,
    alias: _ResourceFact,
    project_application: _ResourceFact,
    virtual_key: _ResourceFact,
) -> _ResourceFact:
    if (
        not provider.resource_ids
        or not virtual_key.resource_ids
        or not model.resource_ids
        or not alias.resource_ids
    ):
        return _ResourceFact(False, False, "none", "verified=0")
    key_slots = ",".join("?" for _ in virtual_key.resource_ids)
    provider_slots = ",".join("?" for _ in provider.resource_ids)
    model_slots = ",".join("?" for _ in model.resource_ids)
    alias_slots = ",".join("?" for _ in alias.resource_ids)
    rows = connection.execute(
        f"""
        SELECT request_log.created_at,request_log.virtual_key_id,request_log.model_id,
          request_log.provider_id,request_log.project_id,request_log.application_id,
          alias_row.model_alias_id,alias_row.updated_at alias_updated_at,
          usage.created_at usage_created_at
        FROM request_logs request_log
        JOIN usage_ledger usage
          ON usage.tenant_id=request_log.tenant_id
         AND usage.request_id=request_log.request_id
        JOIN model_aliases alias_row
          ON alias_row.tenant_id=request_log.tenant_id
         AND alias_row.default_model_id=request_log.model_id
         AND alias_row.alias=request_log.request_model
         AND alias_row.status='active'
         AND alias_row.environment_id=request_log.environment_id
        JOIN virtual_keys virtual_key
          ON virtual_key.tenant_id=request_log.tenant_id
         AND virtual_key.virtual_key_id=request_log.virtual_key_id
         AND virtual_key.status='active'
         AND virtual_key.environment_id=request_log.environment_id
         AND virtual_key.project_id=request_log.project_id
         AND virtual_key.application_id=request_log.application_id
        WHERE request_log.tenant_id=? AND request_log.status='success'
          AND request_log.virtual_key_id IN({key_slots})
          AND request_log.provider_id IN({provider_slots})
          AND request_log.model_id IN({model_slots})
          AND alias_row.model_alias_id IN({alias_slots})
          AND usage.status='recorded' AND usage.usage_source!='response_cache'
          AND usage.virtual_key_id=request_log.virtual_key_id
          AND usage.model_id=request_log.model_id
          AND usage.provider_id=request_log.provider_id
          AND usage.project_id=request_log.project_id
          AND usage.application_id=request_log.application_id
          AND usage.environment_id=request_log.environment_id
        ORDER BY request_log.created_at DESC LIMIT 20
        """,
        (
            tenant_id,
            *virtual_key.resource_ids,
            *provider.resource_ids,
            *model.resource_ids,
            *alias.resource_ids,
        ),
    ).fetchall()
    updates: dict[str, datetime] = {item_id: value for item_id, value in provider.updated_at_by_id}
    updates.update(model.updated_at_by_id)
    updates.update(alias.updated_at_by_id)
    updates.update(project_application.updated_at_by_id)
    updates.update(virtual_key.updated_at_by_id)
    alias_ids = set(alias.resource_ids)
    pairs = set(project_application.resource_ids)
    verified = False
    last_call: datetime | None = None
    for row in rows:
        pair = f"{row['project_id']}\x1f{row['application_id']}"
        if str(row["model_alias_id"]) not in alias_ids or pair not in pairs:
            continue
        request_time = _parse_time(row["created_at"])
        required = [
            workspace_created_at,
            updates.get(str(row["virtual_key_id"]), workspace_created_at),
            updates.get(str(row["model_id"]), workspace_created_at),
            updates.get(str(row["provider_id"]), workspace_created_at),
            updates.get(str(row["model_alias_id"]), workspace_created_at),
            updates.get(pair, workspace_created_at),
        ]
        if request_time >= max(required) and _parse_time(row["usage_created_at"]) >= request_time:
            verified = True
            last_call = request_time
            break
    return _ResourceFact(
        verified,
        bool(rows),
        "verified" if verified else "none",
        f"verified={int(verified)};time={_db_time(last_call) if last_call else 'none'}",
    )


def _valid_confirmation(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    step_id: str,
) -> bool:
    row = connection.execute(
        """
        SELECT acknowledgement.action,acknowledgement.membership_revision,
          member.revision current_revision,member.status,member.role_code
        FROM onboarding_acknowledgements acknowledgement
        JOIN workspace_memberships member
          ON member.workspace_id=acknowledgement.workspace_id
         AND member.tenant_id=acknowledgement.tenant_id
         AND member.workspace_membership_id=acknowledgement.actor_membership_id
         AND member.account_id=acknowledgement.actor_account_id
        WHERE acknowledgement.workspace_id=? AND acknowledgement.tenant_id=?
          AND acknowledgement.step_id=?
          AND acknowledgement.action IN('confirm','reopen')
        ORDER BY acknowledgement.created_at DESC,
          acknowledgement.onboarding_acknowledgement_id DESC LIMIT 1
        """,
        (workspace_id, tenant_id, step_id),
    ).fetchone()
    return bool(
        row
        and row["action"] == "confirm"
        and row["status"] == "active"
        and row["role_code"] != "service_account"
        and int(row["membership_revision"]) == int(row["current_revision"])
    )


def _confirmation_candidate_ready(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    workspace_kind: str,
    step_id: OnboardingStepId,
) -> bool:
    if step_id == "workspace_profile":
        return _profile_fact(
            connection, workspace_id, tenant_id, workspace_kind
        ).candidate_ready
    if step_id == "environment":
        return _environment_fact(connection, workspace_id, tenant_id).candidate_ready
    return False


def _latest_ack_action(
    connection: sqlite3.Connection,
    workspace_id: str,
    step_id: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT action FROM onboarding_acknowledgements
        WHERE workspace_id=? AND step_id=?
        ORDER BY created_at DESC,onboarding_acknowledgement_id DESC LIMIT 1
        """,
        (workspace_id, step_id),
    ).fetchone()
    return str(row["action"]) if row else None


def _authorize_step_action(
    context: WorkspaceSessionContext,
    evaluation: _Evaluation,
    step_id: OnboardingStepId,
    action: str,
) -> None:
    step = next(item for item in evaluation.steps if item.step_id == step_id)
    if step_id == "workspace_profile":
        allowed = context.membership_role in {"owner", "admin"}
        if not allowed:
            raise ApiError("onboarding.permission_denied")
    elif context.membership_role not in {"owner", "admin", "developer"}:
        raise ApiError("onboarding.permission_denied")
    if action == "confirm":
        if step_id not in {"workspace_profile", "environment"} or step.status not in {
            "current",
            "deferred",
            "needs_attention",
        }:
            raise ApiError("onboarding.action_invalid")
    elif action == "reopen":
        if step_id not in {"workspace_profile", "environment"} or step.status != "complete":
            raise ApiError("onboarding.action_invalid")
    elif action == "defer" and step.status not in {"current", "needs_attention"}:
        raise ApiError("onboarding.action_invalid")


def _persist_evaluation(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    *,
    old_progress: sqlite3.Row,
    evaluation: _Evaluation,
    trigger_source: Literal["manual_recheck", "step_action"],
    idempotency_key: str,
    request_sha256: str,
    request_id: str,
    trace_id: str,
) -> sqlite3.Row:
    old_steps = {
        str(row["step_id"]): row
        for row in connection.execute(
            "SELECT * FROM onboarding_step_facts WHERE workspace_id=?",
            (context.workspace_id,),
        ).fetchall()
    }
    generation = int(old_progress["generation"]) + 1
    revision = int(old_progress["revision"]) + 1
    now = evaluation.evaluated_at
    status = "active" if evaluation.completed_steps == 8 else "incomplete"
    was_active = old_progress["status"] == "active"
    activated_at = old_progress["activated_at"]
    if status == "active" and not was_active:
        activated_at = _db_time(now)
    changed = connection.execute(
        """
        UPDATE onboarding_progress
        SET generation=?,completed_steps=?,status=?,projection_sha256=?,evaluated_at=?,
          activated_at=?,revision=?,updated_at=?
        WHERE workspace_id=? AND tenant_id=? AND revision=?
        """,
        (
            generation,
            evaluation.completed_steps,
            status,
            evaluation.projection_sha256,
            _db_time(now),
            activated_at,
            revision,
            _db_time(now),
            context.workspace_id,
            context.tenant_id,
            old_progress["revision"],
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("onboarding.revision_conflict")
    for step in evaluation.steps:
        old = old_steps[step.step_id]
        first_satisfied = old["first_satisfied_at"]
        last_satisfied = old["last_satisfied_at"]
        if step.status == "complete":
            first_satisfied = first_satisfied or _db_time(now)
            last_satisfied = _db_time(now)
        connection.execute(
            """
            UPDATE onboarding_step_facts
            SET status=?,source=?,evidence_sha256=?,source_generation=?,
              first_satisfied_at=?,last_satisfied_at=?,last_checked_at=?,
              revision=revision+1,updated_at=?
            WHERE workspace_id=? AND tenant_id=? AND step_id=?
            """,
            (
                step.status,
                step.source,
                step.evidence_sha256,
                generation,
                first_satisfied,
                last_satisfied,
                _db_time(now),
                _db_time(now),
                context.workspace_id,
                context.tenant_id,
                step.step_id,
            ),
        )
    checkpoint_id = _id("onc_")
    checkpoint_result = (
        "activated"
        if status == "active" and not was_active
        else "active"
        if status == "active"
        else "activation_lost"
        if was_active
        else "incomplete"
    )
    connection.execute(
        """
        INSERT INTO activation_checkpoints(
          activation_checkpoint_id,workspace_id,tenant_id,actor_membership_id,
          actor_account_id,membership_revision,generation,completed_bitset,
          projection_sha256,result,trigger_source,idempotency_key_hmac,request_sha256,
          result_revision,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            checkpoint_id,
            context.workspace_id,
            context.tenant_id,
            context.membership_id,
            context.account_id,
            _membership_revision(connection, context),
            generation,
            evaluation.bitset,
            evaluation.projection_sha256,
            checkpoint_result,
            trigger_source,
            _idempotency_hmac(config, idempotency_key),
            request_sha256,
            revision,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )
    for step in evaluation.steps:
        old_complete = old_steps[step.step_id]["status"] == "complete"
        new_complete = step.status == "complete"
        if old_complete == new_complete:
            continue
        _insert_event(
            connection,
            context,
            checkpoint_id=checkpoint_id,
            step_id=step.step_id,
            event_type=(
                "onboarding.step_completed" if new_complete else "onboarding.step_reopened"
            ),
            source=step.source,
            generation=generation,
            evidence_sha256=step.evidence_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    if status == "active" and not was_active:
        _insert_event(
            connection,
            context,
            checkpoint_id=checkpoint_id,
            step_id=None,
            event_type="workspace.activated",
            source="verified",
            generation=generation,
            evidence_sha256=evaluation.projection_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    elif was_active and status != "active":
        _insert_event(
            connection,
            context,
            checkpoint_id=checkpoint_id,
            step_id=None,
            event_type="workspace.activation_lost",
            source="none",
            generation=generation,
            evidence_sha256=evaluation.projection_sha256,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    return _progress_row(connection, context.workspace_id)


def _insert_event(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    checkpoint_id: str,
    step_id: str | None,
    event_type: str,
    source: str,
    generation: int,
    evidence_sha256: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO onboarding_events(
          onboarding_event_id,workspace_id,tenant_id,activation_checkpoint_id,step_id,
          event_type,source,generation,evidence_sha256,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("one_"),
            context.workspace_id,
            context.tenant_id,
            checkpoint_id,
            step_id,
            event_type,
            source,
            generation,
            evidence_sha256,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _response(
    evaluation: _Evaluation,
    progress: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
) -> WorkspaceOnboardingResponse:
    public_steps: list[WorkspaceOnboardingStep] = []
    for index, step in enumerate(evaluation.steps, start=1):
        title, default_action, target = _STEP_META[step.step_id]
        action = "view" if step.status == "complete" else default_action
        blocking_code = None
        if step.status == "blocked":
            blocking_code = f"requires.{_STEP_IDS[index - 2]}" if index > 1 else None
        elif step.status == "needs_attention":
            blocking_code = "evidence_changed"
        elif step.source == "legacy_seed":
            blocking_code = "legacy_configuration"
        public_steps.append(
            WorkspaceOnboardingStep(
                id=step.step_id,
                order=index,
                title=title,
                status=step.status,
                source=step.source,
                action=action,
                navigation_target=target,
                blocking_code=blocking_code,
                last_checked_at=step.checked_at,
            )
        )
    live_status = "active" if evaluation.completed_steps == 8 else "incomplete"
    return WorkspaceOnboardingResponse(
        workspace_kind=evaluation.workspace_kind,
        completed_steps=evaluation.completed_steps,
        status=live_status,
        generation=int(progress["generation"]),
        revision=int(progress["revision"]),
        activated_at=_optional_time(progress["activated_at"]),
        evaluated_at=evaluation.evaluated_at,
        steps=tuple(public_steps),
        request_id=request_id,
        trace_id=trace_id,
    )


def _progress_row(connection: sqlite3.Connection, workspace_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM onboarding_progress WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    if row is None:
        raise ApiError("onboarding.unavailable")
    return row


def _membership_revision(
    connection: sqlite3.Connection, context: WorkspaceSessionContext
) -> int:
    row = connection.execute(
        """
        SELECT revision FROM workspace_memberships
        WHERE workspace_id=? AND tenant_id=? AND workspace_membership_id=?
          AND account_id=? AND status='active'
        """,
        (
            context.workspace_id,
            context.tenant_id,
            context.membership_id,
            context.account_id,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("workspace_session.revoked")
    return int(row["revision"])


def _acknowledgement_replay(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    *,
    idempotency_key: str,
    request_sha256: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT request_sha256 FROM onboarding_acknowledgements
        WHERE actor_membership_id=? AND idempotency_key_hmac=?
        """,
        (context.membership_id, _idempotency_hmac(config, idempotency_key)),
    ).fetchone()
    if row is not None and not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("onboarding.idempotency_conflict")
    return row


def _checkpoint_replay(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: WorkspaceSessionContext,
    *,
    idempotency_key: str,
    request_sha256: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT request_sha256 FROM activation_checkpoints
        WHERE actor_membership_id=? AND idempotency_key_hmac=?
        """,
        (context.membership_id, _idempotency_hmac(config, idempotency_key)),
    ).fetchone()
    if row is not None and not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("onboarding.idempotency_conflict")
    return row


def _step_id(value: str) -> OnboardingStepId:
    if value not in _STEP_IDS:
        raise ApiError("onboarding.step_not_found")
    return value  # type: ignore[return-value]


def _require_schema(connection: sqlite3.Connection) -> None:
    if not onboarding_schema_available_in_transaction(connection):
        raise ApiError("onboarding.unavailable")


def _validate_idempotency(value: str) -> None:
    if not _IDEMPOTENCY_RE.fullmatch(value):
        raise ApiError("onboarding.request_invalid")


def _request_hash(payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        b"prd097/onboarding/idempotency/v1\x00" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _evidence_hash(
    step_id: str, fact: _ResourceFact, status: OnboardingStepStatus
) -> str:
    canonical = json.dumps(
        {
            "candidate": fact.candidate_ready,
            "complete": fact.complete,
            "kind": _EVIDENCE_KIND[step_id],
            "source": fact.source,
            "status": status,
            "value": fact.checked_value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _projection_hash(
    workspace_kind: str, steps: list[_EvaluatedStep], bitset: int
) -> str:
    canonical = json.dumps(
        {
            "bitset": bitset,
            "kind": workspace_kind,
            "steps": [
                {"id": step.step_id, "source": step.source, "status": step.status}
                for step in steps
            ],
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("onboarding.unavailable") from exc


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _log_projection(
    context: WorkspaceSessionContext,
    progress: sqlite3.Row,
    step_id: str | None,
    action: str,
    request_id: str,
    trace_id: str,
) -> None:
    logger.info(
        "workspace.onboarding_updated",
        extra={
            "event": "workspace.onboarding_updated",
            "workspace_id": context.workspace_id,
            "step_id": step_id,
            "action": action,
            "generation": int(progress["generation"]),
            "status": str(progress["status"]),
            "request_id": request_id,
            "trace_id": trace_id,
        },
    )
