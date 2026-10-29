"""Database-authoritative SCIM directory and Membership lifecycle services."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.scim_directory import (
    SCIM_GROUP_SCHEMA,
    SCIM_LIST_SCHEMA,
    SCIM_PATCH_SCHEMA,
    SCIM_USER_SCHEMA,
    choose_lowest_risk_role,
    normalize_scim_external_id,
    normalize_scim_user_name,
    parse_scim_filter,
    parse_scim_version,
    scim_hmac,
)

from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .scim_models import (
    ScimConflictResolveRequest,
    ScimConflictResponse,
    ScimConflictSummary,
    ScimDirectorySummaryResponse,
    ScimGroupSummary,
    ScimRoleMappingRequest,
    ScimRoleMappingResponse,
    ScimRoleMappingSummary,
    ScimSyncListResponse,
    ScimSyncSummary,
    ScimTokenCreateRequest,
    ScimTokenResponse,
    ScimTokenSummary,
)

_READ_ROLES = frozenset({"owner", "admin", "security_admin", "auditor", "viewer"})
_MANAGE_ROLES = frozenset({"owner", "admin", "security_admin"})
_TOKEN = re.compile(r"^mhscim_[A-Za-z0-9_-]{32,220}$", re.ASCII)
_RESOURCE_ID = re.compile(r"^(?:sul|sgl)_[0-9a-f]{32}$")
_SCIM_MAX_COUNT = 200
_TOKEN_ROTATION_GRACE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ScimTokenContext:
    token_id: str
    workspace_id: str
    tenant_id: str
    actor_account_id: str
    status: str


class ScimServiceError(Exception):
    def __init__(self, status: int, scim_type: str | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.scim_type = scim_type
        self.detail = detail


def get_scim_directory_summary(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> ScimDirectorySummaryResponse:
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            workspace_id=workspace_id,
            manage=False,
        )
        result = _directory_summary(connection, context, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def list_scim_syncs(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> ScimSyncListResponse:
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            workspace_id=workspace_id,
            manage=False,
        )
        result = ScimSyncListResponse(
            sync_runs=tuple(
                _sync_summary(row)
                for row in connection.execute(
                    """SELECT * FROM scim_sync_runs WHERE workspace_id=? AND tenant_id=?
                         ORDER BY started_at DESC,scim_sync_run_id DESC LIMIT 100""",
                    (context.workspace_id, context.tenant_id),
                )
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def create_scim_token(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    idempotency_key: str,
    payload: ScimTokenCreateRequest,
    request_id: str,
    trace_id: str,
) -> ScimTokenResponse:
    request_sha256 = _digest({"expires_in_days": payload.expires_in_days})
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection, config, context, "token.create", idempotency_key, request_sha256
        )
        if replay is not None:
            row = _owned(connection, "scim_tokens", "scim_token_id", replay["result_id"], context)
            if row is None:
                raise ApiError("scim.unavailable")
            result = ScimTokenResponse(
                token=_token_summary(row),
                bearer_token=None,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        if (
            connection.execute(
                "SELECT 1 FROM scim_tokens WHERE workspace_id=? AND status='active'",
                (context.workspace_id,),
            ).fetchone()
            is not None
        ):
            raise ApiError("scim.token_conflict")
        now = _utcnow()
        raw_token = "mhscim_" + secrets.token_urlsafe(36)
        token_id = _id("sct")
        connection.execute(
            """INSERT INTO scim_tokens(
                 scim_token_id,workspace_id,tenant_id,token_hmac,token_prefix,scope_code,
                 status,revision,expires_at,grace_expires_at,last_used_at,
                 created_by_membership_id,created_by_account_id,created_at,updated_at)
               VALUES(?,?,?,?,?,'users_groups','active',1,?,NULL,NULL,?,?,?,?)""",
            (
                token_id,
                context.workspace_id,
                context.tenant_id,
                scim_hmac(config.enterprise_sso_hmac_secret, "token", raw_token),
                raw_token[:14],
                _db_time(now + timedelta(days=payload.expires_in_days)),
                context.membership_id,
                context.account_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        _insert_command(
            connection,
            config,
            context,
            "token.create",
            idempotency_key,
            request_sha256,
            token_id,
            now,
        )
        row = _owned(connection, "scim_tokens", "scim_token_id", token_id, context)
        assert row is not None
        result = ScimTokenResponse(
            token=_token_summary(row),
            bearer_token=raw_token,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("scim.token_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def rotate_scim_token(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    token_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: ScimTokenCreateRequest,
    request_id: str,
    trace_id: str,
) -> ScimTokenResponse:
    request_sha256 = _digest(
        {"token_id": token_id, "revision": expected_revision, "days": payload.expires_in_days}
    )
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection, config, context, "token.rotate", idempotency_key, request_sha256
        )
        if replay is not None:
            row = _owned(connection, "scim_tokens", "scim_token_id", replay["result_id"], context)
            if row is None:
                raise ApiError("scim.unavailable")
            result = ScimTokenResponse(
                token=_token_summary(row),
                bearer_token=None,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        current = _owned(connection, "scim_tokens", "scim_token_id", token_id, context)
        if current is None:
            raise ApiError("scim.not_found")
        if current["status"] != "active" or current["revision"] != expected_revision:
            raise ApiError("scim.revision_conflict")
        now = _utcnow()
        connection.execute(
            """UPDATE scim_tokens SET status='rotating',revision=revision+1,
                 grace_expires_at=?,updated_at=? WHERE scim_token_id=?""",
            (
                _db_time(now + _TOKEN_ROTATION_GRACE),
                _later_stamp(current["updated_at"], now),
                token_id,
            ),
        )
        raw_token = "mhscim_" + secrets.token_urlsafe(36)
        new_id = _id("sct")
        connection.execute(
            """INSERT INTO scim_tokens(
                 scim_token_id,workspace_id,tenant_id,token_hmac,token_prefix,scope_code,
                 status,revision,expires_at,grace_expires_at,last_used_at,
                 created_by_membership_id,created_by_account_id,created_at,updated_at)
               VALUES(?,?,?,?,?,'users_groups','active',1,?,NULL,NULL,?,?,?,?)""",
            (
                new_id,
                context.workspace_id,
                context.tenant_id,
                scim_hmac(config.enterprise_sso_hmac_secret, "token", raw_token),
                raw_token[:14],
                _db_time(now + timedelta(days=payload.expires_in_days)),
                context.membership_id,
                context.account_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        _insert_command(
            connection,
            config,
            context,
            "token.rotate",
            idempotency_key,
            request_sha256,
            new_id,
            now,
        )
        row = _owned(connection, "scim_tokens", "scim_token_id", new_id, context)
        assert row is not None
        result = ScimTokenResponse(
            token=_token_summary(row),
            bearer_token=raw_token,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("scim.token_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def upsert_scim_role_mapping(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: ScimRoleMappingRequest,
    request_id: str,
    trace_id: str,
) -> ScimRoleMappingResponse:
    request_sha256 = _digest(
        {"group": payload.scim_group_link_id, "role": payload.role_code, "rev": expected_revision}
    )
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection, config, context, "mapping.upsert", idempotency_key, request_sha256
        )
        if replay is not None:
            row = _mapping_row(connection, context, replay["result_id"])
            if row is None:
                raise ApiError("scim.unavailable")
            result = ScimRoleMappingResponse(
                mapping=_mapping_summary(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        group = _owned(
            connection,
            "scim_group_links",
            "scim_group_link_id",
            payload.scim_group_link_id,
            context,
        )
        if group is None:
            raise ApiError("scim.not_found")
        current = connection.execute(
            "SELECT * FROM scim_role_mappings WHERE scim_group_link_id=?",
            (payload.scim_group_link_id,),
        ).fetchone()
        now = _utcnow()
        if current is None:
            if expected_revision != 0:
                raise ApiError("scim.revision_conflict")
            mapping_id = _id("srm")
            connection.execute(
                """INSERT INTO scim_role_mappings(
                     scim_role_mapping_id,workspace_id,tenant_id,scim_group_link_id,
                     role_code,status,revision,updated_by_account_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',1,?,?,?)""",
                (
                    mapping_id,
                    context.workspace_id,
                    context.tenant_id,
                    payload.scim_group_link_id,
                    payload.role_code,
                    context.account_id,
                    _db_time(now),
                    _db_time(now),
                ),
            )
        else:
            if current["revision"] != expected_revision:
                raise ApiError("scim.revision_conflict")
            mapping_id = current["scim_role_mapping_id"]
            connection.execute(
                """UPDATE scim_role_mappings SET role_code=?,status='active',revision=revision+1,
                     updated_by_account_id=?,updated_at=? WHERE scim_role_mapping_id=?""",
                (
                    payload.role_code,
                    context.account_id,
                    _later_stamp(current["updated_at"], now),
                    mapping_id,
                ),
            )
        actor = ScimTokenContext(
            token_id="sct_" + "0" * 32,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            actor_account_id=context.account_id,
            status="active",
        )
        _recompute_group_roles(connection, actor, request_id, trace_id, now)
        _insert_command(
            connection,
            config,
            context,
            "mapping.upsert",
            idempotency_key,
            request_sha256,
            mapping_id,
            now,
        )
        row = _mapping_row(connection, context, mapping_id)
        assert row is not None
        result = ScimRoleMappingResponse(
            mapping=_mapping_summary(row),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("scim.conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def resolve_scim_conflict(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    conflict_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: ScimConflictResolveRequest,
    request_id: str,
    trace_id: str,
) -> ScimConflictResponse:
    request_sha256 = _digest(
        {
            "conflict": conflict_id,
            "revision": expected_revision,
            "resolution": payload.resolution_code,
        }
    )
    connection = _connect_admin(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _authenticate_admin(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            workspace_id=workspace_id,
            manage=True,
        )
        replay = _command_replay(
            connection, config, context, "conflict.resolve", idempotency_key, request_sha256
        )
        if replay is not None:
            row = _owned(
                connection, "scim_conflicts", "scim_conflict_id", replay["result_id"], context
            )
            if row is None:
                raise ApiError("scim.unavailable")
            result = ScimConflictResponse(
                conflict=_conflict_summary(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        row = _owned(connection, "scim_conflicts", "scim_conflict_id", conflict_id, context)
        if row is None:
            raise ApiError("scim.not_found")
        if row["status"] != "open" or row["revision"] != expected_revision:
            raise ApiError("scim.revision_conflict")
        now = _utcnow()
        _apply_conflict_resolution(
            connection,
            context,
            row,
            payload.resolution_code,
            request_id,
            trace_id,
            now,
        )
        status = "ignored" if payload.resolution_code == "ignore_directory" else "resolved"
        connection.execute(
            """UPDATE scim_conflicts SET status=?,revision=revision+1,resolution_code=?,
                 resolved_by_account_id=?,resolved_at=?,updated_at=? WHERE scim_conflict_id=?""",
            (
                status,
                payload.resolution_code,
                context.account_id,
                _db_time(now),
                _later_stamp(row["updated_at"], now),
                conflict_id,
            ),
        )
        _insert_command(
            connection,
            config,
            context,
            "conflict.resolve",
            idempotency_key,
            request_sha256,
            conflict_id,
            now,
        )
        updated = _owned(connection, "scim_conflicts", "scim_conflict_id", conflict_id, context)
        assert updated is not None
        result = ScimConflictResponse(
            conflict=_conflict_summary(updated),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("scim.unavailable") from None
    finally:
        connection.close()


def list_scim_users(
    config: AppConfig,
    *,
    bearer_token: str,
    filter_text: str | None,
    start_index: int,
    count: int,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        connection.execute("BEGIN")
        token = _authenticate_token(connection, config, bearer_token, write=False)
        where = "workspace_id=? AND tenant_id=?"
        params: list[object] = [token.workspace_id, token.tenant_id]
        if filter_text is not None:
            parsed = parse_scim_filter(filter_text)
            column = {
                "userName": "user_name_normalized",
                "externalId": "external_id",
                "displayName": "display_name",
            }[parsed.attribute]
            where += f" AND {column}=?"
            params.append(parsed.value)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM scim_user_links WHERE {where}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM scim_user_links WHERE {where}
                  ORDER BY created_at,scim_user_link_id LIMIT ? OFFSET ?""",
            (*params, count, start_index - 1),
        ).fetchall()
        result = _list_response(rows, total, start_index, [_scim_user(row) for row in rows])
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except ValueError:
        _rollback(connection)
        raise ScimServiceError(400, "invalidFilter", "Unsupported SCIM filter.") from None
    except (sqlite3.Error, DbError, KeyError, TypeError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def get_scim_user(config: AppConfig, *, bearer_token: str, resource_id: str) -> dict[str, object]:
    return _get_scim_resource(
        config, bearer_token, resource_id, "scim_user_links", "scim_user_link_id", _scim_user
    )


def create_scim_user(
    config: AppConfig,
    *,
    bearer_token: str,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        parsed = _parse_user(payload)
        connection.execute("BEGIN IMMEDIATE")
        token = _authenticate_token(connection, config, bearer_token, write=True)
        run_id = _start_sync(connection, config, token)
        external_hmac = scim_hmac(
            config.enterprise_sso_hmac_secret, "external_id", parsed["external_id"]
        )
        user_hmac = scim_hmac(config.enterprise_sso_hmac_secret, "user_name", parsed["user_name"])
        if (
            connection.execute(
                """SELECT 1 FROM scim_user_links WHERE workspace_id=?
                 AND(external_id_hmac=? OR user_name_hmac=?)""",
                (token.workspace_id, external_hmac, user_hmac),
            ).fetchone()
            is not None
        ):
            raise ScimServiceError(409, "uniqueness", "SCIM user already exists.")
        domain = str(parsed["user_name"]).rsplit("@", 1)[1]
        verified = connection.execute(
            """SELECT 1 FROM verified_domains
                 WHERE workspace_id=? AND tenant_id=? AND domain_normalized=?
                   AND status='verified'""",
            (token.workspace_id, token.tenant_id, domain),
        ).fetchone()
        account = connection.execute(
            """SELECT account.*,member.workspace_membership_id,member.role_code,
                      member.status member_status,
                      member.lifecycle_source,member.revision member_revision,member.legacy_user_id
                 FROM global_accounts account
                 LEFT JOIN workspace_memberships member ON member.account_id=account.account_id
                   AND member.workspace_id=? AND member.tenant_id=?
                WHERE account.email_normalized=? AND account.status='active'
                  AND account.login_enabled=1""",
            (token.workspace_id, token.tenant_id, parsed["user_name"]),
        ).fetchone()
        link_id = _id("sul")
        now = _utcnow()
        active = bool(parsed["active"])
        state = "pending"
        account_id = None
        membership_id = None
        conflict_reason = None
        if verified is not None and account is not None:
            account_id = account["account_id"]
            membership_id = account["workspace_membership_id"]
            if membership_id is None:
                membership_id = _create_scim_membership(
                    connection, token, account, display_name=str(parsed["display_name"]), now=now
                )
            if membership_id is not None:
                member = connection.execute(
                    "SELECT * FROM workspace_memberships WHERE workspace_membership_id=?",
                    (membership_id,),
                ).fetchone()
                assert member is not None
                if not active and member["role_code"] == "owner":
                    state = "conflict"
                    conflict_reason = "owner_protected"
                else:
                    state = "active" if active else "disabled"
                    if not active and member["status"] != "disabled":
                        _disable_membership(connection, token, member, request_id, trace_id, now)
            else:
                account_id = None
        if state == "pending" and not active:
            state = "disabled"
        connection.execute(
            """INSERT INTO scim_user_links(
                 scim_user_link_id,workspace_id,tenant_id,external_id,external_id_hmac,
                 user_name_normalized,user_name_hmac,display_name,account_id,membership_id,
                 source_version,state,revision,disabled_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (
                link_id,
                token.workspace_id,
                token.tenant_id,
                parsed["external_id"],
                external_hmac,
                parsed["user_name"],
                user_hmac,
                parsed["display_name"],
                account_id,
                membership_id,
                parsed["source_version"],
                state,
                _db_time(now) if state == "disabled" else None,
                _db_time(now),
                _db_time(now),
            ),
        )
        if state == "pending":
            connection.execute(
                """INSERT INTO scim_pending_invitations(
                     scim_pending_invitation_id,workspace_id,tenant_id,scim_user_link_id,
                     target_email_hmac,target_role_code,status,revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,'viewer','pending',1,?,?)""",
                (
                    _id("spi"),
                    token.workspace_id,
                    token.tenant_id,
                    link_id,
                    user_hmac,
                    _db_time(now),
                    _db_time(now),
                ),
            )
        if conflict_reason is not None:
            _create_conflict(
                connection,
                token,
                "user",
                link_id,
                conflict_reason,
                "1",
                str(parsed["source_version"]),
                "disable_membership",
                None,
                request_id,
                trace_id,
                now,
            )
        _finish_sync(
            connection,
            run_id,
            token,
            created=1,
            conflicts=1 if conflict_reason else 0,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = connection.execute(
            "SELECT * FROM scim_user_links WHERE scim_user_link_id=?", (link_id,)
        ).fetchone()
        assert row is not None
        result = _scim_user(row)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except ValueError:
        _rollback(connection)
        raise ScimServiceError(400, "invalidValue", "SCIM user is invalid.") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ScimServiceError(
            409, "uniqueness", "SCIM user conflicts with directory state."
        ) from None
    except (sqlite3.Error, DbError, KeyError, TypeError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def patch_scim_user(
    config: AppConfig,
    *,
    bearer_token: str,
    resource_id: str,
    expected_revision: int,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        operations = _parse_patch(payload, allowed=frozenset({"active", "displayName"}))
        connection.execute("BEGIN IMMEDIATE")
        token = _authenticate_token(connection, config, bearer_token, write=True)
        row = _token_owned_resource(
            connection, token, "scim_user_links", "scim_user_link_id", resource_id
        )
        if row is None:
            raise ScimServiceError(404, None, "SCIM user was not found.")
        if row["revision"] != expected_revision:
            raise ScimServiceError(412, "mutability", "SCIM resource version is stale.")
        run_id = _start_sync(connection, config, token)
        now = _utcnow()
        display_name = row["display_name"]
        state = row["state"]
        disabled = 0
        for operation in operations:
            if operation["path"] == "displayName":
                display_name = _bounded_text(operation["value"], 120)
            elif operation["path"] == "active":
                value = operation["value"]
                if type(value) is not bool:
                    raise ScimServiceError(400, "invalidValue", "SCIM active must be boolean.")
                if value and state == "disabled":
                    raise ScimServiceError(
                        409, "mutability", "Disabled SCIM users require manual recovery."
                    )
                if not value and state != "disabled":
                    member = None
                    if row["membership_id"] is not None:
                        member = connection.execute(
                            "SELECT * FROM workspace_memberships WHERE workspace_membership_id=?",
                            (row["membership_id"],),
                        ).fetchone()
                    if member is not None and member["role_code"] == "owner":
                        connection.execute(
                            """UPDATE scim_user_links SET state='conflict',
                                 source_version=source_version+1,revision=revision+1,
                                 disabled_at=NULL,updated_at=? WHERE scim_user_link_id=?""",
                            (_later_stamp(row["updated_at"], now), resource_id),
                        )
                        _create_conflict(
                            connection,
                            token,
                            "user",
                            resource_id,
                            "owner_protected",
                            str(member["revision"]),
                            str(expected_revision),
                            "disable_membership",
                            None,
                            request_id,
                            trace_id,
                            now,
                        )
                        _finish_sync(
                            connection,
                            run_id,
                            token,
                            conflicts=1,
                            request_id=request_id,
                            trace_id=trace_id,
                            now=now,
                        )
                        connection.commit()
                        raise ScimServiceError(
                            409, "mutability", "Workspace owner cannot be disabled by SCIM."
                        )
                    if member is not None and member["status"] != "disabled":
                        _disable_membership(connection, token, member, request_id, trace_id, now)
                        disabled = 1
                    state = "disabled"
        connection.execute(
            """UPDATE scim_user_links SET display_name=?,state=?,source_version=source_version+1,
                 revision=revision+1,disabled_at=?,updated_at=? WHERE scim_user_link_id=?""",
            (
                display_name,
                state,
                _db_time(now) if state == "disabled" else None,
                _later_stamp(row["updated_at"], now),
                resource_id,
            ),
        )
        _finish_sync(
            connection,
            run_id,
            token,
            updated=1,
            disabled=disabled,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        updated = connection.execute(
            "SELECT * FROM scim_user_links WHERE scim_user_link_id=?", (resource_id,)
        ).fetchone()
        assert updated is not None
        result = _scim_user(updated)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except (ValueError, TypeError):
        _rollback(connection)
        raise ScimServiceError(400, "invalidValue", "SCIM patch is invalid.") from None
    except (sqlite3.Error, DbError, KeyError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def list_scim_groups(
    config: AppConfig,
    *,
    bearer_token: str,
    filter_text: str | None,
    start_index: int,
    count: int,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        connection.execute("BEGIN")
        token = _authenticate_token(connection, config, bearer_token, write=False)
        where = "workspace_id=? AND tenant_id=?"
        params: list[object] = [token.workspace_id, token.tenant_id]
        if filter_text is not None:
            parsed = parse_scim_filter(filter_text)
            if parsed.attribute not in {"externalId", "displayName"}:
                raise ValueError
            column = "external_id" if parsed.attribute == "externalId" else "display_name"
            where += f" AND {column}=?"
            params.append(parsed.value)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM scim_group_links WHERE {where}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM scim_group_links WHERE {where}
                  ORDER BY created_at,scim_group_link_id LIMIT ? OFFSET ?""",
            (*params, count, start_index - 1),
        ).fetchall()
        resources = [_scim_group(connection, row) for row in rows]
        result = _list_response(rows, total, start_index, resources)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except ValueError:
        _rollback(connection)
        raise ScimServiceError(400, "invalidFilter", "Unsupported SCIM filter.") from None
    except (sqlite3.Error, DbError, KeyError, TypeError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def get_scim_group(config: AppConfig, *, bearer_token: str, resource_id: str) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        connection.execute("BEGIN")
        token = _authenticate_token(connection, config, bearer_token, write=False)
        row = _token_owned_resource(
            connection, token, "scim_group_links", "scim_group_link_id", resource_id
        )
        if row is None:
            raise ScimServiceError(404, None, "SCIM group was not found.")
        result = _scim_group(connection, row)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def create_scim_group(
    config: AppConfig,
    *,
    bearer_token: str,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        parsed = _parse_group(payload)
        connection.execute("BEGIN IMMEDIATE")
        token = _authenticate_token(connection, config, bearer_token, write=True)
        run_id = _start_sync(connection, config, token)
        external_hmac = scim_hmac(
            config.enterprise_sso_hmac_secret, "external_id", parsed["external_id"]
        )
        now = _utcnow()
        group_id = _id("sgl")
        connection.execute(
            """INSERT INTO scim_group_links(
                 scim_group_link_id,workspace_id,tenant_id,external_id,external_id_hmac,
                 display_name,source_version,state,revision,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'active',1,?,?)""",
            (
                group_id,
                token.workspace_id,
                token.tenant_id,
                parsed["external_id"],
                external_hmac,
                parsed["display_name"],
                parsed["source_version"],
                _db_time(now),
                _db_time(now),
            ),
        )
        _replace_group_members(
            connection, token, group_id, parsed["members"], int(parsed["source_version"]), now
        )
        _finish_sync(
            connection, run_id, token, created=1, request_id=request_id, trace_id=trace_id, now=now
        )
        row = connection.execute(
            "SELECT * FROM scim_group_links WHERE scim_group_link_id=?", (group_id,)
        ).fetchone()
        assert row is not None
        result = _scim_group(connection, row)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except (ValueError, TypeError):
        _rollback(connection)
        raise ScimServiceError(400, "invalidValue", "SCIM group is invalid.") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ScimServiceError(
            409, "uniqueness", "SCIM group conflicts with directory state."
        ) from None
    except (sqlite3.Error, DbError, KeyError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def patch_scim_group(
    config: AppConfig,
    *,
    bearer_token: str,
    resource_id: str,
    expected_revision: int,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
) -> dict[str, object]:
    connection = _connect_scim(config)
    try:
        operations = _parse_patch(payload, allowed=frozenset({"displayName", "members"}))
        connection.execute("BEGIN IMMEDIATE")
        token = _authenticate_token(connection, config, bearer_token, write=True)
        row = _token_owned_resource(
            connection, token, "scim_group_links", "scim_group_link_id", resource_id
        )
        if row is None:
            raise ScimServiceError(404, None, "SCIM group was not found.")
        if row["revision"] != expected_revision:
            raise ScimServiceError(412, "mutability", "SCIM resource version is stale.")
        run_id = _start_sync(connection, config, token)
        now = _utcnow()
        display_name = row["display_name"]
        for operation in operations:
            if operation["path"] == "displayName":
                display_name = _bounded_text(operation["value"], 120)
            else:
                members = _member_values(operation["value"])
                if operation["op"] == "replace":
                    _replace_group_members(
                        connection, token, resource_id, members, expected_revision + 1, now
                    )
                elif operation["op"] == "add":
                    _add_group_members(
                        connection, token, resource_id, members, expected_revision + 1, now
                    )
                else:
                    _remove_group_members(connection, token, resource_id, members, now)
        connection.execute(
            """UPDATE scim_group_links SET display_name=?,source_version=source_version+1,
                 revision=revision+1,updated_at=? WHERE scim_group_link_id=?""",
            (display_name, _later_stamp(row["updated_at"], now), resource_id),
        )
        _recompute_group_roles(connection, token, request_id, trace_id, now)
        _finish_sync(
            connection, run_id, token, updated=1, request_id=request_id, trace_id=trace_id, now=now
        )
        updated = connection.execute(
            "SELECT * FROM scim_group_links WHERE scim_group_link_id=?", (resource_id,)
        ).fetchone()
        assert updated is not None
        result = _scim_group(connection, updated)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except (ValueError, TypeError):
        _rollback(connection)
        raise ScimServiceError(400, "invalidValue", "SCIM patch is invalid.") from None
    except (sqlite3.Error, DbError, KeyError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def validate_page(start_index: object, count: object) -> tuple[int, int]:
    try:
        start = int(start_index) if start_index is not None else 1
        limit = int(count) if count is not None else 100
    except (TypeError, ValueError):
        raise ScimServiceError(400, "invalidValue", "SCIM pagination is invalid.") from None
    if start < 1 or limit < 0 or limit > _SCIM_MAX_COUNT:
        raise ScimServiceError(400, "invalidValue", "SCIM pagination is invalid.")
    return start, limit


def _parse_user(payload: dict[str, object]) -> dict[str, object]:
    allowed = {"schemas", "userName", "externalId", "displayName", "active", "name", "meta"}
    if set(payload) - allowed or payload.get("schemas") != [SCIM_USER_SCHEMA]:
        raise ValueError
    user_name = normalize_scim_user_name(payload.get("userName"))
    external_id = normalize_scim_external_id(payload.get("externalId"))
    display_name = _bounded_text(payload.get("displayName"), 120)
    active = payload.get("active", True)
    if type(active) is not bool:
        raise ValueError
    meta = payload.get("meta")
    source_version = 1
    if meta is not None:
        if type(meta) is not dict or set(meta) != {"version"}:
            raise ValueError
        source_version = parse_scim_version(meta.get("version"))
    name = payload.get("name")
    if name is not None and (type(name) is not dict or set(name) - {"givenName", "familyName"}):
        raise ValueError
    return {
        "user_name": user_name,
        "external_id": external_id,
        "display_name": display_name,
        "active": active,
        "source_version": source_version,
    }


def _parse_group(payload: dict[str, object]) -> dict[str, object]:
    allowed = {"schemas", "externalId", "displayName", "members", "meta"}
    if set(payload) - allowed or payload.get("schemas") != [SCIM_GROUP_SCHEMA]:
        raise ValueError
    external_id = normalize_scim_external_id(payload.get("externalId"))
    display_name = _bounded_text(payload.get("displayName"), 120)
    members = _member_values(payload.get("members", []))
    meta = payload.get("meta")
    source_version = 1
    if meta is not None:
        if type(meta) is not dict or set(meta) != {"version"}:
            raise ValueError
        source_version = parse_scim_version(meta.get("version"))
    return {
        "external_id": external_id,
        "display_name": display_name,
        "members": members,
        "source_version": source_version,
    }


def _parse_patch(payload: dict[str, object], *, allowed: frozenset[str]) -> list[dict[str, object]]:
    if set(payload) != {"schemas", "Operations"} or payload.get("schemas") != [SCIM_PATCH_SCHEMA]:
        raise ValueError
    raw = payload.get("Operations")
    if type(raw) is not list or not 1 <= len(raw) <= 20:
        raise ValueError
    operations: list[dict[str, object]] = []
    for item in raw:
        if type(item) is not dict or set(item) != {"op", "path", "value"}:
            raise ValueError
        op = item.get("op")
        path = item.get("path")
        if not isinstance(op, str) or op.casefold() not in {"add", "replace", "remove"}:
            raise ValueError
        if not isinstance(path, str) or path not in allowed:
            raise ValueError
        operations.append({"op": op.casefold(), "path": path, "value": item.get("value")})
    return operations


def _authenticate_admin(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None,
    workspace_id: str,
    manage: bool,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection, config, session_token=session_token, csrf_token=csrf_token, touch=False
    )
    if context.workspace_id != workspace_id:
        raise ApiError("scim.not_found")
    roles = _MANAGE_ROLES if manage else _READ_ROLES
    permission = "scim.manage" if manage else "scim.read"
    if context.membership_role not in roles or permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


def _authenticate_token(
    connection: sqlite3.Connection, config: AppConfig, raw_token: str, *, write: bool
) -> ScimTokenContext:
    if not isinstance(raw_token, str) or _TOKEN.fullmatch(raw_token) is None:
        raise ScimServiceError(401, None, "SCIM bearer is invalid.")
    digest = scim_hmac(config.enterprise_sso_hmac_secret, "token", raw_token)
    now = _utcnow()
    row = connection.execute(
        """SELECT * FROM scim_tokens WHERE token_hmac=? AND expires_at>?
             AND(status='active' OR(status='rotating' AND grace_expires_at>?))""",
        (digest, _db_time(now), _db_time(now)),
    ).fetchone()
    if row is None:
        raise ScimServiceError(401, None, "SCIM bearer is invalid.")
    if row["status"] == "rotating":
        running = connection.execute(
            "SELECT 1 FROM scim_sync_runs WHERE scim_token_id=? AND status='running'",
            (row["scim_token_id"],),
        ).fetchone()
        if running is None or write:
            raise ScimServiceError(401, None, "Rotated SCIM bearer cannot start a new sync.")
    connection.execute(
        "UPDATE scim_tokens SET last_used_at=?,updated_at=? WHERE scim_token_id=?",
        (_db_time(now), _later_stamp(row["updated_at"], now), row["scim_token_id"]),
    )
    return ScimTokenContext(
        token_id=row["scim_token_id"],
        workspace_id=row["workspace_id"],
        tenant_id=row["tenant_id"],
        actor_account_id=row["created_by_account_id"],
        status=row["status"],
    )


def _start_sync(connection: sqlite3.Connection, config: AppConfig, token: ScimTokenContext) -> str:
    now = _utcnow()
    run_id = _id("ssr")
    connection.execute(
        """INSERT INTO scim_sync_runs(
             scim_sync_run_id,workspace_id,tenant_id,scim_token_id,status,cursor_hmac,
             created_count,updated_count,disabled_count,conflict_count,safe_outcome_code,
             started_at,completed_at,updated_at)
           VALUES(?,?,?,?,'running',?,0,0,0,0,'sync.running',?,NULL,?)""",
        (
            run_id,
            token.workspace_id,
            token.tenant_id,
            token.token_id,
            scim_hmac(config.enterprise_sso_hmac_secret, "cursor", run_id),
            _db_time(now),
            _db_time(now),
        ),
    )
    return run_id


def _finish_sync(
    connection: sqlite3.Connection,
    run_id: str,
    token: ScimTokenContext,
    *,
    created: int = 0,
    updated: int = 0,
    disabled: int = 0,
    conflicts: int = 0,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """UPDATE scim_sync_runs SET status='completed',created_count=?,updated_count=?,
             disabled_count=?,conflict_count=?,safe_outcome_code='sync.completed',
             completed_at=?,updated_at=? WHERE scim_sync_run_id=? AND status='running'""",
        (created, updated, disabled, conflicts, _db_time(now), _db_time(now), run_id),
    )
    _event(
        connection,
        token,
        "scim.sync_completed",
        "sync",
        run_id,
        1,
        {
            "created": created,
            "updated": updated,
            "disabled": disabled,
            "conflicts": conflicts,
            "schema_version": 1,
        },
        request_id,
        trace_id,
        now,
    )


def _disable_membership(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    member: sqlite3.Row,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    stamp = _later_stamp(member["updated_at"], now)
    connection.execute(
        """UPDATE workspace_memberships SET status='disabled',revision=revision+1,
             updated_by_account_id=?,updated_at=? WHERE workspace_membership_id=?""",
        (token.actor_account_id, stamp, member["workspace_membership_id"]),
    )
    _event(
        connection,
        token,
        "membership.disabled_by_scim",
        "membership",
        member["workspace_membership_id"],
        int(member["revision"]) + 1,
        {"schema_version": 1, "outcome": "membership_disabled"},
        request_id,
        trace_id,
        now,
    )


def _create_scim_membership(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    account: sqlite3.Row,
    *,
    display_name: str,
    now: datetime,
) -> str | None:
    credential = connection.execute(
        """SELECT secret_hash FROM account_credentials WHERE account_id=?
             AND credential_type='password' AND status='active'""",
        (account["account_id"],),
    ).fetchone()
    if credential is None:
        return None
    stamp = _db_time(now)
    user_id = _id("usr")
    claim_id = _id("wpc")
    connection.execute(
        """INSERT INTO workspace_user_projection_claims(
             workspace_user_projection_claim_id,workspace_id,tenant_id,account_id,
             legacy_user_id,credential_revision,email_normalized,status,expires_at,
             consumed_at,created_at)
           VALUES(?,?,?,?,?,?,?,'pending',?,NULL,?)""",
        (
            claim_id,
            token.workspace_id,
            token.tenant_id,
            account["account_id"],
            user_id,
            account["credential_revision"],
            account["email_normalized"],
            _db_time(now + timedelta(minutes=5)),
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO users(
             user_id,tenant_id,email,display_name,password_hash,role,status,created_at,
             updated_at,failed_login_count,locked_until,last_login_at,password_changed_at)
           VALUES(?,?,?,?,?,'viewer','active',?,?,0,NULL,NULL,?)""",
        (
            user_id,
            token.tenant_id,
            account["email_normalized"],
            display_name,
            credential["secret_hash"],
            stamp,
            stamp,
            stamp,
        ),
    )
    claim = connection.execute(
        """SELECT status FROM workspace_user_projection_claims
             WHERE workspace_user_projection_claim_id=?""",
        (claim_id,),
    ).fetchone()
    if claim is None or claim["status"] != "consumed":
        raise sqlite3.IntegrityError("scim_projection_not_consumed")
    membership_id = _id("wmb")
    connection.execute(
        """INSERT INTO workspace_memberships(
             workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
             role_code,status,source,revision,created_by_account_id,updated_by_account_id,
             created_at,updated_at,lifecycle_source)
           VALUES(?,?,?,?,?,'viewer','active','membership_invitation',1,?,?,?,?,'scim')""",
        (
            membership_id,
            token.workspace_id,
            token.tenant_id,
            account["account_id"],
            user_id,
            token.actor_account_id,
            token.actor_account_id,
            stamp,
            stamp,
        ),
    )
    return membership_id


def _apply_conflict_resolution(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    conflict: sqlite3.Row,
    resolution_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if resolution_code == "accept_directory":
        _accept_directory_resolution(
            connection,
            context,
            conflict,
            request_id,
            trace_id,
            now,
        )
        return
    if (
        conflict["directory_action_code"] != "disable_membership"
        or conflict["subject_type"] != "user"
    ):
        return
    link = _owned(
        connection,
        "scim_user_links",
        "scim_user_link_id",
        conflict["subject_id"],
        context,
    )
    if link is None or link["membership_id"] is None:
        raise ApiError("scim.conflict")
    member = _owned_membership(connection, context, link["membership_id"])
    if member is None:
        raise ApiError("scim.conflict")
    state = "active" if member["status"] == "active" else "disabled"
    connection.execute(
        """UPDATE scim_user_links SET state=?,revision=revision+1,disabled_at=?,
             updated_at=? WHERE scim_user_link_id=?""",
        (
            state,
            _db_time(now) if state == "disabled" else None,
            _later_stamp(link["updated_at"], now),
            link["scim_user_link_id"],
        ),
    )


def _accept_directory_resolution(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    conflict: sqlite3.Row,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    action = conflict["directory_action_code"]
    actor = ScimTokenContext(
        token_id="sct_" + "0" * 32,
        workspace_id=context.workspace_id,
        tenant_id=context.tenant_id,
        actor_account_id=context.account_id,
        status="active",
    )
    if action == "disable_membership" and conflict["subject_type"] == "user":
        link = _owned(
            connection,
            "scim_user_links",
            "scim_user_link_id",
            conflict["subject_id"],
            context,
        )
        if link is None or link["membership_id"] is None:
            raise ApiError("scim.conflict")
        member = _owned_membership(connection, context, link["membership_id"])
        if member is None or member["role_code"] == "owner":
            raise ApiError("scim.conflict")
        if member["status"] != "disabled":
            _disable_membership(connection, actor, member, request_id, trace_id, now)
        connection.execute(
            """UPDATE scim_user_links SET state='disabled',revision=revision+1,
                 disabled_at=?,updated_at=? WHERE scim_user_link_id=?""",
            (
                _db_time(now),
                _later_stamp(link["updated_at"], now),
                link["scim_user_link_id"],
            ),
        )
        return
    if action == "set_role" and conflict["subject_type"] == "membership":
        member = _owned_membership(connection, context, conflict["subject_id"])
        target_role = conflict["directory_target_role_code"]
        if (
            member is None
            or member["status"] != "active"
            or member["role_code"] == "owner"
            or target_role is None
        ):
            raise ApiError("scim.conflict")
        if member["role_code"] != target_role:
            connection.execute(
                """UPDATE workspace_memberships SET role_code=?,revision=revision+1,
                     updated_by_account_id=?,updated_at=? WHERE workspace_membership_id=?""",
                (
                    target_role,
                    context.account_id,
                    _later_stamp(member["updated_at"], now),
                    member["workspace_membership_id"],
                ),
            )
        return
    raise ApiError("scim.conflict")


def _owned_membership(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    membership_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT * FROM workspace_memberships
             WHERE workspace_membership_id=? AND workspace_id=? AND tenant_id=?""",
        (membership_id, context.workspace_id, context.tenant_id),
    ).fetchone()


def _create_conflict(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    subject_type: str,
    subject_id: str,
    reason: str,
    current_revision: str,
    directory_revision: str,
    directory_action_code: str,
    directory_target_role_code: str | None,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    conflict_id = _id("scf")
    connection.execute(
        """INSERT INTO scim_conflicts(
             scim_conflict_id,workspace_id,tenant_id,subject_type,subject_id,reason_code,
             directory_action_code,directory_target_role_code,current_revision_hash,
             directory_revision_hash,status,revision,resolution_code,resolved_by_account_id,
             resolved_at,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,'open',1,NULL,NULL,NULL,?,?)""",
        (
            conflict_id,
            token.workspace_id,
            token.tenant_id,
            subject_type,
            subject_id,
            reason,
            directory_action_code,
            directory_target_role_code,
            hashlib.sha256(current_revision.encode("utf-8")).hexdigest(),
            hashlib.sha256(directory_revision.encode("utf-8")).hexdigest(),
            _db_time(now),
            _db_time(now),
        ),
    )
    _event(
        connection,
        token,
        "scim.conflict_detected",
        "conflict",
        conflict_id,
        1,
        {"schema_version": 1, "reason_code": reason},
        request_id,
        trace_id,
        now,
    )
    return conflict_id


def _event(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    event_type: str,
    subject_type: str,
    subject_id: str,
    revision: int,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    payload_json = _canonical(payload)
    connection.execute(
        """INSERT INTO scim_event_outbox(
             scim_event_id,workspace_id,tenant_id,actor_account_id,event_type,subject_type,
             subject_id,subject_revision,request_id,trace_id,payload_json,payload_sha256,
             delivery_status,attempt_count,next_attempt_at,delivered_at,occurred_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',0,?,NULL,?,?)""",
        (
            _id("sev"),
            token.workspace_id,
            token.tenant_id,
            token.actor_account_id,
            event_type,
            subject_type,
            subject_id,
            revision,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _recompute_group_roles(
    connection: sqlite3.Connection,
    actor: ScimTokenContext,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    users = connection.execute(
        """SELECT * FROM scim_user_links
            WHERE workspace_id=? AND tenant_id=? AND membership_id IS NOT NULL""",
        (actor.workspace_id, actor.tenant_id),
    ).fetchall()
    for user in users:
        roles = [
            row[0]
            for row in connection.execute(
                """SELECT mapping.role_code FROM scim_group_members member
                     JOIN scim_role_mappings mapping
                       ON mapping.scim_group_link_id=member.scim_group_link_id
                    WHERE member.scim_user_link_id=? AND member.status='active'
                      AND mapping.status='active'""",
                (user["scim_user_link_id"],),
            )
        ]
        member = connection.execute(
            "SELECT * FROM workspace_memberships WHERE workspace_membership_id=?",
            (user["membership_id"],),
        ).fetchone()
        target = choose_lowest_risk_role(roles)
        if target is None:
            if member is None or member["lifecycle_source"] != "scim":
                continue
            target = "viewer"
        if member is None or member["role_code"] == target:
            continue
        if member["role_code"] == "owner" or member["lifecycle_source"] != "scim":
            reason = "owner_protected" if member["role_code"] == "owner" else "human_membership"
            _create_conflict(
                connection,
                actor,
                "membership",
                member["workspace_membership_id"],
                reason,
                str(member["revision"]),
                str(user["source_version"]),
                "set_role",
                target,
                request_id,
                trace_id,
                now,
            )
            continue
        connection.execute(
            """UPDATE workspace_memberships SET role_code=?,revision=revision+1,
                 updated_by_account_id=?,updated_at=? WHERE workspace_membership_id=?""",
            (
                target,
                actor.actor_account_id,
                _later_stamp(member["updated_at"], now),
                member["workspace_membership_id"],
            ),
        )


def _replace_group_members(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    group_id: str,
    members: list[str],
    version: int,
    now: datetime,
) -> None:
    connection.execute(
        """UPDATE scim_group_members SET status='removed',updated_at=?
             WHERE scim_group_link_id=? AND status='active'""",
        (_db_time(now), group_id),
    )
    _add_group_members(connection, token, group_id, members, version, now)


def _add_group_members(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    group_id: str,
    members: list[str],
    version: int,
    now: datetime,
) -> None:
    for user_id in members:
        user = _token_owned_resource(
            connection, token, "scim_user_links", "scim_user_link_id", user_id
        )
        if user is None:
            raise ScimServiceError(400, "invalidValue", "SCIM group member is invalid.")
        existing = connection.execute(
            "SELECT * FROM scim_group_members WHERE scim_group_link_id=? AND scim_user_link_id=?",
            (group_id, user_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO scim_group_members(
                     scim_group_member_id,workspace_id,tenant_id,scim_group_link_id,
                     scim_user_link_id,status,source_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',?,?,?)""",
                (
                    _id("sgm"),
                    token.workspace_id,
                    token.tenant_id,
                    group_id,
                    user_id,
                    version,
                    _db_time(now),
                    _db_time(now),
                ),
            )
        else:
            connection.execute(
                """UPDATE scim_group_members
                     SET status='active',source_version=?,updated_at=?
                     WHERE scim_group_member_id=?""",
                (
                    max(version, int(existing["source_version"])),
                    _db_time(now),
                    existing["scim_group_member_id"],
                ),
            )


def _remove_group_members(
    connection: sqlite3.Connection,
    token: ScimTokenContext,
    group_id: str,
    members: list[str],
    now: datetime,
) -> None:
    del token
    for user_id in members:
        connection.execute(
            """UPDATE scim_group_members SET status='removed',updated_at=?
                 WHERE scim_group_link_id=? AND scim_user_link_id=? AND status='active'""",
            (_db_time(now), group_id, user_id),
        )


def _get_scim_resource(config, bearer_token, resource_id, table, id_column, serializer):
    connection = _connect_scim(config)
    try:
        connection.execute("BEGIN")
        token = _authenticate_token(connection, config, bearer_token, write=False)
        row = _token_owned_resource(connection, token, table, id_column, resource_id)
        if row is None:
            raise ScimServiceError(404, None, "SCIM resource was not found.")
        result = serializer(row)
        connection.commit()
        return result
    except ScimServiceError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError):
        _rollback(connection)
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None
    finally:
        connection.close()


def _scim_user(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": row["scim_user_link_id"],
        "externalId": row["external_id"],
        "userName": row["user_name_normalized"],
        "displayName": row["display_name"],
        "active": row["state"] == "active",
        "meta": {"resourceType": "User", "version": f'W/"{row["revision"]}"'},
    }


def _scim_group(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    members = [
        {"value": item["scim_user_link_id"]}
        for item in connection.execute(
            """SELECT scim_user_link_id FROM scim_group_members
                 WHERE scim_group_link_id=? AND status='active'
                 ORDER BY scim_user_link_id""",
            (row["scim_group_link_id"],),
        )
    ]
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": row["scim_group_link_id"],
        "externalId": row["external_id"],
        "displayName": row["display_name"],
        "members": members,
        "meta": {"resourceType": "Group", "version": f'W/"{row["revision"]}"'},
    }


def _list_response(
    rows, total: int, start: int, resources: list[dict[str, object]]
) -> dict[str, object]:
    del rows
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def _directory_summary(connection, context, request_id, trace_id):
    tokens = tuple(
        _token_summary(row)
        for row in connection.execute(
            """SELECT * FROM scim_tokens WHERE workspace_id=? AND tenant_id=?
                 ORDER BY created_at DESC LIMIT 10""",
            (context.workspace_id, context.tenant_id),
        )
    )
    groups = tuple(
        ScimGroupSummary(
            scim_group_link_id=row["scim_group_link_id"],
            display_name=row["display_name"],
            state=row["state"],
            revision=int(row["revision"]),
            updated_at=_parse_time(row["updated_at"]),
        )
        for row in connection.execute(
            """SELECT * FROM scim_group_links
                 WHERE workspace_id=? AND tenant_id=?
                 ORDER BY display_name,scim_group_link_id LIMIT 100""",
            (context.workspace_id, context.tenant_id),
        )
    )
    mappings = tuple(
        _mapping_summary(row)
        for row in connection.execute(
            """SELECT mapping.*,group_link.display_name group_display_name
                 FROM scim_role_mappings mapping JOIN scim_group_links group_link
                   ON group_link.scim_group_link_id=mapping.scim_group_link_id
                WHERE mapping.workspace_id=? AND mapping.tenant_id=?
                ORDER BY group_link.display_name,mapping.scim_role_mapping_id LIMIT 100""",
            (context.workspace_id, context.tenant_id),
        )
    )
    syncs = tuple(
        _sync_summary(row)
        for row in connection.execute(
            """SELECT * FROM scim_sync_runs WHERE workspace_id=? AND tenant_id=?
                 ORDER BY started_at DESC LIMIT 100""",
            (context.workspace_id, context.tenant_id),
        )
    )
    conflicts = tuple(
        _conflict_summary(row)
        for row in connection.execute(
            """SELECT * FROM scim_conflicts WHERE workspace_id=? AND tenant_id=?
                 ORDER BY created_at DESC LIMIT 100""",
            (context.workspace_id, context.tenant_id),
        )
    )
    counts = connection.execute(
        """SELECT
          (SELECT COUNT(*) FROM scim_pending_invitations
            WHERE workspace_id=? AND tenant_id=? AND status='pending'),
          (SELECT COUNT(*) FROM scim_user_links
            WHERE workspace_id=? AND tenant_id=? AND state='active'),
          (SELECT COUNT(*) FROM scim_group_links
            WHERE workspace_id=? AND tenant_id=? AND state='active')""",
        (context.workspace_id, context.tenant_id) * 3,
    ).fetchone()
    return ScimDirectorySummaryResponse(
        workspace_id=context.workspace_id,
        tokens=tokens,
        groups=groups,
        role_mappings=mappings,
        sync_runs=syncs,
        conflicts=conflicts,
        pending_invitation_count=int(counts[0]),
        linked_user_count=int(counts[1]),
        linked_group_count=int(counts[2]),
        request_id=request_id,
        trace_id=trace_id,
    )


def _token_summary(row):
    return ScimTokenSummary(
        scim_token_id=row["scim_token_id"],
        token_prefix=row["token_prefix"],
        status=row["status"],
        revision=int(row["revision"]),
        expires_at=_parse_time(row["expires_at"]),
        grace_expires_at=_optional_time(row["grace_expires_at"]),
        last_used_at=_optional_time(row["last_used_at"]),
    )


def _mapping_summary(row):
    return ScimRoleMappingSummary(
        scim_role_mapping_id=row["scim_role_mapping_id"],
        scim_group_link_id=row["scim_group_link_id"],
        group_display_name=row["group_display_name"],
        role_code=row["role_code"],
        status=row["status"],
        revision=int(row["revision"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _sync_summary(row):
    return ScimSyncSummary(
        scim_sync_run_id=row["scim_sync_run_id"],
        status=row["status"],
        created_count=int(row["created_count"]),
        updated_count=int(row["updated_count"]),
        disabled_count=int(row["disabled_count"]),
        conflict_count=int(row["conflict_count"]),
        safe_outcome_code=row["safe_outcome_code"],
        started_at=_parse_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _conflict_summary(row):
    return ScimConflictSummary(
        scim_conflict_id=row["scim_conflict_id"],
        subject_type=row["subject_type"],
        reason_code=row["reason_code"],
        status=row["status"],
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
        resolved_at=_optional_time(row["resolved_at"]),
    )


def _mapping_row(connection, context, mapping_id):
    return connection.execute(
        """SELECT mapping.*,group_link.display_name group_display_name
             FROM scim_role_mappings mapping JOIN scim_group_links group_link
               ON group_link.scim_group_link_id=mapping.scim_group_link_id
            WHERE mapping.scim_role_mapping_id=? AND mapping.workspace_id=?
              AND mapping.tenant_id=?""",
        (mapping_id, context.workspace_id, context.tenant_id),
    ).fetchone()


def _owned(connection, table, id_column, resource_id, context):
    if not isinstance(resource_id, str) or len(resource_id) != 36:
        return None
    return connection.execute(
        f"SELECT * FROM {table} WHERE {id_column}=? AND workspace_id=? AND tenant_id=?",
        (resource_id, context.workspace_id, context.tenant_id),
    ).fetchone()


def _token_owned_resource(connection, token, table, id_column, resource_id):
    if not isinstance(resource_id, str) or _RESOURCE_ID.fullmatch(resource_id) is None:
        return None
    return connection.execute(
        f"SELECT * FROM {table} WHERE {id_column}=? AND workspace_id=? AND tenant_id=?",
        (resource_id, token.workspace_id, token.tenant_id),
    ).fetchone()


def _command_replay(connection, config, context, action, key, request_sha256):
    row = connection.execute(
        """SELECT * FROM scim_commands WHERE workspace_id=? AND actor_account_id=?
             AND action=? AND idempotency_hmac=?""",
        (
            context.workspace_id,
            context.account_id,
            action,
            scim_hmac(
                config.enterprise_sso_hmac_secret,
                "idempotency",
                f"{context.workspace_id}\x1f{context.account_id}\x1f{action}\x1f{key}",
            ),
        ),
    ).fetchone()
    if row is not None and not secrets.compare_digest(row["request_sha256"], request_sha256):
        raise ApiError("scim.idempotency_conflict")
    return row


def _insert_command(connection, config, context, action, key, request_sha256, result_id, now):
    connection.execute(
        """INSERT INTO scim_commands(
             scim_command_id,workspace_id,tenant_id,actor_account_id,action,
             idempotency_hmac,request_sha256,result_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            _id("scm"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            scim_hmac(
                config.enterprise_sso_hmac_secret,
                "idempotency",
                f"{context.workspace_id}\x1f{context.account_id}\x1f{action}\x1f{key}",
            ),
            request_sha256,
            result_id,
            _db_time(now),
        ),
    )


def _member_values(value: object) -> list[str]:
    if type(value) is not list or len(value) > 10_000:
        raise ValueError
    result: list[str] = []
    for item in value:
        if type(item) is not dict or set(item) != {"value"}:
            raise ValueError
        resource_id = item.get("value")
        if (
            not isinstance(resource_id, str)
            or not resource_id.startswith("sul_")
            or _RESOURCE_ID.fullmatch(resource_id) is None
        ):
            raise ValueError
        if resource_id not in result:
            result.append(resource_id)
    return result


def _bounded_text(value: object, maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= maximum:
        raise ValueError
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect_admin(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("scim.unavailable") from None


def _connect_scim(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ScimServiceError(503, None, "SCIM service is temporarily unavailable.") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _later_stamp(previous: str, now: datetime) -> str:
    return _db_time(max(now, _parse_time(previous) + timedelta(milliseconds=1)))


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


__all__ = [
    "ScimServiceError",
    "create_scim_group",
    "create_scim_token",
    "create_scim_user",
    "get_scim_directory_summary",
    "get_scim_group",
    "get_scim_user",
    "list_scim_groups",
    "list_scim_syncs",
    "list_scim_users",
    "patch_scim_group",
    "patch_scim_user",
    "resolve_scim_conflict",
    "rotate_scim_token",
    "upsert_scim_role_mapping",
    "validate_page",
]
