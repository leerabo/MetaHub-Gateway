"""Database-authoritative PRD-146 Workspace navigation services."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth.service import authentication_database_deadline
from metahub_control.identity.service import authenticate_global_session_in_transaction
from metahub_control.workspace_access.service import (
    WorkspaceActorPermissionSnapshot,
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
    workspace_actor_permissions_in_transaction,
)

from .catalog import (
    ADVANCED_TOOL_SPECS,
    CONTRACT_SPECS,
    GROUP_CODES,
    LEGACY_SOURCE_IDS_0137,
    ROLE_TASK_SPECS,
    AdvancedToolSpec,
    ContractSpec,
)
from .models import (
    AdvancedTool,
    AdvancedToolOpenResponse,
    AdvancedToolsResponse,
    GettingStartedResponse,
    NavigationGroup,
    NavigationItem,
    NavigationPreferencePatch,
    NavigationPreferenceProjection,
    NavigationPreferenceResponse,
    RoleTask,
    WorkspaceNavigationResponse,
    WorkspacePageVisitRequest,
    WorkspacePageVisitResponse,
)


_CONTRACT_VERSION = "workspace-navigation-v1"
_SOURCE_CATALOG_VERSION = "console-navigation-0137"
# Page-view telemetry is best-effort and must never hold an authentication
# boundary behind SQLite writer admission. Keep its total writer wait below
# the console logout/navigation budget; normal uncontended writes are unchanged.
_PAGE_VISIT_WRITE_DEADLINE_SECONDS = 2.0
_PUBLIC_PLACEMENTS = frozenset(
    {"primary", "detail", "advanced", "account", "developer_center", "task_home"}
)
_SIDEBAR_PLACEMENTS = frozenset({"primary", "task_home", "developer_center"})
_LEGACY_PLATFORM_INTERNAL_SOURCE_ID = "environments"
_LEGACY_TENANT_DATA_SOURCE_ID = "tenant-data-lifecycle"


def get_workspace_navigation(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceNavigationResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _workspace_actor(connection, config, session_token)
        _require_workspace(actor, "workspace.navigation.read")
        contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
        projection = _ensure_projection(connection, contract)
        visible = _visible_specs(connection, projection, actor.permission_codes)
        primary = tuple(item for item in visible if item.placement in {"primary", "task_home"})
        primary_groups = {item.group_code for item in primary if item.group_code is not None}
        visible_groups = {
            item.group_code for item in visible
            if item.placement in _SIDEBAR_PLACEMENTS and item.group_code is not None
        }
        preference = _preference_projection(connection, actor, contract, primary, visible_groups)
        groups = tuple(
            NavigationGroup(
                group_code=group_code,
                label_key=f"navigation.group.{group_code}",
                expanded=preference.expanded_group_code == group_code,
                items=tuple(
                    NavigationItem(
                        route_code=item.route_code,
                        label_key=item.label_key,
                        description_key=item.description_key,
                        kind="task_home" if item.placement == "task_home" else "primary",
                        order=item.order,
                        required_permissions=item.permissions,
                    )
                    for item in primary
                    if item.group_code == group_code
                ),
            )
            for group_code in GROUP_CODES
            if group_code in primary_groups
        )
        item_set_digest = _keyed_digest(
            config,
            "navigation-item-set",
            actor.authorization_fingerprint,
            [item.route_code for item in visible],
        )
        navigation_epoch = _record_member_navigation_state(
            connection,
            config,
            actor,
            contract,
            item_set_digest,
            request_id,
            trace_id,
        )
        advanced_available = "workspace.advanced_tool.read" in actor.permission_codes and any(
            _allowed(tool.permissions, actor.permission_codes) for tool in ADVANCED_TOOL_SPECS
        )
        result = WorkspaceNavigationResponse(
            page_visit_session_ref=_pseudonym(
                config, "page-visit-session", actor.workspace_session_id,
            ),
            owner_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
            account_locale=_account_locale(connection, actor.account_id),
            contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
            contract_version=_CONTRACT_VERSION,
            contract_generation=int(contract["generation"]),
            projection_generation=int(projection["generation"]),
            projection_status=str(projection["status"]),
            navigation_epoch=navigation_epoch,
            groups=groups,
            detail_route_codes=tuple(
                item.route_code for item in visible if item.placement == "detail"
            ),
            developer_route_codes=tuple(
                item.route_code for item in visible if item.placement == "developer_center"
            ),
            account_action_codes=tuple(
                item.route_code for item in visible if item.placement == "account"
            ),
            advanced_tools_available=advanced_available,
            preference=preference,
            item_set_digest=item_set_digest,
            membership_revision=_membership_revision(connection, actor.membership_id),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            observed_at=_parse_time(str(projection["observed_at"])),
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_audit(
            connection,
            config,
            actor,
            "workspace.navigation.read",
            contract,
            "success",
            request_id,
            trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        if connection is not None:
            _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        if connection is not None:
            _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def get_workspace_getting_started(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> GettingStartedResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _workspace_actor(connection, config, session_token)
        _require_workspace(actor, "workspace.task_home.read")
        contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
        _ensure_projection(connection, contract)
        version, task_rows = _ensure_task_home(
            connection, config, actor, contract, request_id, trace_id
        )
        tasks = tuple(
            RoleTask(
                task_code=str(row["task_code"]),
                title_key=f"task.{row['task_code']}.title",
                description_key=f"task.{row['task_code']}.description",
                status=str(row["status"]),
                priority=int(row["priority"]),
                action_route_code=row["action_route_code"],
                action_enabled=(
                    row["action_route_code"] is not None
                    and row["status"] in {"ready", "needs_attention", "completed"}
                ),
                source_freshness=str(row["freshness"]),
            )
            for row in task_rows
        )
        next_task = next(
            (task.task_code for task in tasks
             if task.action_enabled and task.status != "completed"),
            None,
        )
        navigation_epoch = _current_navigation_epoch(connection, actor.membership_id)
        result = GettingStartedResponse(
            owner_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
            role_code=actor.membership_role,
            task_generation=int(version["generation"]),
            tasks=tasks,
            next_task_code=next_task,
            completed_count=sum(task.status == "completed" for task in tasks),
            needs_attention_count=sum(task.status == "needs_attention" for task in tasks),
            source_freshness=("fresh" if version["status"] == "ready" else "stale"),
            evidence_digest=str(version["source_digest"]),
            contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
            contract_generation=int(contract["generation"]),
            navigation_epoch=navigation_epoch,
            membership_revision=_membership_revision(connection, actor.membership_id),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            observed_at=_parse_time(str(version["observed_at"])),
            request_id=request_id,
            trace_id=trace_id,
        )
        outcome = "empty" if not tasks else "success"
        _write_audit(
            connection,
            config,
            actor,
            "workspace.task_home.read",
            contract,
            outcome,
            request_id,
            trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        if connection is not None:
            _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        if connection is not None:
            _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def get_workspace_advanced_tools(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> AdvancedToolsResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _workspace_actor(connection, config, session_token)
        _require_workspace(actor, "workspace.advanced_tool.read")
        contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
        _ensure_projection(connection, contract)
        heads = _ensure_advanced_entitlements(connection, actor.workspace_id, actor.tenant_id)
        tools = tuple(
            _advanced_tool(heads[spec.tool_code], spec)
            for spec in ADVANCED_TOOL_SPECS
            if _allowed(spec.permissions, actor.permission_codes)
        )
        generation = max((int(row["generation"]) for row in heads.values()), default=1)
        result = AdvancedToolsResponse(
            owner_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
            tools=tools,
            entitlement_generation=generation,
            contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
            contract_generation=int(contract["generation"]),
            navigation_epoch=_current_navigation_epoch(connection, actor.membership_id),
            membership_revision=_membership_revision(connection, actor.membership_id),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            observed_at=_now(),
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_audit(
            connection,
            config,
            actor,
            "workspace.advanced_tool.read",
            contract,
            "success" if tools else "empty",
            request_id,
            trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def change_navigation_preference(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    patch: NavigationPreferencePatch,
    request_id: str,
    trace_id: str,
) -> NavigationPreferenceResponse:
    connection = _connect(config, "identity.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        member = _identity_membership(connection, identity.account_id, patch.workspace_id)
        actor = workspace_actor_permissions_in_transaction(
            connection,
            account_id=identity.account_id,
            workspace_id=str(member["workspace_id"]),
            tenant_id=str(member["tenant_id"]),
        )
        contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
        projection = _ensure_projection(connection, contract)
        if not hmac.compare_digest(
            str(contract["navigation_contract_snapshot_id"]), patch.contract_snapshot_id
        ):
            raise ApiError("identity.revision_conflict")
        visible = _visible_specs(connection, projection, actor.permission_codes)
        primary_routes = {
            item.route_code for item in visible if item.placement in {"primary", "task_home"}
        }
        visible_groups = {
            item.group_code
            for item in visible
            if item.placement in _SIDEBAR_PLACEMENTS and item.group_code is not None
        }
        if (
            "last_primary_route_code" in patch.model_fields_set
            and patch.last_primary_route_code is not None
            and patch.last_primary_route_code not in primary_routes
        ):
            raise ApiError("identity.request_invalid")
        if (
            "expanded_group_code" in patch.model_fields_set
            and patch.expanded_group_code is not None
            and patch.expanded_group_code not in visible_groups
        ):
            raise ApiError("identity.request_invalid")
        current = _active_preference(connection, actor.account_id, actor.workspace_id)
        current_revision = int(current["revision"]) if current is not None else 0
        request_payload = {
            "workspace_id": patch.workspace_id,
            "contract_snapshot_id": patch.contract_snapshot_id,
            "expected_revision": expected_revision,
            "fields": sorted(patch.model_fields_set),
            "last_primary_route_code": patch.last_primary_route_code,
            "expanded_group_code": patch.expanded_group_code,
            "sidebar_collapsed": patch.sidebar_collapsed,
        }
        request_sha256 = _sha(request_payload)
        idempotency_hmac = _idempotency_hmac(
            config,
            "preference",
            str(member["workspace_membership_id"]),
            idempotency_key,
        )
        replay = connection.execute(
            """SELECT * FROM workspace_navigation_preference_commands
               WHERE workspace_id=? AND tenant_id=? AND account_id=?
                 AND workspace_membership_id=? AND idempotency_key_hmac=?""",
            (
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                str(member["workspace_membership_id"]),
                idempotency_hmac,
            ),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("identity.idempotency_conflict")
            response = _preference_replay(
                replay,
                actor,
                contract,
                int(member["revision"]),
                request_id,
                trace_id,
            )
            connection.commit()
            return response
        if expected_revision != current_revision:
            raise ApiError("identity.revision_conflict")
        old_route = current["last_primary_route_code"] if current is not None else None
        old_group = current["expanded_group_code"] if current is not None else None
        old_sidebar = bool(current["sidebar_collapsed"]) if current is not None else False
        new_route = (
            patch.last_primary_route_code
            if "last_primary_route_code" in patch.model_fields_set
            else old_route
        )
        new_group = (
            patch.expanded_group_code
            if "expanded_group_code" in patch.model_fields_set
            else old_group
        )
        new_sidebar = (
            patch.sidebar_collapsed
            if "sidebar_collapsed" in patch.model_fields_set
            else old_sidebar
        )
        changed = current is None or (old_route, old_group, old_sidebar) != (
            new_route,
            new_group,
            new_sidebar,
        )
        result_revision = current_revision + 1 if changed else current_revision
        stamp = _db_time(now)
        if current is None:
            preference_id = _id("nvp_")
            connection.execute(
                """INSERT INTO workspace_navigation_preferences(
                     navigation_preference_id,workspace_id,tenant_id,account_id,
                     workspace_membership_id,membership_revision,last_primary_route_code,
                     expanded_group_code,sidebar_collapsed,contract_snapshot_id,status,
                     revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'active',1,?,?)""",
                (
                    preference_id,
                    actor.workspace_id,
                    actor.tenant_id,
                    actor.account_id,
                    str(member["workspace_membership_id"]),
                    int(member["revision"]),
                    new_route,
                    new_group,
                    int(bool(new_sidebar)),
                    str(contract["navigation_contract_snapshot_id"]),
                    stamp,
                    stamp,
                ),
            )
            outcome = "created"
        else:
            preference_id = str(current["navigation_preference_id"])
            outcome = "changed" if changed else "unchanged"
            if changed:
                updated = connection.execute(
                    """UPDATE workspace_navigation_preferences
                       SET membership_revision=?,last_primary_route_code=?,expanded_group_code=?,
                         sidebar_collapsed=?,contract_snapshot_id=?,revision=?,updated_at=?
                       WHERE navigation_preference_id=? AND revision=? AND status='active'""",
                    (
                        int(member["revision"]),
                        new_route,
                        new_group,
                        int(bool(new_sidebar)),
                        str(contract["navigation_contract_snapshot_id"]),
                        result_revision,
                        stamp,
                        preference_id,
                        current_revision,
                    ),
                ).rowcount
                if updated != 1:
                    raise ApiError("identity.revision_conflict")
        response = NavigationPreferenceResponse(
            preference_id=preference_id,
            workspace_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
            contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
            contract_generation=int(contract["generation"]),
            navigation_epoch=_current_navigation_epoch(
                connection, str(member["workspace_membership_id"])
            ),
            membership_revision=int(member["revision"]),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            last_primary_route_code=new_route,
            expanded_group_code=new_group,
            sidebar_collapsed=bool(new_sidebar),
            revision=result_revision,
            changed=changed,
            idempotent_replay=False,
            updated_at=now,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.execute(
            """INSERT INTO workspace_navigation_preference_commands(
                 navigation_preference_command_id,workspace_id,tenant_id,account_id,
                 workspace_membership_id,idempotency_key_hmac,request_sha256,
                 revision_before,revision_after,response_json,outcome,request_id,trace_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _id("npc_"),
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                str(member["workspace_membership_id"]),
                idempotency_hmac,
                request_sha256,
                current_revision,
                result_revision,
                _json(response.model_dump(mode="json")),
                outcome,
                request_id,
                trace_id,
                stamp,
            ),
        )
        _write_identity_audit(
            connection, config, identity.account_id, actor, contract, request_id, trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("identity.idempotency_conflict") from None
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("identity.unavailable") from None
    finally:
        connection.close()


def open_workspace_advanced_tool(
    config: AppConfig,
    *,
    session_token: str,
    tool_code: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> AdvancedToolOpenResponse:
    connection = _connect(config, "workspace_session.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _workspace_actor(connection, config, session_token)
        _require_workspace(actor, "workspace.advanced_tool.open")
        spec = next((item for item in ADVANCED_TOOL_SPECS if item.tool_code == tool_code), None)
        if spec is None or not _allowed(spec.permissions, actor.permission_codes):
            raise ApiError("workspace_session.permission_denied")
        contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
        heads = _ensure_advanced_entitlements(connection, actor.workspace_id, actor.tenant_id)
        entitlement = heads[tool_code]
        request_sha256 = _sha(
            {
                "tool_code": tool_code,
                "expected_revision": expected_revision,
                "contract": contract["navigation_contract_snapshot_id"],
                "permission_fingerprint": actor.authorization_fingerprint,
            }
        )
        idempotency_hmac = _idempotency_hmac(
            config,
            "advanced-open",
            actor.membership_id,
            tool_code,
            idempotency_key,
        )
        replay = connection.execute(
            """SELECT * FROM workspace_navigation_events
               WHERE event_type='advanced_tool.opened' AND workspace_id=? AND tenant_id=?
                 AND aggregate_id=? AND idempotency_key_hmac=?""",
            (actor.workspace_id, actor.tenant_id, tool_code, idempotency_hmac),
        ).fetchone()
        if int(entitlement["revision"]) != expected_revision:
            raise ApiError("identity.revision_conflict")
        if entitlement["availability"] != "available":
            raise ApiError("workspace_session.unavailable")
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("identity.idempotency_conflict")
            payload = json.loads(str(replay["payload_json"]))
            result = AdvancedToolOpenResponse(
                owner_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
                tool_code=str(payload["tool_code"]),
                route_code=str(payload["route_code"]),
                contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
                contract_generation=int(contract["generation"]),
                entitlement_revision=int(payload["entitlement_revision"]),
                navigation_epoch=_current_navigation_epoch(connection, actor.membership_id),
                membership_revision=_membership_revision(connection, actor.membership_id),
                permission_revision=actor.permission_revision,
                permission_fingerprint=actor.authorization_fingerprint,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        payload = {
            "tool_code": tool_code,
            "route_code": spec.route_code,
            "entitlement_revision": int(entitlement["revision"]),
            "actor_pseudonym": _pseudonym(config, "workspace-actor", actor.account_id),
        }
        _insert_event(
            connection,
            actor.workspace_id,
            actor.tenant_id,
            "advanced_tool.opened",
            "advanced_tool",
            tool_code,
            int(entitlement["generation"]),
            _sha({"tool": tool_code, "idempotency": idempotency_hmac}),
            payload,
            request_id,
            trace_id,
            idempotency_hmac=idempotency_hmac,
            request_sha256=request_sha256,
        )
        result = AdvancedToolOpenResponse(
            owner_ref=_owner_ref(config, actor.workspace_id, actor.tenant_id),
            tool_code=tool_code,
            route_code=spec.route_code,
            contract_snapshot_id=str(contract["navigation_contract_snapshot_id"]),
            contract_generation=int(contract["generation"]),
            entitlement_revision=int(entitlement["revision"]),
            navigation_epoch=_current_navigation_epoch(connection, actor.membership_id),
            membership_revision=_membership_revision(connection, actor.membership_id),
            permission_revision=actor.permission_revision,
            permission_fingerprint=actor.authorization_fingerprint,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        _write_audit(
            connection,
            config,
            actor,
            "workspace.advanced_tool.open",
            contract,
            "success",
            request_id,
            trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("identity.idempotency_conflict") from None
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def record_workspace_page_visit(
    config: AppConfig,
    *,
    session_token: str,
    payload: WorkspacePageVisitRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> WorkspacePageVisitResponse:
    connection = None
    try:
        # Page visits are best-effort telemetry from navigation and unload
        # beacons. Do the authority and replay preflight in a deferred read
        # transaction, then acquire the writer only for the final authority
        # check and INSERT. Navigation reads also persist audit facts, so
        # holding BEGIN IMMEDIATE across the preflight made otherwise harmless
        # beacons compete with the main page load for the same SQLite writer.
        deadline = time.monotonic() + _PAGE_VISIT_WRITE_DEADLINE_SECONDS
        with database_operation_deadline(deadline):
            connection = _connect(config, "workspace_session.unavailable")
            connection.execute("BEGIN")
            actor, spec = _page_visit_authority(
                connection, config, session_token, payload, monotonic_deadline=deadline,
            )
            event_id = "cpe_" + _idempotency_hmac(
                config, "page-visit", actor.workspace_session_id, idempotency_key,
            )
            request_sha256 = _sha(payload.model_dump(mode="json"))
            replay = connection.execute(
                "SELECT visit_request_sha256 FROM console_page_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["visit_request_sha256"]), request_sha256):
                    raise ApiError("identity.idempotency_conflict")
            if replay is not None:
                # A replay does not need a writer. The preflight already
                # authenticated the current session and verified the original
                # request hash, so release the read snapshot immediately.
                connection.commit()
                return WorkspacePageVisitResponse(
                    event_id=event_id, idempotent_replay=True,
                    permission_fingerprint=actor.authorization_fingerprint,
                    request_id=request_id, trace_id=trace_id,
                )

            # Release the read snapshot before writer admission. This is the
            # important contention boundary: a navigation request can now
            # finish its own short write without waiting behind this beacon's
            # authentication queries.
            connection.commit()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise sqlite3.OperationalError("page visit writer deadline exceeded")
                connection.execute(
                    f"PRAGMA busy_timeout = {max(1, min(250, int(remaining * 1000)))}"
                )
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).casefold() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.025)

            final_actor, final_spec = _page_visit_authority(
                connection, config, session_token, payload,
                final=True, monotonic_deadline=deadline,
            )
            final_event_id = "cpe_" + _idempotency_hmac(
                config, "page-visit", final_actor.workspace_session_id, idempotency_key,
            )
            replay = connection.execute(
                "SELECT visit_request_sha256 FROM console_page_events WHERE event_id=?",
                (final_event_id,),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["visit_request_sha256"]), request_sha256):
                    raise ApiError("identity.idempotency_conflict")
                connection.commit()
                return WorkspacePageVisitResponse(
                    event_id=final_event_id, idempotent_replay=True,
                    permission_fingerprint=final_actor.authorization_fingerprint,
                    request_id=request_id, trace_id=trace_id,
                )
            event_type = {
                "overview": "dashboard.viewed",
                "provider-health": "status.viewed",
                "migration-guide": "migration.guide_viewed",
            }.get(final_spec.source_id, "console.page_viewed")
            connection.execute(
                """INSERT INTO console_page_events(
                  event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
                  target_section,path,request_id,trace_id,payload_json,created_at,
                  workspace_session_id,visit_request_sha256
                ) VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)""",
                (
                    final_event_id, final_actor.tenant_id, final_actor.legacy_user_id, event_type,
                    final_spec.source_id, final_spec.source_id, f"/#{final_spec.source_id}", request_id, trace_id,
                    _json({"hash": f"#{final_spec.source_id}", "source": payload.source,
                           "viewport_class": payload.viewport_class}),
                    _db_time(_now()), final_actor.workspace_session_id, request_sha256,
                ),
            )
            connection.commit()
            return WorkspacePageVisitResponse(
                event_id=final_event_id, idempotent_replay=False,
                permission_fingerprint=final_actor.authorization_fingerprint,
                request_id=request_id, trace_id=trace_id,
            )
    except ApiError:
        if connection is not None:
            _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        if connection is not None:
            _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        if connection is not None:
            connection.close()


def _page_visit_authority(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    payload: WorkspacePageVisitRequest,
    *,
    final: bool = False,
    monotonic_deadline: float | None = None,
) -> tuple[WorkspaceSessionContext, ContractSpec]:
    actor = _workspace_actor(
        connection, config, session_token, monotonic_deadline=monotonic_deadline,
    )
    _require_workspace(actor, "workspace.navigation.read")
    session_ref = _pseudonym(config, "page-visit-session", actor.workspace_session_id)
    if (
        payload.page_visit_session_ref != session_ref
        or payload.permission_fingerprint != actor.authorization_fingerprint
    ):
        raise ApiError("workspace_session.permission_denied")
    contract = _ensure_contract(connection, actor.workspace_id, actor.tenant_id)
    if (
        payload.contract_snapshot_id != contract["navigation_contract_snapshot_id"]
        or payload.navigation_epoch != _current_navigation_epoch(connection, actor.membership_id)
    ):
        raise ApiError("identity.revision_conflict")
    if final:
        # The first authority pass already verified the materialized projection.
        # Recheck the live actor, contract snapshot and epoch after the insert,
        # then resolve the route from the immutable catalog without rebuilding
        # the projection and its source rows a second time.
        spec = next(
            (
                item for item in CONTRACT_SPECS
                if item.route_code == payload.route_code
                and item.placement in {"primary", "detail", "task_home", "developer_center"}
                and _contract_item_allowed(item, actor.permission_codes)
            ),
            None,
        )
        if spec is None:
            raise ApiError("workspace_session.permission_denied")
        return actor, spec
    visible = _visible_specs(
        connection, _ensure_projection(connection, contract), actor.permission_codes,
    )
    spec = next((item for item in visible if item.route_code == payload.route_code
                 and item.placement in {
                     "primary", "detail", "task_home", "developer_center",
                 }), None)
    if spec is None:
        raise ApiError("workspace_session.permission_denied")
    return actor, spec


def _workspace_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    *,
    monotonic_deadline: float | None = None,
) -> WorkspaceSessionContext:
    if monotonic_deadline is None:
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            touch=False,
        )
    else:
        with authentication_database_deadline(
            connection,
            monotonic_deadline=monotonic_deadline,
            error_code="workspace_session.unavailable",
        ):
            actor = authenticate_workspace_session_in_transaction(
                connection,
                config,
                session_token=session_token,
                touch=False,
            )
    if actor.membership_role == "service_account" or not actor.legacy_user_id:
        raise ApiError("workspace_session.permission_denied")
    return actor


def _require_workspace(actor: WorkspaceSessionContext, permission: str) -> None:
    if permission not in actor.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _ensure_contract(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> sqlite3.Row:
    source_sha = _sha({"version": _SOURCE_CATALOG_VERSION, "ids": LEGACY_SOURCE_IDS_0137})
    disposition_sha = _sha(
        [
            (item.source_id, item.disposition, item.placement, item.route_code)
            for item in CONTRACT_SPECS
            if item.source_kind == "excel"
        ]
    )
    extra_sha = _sha(
        [
            (item.source_id, item.placement, item.route_code)
            for item in CONTRACT_SPECS
            if item.source_kind == "extra"
        ]
    )
    head = connection.execute(
        """SELECT snapshot.* FROM workspace_navigation_contract_heads head
           JOIN workspace_navigation_contract_snapshots snapshot
             ON snapshot.navigation_contract_snapshot_id=head.navigation_contract_snapshot_id
           WHERE head.workspace_id=? AND head.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    if head is not None:
        legacy_disposition_sha = _sha(
            [
                (
                    item.source_id,
                    item.disposition,
                    _legacy_placement(item),
                    item.route_code,
                )
                for item in CONTRACT_SPECS
                if item.source_kind == "excel"
            ]
        )
        if (
            head["status"] != "active"
            or head["contract_version"] != _CONTRACT_VERSION
            or int(head["item_count"]) != len(CONTRACT_SPECS)
            or not hmac.compare_digest(str(head["source_catalog_sha256"]), source_sha)
            or (
                not hmac.compare_digest(str(head["disposition_sha256"]), disposition_sha)
                and not hmac.compare_digest(str(head["disposition_sha256"]), legacy_disposition_sha)
            )
            or not hmac.compare_digest(str(head["extra_sha256"]), extra_sha)
        ):
            raise ApiError("workspace_session.unavailable")
        count = connection.execute(
            """SELECT count(*) FROM workspace_navigation_contract_items
               WHERE navigation_contract_snapshot_id=?""",
            (head["navigation_contract_snapshot_id"],),
        ).fetchone()[0]
        if count != len(CONTRACT_SPECS):
            raise ApiError("workspace_session.unavailable")
        return head
    stamp = _db_time(_now())
    snapshot_id = _stable_id("ncs_", f"{workspace_id}:{tenant_id}:{_CONTRACT_VERSION}:1")
    connection.execute(
        """INSERT INTO workspace_navigation_contract_snapshots(
             navigation_contract_snapshot_id,workspace_id,tenant_id,contract_version,
             source_catalog_version,source_catalog_sha256,disposition_sha256,extra_sha256,
             item_count,status,revision,generation,created_at)
           VALUES(?,?,?,?,?,?,?,?,68,'active',1,1,?)""",
        (
            snapshot_id,
            workspace_id,
            tenant_id,
            _CONTRACT_VERSION,
            _SOURCE_CATALOG_VERSION,
            source_sha,
            disposition_sha,
            extra_sha,
            stamp,
        ),
    )
    for item in CONTRACT_SPECS:
        permission_json = _json(list(item.permissions))
        item_sha = _sha(
            (
                item.source_id,
                item.source_kind,
                item.disposition,
                item.placement,
                item.route_code,
                item.group_code,
                item.permissions,
                item.order,
            )
        )
        connection.execute(
            """INSERT INTO workspace_navigation_contract_items(
                 navigation_contract_item_id,navigation_contract_snapshot_id,workspace_id,
                 tenant_id,contract_generation,source_navigation_id,source_kind,
                 disposition_code,placement,route_code,group_code,
                 required_permissions_json,display_order,item_sha256,created_at)
               VALUES(?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)""",
            (
                _stable_id("nci_", f"{snapshot_id}:{item.source_id}"),
                snapshot_id,
                workspace_id,
                tenant_id,
                item.source_id,
                item.source_kind,
                item.disposition,
                item.placement,
                item.route_code,
                item.group_code,
                permission_json,
                item.order,
                item_sha,
                stamp,
            ),
        )
    connection.execute(
        """INSERT INTO workspace_navigation_contract_heads(
             workspace_id,tenant_id,navigation_contract_snapshot_id,generation,revision,updated_at)
           VALUES(?,?,?,1,1,?)""",
        (workspace_id, tenant_id, snapshot_id, stamp),
    )
    return connection.execute(
        """SELECT * FROM workspace_navigation_contract_snapshots
           WHERE navigation_contract_snapshot_id=?""",
        (snapshot_id,),
    ).fetchone()


def _ensure_projection(connection: sqlite3.Connection, contract: sqlite3.Row) -> sqlite3.Row:
    workspace_id = str(contract["workspace_id"])
    tenant_id = str(contract["tenant_id"])
    projection = connection.execute(
        """SELECT version.* FROM workspace_navigation_projection_heads head
           JOIN workspace_navigation_projection_versions version
             ON version.workspace_navigation_projection_id=head.workspace_navigation_projection_id
           WHERE head.workspace_id=? AND head.tenant_id=?""",
        (workspace_id, tenant_id),
    ).fetchone()
    public_specs = tuple(item for item in CONTRACT_SPECS if item.placement in _PUBLIC_PLACEMENTS)
    content_sha = _sha(
        [
            (item.source_id, item.placement, item.route_code, item.permissions)
            for item in public_specs
        ]
    )
    legacy_content_sha = _sha(
        [
            (
                item.source_id,
                _legacy_placement(item),
                item.route_code,
                _legacy_permissions(item),
            )
            for item in CONTRACT_SPECS
            if item.placement in _PUBLIC_PLACEMENTS
            or item.source_id == _LEGACY_PLATFORM_INTERNAL_SOURCE_ID
        ]
    )
    if projection is not None:
        if projection["navigation_contract_snapshot_id"] != contract[
            "navigation_contract_snapshot_id"
        ] or (
            not hmac.compare_digest(str(projection["content_sha256"]), content_sha)
            and not hmac.compare_digest(str(projection["content_sha256"]), legacy_content_sha)
        ):
            raise ApiError("workspace_session.unavailable")
        return projection
    stamp = _db_time(_now())
    projection_id = _stable_id("wnp_", f"{workspace_id}:{tenant_id}:{content_sha}:1")
    connection.execute(
        """INSERT INTO workspace_navigation_projection_versions(
             workspace_navigation_projection_id,workspace_id,tenant_id,
             navigation_contract_snapshot_id,contract_generation,entitlement_generation,
             status,content_sha256,generation,observed_at,created_at)
           VALUES(?,?,?,?,1,1,'ready',?,1,?,?)""",
        (
            projection_id,
            workspace_id,
            tenant_id,
            contract["navigation_contract_snapshot_id"],
            content_sha,
            stamp,
            stamp,
        ),
    )
    for item in public_specs:
        permission_json = _json(list(item.permissions))
        connection.execute(
            """INSERT INTO workspace_navigation_projection_items(
                 workspace_navigation_projection_id,workspace_id,tenant_id,
                 projection_generation,source_navigation_id,route_code,placement,
                 group_code,required_permissions_json,display_order,item_sha256)
               VALUES(?,?,?,1,?,?,?,?,?,?,?)""",
            (
                projection_id,
                workspace_id,
                tenant_id,
                item.source_id,
                item.route_code,
                item.placement,
                item.group_code,
                permission_json,
                item.order,
                _sha((item.source_id, item.route_code, item.placement, item.permissions)),
            ),
        )
    for source_code, revision, watermark in (
        ("catalog", 1, _SOURCE_CATALOG_VERSION),
        ("permissions", 1, "workspace-session-current"),
        ("entitlements", 1, "advanced-tool-v1"),
        ("deployment", 1, "deployment-current"),
    ):
        connection.execute(
            """INSERT INTO workspace_navigation_projection_sources(
                 workspace_navigation_projection_id,workspace_id,tenant_id,
                 projection_generation,source_code,source_revision,source_watermark,freshness)
               VALUES(?,?,?,1,?,?,?,'fresh')""",
            (projection_id, workspace_id, tenant_id, source_code, revision, watermark),
        )
    connection.execute(
        """INSERT INTO workspace_navigation_projection_heads(
             workspace_id,tenant_id,workspace_navigation_projection_id,generation,revision,updated_at)
           VALUES(?,?,?,1,1,?)""",
        (workspace_id, tenant_id, projection_id, stamp),
    )
    return connection.execute(
        """SELECT * FROM workspace_navigation_projection_versions
           WHERE workspace_navigation_projection_id=?""",
        (projection_id,),
    ).fetchone()


def _visible_specs(
    connection: sqlite3.Connection,
    projection: sqlite3.Row,
    permission_codes: tuple[str, ...],
) -> tuple[ContractSpec, ...]:
    projection_id = str(projection["workspace_navigation_projection_id"])
    expected = {
        item.source_id: item for item in CONTRACT_SPECS if item.placement in _PUBLIC_PLACEMENTS
    }
    rows = connection.execute(
        """SELECT * FROM workspace_navigation_projection_items
           WHERE workspace_navigation_projection_id=?
           ORDER BY display_order,source_navigation_id""",
        (projection_id,),
    ).fetchall()
    legacy_rows = [
        row for row in rows
        if row["source_navigation_id"] == _LEGACY_PLATFORM_INTERNAL_SOURCE_ID
        and row["placement"] == "primary"
    ]
    if len(legacy_rows) > 1 or len(rows) - len(legacy_rows) != len(expected):
        raise ApiError("workspace_session.unavailable")
    visible: list[ContractSpec] = []
    for row in rows:
        source_id = str(row["source_navigation_id"])
        if source_id == _LEGACY_PLATFORM_INTERNAL_SOURCE_ID and row["placement"] == "primary":
            legacy_item = next(
                item for item in CONTRACT_SPECS
                if item.source_id == _LEGACY_PLATFORM_INTERNAL_SOURCE_ID
            )
            try:
                legacy_permissions = tuple(json.loads(str(row["required_permissions_json"])))
            except (json.JSONDecodeError, TypeError, ValueError):
                raise ApiError("workspace_session.unavailable") from None
            if (
                row["route_code"] != legacy_item.route_code
                or row["group_code"] != legacy_item.group_code
                or int(row["display_order"]) != legacy_item.order
                or legacy_permissions != _legacy_permissions(legacy_item)
                or row["item_sha256"]
                != _sha(
                    (
                        legacy_item.source_id,
                        legacy_item.route_code,
                        "primary",
                        _legacy_permissions(legacy_item),
                    )
                )
            ):
                raise ApiError("workspace_session.unavailable")
            continue
        item = expected.get(source_id)
        try:
            permissions = tuple(json.loads(str(row["required_permissions_json"])))
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ApiError("workspace_session.unavailable") from None
        if item is None:
            raise ApiError("workspace_session.unavailable")
        legacy_permissions = (
            _legacy_permissions(item)
            if item.source_id == _LEGACY_TENANT_DATA_SOURCE_ID
            else item.permissions
        )
        if (
            row["route_code"] != item.route_code
            or row["placement"] != item.placement
            or row["group_code"] != item.group_code
            or int(row["display_order"]) != item.order
            or (permissions != item.permissions and permissions != legacy_permissions)
            or row["item_sha256"] != _sha(
                (
                    item.source_id,
                    item.route_code,
                    item.placement,
                    permissions,
                )
            )
        ):
            raise ApiError("workspace_session.unavailable")
        if _contract_item_allowed(item, permission_codes) or (
            item.source_id == _LEGACY_TENANT_DATA_SOURCE_ID
            and _allowed(legacy_permissions, permission_codes)
        ):
            visible.append(item)
    sources = connection.execute(
        """SELECT source_code,source_revision,source_watermark,freshness
           FROM workspace_navigation_projection_sources
           WHERE workspace_navigation_projection_id=? ORDER BY source_code""",
        (projection_id,),
    ).fetchall()
    expected_sources = {
        "catalog": (1, _SOURCE_CATALOG_VERSION),
        "deployment": (1, "deployment-current"),
        "entitlements": (1, "advanced-tool-v1"),
        "permissions": (1, "workspace-session-current"),
    }
    actual_sources = {
        str(row["source_code"]): (
            int(row["source_revision"]),
            str(row["source_watermark"]),
            str(row["freshness"]),
        )
        for row in sources
    }
    if actual_sources != {
        code: (revision, watermark, "fresh")
        for code, (revision, watermark) in expected_sources.items()
    }:
        raise ApiError("workspace_session.unavailable")
    return tuple(visible)


def _allowed(required: tuple[str, ...], granted: tuple[str, ...]) -> bool:
    return set(required) <= set(granted)


def _contract_item_allowed(item: ContractSpec, granted: tuple[str, ...]) -> bool:
    if item.source_id == "usage":
        return bool({"billing.usage.read", "virtual_key.read"} & set(granted))
    if item.source_id == _LEGACY_TENANT_DATA_SOURCE_ID:
        # Organization owners/admins already have the tenant settings read
        # capability. Use it for the lifecycle overview while keeping export,
        # download and deletion on their dedicated permissions.
        return bool({"tenant_data.read", "tenant.settings.read"} & set(granted))
    return _allowed(item.permissions, granted)


def _legacy_placement(item: ContractSpec) -> str:
    return (
        "primary"
        if item.source_id == _LEGACY_PLATFORM_INTERNAL_SOURCE_ID
        else item.placement
    )


def _legacy_permissions(item: ContractSpec) -> tuple[str, ...]:
    if item.source_id == _LEGACY_TENANT_DATA_SOURCE_ID:
        return ("tenant_data.read",)
    return item.permissions


def _preference_projection(
    connection: sqlite3.Connection,
    actor: WorkspaceSessionContext,
    contract: sqlite3.Row,
    primary: tuple[ContractSpec, ...],
    visible_groups: set[str],
) -> NavigationPreferenceProjection:
    row = _active_preference(connection, actor.account_id, actor.workspace_id)
    if (
        row is None
        or row["workspace_membership_id"] != actor.membership_id
        or int(row["membership_revision"]) != _membership_revision(connection, actor.membership_id)
        or row["contract_snapshot_id"] != contract["navigation_contract_snapshot_id"]
    ):
        return NavigationPreferenceProjection(
            revision=0,
            last_primary_route_code=None,
            expanded_group_code=None,
            sidebar_collapsed=False,
        )
    routes = {item.route_code for item in primary}
    route = row["last_primary_route_code"]
    group = row["expanded_group_code"]
    return NavigationPreferenceProjection(
        revision=int(row["revision"]),
        last_primary_route_code=str(route) if route in routes else None,
        expanded_group_code=str(group) if group in visible_groups else None,
        sidebar_collapsed=bool(row["sidebar_collapsed"]),
    )


def _active_preference(
    connection: sqlite3.Connection, account_id: str, workspace_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM workspace_navigation_preferences
           WHERE account_id=? AND workspace_id=? AND status='active'""",
        (account_id, workspace_id),
    ).fetchone()


def _record_member_navigation_state(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    contract: sqlite3.Row,
    item_set_digest: str,
    request_id: str,
    trace_id: str,
) -> int:
    revision = _membership_revision(connection, actor.membership_id)
    row = connection.execute(
        "SELECT * FROM workspace_navigation_member_states WHERE workspace_membership_id=?",
        (actor.membership_id,),
    ).fetchone()
    stamp = _db_time(_now())
    if row is None:
        connection.execute(
            """INSERT INTO workspace_navigation_member_states(
                 workspace_membership_id,workspace_id,tenant_id,membership_revision,
                 permission_fingerprint,contract_snapshot_id,item_set_digest,
                 navigation_epoch,updated_at)
               VALUES(?,?,?,?,?,?,?,1,?)""",
            (
                actor.membership_id,
                actor.workspace_id,
                actor.tenant_id,
                revision,
                actor.authorization_fingerprint,
                contract["navigation_contract_snapshot_id"],
                item_set_digest,
                stamp,
            ),
        )
        return 1
    changed = (
        int(row["membership_revision"]) != revision
        or row["permission_fingerprint"] != actor.authorization_fingerprint
        or row["contract_snapshot_id"] != contract["navigation_contract_snapshot_id"]
        or row["item_set_digest"] != item_set_digest
    )
    if not changed:
        return int(row["navigation_epoch"])
    epoch = int(row["navigation_epoch"]) + 1
    connection.execute(
        """UPDATE workspace_navigation_member_states
           SET membership_revision=?,permission_fingerprint=?,contract_snapshot_id=?,
             item_set_digest=?,navigation_epoch=?,updated_at=?
           WHERE workspace_membership_id=? AND navigation_epoch=?""",
        (
            revision,
            actor.authorization_fingerprint,
            contract["navigation_contract_snapshot_id"],
            item_set_digest,
            epoch,
            stamp,
            actor.membership_id,
            int(row["navigation_epoch"]),
        ),
    )
    payload = {
        "contract_snapshot_id": contract["navigation_contract_snapshot_id"],
        "navigation_epoch": epoch,
        "membership_pseudonym": _pseudonym(config, "workspace-membership", actor.membership_id),
        "old_item_set_digest": row["item_set_digest"],
        "new_item_set_digest": item_set_digest,
    }
    _insert_event(
        connection,
        actor.workspace_id,
        actor.tenant_id,
        "workspace_navigation.changed",
        "navigation",
        actor.membership_id,
        epoch,
        _sha((actor.membership_id, epoch, item_set_digest)),
        payload,
        request_id,
        trace_id,
    )
    return epoch


def _current_navigation_epoch(connection: sqlite3.Connection, membership_id: str) -> int:
    row = connection.execute(
        """SELECT navigation_epoch FROM workspace_navigation_member_states
           WHERE workspace_membership_id=?""",
        (membership_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 1


_TASK_SOURCE_QUERIES = {
    "workspace_profile": (
        "SELECT count(*) FROM organization_profiles WHERE workspace_id=? AND length(trim(name))>0",
        "workspace",
    ),
    "members": (
        "SELECT count(*) FROM workspace_memberships WHERE workspace_id=? "
        "AND status='active' AND role_code!='service_account'",
        "workspace",
    ),
    "subscription": (
        "SELECT count(*) FROM workspace_subscription_projections "
        "WHERE workspace_id=? AND projection_state!='unknown'",
        "workspace",
    ),
    "providers": (
        "SELECT count(*) FROM providers p WHERE p.tenant_id=? AND p.status='active' "
        "AND p.health_status='healthy' AND (p.auth_type='none' OR EXISTS("
        "SELECT 1 FROM provider_credentials c WHERE c.tenant_id=p.tenant_id "
        "AND c.provider_id=p.provider_id AND c.status='active' AND length(c.encrypted_key)>0))",
        "tenant",
    ),
    "models": (
        "SELECT count(*) FROM model_aliases a JOIN models m "
        "ON m.tenant_id=a.tenant_id AND m.model_id=a.default_model_id "
        "JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id "
        "WHERE a.tenant_id=? AND a.status='active' AND m.status='active' AND p.status='active' "
        "AND p.health_status='healthy' AND (p.auth_type='none' OR EXISTS("
        "SELECT 1 FROM provider_credentials c WHERE c.tenant_id=p.tenant_id "
        "AND c.provider_id=p.provider_id AND c.status='active' AND length(c.encrypted_key)>0))",
        "tenant",
    ),
    "projects": ("SELECT count(*) FROM projects WHERE tenant_id=?", "tenant"),
    "consumer_keys": (
        "SELECT count(*) FROM virtual_keys k JOIN virtual_key_consumers b "
        "ON b.tenant_id=k.tenant_id AND b.virtual_key_id=k.virtual_key_id "
        "JOIN workspace_consumers c ON c.tenant_id=b.tenant_id AND c.consumer_id=b.consumer_id "
        "WHERE k.tenant_id=? AND k.status='active' AND c.status='active'",
        "tenant",
    ),
    "usage": (
        "SELECT count(*) FROM request_logs WHERE tenant_id=? AND status='success'",
        "tenant",
    ),
    "policies": ("SELECT count(*) FROM policies WHERE tenant_id=?", "tenant"),
    "dlp": ("SELECT count(*) FROM dlp_rules WHERE tenant_id=?", "tenant"),
    "audit": ("SELECT count(*) FROM audit_logs WHERE tenant_id=?", "tenant"),
    "budgets": ("SELECT count(*) FROM budget_rules WHERE tenant_id=?", "tenant"),
    "finops": (
        "SELECT count(*) FROM internal_finops_ledger_owner_bindings WHERE workspace_id=?",
        "workspace",
    ),
    "data_lifecycle": (
        "SELECT count(*) FROM tenant_data_states WHERE tenant_id=? AND lifecycle_status='active'",
        "tenant",
    ),
    "status": (
        "SELECT count(*) FROM workspaces WHERE workspace_id=? AND status='active'",
        "workspace",
    ),
}


def _task_source_facts(
    connection: sqlite3.Connection,
    actor: WorkspaceSessionContext,
    source_codes: frozenset[str],
) -> dict[str, tuple[int, str]]:
    facts: dict[str, tuple[int, str]] = {}
    for source_code in sorted(source_codes):
        query, owner_kind = _TASK_SOURCE_QUERIES[source_code]
        owner_value = actor.workspace_id if owner_kind == "workspace" else actor.tenant_id
        try:
            count = connection.execute(query, (owner_value,)).fetchone()[0]
            facts[source_code] = (int(count), "fresh")
        except sqlite3.Error:
            facts[source_code] = (0, "unavailable")
    return facts


def _ensure_task_home(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    contract: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    specs = tuple(
        task
        for task in ROLE_TASK_SPECS
        if actor.membership_role in task.roles
        and _allowed(task.permissions, actor.permission_codes)
    )
    facts = _task_source_facts(
        connection,
        actor,
        frozenset(task.source_code for task in specs),
    )
    source_digest = _sha(
        {
            "task_action_revision": 2,
            "role": actor.membership_role,
            "membership_revision": _membership_revision(connection, actor.membership_id),
            "permission_fingerprint": actor.authorization_fingerprint,
            "contract": contract["navigation_contract_snapshot_id"],
            "facts": facts,
            "tasks": [
                (
                    task.task_code, task.source_code, task.action_route_code,
                    task.priority, task.writable,
                )
                for task in specs
            ],
        }
    )
    head = connection.execute(
        """SELECT version.* FROM workspace_role_task_home_heads head
           JOIN workspace_role_task_home_versions version
             ON version.role_task_home_version_id=head.role_task_home_version_id
           WHERE head.workspace_membership_id=?""",
        (actor.membership_id,),
    ).fetchone()
    if head is not None and hmac.compare_digest(str(head["source_digest"]), source_digest):
        return head, _task_rows(connection, str(head["role_task_home_version_id"]))
    old_tasks = (
        {
            str(row["task_code"]): str(row["status"])
            for row in _task_rows(connection, str(head["role_task_home_version_id"]))
        }
        if head is not None
        else {}
    )
    generation = int(head["generation"]) + 1 if head is not None else 1
    stamp = _db_time(_now())
    version_id = _stable_id("rth_", f"{actor.membership_id}:{generation}:{source_digest}")
    overall = "stale" if any(value[1] == "unavailable" for value in facts.values()) else "ready"
    connection.execute(
        """INSERT INTO workspace_role_task_home_versions(
             role_task_home_version_id,workspace_id,tenant_id,workspace_membership_id,
             membership_revision,role_code,contract_snapshot_id,contract_generation,
             source_digest,status,generation,observed_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version_id,
            actor.workspace_id,
            actor.tenant_id,
            actor.membership_id,
            _membership_revision(connection, actor.membership_id),
            actor.membership_role,
            contract["navigation_contract_snapshot_id"],
            int(contract["generation"]),
            source_digest,
            overall,
            generation,
            stamp,
            stamp,
        ),
    )
    for source_code in sorted({task.source_code for task in specs}):
        count, freshness = facts[source_code]
        evidence = _sha((source_code, count, freshness))
        connection.execute(
            """INSERT INTO workspace_role_task_home_sources(
                 role_task_home_version_id,source_code,source_revision,source_watermark,
                 evidence_digest,freshness) VALUES(?,?,?,?,?,?)""",
            (version_id, source_code, count, f"count:{count}", evidence, freshness),
        )
    for task in specs:
        count, freshness = facts[task.source_code]
        if freshness == "unavailable":
            status = "blocked"
        elif task.source_code == "members":
            status = "completed" if count > 1 else "ready"
        elif count > 0:
            status = "completed"
        elif task.writable:
            status = "ready"
        else:
            status = "not_started"
        action = (
            task.action_route_code
            if task.writable and status in {"ready", "needs_attention", "completed"}
            else None
        )
        connection.execute(
            """INSERT INTO workspace_role_task_home_tasks(
                 role_task_home_version_id,workspace_id,tenant_id,workspace_membership_id,
                 task_code,source_code,source_revision,evidence_digest,status,priority,
                 action_route_code,completion_generation,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                actor.workspace_id,
                actor.tenant_id,
                actor.membership_id,
                task.task_code,
                task.source_code,
                count,
                _sha((task.task_code, count, freshness, source_digest)),
                status,
                task.priority,
                action,
                generation if status == "completed" else 0,
                stamp,
            ),
        )
        if (
            head is not None
            and status == "completed"
            and old_tasks.get(task.task_code) != "completed"
        ):
            _insert_event(
                connection,
                actor.workspace_id,
                actor.tenant_id,
                "role_task.completed",
                "task",
                f"{actor.membership_id}:{task.task_code}",
                generation,
                _sha((actor.membership_id, task.task_code, generation)),
                {
                    "task_code": task.task_code,
                    "role_code": actor.membership_role,
                    "membership_pseudonym": _pseudonym(
                        config, "workspace-membership", actor.membership_id
                    ),
                    "source_evidence_digest": _sha((task.source_code, count, freshness)),
                },
                request_id,
                trace_id,
            )
    if head is None:
        connection.execute(
            """INSERT INTO workspace_role_task_home_heads(
                 workspace_membership_id,workspace_id,tenant_id,role_task_home_version_id,
                 generation,revision,updated_at) VALUES(?,?,?,?,?,1,?)""",
            (
                actor.membership_id,
                actor.workspace_id,
                actor.tenant_id,
                version_id,
                generation,
                stamp,
            ),
        )
    else:
        connection.execute(
            """UPDATE workspace_role_task_home_heads
               SET role_task_home_version_id=?,generation=?,revision=revision+1,updated_at=?
               WHERE workspace_membership_id=? AND generation=?""",
            (version_id, generation, stamp, actor.membership_id, int(head["generation"])),
        )
    version = connection.execute(
        "SELECT * FROM workspace_role_task_home_versions WHERE role_task_home_version_id=?",
        (version_id,),
    ).fetchone()
    return version, _task_rows(connection, version_id)


def _task_rows(connection: sqlite3.Connection, version_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT task.*,source.freshness FROM workspace_role_task_home_tasks task
           JOIN workspace_role_task_home_sources source
             ON source.role_task_home_version_id=task.role_task_home_version_id
            AND source.source_code=task.source_code
           WHERE task.role_task_home_version_id=? ORDER BY task.priority,task.task_code""",
        (version_id,),
    ).fetchall()


def _ensure_advanced_entitlements(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str
) -> dict[str, sqlite3.Row]:
    deployment_count = connection.execute(
        """SELECT count(*) FROM edge_deployments
           WHERE workspace_id=? AND tenant_id=? AND status='active'""",
        (workspace_id, tenant_id),
    ).fetchone()[0]
    rows: dict[str, sqlite3.Row] = {}
    stamp = _db_time(_now())
    for spec in ADVANCED_TOOL_SPECS:
        availability = (
            "blocked" if spec.requires_deployment and deployment_count == 0 else "available"
        )
        reason_code = "deployment.required" if availability == "blocked" else None
        entitlement_sha = _sha(
            (
                spec.tool_code,
                spec.route_code,
                spec.permissions,
                availability,
                reason_code,
                deployment_count,
            )
        )
        current = connection.execute(
            """SELECT version.* FROM workspace_advanced_tool_entitlement_heads head
               JOIN workspace_advanced_tool_entitlement_versions version
                 ON version.advanced_tool_entitlement_id=head.advanced_tool_entitlement_id
               WHERE head.workspace_id=? AND head.tenant_id=? AND head.tool_code=?""",
            (workspace_id, tenant_id, spec.tool_code),
        ).fetchone()
        if current is not None and hmac.compare_digest(
            str(current["entitlement_sha256"]), entitlement_sha
        ):
            rows[spec.tool_code] = current
            continue
        generation = int(current["generation"]) + 1 if current is not None else 1
        revision = int(current["revision"]) + 1 if current is not None else 1
        entitlement_id = _stable_id(
            "ate_", f"{workspace_id}:{tenant_id}:{spec.tool_code}:{generation}:{entitlement_sha}"
        )
        connection.execute(
            """INSERT INTO workspace_advanced_tool_entitlement_versions(
                 advanced_tool_entitlement_id,workspace_id,tenant_id,tool_code,route_code,
                 required_permissions_json,availability,reason_code,source_revision,
                 generation,revision,entitlement_sha256,observed_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entitlement_id,
                workspace_id,
                tenant_id,
                spec.tool_code,
                spec.route_code,
                _json(list(spec.permissions)),
                availability,
                reason_code,
                int(deployment_count),
                generation,
                revision,
                entitlement_sha,
                stamp,
                stamp,
            ),
        )
        for source_code, source_revision, watermark in (
            ("catalog", 1, "advanced-tool-v1"),
            ("product", 1, "workspace-entitlement-current"),
            ("deployment", int(deployment_count), f"active:{deployment_count}"),
        ):
            connection.execute(
                """INSERT INTO workspace_advanced_tool_entitlement_sources(
                     advanced_tool_entitlement_id,source_code,source_revision,
                     source_watermark,source_sha256) VALUES(?,?,?,?,?)""",
                (
                    entitlement_id,
                    source_code,
                    source_revision,
                    watermark,
                    _sha((source_code, source_revision, watermark)),
                ),
            )
        if current is None:
            connection.execute(
                """INSERT INTO workspace_advanced_tool_entitlement_heads(
                     workspace_id,tenant_id,tool_code,advanced_tool_entitlement_id,
                     generation,revision,updated_at) VALUES(?,?,?,?,?,1,?)""",
                (workspace_id, tenant_id, spec.tool_code, entitlement_id, generation, stamp),
            )
        else:
            connection.execute(
                """UPDATE workspace_advanced_tool_entitlement_heads
                   SET advanced_tool_entitlement_id=?,generation=?,revision=revision+1,updated_at=?
                   WHERE workspace_id=? AND tenant_id=? AND tool_code=? AND generation=?""",
                (
                    entitlement_id,
                    generation,
                    stamp,
                    workspace_id,
                    tenant_id,
                    spec.tool_code,
                    int(current["generation"]),
                ),
            )
        rows[spec.tool_code] = connection.execute(
            """SELECT * FROM workspace_advanced_tool_entitlement_versions
               WHERE advanced_tool_entitlement_id=?""",
            (entitlement_id,),
        ).fetchone()
    return rows


def _advanced_tool(row: sqlite3.Row, spec: AdvancedToolSpec) -> AdvancedTool:
    availability = str(row["availability"])
    return AdvancedTool(
        tool_code=spec.tool_code,
        route_code=spec.route_code,
        label_key=f"advanced_tool.{spec.tool_code}.label",
        availability=availability,
        reason_code=row["reason_code"],
        action_enabled=availability == "available",
        entitlement_revision=int(row["revision"]),
        observed_at=_parse_time(str(row["observed_at"])),
    )


def _identity_membership(
    connection: sqlite3.Connection, account_id: str, workspace_id: str
) -> sqlite3.Row:
    rows = connection.execute(
        """SELECT member.* FROM workspace_memberships member
           JOIN workspace_tenant_bindings binding
             ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
           JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
           WHERE member.account_id=? AND member.workspace_id=? AND member.status='active'
             AND member.role_code!='service_account' AND binding.status='active'
             AND workspace.status='active'""",
        (account_id, workspace_id),
    ).fetchall()
    if len(rows) != 1:
        raise ApiError("workspace.not_found")
    return rows[0]


def _account_locale(connection: sqlite3.Connection, account_id: str) -> str:
    rows = connection.execute(
        """SELECT locale FROM account_preferences
           WHERE account_id=? AND status='active'""",
        (account_id,),
    ).fetchall()
    if len(rows) != 1 or str(rows[0]["locale"]) not in {"zh-CN", "en-US"}:
        raise ApiError("workspace_session.unavailable")
    return str(rows[0]["locale"])


def _membership_revision(connection: sqlite3.Connection, membership_id: str) -> int:
    rows = connection.execute(
        """SELECT revision FROM workspace_memberships
           WHERE workspace_membership_id=? AND status='active' AND role_code!='service_account'""",
        (membership_id,),
    ).fetchall()
    if len(rows) != 1 or type(rows[0][0]) is not int or int(rows[0][0]) < 1:
        raise ApiError("workspace_session.permission_denied")
    return int(rows[0][0])


def _preference_replay(
    row: sqlite3.Row,
    actor: WorkspaceActorPermissionSnapshot,
    contract: sqlite3.Row,
    membership_revision: int,
    request_id: str,
    trace_id: str,
) -> NavigationPreferenceResponse:
    payload = json.loads(str(row["response_json"]))
    if (
        payload.get("contract_snapshot_id") != contract["navigation_contract_snapshot_id"]
        or payload.get("membership_revision") != membership_revision
        or payload.get("permission_revision") != actor.permission_revision
        or not hmac.compare_digest(
            str(payload.get("permission_fingerprint", "")),
            actor.authorization_fingerprint,
        )
    ):
        raise ApiError("identity.revision_conflict")
    payload["idempotent_replay"] = True
    payload["request_id"] = request_id
    payload["trace_id"] = trace_id
    payload["updated_at"] = _parse_time(str(payload["updated_at"]))
    return NavigationPreferenceResponse.model_validate(payload)


def _insert_event(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    generation: int,
    event_key_sha256: str,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
    *,
    idempotency_hmac: str | None = None,
    request_sha256: str | None = None,
) -> None:
    stamp = _db_time(_now())
    event_id = _id("wne_")
    connection.execute(
        """INSERT INTO workspace_navigation_events(
             workspace_navigation_event_id,workspace_id,tenant_id,event_type,
             aggregate_type,aggregate_id,generation,event_key_sha256,
             idempotency_key_hmac,request_sha256,payload_json,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            workspace_id,
            tenant_id,
            event_type,
            aggregate_type,
            aggregate_id,
            generation,
            event_key_sha256,
            idempotency_hmac,
            request_sha256,
            _json(payload),
            request_id,
            trace_id,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO workspace_navigation_event_outbox(
             workspace_navigation_event_outbox_id,workspace_navigation_event_id,status,
             attempt_count,next_attempt_at,lease_owner_id,lease_generation,
             lease_token_hmac,lease_expires_at,dispatched_at,created_at,updated_at)
           VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
        (_id("wno_"), event_id, stamp, stamp, stamp),
    )


def _write_audit(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    action_code: str,
    contract: sqlite3.Row,
    outcome: str,
    request_id: str,
    trace_id: str,
) -> None:
    _insert_audit(
        connection,
        config,
        "workspace",
        actor.account_id,
        actor.workspace_id,
        actor.tenant_id,
        action_code,
        actor.permission_revision,
        actor.authorization_fingerprint,
        int(contract["generation"]),
        outcome,
        request_id,
        trace_id,
    )


def _write_identity_audit(
    connection: sqlite3.Connection,
    config: AppConfig,
    account_id: str,
    actor: WorkspaceActorPermissionSnapshot,
    contract: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> None:
    _insert_audit(
        connection,
        config,
        "identity",
        account_id,
        actor.workspace_id,
        actor.tenant_id,
        "identity.navigation_preference.change",
        actor.permission_revision,
        actor.authorization_fingerprint,
        int(contract["generation"]),
        "success",
        request_id,
        trace_id,
    )


def _insert_audit(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor_domain: str,
    account_id: str,
    workspace_id: str,
    tenant_id: str,
    action_code: str,
    permission_revision: int,
    permission_fingerprint: str,
    contract_generation: int,
    outcome: str,
    request_id: str,
    trace_id: str,
) -> None:
    connection.execute(
        """INSERT INTO workspace_navigation_read_audits(
             workspace_navigation_read_audit_id,actor_domain,actor_pseudonym,
             owner_digest,action_code,permission_revision,permission_fingerprint,
             contract_generation,outcome,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("wna_"),
            actor_domain,
            _pseudonym(config, f"{actor_domain}-actor", account_id),
            _owner_ref(config, workspace_id, tenant_id),
            action_code,
            permission_revision,
            permission_fingerprint,
            contract_generation,
            outcome,
            request_id,
            trace_id,
            _db_time(_now()),
        ),
    )


def _owner_ref(config: AppConfig, workspace_id: str, tenant_id: str) -> str:
    return _pseudonym(config, "workspace-owner", f"{workspace_id}:{tenant_id}")


def _keyed_digest(config: AppConfig, namespace: str, *values: object) -> str:
    return hmac.new(
        str(config.cache_affinity_hmac_secret).encode("utf-8"),
        _json([namespace, *values]).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _idempotency_hmac(config: AppConfig, *scope_and_value: str) -> str:
    return _keyed_digest(config, "workspace-navigation-idempotency", *scope_and_value)


def _pseudonym(config: AppConfig, namespace: str, value: str) -> str:
    return _keyed_digest(config, namespace, value)


def _connect(config: AppConfig, error_code: str) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except (sqlite3.Error, DbError):
        raise ApiError(error_code) from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _stable_id(prefix: str, seed: str) -> str:
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = (
    "change_navigation_preference",
    "get_workspace_advanced_tools",
    "get_workspace_getting_started",
    "get_workspace_navigation",
    "open_workspace_advanced_tool",
)
