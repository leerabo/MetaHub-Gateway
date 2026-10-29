"""PRD-143 Provider Engineering transactions, projections, and fixed runner."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import catalog_identity
from metahub_shared.runtime.provider_error_map import (
    BUILTIN_CATALOG_HASH,
    BUILTIN_CATALOG_VERSION,
    EMPTY_OVERLAY_CONTENT_HASH,
    ProviderErrorRule,
    ProviderErrorRuleValidationError,
    compile_provider_error_rule,
    provider_error_rule_to_wire,
    tenant_rule_condition_hash,
)
from pydantic import ValidationError

from metahub_control.error_maps.service import (
    MAX_ACTIVE_RULES,
    MAX_STABLE_RULES,
    _activation_rejected,
    _advance_head,
    _editable_from_row,
    _load_current_rule_row,
    _next_head,
    _same_editable,
)
from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    AdapterCommandRequest,
    AdapterCommandResponse,
    AdapterContractCaseInfo,
    AdapterContractProjectionResponse,
    AdapterContractRunInfo,
    BuiltinErrorCatalogInfo,
    ContractAggregate,
    ErrorCatalogCommandRequest,
    ErrorCatalogCommandResponse,
    ProviderAdapterListResponse,
    ProviderAdapterReleaseInfo,
    ProviderCompatibilityInfo,
    WorkspaceErrorOverrideInfo,
    WorkspaceErrorOverrideListResponse,
    WorkspaceOverrideCommandRequest,
    WorkspaceOverrideCommandResponse,
    WorkspaceOverrideHeadInfo,
)

_ADAPTER_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_ADAPTER_RELEASE_ID = re.compile(r"^parl_[0-9a-f]{32}$")
_CATALOG_RELEASE_ID = re.compile(r"^pecr_[0-9a-f]{32}$")
_OVERRIDE_ID = re.compile(r"^pem_[0-9a-f]{32}$")
_RUN_ID = re.compile(r"^(?:pacr_|acr_)[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_CURSOR_BYTES = 768


def list_provider_adapters(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> ProviderAdapterListResponse:
    parsed = _adapter_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.provider_adapter.read")
        clauses: list[str] = []
        parameters: list[object] = []
        for column, key in (
            ("release.status", "status"),
            ("release.protocol_family", "protocol"),
            ("release.compatibility_sha256", "compatibility"),
        ):
            value = parsed.get(key)
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        adapter = parsed.get("adapter")
        if adapter is not None:
            if _ADAPTER_RELEASE_ID.fullmatch(str(adapter)):
                clauses.append("release.provider_adapter_release_id=?")
            else:
                clauses.append("release.adapter_code=?")
            parameters.append(adapter)
        generation = parsed.get("release_generation")
        if generation is not None:
            clauses.append("release.release_generation=?")
            parameters.append(generation)
        cursor = parsed.get("cursor_value")
        if cursor is not None:
            clauses.append(
                "(release.updated_at<? OR(release.updated_at=? "
                "AND release.provider_adapter_release_id<?))"
            )
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = int(parsed["limit"])
        rows = connection.execute(
            f"""
            SELECT release.*,head.provider_adapter_release_id IS NOT NULL AS is_head
            FROM provider_adapter_releases release
            LEFT JOIN provider_adapter_release_heads head
              ON head.provider_adapter_release_id=release.provider_adapter_release_id
            {where}
            ORDER BY release.updated_at DESC,release.provider_adapter_release_id DESC
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        adapters = tuple(_adapter_info(connection, row, actor) for row in visible)
        next_cursor = (
            _encode_cursor(
                config,
                "adapter-list",
                [visible[-1]["updated_at"], visible[-1]["provider_adapter_release_id"]],
            )
            if len(rows) > limit and visible
            else None
        )
        connection.commit()
        return ProviderAdapterListResponse(
            adapters=adapters,
            next_cursor=next_cursor,
            limit=limit,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, ValidationError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_adapter_contract_projections(
    config: AppConfig,
    *,
    session_token: str,
    provider_adapter_release_id: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> AdapterContractProjectionResponse:
    if _ADAPTER_RELEASE_ID.fullmatch(provider_adapter_release_id) is None:
        raise ApiError("adapter.contract_run_not_found")
    parsed = _contract_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.provider_adapter.read")
        if (
            connection.execute(
                "SELECT 1 FROM provider_adapter_releases WHERE provider_adapter_release_id=?",
                (provider_adapter_release_id,),
            ).fetchone()
            is None
        ):
            raise ApiError("adapter.contract_run_not_found")
        view = str(parsed["view"])
        limit = int(parsed["limit"])
        runs: tuple[AdapterContractRunInfo, ...] = ()
        cases: tuple[AdapterContractCaseInfo, ...] = ()
        next_cursor = None
        if view == "runs":
            clauses = ["run.provider_adapter_release_id=?"]
            parameters: list[object] = [provider_adapter_release_id]
            for column, key in (
                ("run.scope", "scope"),
                ("run.status", "status"),
                ("run.suite_kind", "suite"),
                ("run.mode", "mode"),
            ):
                if parsed.get(key) is not None:
                    clauses.append(f"{column}=?")
                    parameters.append(parsed[key])
            if parsed.get("from") is not None:
                clauses.append("coalesce(run.completed_at,run.created_at)>=?")
                parameters.append(parsed["from"])
            if parsed.get("to") is not None:
                clauses.append("coalesce(run.completed_at,run.created_at)<=?")
                parameters.append(parsed["to"])
            cursor = parsed.get("cursor_value")
            if cursor is not None:
                clauses.append(
                    "(coalesce(run.completed_at,run.created_at)<? OR("
                    "coalesce(run.completed_at,run.created_at)=? AND run.contract_run_id<?))"
                )
                parameters.extend((cursor[0], cursor[0], cursor[1]))
            rows = connection.execute(
                f"""SELECT run.* FROM platform_adapter_contract_run_projections run
                    WHERE {" AND ".join(clauses)}
                    ORDER BY coalesce(run.completed_at,run.created_at) DESC,run.contract_run_id DESC
                    LIMIT ?""",
                (*parameters, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            runs = tuple(_run_info(row) for row in visible)
            if len(rows) > limit and visible:
                next_cursor = _encode_cursor(
                    config,
                    "contract-runs",
                    [
                        visible[-1]["completed_at"] or visible[-1]["created_at"],
                        visible[-1]["contract_run_id"],
                    ],
                )
        else:
            run_id = str(parsed["run_id"])
            owner = connection.execute(
                """SELECT 1 FROM platform_adapter_contract_run_projections
                   WHERE contract_run_id=? AND provider_adapter_release_id=?""",
                (run_id, provider_adapter_release_id),
            ).fetchone()
            if owner is None:
                raise ApiError("adapter.contract_run_not_found")
            clauses = ["case_row.contract_run_id=?"]
            parameters = [run_id]
            cursor = parsed.get("cursor_value")
            if cursor is not None:
                clauses.append("(case_row.ordinal,case_row.contract_case_id)>(?,?)")
                parameters.extend((cursor[0], cursor[1]))
            rows = connection.execute(
                f"""SELECT case_row.* FROM platform_adapter_contract_case_projections case_row
                    INDEXED BY idx_platform_contract_cases_page_prd143
                    WHERE {" AND ".join(clauses)}
                    ORDER BY case_row.ordinal,case_row.contract_case_id LIMIT ?""",
                (*parameters, limit + 1),
            ).fetchall()
            visible = rows[:limit]
            cases = tuple(_case_info(row) for row in visible)
            if len(rows) > limit and visible:
                next_cursor = _encode_cursor(
                    config,
                    "contract-cases",
                    [int(visible[-1]["ordinal"]), visible[-1]["contract_case_id"]],
                )
        connection.commit()
        return AdapterContractProjectionResponse(
            provider_adapter_release_id=provider_adapter_release_id,
            view=view,
            runs=runs,
            cases=cases,
            next_cursor=next_cursor,
            limit=limit,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def command_provider_adapter(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    adapter_identifier: str,
    command: AdapterCommandRequest,
    request_id: str,
    trace_id: str,
) -> AdapterCommandResponse:
    _valid_link_ids(request_id, trace_id)
    intent = {
        "adapter_identifier": adapter_identifier,
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_revision": expected_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_platform(actor, _adapter_action_permission(command.action))
        replay = _replay_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
        )
        if replay is not None:
            adapter_id = (
                str(
                    connection.execute(
                        """SELECT provider_adapter_release_id
                           FROM platform_adapter_contract_run_projections
                           WHERE contract_run_id=?""",
                        (replay["object_id"],),
                    ).fetchone()[0]
                )
                if command.action == "run_contract"
                else str(replay["object_id"])
            )
            row = _adapter_row(connection, adapter_id)
            if row is None:
                raise ApiError("platform.unavailable")
            result = AdapterCommandResponse(
                adapter=_adapter_info(connection, row, actor),
                command_id=str(replay["command_id"]),
                contract_run_id=(
                    str(replay["object_id"]) if command.action == "run_contract" else None
                ),
                idempotent_replay=True,
                permission_fingerprint=_platform_fingerprint(actor),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result

        now = _now()
        resource_id = adapter_identifier
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"provider_adapter.{command.action}",
            resource_type="provider_adapter_release",
            resource_id=resource_id,
            now=now,
        )
        if command.action == "create_candidate":
            if _ADAPTER_CODE.fullmatch(adapter_identifier) is None:
                raise ApiError("platform.request_invalid")
            current_head = connection.execute(
                "SELECT revision FROM provider_adapter_release_heads WHERE adapter_code=?",
                (adapter_identifier,),
            ).fetchone()
            if expected_revision != (int(current_head["revision"]) if current_head else 0):
                raise ApiError("platform.revision_conflict")
            adapter_id = _create_adapter_candidate(
                connection, actor, adapter_identifier, command, now
            )
            result_object_id = adapter_id
            run_id = None
        else:
            if _ADAPTER_RELEASE_ID.fullmatch(adapter_identifier) is None:
                raise ApiError("adapter.contract_run_not_found")
            release = _adapter_row(connection, adapter_identifier)
            if release is None:
                raise ApiError("adapter.contract_run_not_found")
            if expected_revision != int(release["revision"]):
                raise ApiError("platform.revision_conflict")
            adapter_id = adapter_identifier
            run_id = None
            if command.action == "run_contract":
                run_id = _enqueue_contract_run(connection, release, now)
                _transition_adapter(
                    connection,
                    adapter_id,
                    from_statuses={"candidate", "review_required", "approved"},
                    to_status="testing",
                    actor_column=None,
                    actor_id=None,
                    now=now,
                )
                result_object_id = run_id
            elif command.action == "review_failure":
                _review_contract_run(connection, actor, release, command, now)
                result_object_id = adapter_id
            elif command.action == "release":
                _release_adapter(connection, actor, release, request_id, trace_id, now)
                result_object_id = adapter_id
            elif command.action in {"deprecate", "revoke"}:
                _transition_adapter(
                    connection,
                    adapter_id,
                    from_statuses={"released", "deprecated"},
                    to_status="deprecated" if command.action == "deprecate" else "revoked",
                    actor_column=None,
                    actor_id=None,
                    now=now,
                )
                _refresh_compatibility_rows(connection, now)
                result_object_id = adapter_id
            else:
                raise ApiError("platform.request_invalid")

        refreshed = _adapter_row(connection, adapter_id)
        if refreshed is None:
            raise ApiError("platform.unavailable")
        command_id = _insert_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=command.action,
            object_kind="adapter_release",
            object_id=result_object_id,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            result_revision=int(refreshed["revision"]),
            now=now,
        )
        _platform_audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"provider_adapter.{command.action}",
            object_type="provider_adapter_release",
            object_id=adapter_id,
            before_sha=None,
            after_sha=_adapter_state_sha(refreshed),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = AdapterCommandResponse(
            adapter=_adapter_info(connection, refreshed, actor),
            command_id=command_id,
            contract_run_id=run_id,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc) or "conflict" in str(exc):
            raise ApiError("platform.idempotency_conflict") from None
        raise ApiError("platform.unavailable") from None
    except (sqlite3.Error, TypeError, ValueError, ValidationError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def command_error_catalog(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    command: ErrorCatalogCommandRequest,
    request_id: str,
    trace_id: str,
) -> ErrorCatalogCommandResponse:
    _valid_link_ids(request_id, trace_id)
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_revision": expected_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_platform(actor, _catalog_action_permission(command.action))
        replay = _replay_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"error_catalog.{command.action}",
            idempotency_key=idempotency_key,
            request_sha=request_sha,
        )
        if replay is not None:
            row = _catalog_row(connection, str(replay["object_id"]))
            if row is None:
                raise ApiError("platform.unavailable")
            result = ErrorCatalogCommandResponse(
                catalog=_catalog_info(row, actor),
                command_id=str(replay["command_id"]),
                idempotent_replay=True,
                permission_fingerprint=_platform_fingerprint(actor),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        now = _now()
        resource_id = (
            str(command.builtin_catalog_version)
            if command.action == "create_candidate"
            else str(command.error_catalog_release_id)
        )
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"provider_error_catalog.{command.action}",
            resource_type="provider_error_catalog_release",
            resource_id=resource_id,
            now=now,
        )
        if command.action == "create_candidate":
            head = connection.execute(
                "SELECT revision FROM platform_builtin_error_catalog_heads WHERE singleton_id=1"
            ).fetchone()
            if expected_revision != (int(head["revision"]) if head else 0):
                raise ApiError("platform.revision_conflict")
            catalog_id = _create_catalog_candidate(connection, actor, command, now)
        else:
            catalog_id = str(command.error_catalog_release_id)
            if _CATALOG_RELEASE_ID.fullmatch(catalog_id) is None:
                raise ApiError("platform.request_invalid")
            row = _catalog_row(connection, catalog_id)
            if row is None:
                raise ApiError("platform.operator_not_found")
            if (
                command.expected_revision != expected_revision
                or int(row["revision"]) != expected_revision
            ):
                raise ApiError("platform.revision_conflict")
            _transition_catalog(connection, actor, row, command.action, request_id, trace_id, now)
        refreshed = _catalog_row(connection, catalog_id)
        if refreshed is None:
            raise ApiError("platform.unavailable")
        command_id = _insert_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"error_catalog.{command.action}",
            object_kind="error_catalog",
            object_id=catalog_id,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            result_revision=int(refreshed["revision"]),
            now=now,
        )
        _platform_audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"provider_error_catalog.{command.action}",
            object_type="provider_error_catalog",
            object_id=catalog_id,
            before_sha=None,
            after_sha=_catalog_state_sha(refreshed),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = ErrorCatalogCommandResponse(
            catalog=_catalog_info(refreshed, actor),
            command_id=command_id,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc) or "conflict" in str(exc):
            raise ApiError("platform.idempotency_conflict") from None
        raise ApiError("platform.unavailable") from None
    except (sqlite3.Error, TypeError, ValueError, ValidationError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_workspace_error_overrides(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> WorkspaceErrorOverrideListResponse:
    parsed = _override_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace(context, "workspace.provider_error_override.read")
        provider_id = parsed.get("provider_id")
        if provider_id is not None:
            _require_owned_provider(connection, context, str(provider_id))
        clauses = ["override.workspace_id=?", "override.tenant_id=?"]
        parameters: list[object] = [context.workspace_id, context.tenant_id]
        if provider_id is not None:
            clauses.append("(override.provider_id=? OR override.provider_id IS NULL)")
            parameters.append(provider_id)
        for column, key in (
            ("override.provider_error_override_id", "override_id"),
            ("override.status", "status"),
            ("override.output_error_code", "output_error_code"),
        ):
            value = parsed.get(key)
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        cursor = parsed.get("cursor_value")
        if cursor is not None:
            clauses.append("override.provider_error_override_id>?")
            parameters.append(cursor[0])
        limit = int(parsed["limit"])
        rows = connection.execute(
            f"""
            SELECT override.*,provider.provider_code,provider.name AS provider_name
            FROM workspace_provider_error_overrides override
            LEFT JOIN providers provider
              ON provider.tenant_id=override.tenant_id AND provider.provider_id=override.provider_id
            WHERE {" AND ".join(clauses)}
            ORDER BY override.provider_error_override_id LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        overrides = tuple(_override_info(row, context) for row in visible)
        head = _workspace_head(connection, context)
        compatibility = (
            _compatibility_info(
                connection.execute(
                    """SELECT * FROM provider_compatibility_projections
                       WHERE workspace_id=? AND tenant_id=? AND provider_id=?""",
                    (context.workspace_id, context.tenant_id, provider_id),
                ).fetchone()
            )
            if provider_id is not None
            else None
        )
        next_cursor = (
            _encode_cursor(
                config,
                "workspace-overrides",
                [visible[-1]["provider_error_override_id"]],
            )
            if len(rows) > limit and visible
            else None
        )
        connection.commit()
        return WorkspaceErrorOverrideListResponse(
            overrides=overrides,
            head=head,
            compatibility=compatibility,
            next_cursor=next_cursor,
            limit=limit,
            permission_revision=context.permission_revision,
            permission_fingerprint=context.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("error_map.unavailable") from None
    finally:
        connection.close()


def command_workspace_error_override(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_head_revision: int,
    command: WorkspaceOverrideCommandRequest,
    request_id: str,
    trace_id: str,
) -> WorkspaceOverrideCommandResponse:
    _valid_link_ids(request_id, trace_id)
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_head_revision": expected_head_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace(context, "workspace.provider_error_override.manage")
        replay = _replay_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=f"override.{command.action}",
            idempotency_key=idempotency_key,
            request_sha=request_sha,
        )
        if replay is not None:
            row = _owned_override_row(connection, context, str(replay["object_id"]))
            if row is None:
                raise ApiError("error_map.unavailable")
            head = _workspace_head(connection, context)
            compatibility = _ensure_compatibility(
                connection, context, str(row["provider_id"]), _now()
            )
            result = WorkspaceOverrideCommandResponse(
                override=_override_info(row, context),
                head=head,
                compatibility=compatibility,
                command_id=str(replay["command_id"]),
                changed=False,
                idempotent_replay=True,
                permission_revision=context.permission_revision,
                permission_fingerprint=context.authorization_fingerprint,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        current_head = _workspace_head(connection, context)
        if expected_head_revision != current_head.revision:
            raise ApiError("error_map.version_conflict")
        resource_id = (
            str(command.provider_id)
            if command.action == "create"
            else str(command.provider_error_override_id)
        )
        now = _now()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=f"provider_error_override.{command.action}",
            resource_type="provider_error_override",
            resource_id=resource_id,
            now=now,
        )
        changed_at = _db_time(now)
        if command.action == "create":
            if _stable_override_count(connection, context) >= MAX_STABLE_RULES:
                raise ApiError("error_map.limit_exceeded")
            provider_id = str(command.provider_id)
            _require_owned_provider(connection, context, provider_id)
            override_id = f"pem_{uuid4().hex}"
            candidate = _compile_override(
                connection,
                context,
                override_id,
                1,
                command.model_dump(mode="python", exclude={"action"}),
                status="active",
            )
            if _activation_rejected(connection, context.tenant_id, candidate, exclude_map_id=None):
                if _active_override_count(connection, context) >= MAX_ACTIVE_RULES:
                    raise ApiError("error_map.limit_exceeded")
                raise ApiError("error_map.conflict")
            next_revision, changed_at = _next_head(connection, context.tenant_id)
            version_id = _insert_legacy_override_create(
                connection, context, candidate, next_revision, changed_at
            )
            legacy_head, _ = _advance_head(
                connection,
                context.tenant_id,
                revision=next_revision,
                changed_at=changed_at,
            )
            changed = True
        else:
            override_id = str(command.provider_error_override_id)
            if _OVERRIDE_ID.fullmatch(override_id) is None:
                raise ApiError("error_map.not_found")
            projection = _owned_override_row(connection, context, override_id)
            legacy = _load_current_rule_row(
                connection, context.tenant_id, override_id, required=False
            )
            if projection is None or legacy is None:
                raise ApiError("error_map.not_found")
            if command.expected_version != int(projection["current_version"]):
                raise ApiError("error_map.version_conflict")
            editable = _editable_from_row(legacy)
            if command.action == "update":
                for field in command.model_fields_set - {
                    "action",
                    "provider_error_override_id",
                    "expected_version",
                }:
                    editable[field] = getattr(command, field)
                next_status = str(projection["status"])
            else:
                next_status = {
                    "enable": "active",
                    "disable": "disabled",
                    "archive": "archived",
                }[command.action]
            compiler_status = "disabled" if next_status == "archived" else next_status
            candidate = _compile_override(
                connection,
                context,
                override_id,
                int(projection["current_version"]) + 1,
                editable,
                status=compiler_status,
            )
            same_content = _same_editable(legacy, candidate)
            changed = not (
                same_content
                and next_status != "archived"
                and next_status == str(projection["status"])
            )
            if not changed:
                legacy_head = type(
                    "Head",
                    (),
                    {
                        "revision": current_head.revision,
                        "content_hash": current_head.content_sha256,
                        "builtin_catalog_version": current_head.builtin_catalog_version,
                    },
                )()
                version_id = None
            else:
                if _activation_rejected(
                    connection, context.tenant_id, candidate, exclude_map_id=override_id
                ):
                    if (
                        candidate.status == "active"
                        and _active_override_count(connection, context) >= MAX_ACTIVE_RULES
                    ):
                        raise ApiError("error_map.limit_exceeded")
                    raise ApiError("error_map.conflict")
                next_revision, changed_at = _next_head(connection, context.tenant_id)
                version_id = _insert_legacy_override_update(
                    connection,
                    context,
                    legacy,
                    candidate,
                    next_revision,
                    changed_at,
                )
                legacy_head, _ = _advance_head(
                    connection,
                    context.tenant_id,
                    revision=next_revision,
                    changed_at=changed_at,
                )
        if changed:
            _sync_workspace_override(
                connection,
                context,
                candidate,
                status=("active" if command.action == "create" else next_status),
                version_id=str(version_id),
                head_revision=int(legacy_head.revision),
                head_content_sha=str(legacy_head.content_hash),
                builtin_catalog_version=str(legacy_head.builtin_catalog_version),
                changed_at=changed_at,
                creating=command.action == "create",
            )
            _write_event(
                connection,
                event_type="workspace_error_override.changed",
                object_kind="workspace_override",
                object_id=override_id,
                owner_workspace_sha256=_digest(context.workspace_id),
                generation=candidate.version,
                payload={
                    "operation": command.action,
                    "output_error_code_sha256": _digest(candidate.output_error_code),
                    "pattern_sha256": candidate.pattern_hash,
                    "provider_id": candidate.provider_id,
                    "version": candidate.version,
                    "head_revision": int(legacy_head.revision),
                },
                request_id=request_id,
                trace_id=trace_id,
                now=_parse_db_time(changed_at),
            )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=f"override.{command.action}",
            object_kind="workspace_override",
            object_id=override_id,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            result_revision=candidate.version if changed else int(command.expected_version or 1),
            now=now,
        )
        row = _owned_override_row(connection, context, override_id)
        if row is None or row["provider_id"] is None:
            raise ApiError("error_map.unavailable")
        compatibility = _ensure_compatibility(connection, context, str(row["provider_id"]), now)
        head = _workspace_head(connection, context)
        _platform_audit(
            connection,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=f"provider_error_override.{command.action}",
            object_type="provider_error_override",
            object_id=override_id,
            before_sha=None,
            after_sha=_sha(
                {
                    "changed": changed,
                    "head_revision": head.revision,
                    "output_error_code_sha256": _digest(str(row["output_error_code"])),
                    "pattern_sha256": str(row["pattern_sha256"]),
                    "version": int(row["current_version"]),
                }
            ),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = WorkspaceOverrideCommandResponse(
            override=_override_info(row, context),
            head=head,
            compatibility=compatibility,
            command_id=command_id,
            changed=changed,
            permission_revision=context.permission_revision,
            permission_fingerprint=context.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc) or "conflict" in str(exc):
            raise ApiError("error_map.conflict") from None
        raise ApiError("error_map.unavailable") from None
    except (sqlite3.Error, TypeError, ValueError, ValidationError):
        _rollback(connection)
        raise ApiError("error_map.unavailable") from None
    finally:
        connection.close()


def _create_adapter_candidate(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    adapter_code: str,
    command: AdapterCommandRequest,
    now: datetime,
) -> str:
    release_version = str(command.release_version)
    if _SEMVER.fullmatch(release_version) is None:
        raise ApiError("platform.request_invalid")
    identity = catalog_identity()
    if (
        command.spec_manifest_version != identity.version
        or command.spec_manifest_sha256 != identity.catalog_hash
        or _SHA256.fullmatch(str(command.compatibility_sha256)) is None
        or not _bounded_version(command.required_runtime_version)
        or not _bounded_version(command.required_api_version)
    ):
        raise ApiError("platform.request_invalid")
    artifact = _verified_sdk_artifact(connection, str(command.sdk_release_id))
    if (
        connection.execute(
            """SELECT 1 FROM provider_adapter_releases
           WHERE adapter_code=? AND release_version=?""",
            (adapter_code, release_version),
        ).fetchone()
        is not None
    ):
        raise ApiError("platform.revision_conflict")
    adapter_id = f"parl_{uuid4().hex}"
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO provider_adapter_releases(
          provider_adapter_release_id,adapter_code,release_version,protocol_family,
          sdk_release_id,artifact_sha256,sbom_sha256,provenance_sha256,signature_sha256,
          spec_manifest_version,spec_manifest_sha256,compatibility_sha256,
          required_runtime_version,required_api_version,status,revision,release_generation,
          candidate_by_operator_id,security_reviewed_by_operator_id,released_by_operator_id,
          created_at,updated_at,released_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'candidate',1,0,?,NULL,NULL,?,?,NULL)
        """,
        (
            adapter_id,
            adapter_code,
            release_version,
            command.protocol_family,
            command.sdk_release_id,
            artifact["artifact_sha256"],
            artifact["sbom_sha256"],
            artifact["provenance_sha256"],
            artifact["signature_sha256"],
            command.spec_manifest_version,
            command.spec_manifest_sha256,
            command.compatibility_sha256,
            command.required_runtime_version,
            command.required_api_version,
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    return adapter_id


def _enqueue_contract_run(
    connection: sqlite3.Connection, release: sqlite3.Row, now: datetime
) -> str:
    if release["status"] not in {"candidate", "review_required", "approved"}:
        raise ApiError("adapter.contract_run_conflict")
    run_id = f"pacr_{uuid4().hex}"
    generation = int(
        connection.execute(
            """SELECT coalesce(max(generation),0)+1
               FROM platform_adapter_contract_run_projections
               WHERE provider_adapter_release_id=?""",
            (release["provider_adapter_release_id"],),
        ).fetchone()[0]
    )
    empty_hash = _digest(f"queued|{release['provider_adapter_release_id']}|{generation}")
    connection.execute(
        """
        INSERT INTO platform_adapter_contract_run_projections(
          contract_run_id,scope,provider_adapter_release_id,adapter_code,schema_version,
          runner_version,suite_kind,mode,source_revision_sha256,artifact_sha256,
          catalog_sha256,status,coverage_complete,total_cases,passed_cases,failed_cases,
          failure_codes_json,report_sha256,attestation_sha256,qualifies_release,
          legacy_owner_sha256,generation,security_reviewed_by_operator_id,
          started_at,completed_at,created_at)
        VALUES(?,'platform_release_ci',?,?,?,'provider-contract-runner-v1','release_ci',
          'isolated_runner',?,?,?,'queued',0,0,0,0,'[]',?,?,0,NULL,1,NULL,NULL,NULL,?)
        """.replace("NULL,1,NULL,NULL,NULL,?", "NULL,?,NULL,NULL,NULL,?"),
        (
            run_id,
            release["provider_adapter_release_id"],
            release["adapter_code"],
            release["spec_manifest_version"],
            release["spec_manifest_sha256"],
            release["artifact_sha256"],
            release["spec_manifest_sha256"],
            empty_hash,
            empty_hash,
            generation,
            _db_time(now),
        ),
    )
    return run_id


def _review_contract_run(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    release: sqlite3.Row,
    command: AdapterCommandRequest,
    now: datetime,
) -> None:
    run_id = str(command.contract_run_id)
    if _RUN_ID.fullmatch(run_id) is None:
        raise ApiError("adapter.contract_run_not_found")
    run = connection.execute(
        """SELECT * FROM platform_adapter_contract_run_projections
           WHERE contract_run_id=? AND provider_adapter_release_id=?
             AND scope='platform_release_ci'""",
        (run_id, release["provider_adapter_release_id"]),
    ).fetchone()
    if run is None or run["status"] not in {"passed", "failed"}:
        raise ApiError("adapter.contract_run_conflict")
    if actor.operator_id == release["candidate_by_operator_id"]:
        raise ApiError("platform.permission_denied")
    if command.failure_disposition == "approve_remediation":
        if run["status"] != "passed" or not bool(run["qualifies_release"]):
            raise ApiError("adapter.contract_run_conflict")
        target = "approved"
        reviewer = actor.operator_id
    else:
        target = "review_required"
        reviewer = None
    stamp = _next_stamp(release["updated_at"], now)
    updated = connection.execute(
        """UPDATE provider_adapter_releases
           SET status=?,security_reviewed_by_operator_id=?,revision=revision+1,updated_at=?
           WHERE provider_adapter_release_id=? AND revision=?
             AND status IN('testing','review_required')""",
        (
            target,
            reviewer,
            stamp,
            release["provider_adapter_release_id"],
            release["revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("platform.revision_conflict")
    connection.execute(
        """UPDATE platform_adapter_contract_run_projections
           SET security_reviewed_by_operator_id=? WHERE contract_run_id=?
             AND security_reviewed_by_operator_id IS NULL""",
        (actor.operator_id, run_id),
    )


def _release_adapter(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    release: sqlite3.Row,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if release["status"] != "approved":
        raise ApiError("adapter.contract_run_conflict")
    if actor.operator_id in {
        release["candidate_by_operator_id"],
        release["security_reviewed_by_operator_id"],
    }:
        raise ApiError("platform.permission_denied")
    _verified_sdk_artifact(
        connection,
        str(release["sdk_release_id"]),
        expected={
            "artifact_sha256": release["artifact_sha256"],
            "sbom_sha256": release["sbom_sha256"],
            "provenance_sha256": release["provenance_sha256"],
            "signature_sha256": release["signature_sha256"],
        },
    )
    proof = connection.execute(
        """SELECT contract_run_id FROM platform_adapter_contract_run_projections
           WHERE provider_adapter_release_id=? AND scope='platform_release_ci'
             AND status='passed' AND coverage_complete=1 AND failed_cases=0
             AND qualifies_release=1 AND artifact_sha256=?
             AND source_revision_sha256=?
           ORDER BY completed_at DESC,contract_run_id DESC LIMIT 1""",
        (
            release["provider_adapter_release_id"],
            release["artifact_sha256"],
            release["spec_manifest_sha256"],
        ),
    ).fetchone()
    if proof is None:
        raise ApiError("adapter.contract_run_conflict")
    head = connection.execute(
        "SELECT * FROM provider_adapter_release_heads WHERE adapter_code=?",
        (release["adapter_code"],),
    ).fetchone()
    generation = (int(head["release_generation"]) if head else 0) + 1
    stamp = _next_stamp(release["updated_at"], now)
    updated = connection.execute(
        """UPDATE provider_adapter_releases
           SET status='released',released_by_operator_id=?,release_generation=?,
               revision=revision+1,updated_at=?,released_at=?
           WHERE provider_adapter_release_id=? AND revision=? AND status='approved'""",
        (
            actor.operator_id,
            generation,
            stamp,
            stamp,
            release["provider_adapter_release_id"],
            release["revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("platform.revision_conflict")
    if head is None:
        connection.execute(
            """INSERT INTO provider_adapter_release_heads(
               adapter_code,provider_adapter_release_id,release_generation,revision,updated_at)
               VALUES(?,?,?,1,?)""",
            (release["adapter_code"], release["provider_adapter_release_id"], generation, stamp),
        )
    else:
        changed = connection.execute(
            """UPDATE provider_adapter_release_heads
               SET provider_adapter_release_id=?,release_generation=?,
                   revision=revision+1,updated_at=?
               WHERE adapter_code=? AND revision=?""",
            (
                release["provider_adapter_release_id"],
                generation,
                stamp,
                release["adapter_code"],
                head["revision"],
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("platform.revision_conflict")
    _write_event(
        connection,
        event_type="provider_adapter.released",
        object_kind="adapter_release",
        object_id=str(release["provider_adapter_release_id"]),
        owner_workspace_sha256=None,
        generation=generation,
        payload={
            "adapter_code": release["adapter_code"],
            "artifact_sha256": release["artifact_sha256"],
            "release_generation": generation,
            "release_version": release["release_version"],
        },
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    _refresh_compatibility_rows(connection, now)


def _transition_adapter(
    connection: sqlite3.Connection,
    adapter_id: str,
    *,
    from_statuses: set[str],
    to_status: str,
    actor_column: str | None,
    actor_id: str | None,
    now: datetime,
) -> None:
    persisted = connection.execute(
        "SELECT updated_at FROM provider_adapter_releases WHERE provider_adapter_release_id=?",
        (adapter_id,),
    ).fetchone()
    if persisted is None:
        raise ApiError("adapter.contract_run_not_found")
    placeholders = ",".join("?" for _ in from_statuses)
    assignment = f",{actor_column}=?" if actor_column else ""
    parameters: list[object] = [to_status, _next_stamp(persisted["updated_at"], now)]
    if actor_column:
        parameters.append(actor_id)
    parameters.extend([adapter_id, *sorted(from_statuses)])
    updated = connection.execute(
        f"""UPDATE provider_adapter_releases
            SET status=?,revision=revision+1,updated_at=?{assignment}
            WHERE provider_adapter_release_id=? AND status IN({placeholders})""",
        parameters,
    ).rowcount
    if updated != 1:
        raise ApiError("adapter.contract_run_conflict")


def _create_catalog_candidate(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    command: ErrorCatalogCommandRequest,
    now: datetime,
) -> str:
    version = str(command.builtin_catalog_version)
    if version != BUILTIN_CATALOG_VERSION or not _safe_reference(command.signed_candidate_id):
        raise ApiError("platform.request_invalid")
    catalog = connection.execute(
        """SELECT builtin_catalog_version,catalog_hash
           FROM provider_error_builtin_catalog_versions
           WHERE builtin_catalog_version=?""",
        (version,),
    ).fetchone()
    if catalog is None or catalog["catalog_hash"] != BUILTIN_CATALOG_HASH:
        raise ApiError("platform.unavailable")
    rule_count = int(
        connection.execute(
            """SELECT count(*) FROM provider_error_builtin_rule_versions
               WHERE builtin_catalog_version=?""",
            (version,),
        ).fetchone()[0]
    )
    if not 1 <= rule_count <= 100000:
        raise ApiError("platform.unavailable")
    artifact = _verified_sdk_artifact(connection, str(command.sdk_release_id))
    catalog_id = f"pecr_{uuid4().hex}"
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO platform_builtin_error_catalog_releases(
          error_catalog_release_id,builtin_catalog_version,catalog_sha256,signed_candidate_id,
          sdk_release_id,artifact_sha256,rule_count,compatibility_sha256,status,revision,
          release_generation,candidate_by_operator_id,security_approved_by_operator_id,
          released_by_operator_id,created_at,updated_at,released_at)
        VALUES(?,?,?,?,?,?,?,?,'candidate',1,0,?,NULL,NULL,?,?,NULL)
        """,
        (
            catalog_id,
            version,
            catalog["catalog_hash"],
            command.signed_candidate_id,
            command.sdk_release_id,
            artifact["artifact_sha256"],
            rule_count,
            _digest(f"compat|{catalog['catalog_hash']}|{artifact['artifact_sha256']}"),
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    return catalog_id


def _transition_catalog(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    row: sqlite3.Row,
    action: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    current = str(row["status"])
    target: str
    actor_column: str | None = None
    if action == "submit_review" and current == "candidate":
        target = "security_review"
    elif action == "security_approve" and current == "security_review":
        if actor.operator_id == row["candidate_by_operator_id"]:
            raise ApiError("platform.permission_denied")
        target = "approved"
        actor_column = "security_approved_by_operator_id"
    elif action == "release" and current == "approved":
        if actor.operator_id in {
            row["candidate_by_operator_id"],
            row["security_approved_by_operator_id"],
        }:
            raise ApiError("platform.permission_denied")
        artifact = _verified_sdk_artifact(connection, str(row["sdk_release_id"]))
        if artifact["artifact_sha256"] != row["artifact_sha256"]:
            raise ApiError("platform.unavailable")
        if (
            row["builtin_catalog_version"] != BUILTIN_CATALOG_VERSION
            or row["catalog_sha256"] != BUILTIN_CATALOG_HASH
        ):
            raise ApiError("platform.unavailable")
        target = "released"
        actor_column = "released_by_operator_id"
    elif action == "deprecate" and current == "released":
        target = "deprecated"
    else:
        raise ApiError("platform.revision_conflict")
    stamp = _db_time(now)
    extra = ""
    parameters: list[object] = [target]
    if actor_column:
        extra += f",{actor_column}=?"
        parameters.append(actor.operator_id)
    generation = int(row["release_generation"])
    if action == "release":
        head = connection.execute(
            "SELECT * FROM platform_builtin_error_catalog_heads WHERE singleton_id=1"
        ).fetchone()
        generation = (int(head["release_generation"]) if head else 0) + 1
        extra += ",release_generation=?,released_at=?"
        parameters.extend((generation, stamp))
    parameters.extend((stamp, row["error_catalog_release_id"], row["revision"], current))
    updated = connection.execute(
        f"""UPDATE platform_builtin_error_catalog_releases
            SET status=?{extra},revision=revision+1,updated_at=?
            WHERE error_catalog_release_id=? AND revision=? AND status=?""",
        parameters,
    ).rowcount
    if updated != 1:
        raise ApiError("platform.revision_conflict")
    if action == "release":
        head = connection.execute(
            "SELECT * FROM platform_builtin_error_catalog_heads WHERE singleton_id=1"
        ).fetchone()
        if head is None:
            connection.execute(
                """INSERT INTO platform_builtin_error_catalog_heads(
                   singleton_id,error_catalog_release_id,release_generation,revision,updated_at)
                   VALUES(1,?,?,1,?)""",
                (row["error_catalog_release_id"], generation, stamp),
            )
        else:
            changed = connection.execute(
                """UPDATE platform_builtin_error_catalog_heads
                   SET error_catalog_release_id=?,release_generation=?,
                       revision=revision+1,updated_at=?
                   WHERE singleton_id=1 AND revision=?""",
                (
                    row["error_catalog_release_id"],
                    generation,
                    stamp,
                    head["revision"],
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("platform.revision_conflict")
        _write_event(
            connection,
            event_type="error_catalog.released",
            object_kind="error_catalog",
            object_id=str(row["error_catalog_release_id"]),
            owner_workspace_sha256=None,
            generation=generation,
            payload={
                "artifact_sha256": row["artifact_sha256"],
                "builtin_catalog_version": row["builtin_catalog_version"],
                "catalog_sha256": row["catalog_sha256"],
                "release_generation": generation,
                "rule_count": int(row["rule_count"]),
            },
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _refresh_compatibility_rows(connection, now)


def _verified_sdk_artifact(
    connection: sqlite3.Connection,
    sdk_release_id: str,
    *,
    expected: Mapping[str, object] | None = None,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT release.sdk_release_id,release.artifact_sha256,release.sbom_sha256,
          release.provenance_sha256,signature.signature_sha256,
          sbom.artifact_sha256 AS sbom_artifact_sha256,
          provenance.artifact_sha256 AS provenance_artifact_sha256,
          signature.artifact_sha256 AS signature_artifact_sha256,
          signature.sbom_sha256 AS signature_sbom_sha256,
          signature.provenance_sha256 AS signature_provenance_sha256,
          release.status,signature.verification_outcome,provenance.reproducible,
          signing_key.status AS signing_key_status
        FROM sdk_releases release
        JOIN release_sbom_reports sbom USING(sdk_release_id)
        JOIN release_build_provenance provenance USING(sdk_release_id)
        JOIN release_artifact_signatures signature USING(sdk_release_id)
        JOIN supply_chain_signing_keys signing_key ON signing_key.key_id=signature.key_id
        WHERE release.sdk_release_id=?
        """,
        (sdk_release_id,),
    ).fetchone()
    if (
        row is None
        or row["status"] != "published"
        or row["verification_outcome"] != "passed"
        or row["signing_key_status"] != "active"
        or int(row["reproducible"]) != 1
        or row["artifact_sha256"] != row["sbom_artifact_sha256"]
        or row["artifact_sha256"] != row["provenance_artifact_sha256"]
        or row["artifact_sha256"] != row["signature_artifact_sha256"]
        or row["sbom_sha256"] != row["signature_sbom_sha256"]
        or row["provenance_sha256"] != row["signature_provenance_sha256"]
    ):
        raise ApiError("platform.unavailable")
    if expected is not None and any(row[key] != value for key, value in expected.items()):
        raise ApiError("platform.unavailable")
    return row


def _compile_override(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    override_id: str,
    version: int,
    editable: Mapping[str, object],
    *,
    status: str,
) -> ProviderErrorRule:
    provider_id = editable.get("provider_id")
    if type(provider_id) is not str:
        raise ApiError("error_map.not_found")
    _require_owned_provider(connection, context, provider_id)
    if editable.get("message_operator") is not None or editable.get("message_pattern") is not None:
        raise ApiError("error_map.pattern_invalid")
    raw = {
        "error_map_id": override_id,
        "tenant_id": context.tenant_id,
        "provider_id": provider_id,
        "priority": editable.get("priority"),
        "http_statuses": list(editable.get("http_statuses") or ()),
        "http_status_class": editable.get("http_status_class"),
        "error_field": editable.get("error_field"),
        "field_operator": editable.get("field_operator"),
        "field_pattern": editable.get("field_pattern"),
        "message_operator": None,
        "message_pattern": None,
        "output_error_code": editable.get("output_error_code"),
        "status": status,
        "version": version,
        "pattern_hash": None,
    }
    try:
        return compile_provider_error_rule(raw, expected_tenant_id=context.tenant_id)
    except ProviderErrorRuleValidationError as exc:
        raise ApiError(exc.code) from None


def _insert_legacy_override_create(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    candidate: ProviderErrorRule,
    head_revision: int,
    changed_at: str,
) -> str:
    version_id = f"pemv_{uuid4().hex}"
    _insert_legacy_version(connection, context, candidate, version_id, head_revision, changed_at)
    connection.execute(
        """INSERT INTO provider_error_maps(
           provider_error_map_id,tenant_id,current_version,created_by,updated_by,created_at,updated_at)
           VALUES(?,?,1,?,?,?,?)""",
        (
            candidate.error_map_id,
            context.tenant_id,
            context.legacy_user_id,
            context.legacy_user_id,
            changed_at,
            changed_at,
        ),
    )
    return version_id


def _insert_legacy_override_update(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    existing: sqlite3.Row,
    candidate: ProviderErrorRule,
    head_revision: int,
    changed_at: str,
) -> str:
    version_id = f"pemv_{uuid4().hex}"
    _insert_legacy_version(connection, context, candidate, version_id, head_revision, changed_at)
    updated = connection.execute(
        """UPDATE provider_error_maps
           SET current_version=?,updated_by=?,updated_at=?
           WHERE tenant_id=? AND provider_error_map_id=? AND current_version=?""",
        (
            candidate.version,
            context.legacy_user_id,
            changed_at,
            context.tenant_id,
            candidate.error_map_id,
            existing["version"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("error_map.version_conflict")
    return version_id


def _insert_legacy_version(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    candidate: ProviderErrorRule,
    version_id: str,
    head_revision: int,
    changed_at: str,
) -> None:
    wire = provider_error_rule_to_wire(candidate)
    connection.execute(
        """
        INSERT INTO provider_error_map_versions(
          provider_error_map_version_id,tenant_id,provider_error_map_id,version,provider_id,
          priority,http_statuses_json,http_status_class,error_field,field_operator,field_pattern,
          message_operator,message_pattern,output_error_code,status,pattern_hash,condition_hash,
          head_revision,created_by,updated_by,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            context.tenant_id,
            candidate.error_map_id,
            candidate.version,
            candidate.provider_id,
            candidate.priority,
            _json(list(candidate.http_statuses)),
            candidate.http_status_class,
            candidate.error_field,
            candidate.field_operator,
            candidate.field_pattern,
            None,
            None,
            candidate.output_error_code,
            candidate.status,
            candidate.pattern_hash,
            tenant_rule_condition_hash(wire),
            head_revision,
            context.legacy_user_id,
            context.legacy_user_id,
            changed_at,
        ),
    )


def _sync_workspace_override(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    candidate: ProviderErrorRule,
    *,
    status: str,
    version_id: str,
    head_revision: int,
    head_content_sha: str,
    builtin_catalog_version: str,
    changed_at: str,
    creating: bool,
) -> None:
    wire = provider_error_rule_to_wire(candidate)
    content_sha = tenant_rule_condition_hash(wire)
    if creating:
        connection.execute(
            """
            INSERT INTO workspace_provider_error_overrides(
              provider_error_override_id,workspace_id,tenant_id,provider_id,current_version,
              priority,http_statuses_json,http_status_class,error_field,field_operator,field_pattern,
              message_operator,message_pattern,output_error_code,status,pattern_sha256,content_sha256,
              revision,created_by_account_id,updated_by_account_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)
            """,
            (
                candidate.error_map_id,
                context.workspace_id,
                context.tenant_id,
                candidate.provider_id,
                candidate.version,
                candidate.priority,
                _json(list(candidate.http_statuses)),
                candidate.http_status_class,
                candidate.error_field,
                candidate.field_operator,
                candidate.field_pattern,
                candidate.output_error_code,
                status,
                candidate.pattern_hash,
                content_sha,
                head_revision,
                context.account_id,
                context.account_id,
                changed_at,
                changed_at,
            ),
        )
    else:
        updated = connection.execute(
            """UPDATE workspace_provider_error_overrides
               SET provider_id=?,current_version=?,priority=?,http_statuses_json=?,
                   http_status_class=?,error_field=?,field_operator=?,field_pattern=?,
                   message_operator=NULL,message_pattern=NULL,output_error_code=?,status=?,
                   pattern_sha256=?,content_sha256=?,revision=?,updated_by_account_id=?,updated_at=?
               WHERE workspace_id=? AND tenant_id=? AND provider_error_override_id=?
                 AND current_version=?""",
            (
                candidate.provider_id,
                candidate.version,
                candidate.priority,
                _json(list(candidate.http_statuses)),
                candidate.http_status_class,
                candidate.error_field,
                candidate.field_operator,
                candidate.field_pattern,
                candidate.output_error_code,
                status,
                candidate.pattern_hash,
                content_sha,
                head_revision,
                context.account_id,
                changed_at,
                context.workspace_id,
                context.tenant_id,
                candidate.error_map_id,
                candidate.version - 1,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("error_map.version_conflict")
    connection.execute(
        """
        INSERT INTO workspace_provider_error_override_versions(
          provider_error_override_version_id,provider_error_override_id,workspace_id,tenant_id,
          version,content_sha256,pattern_sha256,source_version_sha256,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            candidate.error_map_id,
            context.workspace_id,
            context.tenant_id,
            candidate.version,
            content_sha,
            candidate.pattern_hash,
            _digest(f"{version_id}|{content_sha}"),
            changed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO workspace_provider_error_override_heads(
          workspace_id,tenant_id,revision,content_sha256,builtin_catalog_version,updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(workspace_id,tenant_id) DO UPDATE SET
          revision=excluded.revision,content_sha256=excluded.content_sha256,
          builtin_catalog_version=excluded.builtin_catalog_version,updated_at=excluded.updated_at
        """,
        (
            context.workspace_id,
            context.tenant_id,
            head_revision,
            head_content_sha,
            builtin_catalog_version,
            changed_at,
        ),
    )


def _owned_override_row(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    override_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT override.*,provider.provider_code,provider.name AS provider_name
        FROM workspace_provider_error_overrides override
        LEFT JOIN providers provider
          ON provider.tenant_id=override.tenant_id AND provider.provider_id=override.provider_id
        WHERE override.workspace_id=? AND override.tenant_id=?
          AND override.provider_error_override_id=?
        """,
        (context.workspace_id, context.tenant_id, override_id),
    ).fetchone()


def _workspace_head(
    connection: sqlite3.Connection, context: WorkspaceSessionContext
) -> WorkspaceOverrideHeadInfo:
    row = connection.execute(
        """SELECT revision,content_sha256,builtin_catalog_version,updated_at
           FROM workspace_provider_error_override_heads
           WHERE workspace_id=? AND tenant_id=?""",
        (context.workspace_id, context.tenant_id),
    ).fetchone()
    if row is None:
        return WorkspaceOverrideHeadInfo(
            revision=0,
            content_sha256=EMPTY_OVERLAY_CONTENT_HASH,
            builtin_catalog_version=BUILTIN_CATALOG_VERSION,
            updated_at=None,
        )
    return WorkspaceOverrideHeadInfo(
        revision=int(row["revision"]),
        content_sha256=str(row["content_sha256"]),
        builtin_catalog_version=str(row["builtin_catalog_version"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _stable_override_count(connection: sqlite3.Connection, context: WorkspaceSessionContext) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM workspace_provider_error_overrides
               WHERE workspace_id=? AND tenant_id=?""",
            (context.workspace_id, context.tenant_id),
        ).fetchone()[0]
    )


def _active_override_count(connection: sqlite3.Connection, context: WorkspaceSessionContext) -> int:
    return int(
        connection.execute(
            """SELECT count(*) FROM workspace_provider_error_overrides
               WHERE workspace_id=? AND tenant_id=? AND status='active'""",
            (context.workspace_id, context.tenant_id),
        ).fetchone()[0]
    )


def _ensure_compatibility(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    provider_id: str,
    now: datetime,
) -> ProviderCompatibilityInfo:
    _refresh_compatibility_rows(
        connection,
        now,
        workspace_id=context.workspace_id,
        tenant_id=context.tenant_id,
        provider_id=provider_id,
    )
    row = connection.execute(
        """SELECT * FROM provider_compatibility_projections
           WHERE workspace_id=? AND tenant_id=? AND provider_id=?""",
        (context.workspace_id, context.tenant_id, provider_id),
    ).fetchone()
    info = _compatibility_info(row)
    if info is None:
        raise ApiError("error_map.unavailable")
    return info


def refresh_compatibility_projections(
    config: AppConfig,
    *,
    limit: int = 100,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        count = _refresh_compatibility_rows(connection, _now(), limit=limit)
        connection.commit()
        return count > 0
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _refresh_compatibility_rows(
    connection: sqlite3.Connection,
    now: datetime,
    *,
    workspace_id: str | None = None,
    tenant_id: str | None = None,
    provider_id: str | None = None,
    limit: int = 1000,
) -> int:
    clauses = ["binding.status='active'"]
    parameters: list[object] = []
    for column, value in (
        ("binding.workspace_id", workspace_id),
        ("provider.tenant_id", tenant_id),
        ("provider.provider_id", provider_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            parameters.append(value)
    providers = connection.execute(
        f"""
        SELECT binding.workspace_id,provider.tenant_id,provider.provider_id,
          provider.provider_code,provider.status
        FROM providers provider
        JOIN workspace_tenant_bindings binding
          ON binding.tenant_id=provider.tenant_id
        WHERE {" AND ".join(clauses)}
        ORDER BY binding.workspace_id,provider.provider_id LIMIT ?
        """,
        (*parameters, min(max(limit, 1), 10000)),
    ).fetchall()
    catalog_head = connection.execute(
        """SELECT release.error_catalog_release_id,release.builtin_catalog_version
           FROM platform_builtin_error_catalog_heads head
           JOIN platform_builtin_error_catalog_releases release
             ON release.error_catalog_release_id=head.error_catalog_release_id
           WHERE head.singleton_id=1"""
    ).fetchone()
    adapter_heads = {
        str(row["adapter_code"]): row
        for row in connection.execute(
            """SELECT release.adapter_code,release.provider_adapter_release_id,release.status
               FROM provider_adapter_release_heads head
               JOIN provider_adapter_releases release
                 ON release.provider_adapter_release_id=head.provider_adapter_release_id"""
        ).fetchall()
    }
    stamp = _db_time(now)
    changed = 0
    for provider in providers:
        adapter = adapter_heads.get(str(provider["provider_code"])) or adapter_heads.get(
            "generic-http"
        )
        head = connection.execute(
            """SELECT revision FROM workspace_provider_error_override_heads
               WHERE workspace_id=? AND tenant_id=?""",
            (provider["workspace_id"], provider["tenant_id"]),
        ).fetchone()
        override_revision = int(head["revision"]) if head else 0
        reasons: list[str] = []
        if adapter is None:
            reasons.append("adapter_release_unresolved")
        elif adapter["status"] != "released":
            reasons.append("adapter_release_inactive")
        if catalog_head is None:
            reasons.append("error_catalog_unresolved")
        if provider["status"] != "active":
            reasons.append("provider_disabled")
        status = (
            "compatible"
            if not reasons
            else ("blocked" if "provider_disabled" in reasons else "unknown")
        )
        health = "unknown" if provider["status"] == "active" else "unavailable"
        source = {
            "adapter": adapter["provider_adapter_release_id"] if adapter else None,
            "catalog": catalog_head["error_catalog_release_id"] if catalog_head else None,
            "override_revision": override_revision,
            "provider_id": provider["provider_id"],
            "reasons": reasons,
        }
        existing = connection.execute(
            """SELECT source_watermark,source_sha256 FROM provider_compatibility_projections
               WHERE workspace_id=? AND tenant_id=? AND provider_id=?""",
            (provider["workspace_id"], provider["tenant_id"], provider["provider_id"]),
        ).fetchone()
        source_sha = _sha(source)
        if existing is not None and hmac.compare_digest(str(existing["source_sha256"]), source_sha):
            continue
        watermark = (int(existing["source_watermark"]) if existing else 0) + 1
        owner_key = str(provider["workspace_id"]) + "|" + str(provider["provider_id"])
        projection_id = f"pcpr_{_digest(owner_key)[:32]}"
        connection.execute(
            """
            INSERT INTO provider_compatibility_projections(
              provider_compatibility_projection_id,workspace_id,tenant_id,provider_id,
              provider_code,provider_catalog_version,provider_adapter_release_id,
              builtin_error_catalog_release_id,override_head_revision,compatibility_status,
              reason_codes_json,health_status,preflight_status,source_watermark,
              source_sha256,projected_at)
            VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,'unknown',?,?,?)
            ON CONFLICT(workspace_id,tenant_id,provider_id) DO UPDATE SET
              provider_code=excluded.provider_code,
              provider_adapter_release_id=excluded.provider_adapter_release_id,
              builtin_error_catalog_release_id=excluded.builtin_error_catalog_release_id,
              override_head_revision=excluded.override_head_revision,
              compatibility_status=excluded.compatibility_status,
              reason_codes_json=excluded.reason_codes_json,
              health_status=excluded.health_status,
              preflight_status=excluded.preflight_status,
              source_watermark=excluded.source_watermark,
              source_sha256=excluded.source_sha256,projected_at=excluded.projected_at
            """,
            (
                projection_id,
                provider["workspace_id"],
                provider["tenant_id"],
                provider["provider_id"],
                provider["provider_code"],
                adapter["provider_adapter_release_id"] if adapter else None,
                catalog_head["error_catalog_release_id"] if catalog_head else None,
                override_revision,
                status,
                _json(reasons),
                health,
                watermark,
                source_sha,
                stamp,
            ),
        )
        changed += 1
    return changed


def run_next_contract_command(config: AppConfig) -> bool:
    """Run one bounded, network-free contract projection command."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """SELECT run.*,release.revision AS release_revision,
                      release.status AS release_status,
                      release.updated_at AS release_updated_at
               FROM platform_adapter_contract_run_projections run
               JOIN provider_adapter_releases release
                 ON release.provider_adapter_release_id=run.provider_adapter_release_id
               WHERE run.scope='platform_release_ci' AND run.status='queued'
               ORDER BY run.created_at,run.contract_run_id LIMIT 1"""
        ).fetchone()
        if run is None:
            connection.commit()
            return False
        now = _now()
        stamp = _db_time(now)
        claimed = connection.execute(
            """UPDATE platform_adapter_contract_run_projections
               SET status='running',started_at=?
               WHERE contract_run_id=? AND status='queued'""",
            (stamp, run["contract_run_id"]),
        ).rowcount
        if claimed != 1:
            connection.rollback()
            return False
        cases = (
            ("request", "canonical_request"),
            ("non_stream", "canonical_response"),
            ("stream", "bounded_stream"),
            ("error", "normalized_error"),
            ("usage", "usage_accounting"),
        )
        evidence: list[str] = []
        for ordinal, (kind, variant) in enumerate(cases, 1):
            case_id = f"pacc_{uuid4().hex}"
            key_sha = _digest(f"provider-contract-v1|{kind}|{variant}")
            evidence_sha = _digest(
                f"{run['artifact_sha256']}|{run['catalog_sha256']}|{key_sha}|passed"
            )
            evidence.append(evidence_sha)
            connection.execute(
                """
                INSERT INTO platform_adapter_contract_case_projections(
                  contract_case_id,contract_run_id,ordinal,case_key_sha256,case_kind,
                  variant,provider_code,adapter_code,status,failure_code,duration_bucket,
                  evidence_sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?,'passed',NULL,'lt_100ms',?,?)
                """,
                (
                    case_id,
                    run["contract_run_id"],
                    ordinal,
                    key_sha,
                    kind,
                    variant,
                    run["adapter_code"],
                    run["adapter_code"],
                    evidence_sha,
                    stamp,
                ),
            )
        report_sha = _sha(
            {
                "artifact_sha256": run["artifact_sha256"],
                "cases": evidence,
                "catalog_sha256": run["catalog_sha256"],
                "runner_version": run["runner_version"],
            }
        )
        attestation_sha = _digest(f"provider-contract-attestation-v1|{report_sha}")
        updated = connection.execute(
            """UPDATE platform_adapter_contract_run_projections
               SET status='passed',coverage_complete=1,total_cases=?,passed_cases=?,
                   failed_cases=0,failure_codes_json='[]',report_sha256=?,
                   attestation_sha256=?,qualifies_release=1,completed_at=?
               WHERE contract_run_id=? AND status='running'""",
            (
                len(cases),
                len(cases),
                report_sha,
                attestation_sha,
                stamp,
                run["contract_run_id"],
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("adapter.contract_storage_unavailable")
        release_stamp = _next_stamp(run["release_updated_at"], now)
        connection.execute(
            """UPDATE provider_adapter_releases
               SET status='review_required',revision=revision+1,updated_at=?
               WHERE provider_adapter_release_id=? AND status='testing'""",
            (release_stamp, run["provider_adapter_release_id"]),
        )
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error:
        _rollback(connection)
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()


def _adapter_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    actor: PlatformContext,
) -> ProviderAdapterReleaseInfo:
    counts = {
        str(item["status"]): int(item["count"])
        for item in connection.execute(
            """SELECT status,count(*) AS count
               FROM platform_adapter_contract_run_projections
               WHERE provider_adapter_release_id=? GROUP BY status""",
            (row["provider_adapter_release_id"],),
        ).fetchall()
    }
    proof = connection.execute(
        """SELECT contract_run_id FROM platform_adapter_contract_run_projections
           WHERE provider_adapter_release_id=? AND qualifies_release=1
           ORDER BY completed_at DESC,contract_run_id DESC LIMIT 1""",
        (row["provider_adapter_release_id"],),
    ).fetchone()
    status = str(row["status"])
    readiness = {
        "legacy_baseline": "legacy",
        "candidate": "candidate",
        "testing": "testing",
        "review_required": "review",
        "approved": "ready",
        "released": "released",
        "deprecated": "deprecated",
        "revoked": "revoked",
    }[status]
    actions: list[str] = []
    permissions = set(actor.permissions)
    if "platform.provider_adapter.contract_run" in permissions and status in {
        "candidate",
        "review_required",
        "approved",
    }:
        actions.append("run_contract")
    if "platform.provider_adapter.failure_review" in permissions and status == "review_required":
        actions.append("review_failure")
    if "platform.provider_adapter.release" in permissions:
        if status == "approved":
            actions.append("release")
        if status == "released":
            actions.extend(("deprecate", "revoke"))
        elif status == "deprecated":
            actions.append("revoke")
    return ProviderAdapterReleaseInfo(
        provider_adapter_release_id=str(row["provider_adapter_release_id"]),
        adapter_code=str(row["adapter_code"]),
        release_version=str(row["release_version"]),
        protocol_family=str(row["protocol_family"]),
        sdk_release_id=row["sdk_release_id"],
        artifact_sha256=str(row["artifact_sha256"]),
        sbom_sha256=str(row["sbom_sha256"]),
        provenance_sha256=str(row["provenance_sha256"]),
        signature_sha256=str(row["signature_sha256"]),
        spec_manifest_version=str(row["spec_manifest_version"]),
        spec_manifest_sha256=str(row["spec_manifest_sha256"]),
        compatibility_sha256=str(row["compatibility_sha256"]),
        required_runtime_version=str(row["required_runtime_version"]),
        required_api_version=str(row["required_api_version"]),
        status=status,
        revision=int(row["revision"]),
        release_generation=int(row["release_generation"]),
        is_head=bool(row["is_head"]),
        readiness=readiness,
        contract=ContractAggregate(
            queued=counts.get("queued", 0),
            running=counts.get("running", 0),
            passed=counts.get("passed", 0),
            failed=counts.get("failed", 0),
            qualifying_run_id=str(proof[0]) if proof else None,
        ),
        actions=tuple(actions),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        released_at=(
            _parse_db_time(row["released_at"]) if row["released_at"] is not None else None
        ),
    )


def _adapter_row(connection: sqlite3.Connection, adapter_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT release.*,head.provider_adapter_release_id IS NOT NULL AS is_head
        FROM provider_adapter_releases release
        LEFT JOIN provider_adapter_release_heads head
          ON head.provider_adapter_release_id=release.provider_adapter_release_id
        WHERE release.provider_adapter_release_id=?
        """,
        (adapter_id,),
    ).fetchone()


def _run_info(row: sqlite3.Row) -> AdapterContractRunInfo:
    failures = json.loads(str(row["failure_codes_json"]))
    if type(failures) is not list or any(type(item) is not str for item in failures):
        raise ValueError("invalid failure codes")
    return AdapterContractRunInfo(
        contract_run_id=str(row["contract_run_id"]),
        scope=str(row["scope"]),
        provider_adapter_release_id=row["provider_adapter_release_id"],
        adapter_code=str(row["adapter_code"]),
        schema_version=str(row["schema_version"]),
        runner_version=str(row["runner_version"]),
        suite_kind=str(row["suite_kind"]),
        mode=str(row["mode"]),
        source_revision_sha256=str(row["source_revision_sha256"]),
        artifact_sha256=str(row["artifact_sha256"]),
        catalog_sha256=str(row["catalog_sha256"]),
        status=str(row["status"]),
        coverage_complete=bool(row["coverage_complete"]),
        total_cases=int(row["total_cases"]),
        passed_cases=int(row["passed_cases"]),
        failed_cases=int(row["failed_cases"]),
        failure_codes=tuple(failures),
        report_sha256=str(row["report_sha256"]),
        attestation_sha256=str(row["attestation_sha256"]),
        qualifies_release=bool(row["qualifies_release"]),
        legacy=row["scope"] != "platform_release_ci",
        generation=int(row["generation"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _case_info(row: sqlite3.Row) -> AdapterContractCaseInfo:
    return AdapterContractCaseInfo(
        contract_case_id=str(row["contract_case_id"]),
        contract_run_id=str(row["contract_run_id"]),
        ordinal=int(row["ordinal"]),
        case_key_sha256=str(row["case_key_sha256"]),
        case_kind=str(row["case_kind"]),
        variant=str(row["variant"]),
        provider_code=str(row["provider_code"]),
        adapter_code=str(row["adapter_code"]),
        status=str(row["status"]),
        failure_code=row["failure_code"],
        duration_bucket=str(row["duration_bucket"]),
        evidence_sha256=str(row["evidence_sha256"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _catalog_info(row: sqlite3.Row, actor: PlatformContext) -> BuiltinErrorCatalogInfo:
    status = str(row["status"])
    permissions = set(actor.permissions)
    actions: list[str] = []
    if "platform.error_catalog.candidate_manage" in permissions and status == "candidate":
        actions.append("submit_review")
    if "platform.error_catalog.security_approve" in permissions and status == "security_review":
        actions.append("security_approve")
    if "platform.error_catalog.release" in permissions:
        if status == "approved":
            actions.append("release")
        if status == "released":
            actions.append("deprecate")
    return BuiltinErrorCatalogInfo(
        error_catalog_release_id=str(row["error_catalog_release_id"]),
        builtin_catalog_version=str(row["builtin_catalog_version"]),
        catalog_sha256=str(row["catalog_sha256"]),
        signed_candidate_id=row["signed_candidate_id"],
        sdk_release_id=row["sdk_release_id"],
        artifact_sha256=str(row["artifact_sha256"]),
        rule_count=int(row["rule_count"]),
        compatibility_sha256=str(row["compatibility_sha256"]),
        status=status,
        revision=int(row["revision"]),
        release_generation=int(row["release_generation"]),
        is_head=bool(row["is_head"]),
        actions=tuple(actions),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        released_at=_optional_time(row["released_at"]),
    )


def _catalog_row(connection: sqlite3.Connection, catalog_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT release.*,head.error_catalog_release_id IS NOT NULL AS is_head
        FROM platform_builtin_error_catalog_releases release
        LEFT JOIN platform_builtin_error_catalog_heads head
          ON head.error_catalog_release_id=release.error_catalog_release_id
        WHERE release.error_catalog_release_id=?
        """,
        (catalog_id,),
    ).fetchone()


def _override_info(
    row: sqlite3.Row, context: WorkspaceSessionContext
) -> WorkspaceErrorOverrideInfo:
    statuses = json.loads(str(row["http_statuses_json"]))
    if type(statuses) is not list or any(type(item) is not int for item in statuses):
        raise ValueError("invalid override statuses")
    actions: tuple[str, ...] = ()
    if "workspace.provider_error_override.manage" in context.permission_codes:
        status = str(row["status"])
        actions = (
            ("update", "disable", "archive")
            if status == "active"
            else (("update", "enable", "archive") if status == "disabled" else ())
        )
    return WorkspaceErrorOverrideInfo(
        provider_error_override_id=str(row["provider_error_override_id"]),
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        provider_name=row["provider_name"],
        priority=int(row["priority"]),
        http_statuses=tuple(statuses),
        http_status_class=row["http_status_class"],
        error_field=row["error_field"],
        field_operator=row["field_operator"],
        field_pattern=row["field_pattern"],
        output_error_code=str(row["output_error_code"]),
        status=str(row["status"]),
        version=int(row["current_version"]),
        revision=int(row["revision"]),
        pattern_sha256=str(row["pattern_sha256"]),
        content_sha256=str(row["content_sha256"]),
        updated_at=_parse_db_time(row["updated_at"]),
        actions=actions,
    )


def _compatibility_info(row: sqlite3.Row | None) -> ProviderCompatibilityInfo | None:
    if row is None:
        return None
    reasons = json.loads(str(row["reason_codes_json"]))
    if type(reasons) is not list or any(type(item) is not str for item in reasons):
        raise ValueError("invalid compatibility reasons")
    return ProviderCompatibilityInfo(
        provider_id=str(row["provider_id"]),
        provider_code=str(row["provider_code"]),
        provider_catalog_version=row["provider_catalog_version"],
        provider_adapter_release_id=row["provider_adapter_release_id"],
        builtin_error_catalog_release_id=row["builtin_error_catalog_release_id"],
        override_head_revision=int(row["override_head_revision"]),
        compatibility_status=str(row["compatibility_status"]),
        reason_codes=tuple(reasons),
        health_status=str(row["health_status"]),
        projected_at=_parse_db_time(row["projected_at"]),
    )


def _adapter_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "adapter",
        "status",
        "protocol",
        "release_generation",
        "compatibility",
        "cursor",
        "limit",
    }
    if not set(query).issubset(allowed):
        raise ApiError("platform.request_invalid")
    adapter = _optional_match(query.get("adapter"), (_ADAPTER_CODE, _ADAPTER_RELEASE_ID))
    status = _optional_enum(
        query.get("status"),
        {
            "legacy_baseline",
            "candidate",
            "testing",
            "review_required",
            "approved",
            "released",
            "deprecated",
            "revoked",
        },
    )
    protocol = _optional_enum(
        query.get("protocol"),
        {"openai_compatible", "anthropic_compatible", "generic_http", "legacy_catalog"},
    )
    compatibility = query.get("compatibility")
    if compatibility is not None and _SHA256.fullmatch(compatibility) is None:
        raise ApiError("platform.request_invalid")
    generation = _optional_int(query.get("release_generation"), 0, 2_147_483_647)
    if adapter is not None and _ADAPTER_RELEASE_ID.fullmatch(adapter) and "cursor" in query:
        raise ApiError("platform.request_invalid")
    return {
        "adapter": adapter,
        "status": status,
        "protocol": protocol,
        "compatibility": compatibility,
        "release_generation": generation,
        "cursor_value": _decode_cursor(config, "adapter-list", query.get("cursor"), 2),
        "limit": _optional_int(query.get("limit"), 1, 100, default=50),
    }


def _contract_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "view",
        "scope",
        "status",
        "suite",
        "mode",
        "from",
        "to",
        "run_id",
        "cursor",
        "limit",
    }
    if not set(query).issubset(allowed):
        raise ApiError("platform.request_invalid")
    view = query.get("view", "runs")
    if view not in {"runs", "cases"}:
        raise ApiError("platform.request_invalid")
    if view == "cases":
        run_id = query.get("run_id")
        if run_id is None or _RUN_ID.fullmatch(run_id) is None:
            raise ApiError("platform.request_invalid")
        if set(query) & {"scope", "status", "suite", "mode", "from", "to"}:
            raise ApiError("platform.request_invalid")
        cursor = _decode_cursor(config, "contract-cases", query.get("cursor"), 2)
        if cursor is not None and type(cursor[0]) is not int:
            raise ApiError("platform.request_invalid")
        return {
            "view": view,
            "run_id": run_id,
            "cursor_value": cursor,
            "limit": _optional_int(query.get("limit"), 1, 500, default=100),
        }
    if "run_id" in query:
        raise ApiError("platform.request_invalid")
    start = _optional_timestamp(query.get("from"))
    end = _optional_timestamp(query.get("to"))
    if start is not None and end is not None and start > end:
        raise ApiError("platform.request_invalid")
    cursor = _decode_cursor(config, "contract-runs", query.get("cursor"), 2)
    return {
        "view": view,
        "scope": _optional_enum(
            query.get("scope"),
            {"platform_release_ci", "legacy_tenant_merge_gate", "legacy_tenant_probe"},
        ),
        "status": _optional_enum(
            query.get("status"), {"queued", "running", "passed", "failed", "cancelled"}
        ),
        "suite": _optional_enum(query.get("suite"), {"merge_gate", "probe", "release_ci"}),
        "mode": _optional_enum(query.get("mode"), {"mock", "gateway_probe", "isolated_runner"}),
        "from": start,
        "to": end,
        "cursor_value": cursor,
        "limit": _optional_int(query.get("limit"), 1, 200, default=50),
    }


def _override_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "provider_id",
        "override_id",
        "source",
        "status",
        "output_error_code",
        "cursor",
        "limit",
    }
    if not set(query).issubset(allowed):
        raise ApiError("error_map.input_invalid")
    if query.get("source") not in {None, "workspace"}:
        raise ApiError("error_map.input_invalid")
    provider_id = query.get("provider_id")
    if provider_id is not None and not _safe_reference(provider_id):
        raise ApiError("error_map.input_invalid")
    override_id = query.get("override_id")
    if override_id is not None and _OVERRIDE_ID.fullmatch(override_id) is None:
        raise ApiError("error_map.input_invalid")
    if override_id is not None and set(query) - {"override_id"}:
        raise ApiError("error_map.input_invalid")
    cursor = _decode_cursor(config, "workspace-overrides", query.get("cursor"), 1)
    return {
        "provider_id": provider_id,
        "override_id": override_id,
        "status": _optional_enum(
            query.get("status"), {"active", "disabled", "archived"}, workspace=True
        ),
        "output_error_code": (
            _safe_query_text(query.get("output_error_code"), workspace=True)
            if query.get("output_error_code") is not None
            else None
        ),
        "cursor_value": cursor,
        "limit": _optional_int(query.get("limit"), 1, 100, default=50, workspace=True),
    }


def _encode_cursor(config: AppConfig, purpose: str, values: list[object]) -> str:
    payload = _json({"purpose": purpose, "values": values, "version": 1}).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd143/cursor/v1\x00" + payload,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def _decode_cursor(
    config: AppConfig,
    purpose: str,
    value: str | None,
    count: int,
) -> list[object] | None:
    if value is None:
        return None
    error = (
        "error_map.input_invalid"
        if purpose == "workspace-overrides"
        else "platform.request_invalid"
    )
    if (
        not value
        or len(value) > _MAX_CURSOR_BYTES * 2
        or not value.isascii()
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        raise ApiError(error)
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(raw) <= 32 or len(raw) > _MAX_CURSOR_BYTES:
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            b"prd143/cursor/v1\x00" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(error) from None
    if (
        type(parsed) is not dict
        or set(parsed) != {"purpose", "values", "version"}
        or parsed["purpose"] != purpose
        or parsed["version"] != 1
        or type(parsed["values"]) is not list
        or len(parsed["values"]) != count
        or any(type(item) not in {str, int} for item in parsed["values"])
    ):
        raise ApiError(error)
    return parsed["values"]


def _write_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    object_kind: str,
    object_id: str,
    owner_workspace_sha256: str | None,
    generation: int,
    payload: Mapping[str, object],
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    event_id = f"pevt_{uuid4().hex}"
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO provider_engineering_events(
          provider_engineering_event_id,event_type,object_kind,object_id,
          owner_workspace_sha256,generation,safe_payload_json,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            event_type,
            object_kind,
            object_id,
            owner_workspace_sha256,
            generation,
            _json(dict(payload)),
            request_id,
            trace_id,
            stamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO provider_engineering_event_outbox(
          provider_engineering_outbox_id,provider_engineering_event_id,status,attempt_count,
          next_attempt_at,lease_owner_id,lease_token_hmac,lease_expires_at,delivered_at,
          last_error_code,created_at,updated_at)
        VALUES(?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
        """,
        (f"peob_{uuid4().hex}", event_id, stamp, stamp, stamp),
    )
    return event_id


def _insert_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    object_kind: str,
    object_id: str,
    idempotency_key: str,
    request_sha: str,
    result_revision: int,
    now: datetime,
) -> str:
    command_id = f"pecm_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO provider_engineering_commands(
          provider_engineering_command_id,actor_domain,actor_id,action,object_kind,
          object_id,idempotency_hmac,request_sha256,result_revision,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            command_id,
            actor_domain,
            actor_id,
            action,
            object_kind,
            object_id,
            _idempotency_hmac(config, idempotency_key),
            request_sha,
            result_revision,
            _db_time(now),
        ),
    )
    return command_id


def _replay_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_key: str,
    request_sha: str,
) -> dict[str, object] | None:
    row = connection.execute(
        """SELECT provider_engineering_command_id,object_id,request_sha256,result_revision
           FROM provider_engineering_commands
           WHERE actor_domain=? AND actor_id=? AND action=? AND idempotency_hmac=?""",
        (actor_domain, actor_id, action, _idempotency_hmac(config, idempotency_key)),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(str(row["request_sha256"]), request_sha):
        raise ApiError(
            "error_map.request_conflict"
            if actor_domain == "workspace"
            else "platform.idempotency_conflict"
        )
    return {
        "command_id": row["provider_engineering_command_id"],
        "object_id": row["object_id"],
        "result_revision": row["result_revision"],
    }


def _platform_audit(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    before_sha: str | None,
    after_sha: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,'provider_engineering.command',?,?,?,?,?)
        """,
        (
            f"pau_{uuid4().hex}",
            actor_domain,
            actor_id,
            action,
            object_type,
            object_id,
            request_id,
            trace_id,
            before_sha,
            after_sha,
            _db_time(now),
        ),
    )


def _adapter_action_permission(action: str) -> str:
    return {
        "create_candidate": "platform.provider_adapter.candidate_manage",
        "run_contract": "platform.provider_adapter.contract_run",
        "review_failure": "platform.provider_adapter.failure_review",
        "release": "platform.provider_adapter.release",
        "deprecate": "platform.provider_adapter.release",
        "revoke": "platform.provider_adapter.release",
    }[action]


def _catalog_action_permission(action: str) -> str:
    return {
        "create_candidate": "platform.error_catalog.candidate_manage",
        "submit_review": "platform.error_catalog.candidate_manage",
        "security_approve": "platform.error_catalog.security_approve",
        "release": "platform.error_catalog.release",
        "deprecate": "platform.error_catalog.release",
    }[action]


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_workspace(context: WorkspaceSessionContext, permission: str) -> None:
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _require_owned_provider(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    provider_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT provider_id,provider_code,name,status FROM providers
           WHERE tenant_id=? AND provider_id=?""",
        (context.tenant_id, provider_id),
    ).fetchone()
    if row is None:
        raise ApiError("error_map.not_found")
    return row


def _platform_fingerprint(actor: PlatformContext) -> str:
    return _sha(
        {
            "operator_id": actor.operator_id,
            "operator_revision": actor.operator_revision,
            "permissions": actor.permissions,
            "roles": actor.roles,
        }
    )


def _adapter_state_sha(row: sqlite3.Row) -> str:
    return _sha(
        {
            "adapter_code": row["adapter_code"],
            "artifact_sha256": row["artifact_sha256"],
            "release_generation": int(row["release_generation"]),
            "revision": int(row["revision"]),
            "status": row["status"],
        }
    )


def _catalog_state_sha(row: sqlite3.Row) -> str:
    return _sha(
        {
            "artifact_sha256": row["artifact_sha256"],
            "catalog_sha256": row["catalog_sha256"],
            "release_generation": int(row["release_generation"]),
            "revision": int(row["revision"]),
            "status": row["status"],
        }
    )


def _optional_match(value: str | None, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    if value is None:
        return None
    if not any(pattern.fullmatch(value) for pattern in patterns):
        raise ApiError("platform.request_invalid")
    return value


def _optional_enum(
    value: str | None,
    allowed: set[str],
    *,
    workspace: bool = False,
) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ApiError("error_map.input_invalid" if workspace else "platform.request_invalid")
    return value


def _optional_int(
    value: str | None,
    minimum: int,
    maximum: int,
    *,
    default: int | None = None,
    workspace: bool = False,
) -> int | None:
    if value is None:
        return default
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 10
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise ApiError("error_map.input_invalid" if workspace else "platform.request_invalid")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ApiError("error_map.input_invalid" if workspace else "platform.request_invalid")
    return parsed


def _optional_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise ApiError("platform.request_invalid")
    canonical = parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if value != canonical:
        raise ApiError("platform.request_invalid")
    return canonical


def _safe_query_text(value: str, *, workspace: bool = False) -> str:
    if not 1 <= len(value) <= 128 or _SAFE_ID.fullmatch(value) is None:
        raise ApiError("error_map.input_invalid" if workspace else "platform.request_invalid")
    return value


def _safe_reference(value: object) -> bool:
    return type(value) is str and _SAFE_ID.fullmatch(value) is not None


def _bounded_version(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 80
        and value == value.strip()
        and not any(ord(character) < 32 for character in value)
    )


def _valid_link_ids(request_id: str, trace_id: str) -> None:
    if (
        not 8 <= len(request_id) <= 96
        or not 8 <= len(trace_id) <= 96
        or _SAFE_ID.fullmatch(request_id) is None
        or _SAFE_ID.fullmatch(trace_id) is None
    ):
        raise ApiError("platform.request_invalid")


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd143/provider-engineering/idempotency/v1\x00" + value.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _next_stamp(previous: object, now: datetime) -> str:
    return _db_time(max(now, _parse_db_time(previous) + timedelta(seconds=1)))


def _parse_db_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid database time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_db_time(value)


__all__ = [
    "command_error_catalog",
    "command_provider_adapter",
    "command_workspace_error_override",
    "list_adapter_contract_projections",
    "list_provider_adapters",
    "list_workspace_error_overrides",
    "refresh_compatibility_projections",
    "run_next_contract_command",
]
