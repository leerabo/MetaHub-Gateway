"""Owner-qualified PRD-139 channel operations and partner projections."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.tenant_data.crypto import TenantDataEnvelopeOwner, encrypt_tenant_data

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.identity.service import authenticate_global_session_in_transaction
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .models import (
    ApprovalSummary,
    AttributionApprovalCommand,
    AttributionRequestCommand,
    AttributionResponse,
    AttributionSummary,
    ChannelPartnerListResponse,
    ChannelPartnerResponse,
    ChannelPartnerSummary,
    CommissionRuleSummary,
    LeadCommand,
    LeadResponse,
    LeadSummary,
    MembershipSummary,
    PartnerCommand,
    PartnerWorkspaceListResponse,
    PartnerWorkspaceSummary,
    PendingAttributionSummary,
    ReferralSummary,
    StatementListResponse,
    StatementSummary,
)


def list_channel_partners(
    config: AppConfig,
    *,
    platform_session_token: str,
    status: str | None,
    tier_code: str | None,
    region_scope: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ChannelPartnerListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _platform_actor(connection, config, platform_session_token, None, "platform.channel.read")
        _require_schema(connection)
        clauses: list[str] = []
        values: list[object] = []
        for column, value, allowed in (
            ("status", status, {"active", "suspended", "retired"}),
            ("tier_code", tier_code, {"registered", "silver", "gold", "strategic"}),
            ("region_scope", region_scope, {"global", "cn", "apac", "emea", "americas"}),
        ):
            if value is not None:
                if value not in allowed:
                    raise ApiError("channel.request_invalid")
                clauses.append(f"{column}=?")
                values.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM channel_partners {where}", values
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT channel_partner_id FROM channel_partners {where}
                ORDER BY updated_at DESC,channel_partner_id LIMIT ? OFFSET ?""",
            (*values, limit, offset),
        ).fetchall()
        response = ChannelPartnerListResponse(
            items=tuple(_partner_summary(connection, row[0]) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def apply_partner_command(
    config: AppConfig,
    command: PartnerCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ChannelPartnerResponse:
    action_code = {
        "create": "partner.create",
        "update": "partner.update",
        "upsert_membership": "membership.upsert",
        "revoke_membership": "membership.revoke",
        "rotate_referral_code": "referral.rotate",
    }[command.operation]
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = _sha(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.channel.manage",
        )
        _require_schema(connection)
        replay = _command_replay(connection, actor.operator_id, action_code, key_sha, request_sha)
        if replay is not None:
            response = ChannelPartnerResponse(
                partner=_partner_summary(connection, replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        if command.operation == "create":
            partner_id = _create_partner(connection, actor, command)
            resource_type, resource_id = "workspace", str(command.owner_workspace_id)
        else:
            partner_id = str(command.channel_partner_id)
            partner = _partner_row(connection, partner_id)
            if int(partner["revision"]) != command.expected_revision:
                raise ApiError("channel.revision_conflict")
            resource_type, resource_id = "channel_partner", partner_id
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"channel.{action_code}",
            resource_type=resource_type,
            resource_id=resource_id,
        )
        issued_referral: str | None = None
        if command.operation == "update":
            _update_partner(connection, actor, partner_id, command)
        elif command.operation == "upsert_membership":
            _upsert_membership(connection, actor, partner_id, command)
            _bump_partner(connection, actor, partner_id, int(command.expected_revision))
        elif command.operation == "revoke_membership":
            _revoke_membership(connection, actor, partner_id, command)
            _bump_partner(connection, actor, partner_id, int(command.expected_revision))
        elif command.operation == "rotate_referral_code":
            issued_referral = _rotate_referral(connection, actor, partner_id, command)
            _bump_partner(connection, actor, partner_id, int(command.expected_revision))
        partner = _partner_row(connection, partner_id)
        _record_action(
            connection,
            actor,
            partner,
            action_code,
            key_sha,
            request_sha,
            partner_id,
            int(partner["revision"]),
        )
        _emit_event(
            connection,
            partner,
            "channel.partner_changed",
            partner_id,
            int(partner["revision"]),
            _digest({"partner_id": partner_id, "revision": int(partner["revision"])}),
        )
        response = ChannelPartnerResponse(
            partner=_partner_summary(connection, partner_id),
            issued_referral_code=issued_referral,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, UnicodeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def register_channel_lead(
    config: AppConfig,
    command: LeadCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> LeadResponse:
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = _sha(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.channel.lead.register",
        )
        _require_schema(connection)
        replay = _command_replay(
            connection, actor.operator_id, "lead.register", key_sha, request_sha
        )
        if replay is not None:
            response = LeadResponse(
                lead=_lead_summary(connection, replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        partner = _partner_row(connection, command.channel_partner_id)
        if partner["status"] != "active" or int(partner["revision"]) != command.expected_revision:
            raise ApiError("channel.revision_conflict")
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code="channel.lead.register",
            resource_type="channel_partner",
            resource_id=command.channel_partner_id,
        )
        referral_id = _referral_id(connection, command.referral_code)
        candidate_owner = (
            _workspace_owner(connection, command.candidate_workspace_id)
            if command.candidate_workspace_id
            else None
        )
        lead_id = _id("cld_")
        contact = json.dumps(
            {"email": command.contact_email, "phone": command.contact_phone},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        secret = config.tenant_data_encryption_secret
        if not secret:
            raise ApiError("channel.unavailable")
        ciphertext = encrypt_tenant_data(
            contact,
            secret=secret,
            owner=TenantDataEnvelopeOwner(
                tenant_id=str(partner["tenant_id"]),
                object_id=lead_id,
                segment_id="contact",
                schema_version="metahub.channel-lead/v1",
                key_version=config.tenant_data_encryption_version,
            ),
            kind="archive",
        )
        now = _db_time(_utcnow())
        connection.execute(
            """INSERT INTO channel_registered_leads(
              channel_lead_id,channel_partner_id,owner_workspace_id,tenant_id,lead_code,
              referral_code_id,region_code,stage,contact_ciphertext,contact_lookup_sha256,
              source_sha256,candidate_workspace_id,candidate_tenant_id,revision,
              created_by_operator_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'registered',?,?,?,?,?,1,?,?,?)""",
            (
                lead_id,
                partner["channel_partner_id"],
                partner["owner_workspace_id"],
                partner["tenant_id"],
                command.lead_code,
                referral_id,
                command.region_code,
                ciphertext,
                _sha(contact.decode("utf-8")),
                command.source_sha256,
                candidate_owner[0] if candidate_owner else None,
                candidate_owner[1] if candidate_owner else None,
                actor.operator_id,
                now,
                now,
            ),
        )
        if referral_id:
            connection.execute(
                """UPDATE channel_referral_codes SET usage_count=usage_count+1,
                     revision=revision+1,updated_at=? WHERE channel_referral_code_id=?""",
                (now, referral_id),
            )
        _record_action(
            connection,
            actor,
            partner,
            "lead.register",
            key_sha,
            request_sha,
            lead_id,
            1,
        )
        _emit_event(
            connection,
            partner,
            "channel.lead_registered",
            lead_id,
            1,
            _digest({"lead_id": lead_id, "stage": "registered", "revision": 1}),
        )
        response = LeadResponse(
            lead=_lead_summary(connection, lead_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, UnicodeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def request_workspace_attribution(
    config: AppConfig,
    command: AttributionRequestCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> AttributionResponse:
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = _sha(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.channel.attribution.request",
        )
        _require_schema(connection)
        replay = _command_replay(
            connection, actor.operator_id, "attribution.request", key_sha, request_sha
        )
        if replay is not None:
            response = AttributionResponse(
                attribution=_attribution_summary(connection, replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        partner = _partner_row(connection, command.channel_partner_id)
        if partner["status"] != "active":
            raise ApiError("channel.partner_not_found")
        target = _workspace_owner(connection, command.target_workspace_id)
        rule = connection.execute(
            """SELECT * FROM channel_commission_rule_snapshots
                WHERE channel_commission_rule_snapshot_id=? AND channel_partner_id=?
                  AND owner_workspace_id=? AND tenant_id=? AND effective_until IS NULL""",
            (
                command.channel_commission_rule_snapshot_id,
                partner["channel_partner_id"],
                partner["owner_workspace_id"],
                partner["tenant_id"],
            ),
        ).fetchone()
        if rule is None:
            raise ApiError("channel.rule_not_found")
        if command.channel_lead_id is not None:
            lead = connection.execute(
                """SELECT 1 FROM channel_registered_leads WHERE channel_lead_id=?
                    AND channel_partner_id=? AND owner_workspace_id=? AND tenant_id=?""",
                (
                    command.channel_lead_id,
                    partner["channel_partner_id"],
                    partner["owner_workspace_id"],
                    partner["tenant_id"],
                ),
            ).fetchone()
            if lead is None:
                raise ApiError("channel.lead_not_found")
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code="channel.attribution.request",
            resource_type="workspace",
            resource_id=target[0],
        )
        current = connection.execute(
            """SELECT channel_partner_id,starts_at,revision FROM workspace_attributions
                WHERE target_workspace_id=? AND target_tenant_id=? AND status='active'""",
            target,
        ).fetchone()
        current_revision = int(current["revision"]) if current is not None else 0
        if current_revision != command.expected_attribution_revision:
            raise ApiError("channel.revision_conflict")
        if current is not None and command.starts_at <= _parse_time(current["starts_at"]):
            raise ApiError("channel.attribution_conflict")
        required = 2 if current is not None else 1
        attribution_id = _id("wat_")
        change_id = _id("atc_")
        now = _db_time(_utcnow())
        starts = _db_time(command.starts_at)
        connection.execute(
            """INSERT INTO workspace_attributions(
              workspace_attribution_id,channel_partner_id,owner_workspace_id,tenant_id,
              target_workspace_id,target_tenant_id,channel_lead_id,
              channel_commission_rule_snapshot_id,status,starts_at,ends_at,revision,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'pending_approval',?,NULL,1,?,?)""",
            (
                attribution_id,
                partner["channel_partner_id"],
                partner["owner_workspace_id"],
                partner["tenant_id"],
                target[0],
                target[1],
                command.channel_lead_id,
                command.channel_commission_rule_snapshot_id,
                starts,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO attribution_changes(
              attribution_change_id,workspace_attribution_id,channel_partner_id,
              owner_workspace_id,tenant_id,target_workspace_id,target_tenant_id,
              before_partner_id,before_attribution_revision,after_partner_id,reason_code,
              requested_by_operator_id,
              requester_permission_revision,required_approvals,approval_generation,
              approval_count,status,revision,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,'pending',1,?,?)""",
            (
                change_id,
                attribution_id,
                partner["channel_partner_id"],
                partner["owner_workspace_id"],
                partner["tenant_id"],
                target[0],
                target[1],
                current["channel_partner_id"] if current else None,
                current_revision or None,
                partner["channel_partner_id"],
                command.reason_code,
                actor.operator_id,
                actor.operator_revision,
                required,
                now,
                now,
            ),
        )
        _record_action(
            connection,
            actor,
            partner,
            "attribution.request",
            key_sha,
            request_sha,
            attribution_id,
            1,
        )
        response = AttributionResponse(
            attribution=_attribution_summary(connection, attribution_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def approve_workspace_attribution(
    config: AppConfig,
    attribution_id: str,
    command: AttributionApprovalCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> AttributionResponse:
    request_sha = _digest(
        {"workspace_attribution_id": attribution_id, **command.model_dump(mode="json")}
    )
    key_sha = _sha(idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.channel.attribution.approve",
        )
        if "platform_owner" not in actor.roles:
            raise ApiError("platform.permission_denied")
        _require_schema(connection)
        replay = _command_replay(
            connection, actor.operator_id, "attribution.approve", key_sha, request_sha
        )
        if replay is not None:
            response = AttributionResponse(
                attribution=_attribution_summary(connection, replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        attribution = _attribution_row(connection, attribution_id)
        change = connection.execute(
            "SELECT * FROM attribution_changes WHERE workspace_attribution_id=?",
            (attribution_id,),
        ).fetchone()
        if (
            change is None
            or change["status"] != "pending"
            or int(change["revision"]) != command.expected_revision
        ):
            raise ApiError("channel.revision_conflict")
        if change["requested_by_operator_id"] == actor.operator_id:
            raise ApiError("channel.approval_independence_required")
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code="channel.attribution.approve",
            resource_type="workspace_attribution",
            resource_id=attribution_id,
        )
        if connection.execute(
            """SELECT 1 FROM attribution_change_approvals
                WHERE attribution_change_id=? AND approval_generation=?
                  AND approver_operator_id=?""",
            (change["attribution_change_id"], change["approval_generation"], actor.operator_id),
        ).fetchone():
            raise ApiError("channel.approval_independence_required")
        now = _db_time(_utcnow())
        connection.execute(
            """INSERT INTO attribution_change_approvals(
              attribution_change_approval_id,attribution_change_id,approval_generation,
              approver_operator_id,approver_permission_revision,decision,reason_code,decided_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                _id("aca_"),
                change["attribution_change_id"],
                change["approval_generation"],
                actor.operator_id,
                actor.operator_revision,
                command.decision,
                command.reason_code,
                now,
            ),
        )
        count = int(change["approval_count"]) + (1 if command.decision == "approved" else 0)
        final = command.decision == "rejected" or count == int(change["required_approvals"])
        change_status = (
            "rejected"
            if command.decision == "rejected"
            else "approved"
            if final
            else "pending"
        )
        attribution_status = (
            "rejected"
            if command.decision == "rejected"
            else "active"
            if final
            else "pending_approval"
        )
        if final and command.decision == "approved":
            _revalidate_attribution_authority(connection, change)
            _revalidate_attribution_dependencies(connection, attribution)
            connection.execute(
                """UPDATE workspace_attributions SET status='superseded',ends_at=?,
                     revision=revision+1,updated_at=?
                   WHERE target_workspace_id=? AND target_tenant_id=? AND status='active'""",
                (
                    attribution["starts_at"],
                    now,
                    attribution["target_workspace_id"],
                    attribution["target_tenant_id"],
                ),
            )
        connection.execute(
            """UPDATE attribution_changes SET approval_count=?,status=?,revision=revision+1,
                 updated_at=? WHERE attribution_change_id=? AND revision=?""",
            (count, change_status, now, change["attribution_change_id"], command.expected_revision),
        )
        connection.execute(
            """UPDATE workspace_attributions SET status=?,revision=revision+1,updated_at=?
               WHERE workspace_attribution_id=?""",
            (attribution_status, now, attribution_id),
        )
        updated = _attribution_row(connection, attribution_id)
        partner = _partner_row(connection, updated["channel_partner_id"])
        _record_action(
            connection,
            actor,
            partner,
            "attribution.approve",
            key_sha,
            request_sha,
            attribution_id,
            int(updated["revision"]),
        )
        _emit_event(
            connection,
            partner,
            "workspace.attribution_changed",
            attribution_id,
            int(updated["revision"]),
            _digest(
                {
                    "attribution_id": attribution_id,
                    "status": attribution_status,
                    "revision": int(updated["revision"]),
                }
            ),
        )
        response = AttributionResponse(
            attribution=_attribution_summary(connection, attribution_id),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def list_channel_statements(
    config: AppConfig,
    *,
    platform_session_token: str,
    channel_partner_id: str | None,
    currency: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> StatementListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _platform_actor(
            connection, config, platform_session_token, None, "platform.channel.statement.read"
        )
        _require_schema(connection)
        clauses: list[str] = []
        values: list[object] = []
        if channel_partner_id is not None:
            if not _valid_id(channel_partner_id, "par_"):
                raise ApiError("channel.request_invalid")
            clauses.append("channel_partner_id=?")
            values.append(channel_partner_id)
        if currency is not None:
            if currency not in {"CNY", "USD"}:
                raise ApiError("channel.request_invalid")
            clauses.append("currency=?")
            values.append(currency)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM channel_statements {where}", values
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM channel_statements {where}
                ORDER BY period_start DESC,channel_statement_id LIMIT ? OFFSET ?""",
            (*values, limit, offset),
        ).fetchall()
        response = StatementListResponse(
            items=tuple(_statement_summary(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.unavailable") from None
    finally:
        connection.close()


def list_partner_workspaces(
    config: AppConfig,
    *,
    global_session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PartnerWorkspaceListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        identity, _now = authenticate_global_session_in_transaction(
            connection, config, session_token=global_session_token, touch=True
        )
        _require_schema(connection)
        membership = connection.execute(
            """SELECT membership.*,partner.status partner_status
                 FROM channel_memberships membership
                 JOIN channel_partners partner
                   ON partner.channel_partner_id=membership.channel_partner_id
                  AND partner.owner_workspace_id=membership.owner_workspace_id
                  AND partner.tenant_id=membership.tenant_id
                WHERE membership.account_id=? AND membership.status='active'
                  AND partner.status='active'
                ORDER BY membership.created_at,membership.channel_membership_id LIMIT 1""",
            (identity.account_id,),
        ).fetchone()
        if membership is None:
            raise ApiError("partner.membership_required")
        partner_id = str(membership["channel_partner_id"])
        total = int(
            connection.execute(
                """SELECT COUNT(*) FROM workspace_attributions attribution
                    JOIN workspace_operational_states state
                      ON state.workspace_id=attribution.target_workspace_id
                     AND state.tenant_id=attribution.target_tenant_id
                   WHERE attribution.channel_partner_id=? AND attribution.owner_workspace_id=?
                     AND attribution.tenant_id=? AND attribution.status='active'""",
                (
                    partner_id,
                    membership["owner_workspace_id"],
                    membership["tenant_id"],
                ),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """SELECT attribution.target_workspace_id,attribution.target_tenant_id,
                      attribution.starts_at,tenant.name,region.region_code,state.status
                 FROM workspace_attributions attribution
                 JOIN tenants tenant ON tenant.tenant_id=attribution.target_tenant_id
                 JOIN workspace_regions region
                   ON region.workspace_id=attribution.target_workspace_id
                  AND region.tenant_id=attribution.target_tenant_id
                 JOIN workspace_operational_states state
                   ON state.workspace_id=attribution.target_workspace_id
                  AND state.tenant_id=attribution.target_tenant_id
                WHERE attribution.channel_partner_id=? AND attribution.owner_workspace_id=?
                  AND attribution.tenant_id=? AND attribution.status='active'
                ORDER BY attribution.starts_at,attribution.workspace_attribution_id
                LIMIT ? OFFSET ?""",
            (
                partner_id,
                membership["owner_workspace_id"],
                membership["tenant_id"],
                limit,
                offset,
            ),
        ).fetchall()
        finance = membership["role_code"] == "partner_finance"
        items = tuple(
            PartnerWorkspaceSummary(
                workspace_id=row["target_workspace_id"],
                display_name=row["name"],
                region_code=row["region_code"],
                attribution_starts_at=_parse_time(row["starts_at"]),
                operational_status=row["status"],
                statement_totals=(
                    _partner_statement_totals(connection, partner_id)
                    if finance
                    else None
                ),
            )
            for row in rows
        )
        response = PartnerWorkspaceListResponse(
            partner=_partner_summary(
                connection, partner_id, membership_account_id=identity.account_id
            ),
            role_code=membership["role_code"],
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("partner.unavailable") from None
    finally:
        connection.close()


def _create_partner(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    command: PartnerCommand,
) -> str:
    assert command.owner_workspace_id and command.owner_account_id
    assert command.partner_code and command.display_name and command.tier_code
    assert command.region_scope and command.contract_sha256
    owner = _workspace_owner(connection, command.owner_workspace_id)
    account = connection.execute(
        "SELECT 1 FROM global_accounts WHERE account_id=? AND status='active' AND login_enabled=1",
        (command.owner_account_id,),
    ).fetchone()
    if account is None:
        raise ApiError("channel.account_not_found")
    partner_id = _id("par_")
    now = _db_time(_utcnow())
    connection.execute(
        """INSERT INTO channel_partners(
          channel_partner_id,owner_workspace_id,tenant_id,partner_code,display_name,
          tier_code,region_scope,contract_sha256,status,revision,created_by_operator_id,
          updated_by_operator_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'active',1,?,?,?,?)""",
        (
            partner_id,
            owner[0],
            owner[1],
            command.partner_code,
            command.display_name,
            command.tier_code,
            command.region_scope,
            command.contract_sha256,
            actor.operator_id,
            actor.operator_id,
            now,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO channel_memberships(
          channel_membership_id,channel_partner_id,owner_workspace_id,tenant_id,account_id,
          role_code,status,revision,invited_by_operator_id,revoked_by_operator_id,
          invitation_sha256,created_at,updated_at,revoked_at)
        VALUES(?,?,?,?,?,'partner_owner','active',1,?,NULL,?,?,?,NULL)""",
        (
            _id("chm_"),
            partner_id,
            owner[0],
            owner[1],
            command.owner_account_id,
            actor.operator_id,
            _digest({"partner_id": partner_id, "account_id": command.owner_account_id}),
            now,
            now,
        ),
    )
    for currency in ("CNY", "USD"):
        connection.execute(
            """INSERT INTO channel_commission_rule_snapshots(
              channel_commission_rule_snapshot_id,channel_partner_id,owner_workspace_id,
              tenant_id,contract_sha256,tier_code,region_scope,currency,commission_bps,
              version,effective_from,effective_until,created_at)
            VALUES(?,?,?,?,?,?,?,?,1000,1,?,NULL,?)""",
            (
                _id("ccr_"),
                partner_id,
                owner[0],
                owner[1],
                command.contract_sha256,
                command.tier_code,
                command.region_scope,
                currency,
                now,
                now,
            ),
        )
    return partner_id


def _update_partner(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner_id: str,
    command: PartnerCommand,
) -> None:
    now = _db_time(_utcnow())
    updated = connection.execute(
        """UPDATE channel_partners SET display_name=coalesce(?,display_name),
             tier_code=coalesce(?,tier_code),region_scope=coalesce(?,region_scope),
             status=coalesce(?,status),revision=revision+1,updated_by_operator_id=?,updated_at=?
           WHERE channel_partner_id=? AND revision=?""",
        (
            command.display_name,
            command.tier_code,
            command.region_scope,
            command.status,
            actor.operator_id,
            now,
            partner_id,
            command.expected_revision,
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("channel.revision_conflict")


def _upsert_membership(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner_id: str,
    command: PartnerCommand,
) -> None:
    assert command.account_id and command.role_code
    if connection.execute(
        "SELECT 1 FROM global_accounts WHERE account_id=? AND status='active' AND login_enabled=1",
        (command.account_id,),
    ).fetchone() is None:
        raise ApiError("channel.account_not_found")
    partner = _partner_row(connection, partner_id)
    now = _db_time(_utcnow())
    existing = connection.execute(
        "SELECT * FROM channel_memberships WHERE channel_partner_id=? AND account_id=?",
        (partner_id, command.account_id),
    ).fetchone()
    if existing is None:
        connection.execute(
            """INSERT INTO channel_memberships(
              channel_membership_id,channel_partner_id,owner_workspace_id,tenant_id,account_id,
              role_code,status,revision,invited_by_operator_id,revoked_by_operator_id,
              invitation_sha256,created_at,updated_at,revoked_at)
            VALUES(?,?,?,?,?,?,'active',1,?,NULL,?,?,?,NULL)""",
            (
                _id("chm_"),
                partner_id,
                partner["owner_workspace_id"],
                partner["tenant_id"],
                command.account_id,
                command.role_code,
                actor.operator_id,
                _digest({"partner_id": partner_id, "account_id": command.account_id}),
                now,
                now,
            ),
        )
    else:
        connection.execute(
            """UPDATE channel_memberships SET role_code=?,status='active',revision=revision+1,
                 revoked_by_operator_id=NULL,revoked_at=NULL,updated_at=?
               WHERE channel_membership_id=?""",
            (command.role_code, now, existing["channel_membership_id"]),
        )


def _revoke_membership(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner_id: str,
    command: PartnerCommand,
) -> None:
    now = _db_time(_utcnow())
    membership = connection.execute(
        """SELECT * FROM channel_memberships WHERE channel_membership_id=?
            AND channel_partner_id=?""",
        (command.channel_membership_id, partner_id),
    ).fetchone()
    if membership is None or membership["status"] != "active":
        raise ApiError("channel.membership_not_found")
    if membership["role_code"] == "partner_owner":
        owners = int(
            connection.execute(
                """SELECT COUNT(*) FROM channel_memberships WHERE channel_partner_id=?
                    AND role_code='partner_owner' AND status='active'""",
                (partner_id,),
            ).fetchone()[0]
        )
        if owners <= 1:
            raise ApiError("channel.last_owner_required")
    connection.execute(
        """UPDATE channel_memberships SET status='revoked',revision=revision+1,
             revoked_by_operator_id=?,revoked_at=?,updated_at=? WHERE channel_membership_id=?""",
        (actor.operator_id, now, now, membership["channel_membership_id"]),
    )


def _rotate_referral(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner_id: str,
    command: PartnerCommand,
) -> str:
    assert command.referral_code
    partner = _partner_row(connection, partner_id)
    now = _db_time(_utcnow())
    connection.execute(
        """UPDATE channel_referral_codes SET status='revoked',revision=revision+1,updated_at=?
           WHERE channel_partner_id=? AND status='active'""",
        (now, partner_id),
    )
    connection.execute(
        """INSERT INTO channel_referral_codes(
          channel_referral_code_id,channel_partner_id,owner_workspace_id,tenant_id,
          lookup_sha256,masked_suffix,status,usage_limit,usage_count,expires_at,
          revision,created_by_operator_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,'active',?,0,?,1,?,?,?)""",
        (
            _id("crc_"),
            partner_id,
            partner["owner_workspace_id"],
            partner["tenant_id"],
            _sha(command.referral_code),
            command.referral_code[-6:],
            command.usage_limit,
            _db_time(command.expires_at) if command.expires_at else None,
            actor.operator_id,
            now,
            now,
        ),
    )
    return command.referral_code


def _bump_partner(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner_id: str,
    expected_revision: int,
) -> None:
    updated = connection.execute(
        """UPDATE channel_partners SET revision=revision+1,updated_by_operator_id=?,updated_at=?
           WHERE channel_partner_id=? AND revision=?""",
        (actor.operator_id, _db_time(_utcnow()), partner_id, expected_revision),
    ).rowcount
    if updated != 1:
        raise ApiError("channel.revision_conflict")


def _partner_summary(
    connection: sqlite3.Connection,
    partner_id: str,
    *,
    membership_account_id: str | None = None,
) -> ChannelPartnerSummary:
    row = _partner_row(connection, partner_id)
    membership_where = " AND account_id=?" if membership_account_id else ""
    membership_values: tuple[object, ...] = (
        (partner_id, membership_account_id) if membership_account_id else (partner_id,)
    )
    memberships = tuple(
        MembershipSummary(
            channel_membership_id=item["channel_membership_id"],
            account_id=item["account_id"],
            role_code=item["role_code"],
            status=item["status"],
            revision=int(item["revision"]),
        )
        for item in connection.execute(
            f"""SELECT * FROM channel_memberships WHERE channel_partner_id=?{membership_where}
                ORDER BY status,role_code,channel_membership_id""",
            membership_values,
        ).fetchall()
    )
    referral = connection.execute(
        """SELECT * FROM channel_referral_codes WHERE channel_partner_id=? AND status='active'
            ORDER BY created_at DESC LIMIT 1""",
        (partner_id,),
    ).fetchone()
    platform_projection = membership_account_id is None
    commission_rules = tuple(
        CommissionRuleSummary(
            channel_commission_rule_snapshot_id=item[
                "channel_commission_rule_snapshot_id"
            ],
            currency=item["currency"],
            commission_bps=int(item["commission_bps"]),
            version=int(item["version"]),
            effective_from=_parse_time(item["effective_from"]),
            effective_until=_optional_time(item["effective_until"]),
        )
        for item in (
            connection.execute(
                """SELECT * FROM channel_commission_rule_snapshots
                    WHERE channel_partner_id=? AND effective_until IS NULL
                    ORDER BY currency,version""",
                (partner_id,),
            ).fetchall()
            if platform_projection
            else ()
        )
    )
    pending_attributions = tuple(
        PendingAttributionSummary(
            workspace_attribution_id=item["workspace_attribution_id"],
            attribution_change_id=item["attribution_change_id"],
            target_workspace_id=item["target_workspace_id"],
            starts_at=_parse_time(item["starts_at"]),
            required_approvals=int(item["required_approvals"]),
            approval_count=int(item["approval_count"]),
            approval_generation=int(item["approval_generation"]),
            revision=int(item["change_revision"]),
        )
        for item in (
            connection.execute(
                """SELECT attribution.workspace_attribution_id,attribution.target_workspace_id,
                          attribution.starts_at,change.attribution_change_id,
                          change.required_approvals,change.approval_count,
                          change.approval_generation,change.revision change_revision
                     FROM workspace_attributions attribution
                     JOIN attribution_changes change
                       ON change.workspace_attribution_id=attribution.workspace_attribution_id
                    WHERE attribution.channel_partner_id=?
                      AND attribution.status='pending_approval' AND change.status='pending'
                    ORDER BY attribution.created_at,attribution.workspace_attribution_id""",
                (partner_id,),
            ).fetchall()
            if platform_projection
            else ()
        )
    )
    return ChannelPartnerSummary(
        channel_partner_id=row["channel_partner_id"],
        owner_workspace_id=row["owner_workspace_id"],
        partner_code=row["partner_code"],
        display_name=row["display_name"],
        tier_code=row["tier_code"],
        region_scope=row["region_scope"],
        contract_sha256=row["contract_sha256"],
        status=row["status"],
        revision=int(row["revision"]),
        memberships=memberships,
        referral=(
            ReferralSummary(
                channel_referral_code_id=referral["channel_referral_code_id"],
                masked_suffix=referral["masked_suffix"],
                status=referral["status"],
                usage_limit=referral["usage_limit"],
                usage_count=int(referral["usage_count"]),
                expires_at=_optional_time(referral["expires_at"]),
                revision=int(referral["revision"]),
            )
            if referral
            else None
        ),
        commission_rules=commission_rules,
        pending_attributions=pending_attributions,
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _lead_summary(connection: sqlite3.Connection, lead_id: str) -> LeadSummary:
    row = connection.execute(
        "SELECT * FROM channel_registered_leads WHERE channel_lead_id=?", (lead_id,)
    ).fetchone()
    if row is None:
        raise ApiError("channel.lead_not_found")
    return LeadSummary(
        channel_lead_id=row["channel_lead_id"],
        channel_partner_id=row["channel_partner_id"],
        lead_code=row["lead_code"],
        region_code=row["region_code"],
        stage=row["stage"],
        source_sha256=row["source_sha256"],
        candidate_workspace_id=row["candidate_workspace_id"],
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
    )


def _attribution_summary(connection: sqlite3.Connection, attribution_id: str) -> AttributionSummary:
    attribution = _attribution_row(connection, attribution_id)
    change = connection.execute(
        "SELECT * FROM attribution_changes WHERE workspace_attribution_id=?", (attribution_id,)
    ).fetchone()
    if change is None:
        raise ApiError("channel.attribution_not_found")
    approvals = tuple(
        ApprovalSummary(
            attribution_change_approval_id=row["attribution_change_approval_id"],
            approver_operator_id=row["approver_operator_id"],
            decision=row["decision"],
            reason_code=row["reason_code"],
            decided_at=_parse_time(row["decided_at"]),
        )
        for row in connection.execute(
            """SELECT * FROM attribution_change_approvals WHERE attribution_change_id=?
                AND approval_generation=? ORDER BY decided_at,attribution_change_approval_id""",
            (change["attribution_change_id"], change["approval_generation"]),
        ).fetchall()
    )
    return AttributionSummary(
        workspace_attribution_id=attribution["workspace_attribution_id"],
        attribution_change_id=change["attribution_change_id"],
        channel_partner_id=attribution["channel_partner_id"],
        target_workspace_id=attribution["target_workspace_id"],
        status=attribution["status"],
        starts_at=_parse_time(attribution["starts_at"]),
        ends_at=_optional_time(attribution["ends_at"]),
        required_approvals=int(change["required_approvals"]),
        approval_count=int(change["approval_count"]),
        approval_generation=int(change["approval_generation"]),
        revision=int(change["revision"]),
        approvals=approvals,
    )


def _statement_summary(row: sqlite3.Row) -> StatementSummary:
    return StatementSummary(
        channel_statement_id=row["channel_statement_id"],
        channel_partner_id=row["channel_partner_id"],
        period_start=_parse_time(row["period_start"]),
        period_end=_parse_time(row["period_end"]),
        currency=row["currency"],
        total_amount_micros=int(row["total_amount_micros"]),
        total_commission_micros=int(row["total_commission_micros"]),
        line_count=int(row["line_count"]),
        reconciliation_sha256=row["reconciliation_sha256"],
        status=row["status"],
        close_revision=int(row["close_revision"]),
        closed_at=_optional_time(row["closed_at"]),
    )


def _partner_statement_totals(connection: sqlite3.Connection, partner_id: str) -> dict[str, int]:
    return {
        str(row["currency"]): int(row["amount"])
        for row in connection.execute(
            """SELECT currency,sum(total_commission_micros) amount FROM channel_statements
                WHERE channel_partner_id=? AND status IN('imported','closed') GROUP BY currency""",
            (partner_id,),
        ).fetchall()
    }


def _platform_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    token: str,
    csrf: str | None,
    permission: str,
) -> PlatformContext:
    actor = authenticate_platform_session_in_transaction(
        connection, config, session_token=token, csrf_token=csrf, touch=True
    )
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")
    return actor


def _consume_step_up(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    grant: str,
    *,
    action_code: str,
    resource_type: str,
    resource_id: str,
) -> None:
    consume_global_step_up_grant_in_transaction(
        connection,
        config,
        account_id=actor.account_id,
        global_session_id=actor.global_session_id,
        grant=grant,
        action_code=action_code,
        resource_type=resource_type,
        resource_id=resource_id,
        now=_utcnow(),
    )


def _command_replay(
    connection: sqlite3.Connection,
    operator_id: str,
    action_code: str,
    key_sha: str,
    request_sha: str,
) -> str | None:
    row = connection.execute(
        """SELECT request_sha256,result_object_id FROM channel_action_commands
            WHERE operator_id=? AND action_code=? AND idempotency_key_sha256=?""",
        (operator_id, action_code, key_sha),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_sha):
        raise ApiError("channel.idempotency_conflict")
    return str(row["result_object_id"])


def _record_action(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    partner: sqlite3.Row,
    action_code: str,
    key_sha: str,
    request_sha: str,
    result_id: str,
    result_revision: int,
) -> None:
    connection.execute(
        """INSERT INTO channel_action_commands(
          channel_action_command_id,operator_id,channel_partner_id,owner_workspace_id,
          tenant_id,action_code,idempotency_key_sha256,request_sha256,result_object_id,
          result_revision,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("cac_"),
            actor.operator_id,
            partner["channel_partner_id"],
            partner["owner_workspace_id"],
            partner["tenant_id"],
            action_code,
            key_sha,
            request_sha,
            result_id,
            result_revision,
            _db_time(_utcnow()),
        ),
    )


def _emit_event(
    connection: sqlite3.Connection,
    partner: sqlite3.Row,
    event_type: str,
    object_id: str,
    revision: int,
    payload_sha: str,
) -> None:
    event_id = _id("cev_")
    now = _db_time(_utcnow())
    connection.execute(
        """INSERT INTO channel_events(
          channel_event_id,channel_partner_id,owner_workspace_id,tenant_id,event_type,
          object_id,object_revision,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            partner["channel_partner_id"],
            partner["owner_workspace_id"],
            partner["tenant_id"],
            event_type,
            object_id,
            revision,
            payload_sha,
            now,
        ),
    )
    connection.execute(
        """INSERT INTO channel_event_outbox(
          channel_event_outbox_id,channel_event_id,channel_partner_id,owner_workspace_id,
          tenant_id,status,attempt_count,fencing_generation,next_attempt_at,delivered_at,
          last_error_code,created_at,updated_at)
        VALUES(?,?,?,?,?,'pending',0,0,?,NULL,NULL,?,?)""",
        (
            _id("ceo_"),
            event_id,
            partner["channel_partner_id"],
            partner["owner_workspace_id"],
            partner["tenant_id"],
            now,
            now,
            now,
        ),
    )


def _revalidate_attribution_dependencies(
    connection: sqlite3.Connection, attribution: sqlite3.Row
) -> None:
    if connection.execute(
        """SELECT 1 FROM channel_partners partner
            JOIN channel_commission_rule_snapshots rule
              ON rule.channel_partner_id=partner.channel_partner_id
             AND rule.owner_workspace_id=partner.owner_workspace_id
             AND rule.tenant_id=partner.tenant_id
           WHERE partner.channel_partner_id=? AND partner.owner_workspace_id=?
             AND partner.tenant_id=? AND partner.status='active'
             AND rule.channel_commission_rule_snapshot_id=? AND rule.effective_until IS NULL""",
        (
            attribution["channel_partner_id"],
            attribution["owner_workspace_id"],
            attribution["tenant_id"],
            attribution["channel_commission_rule_snapshot_id"],
        ),
    ).fetchone() is None:
        raise ApiError("channel.attribution_conflict")
    _workspace_owner(connection, attribution["target_workspace_id"])


def _revalidate_attribution_authority(
    connection: sqlite3.Connection,
    change: sqlite3.Row,
) -> None:
    if not _operator_has_current_permission(
        connection,
        str(change["requested_by_operator_id"]),
        int(change["requester_permission_revision"]),
        "platform.channel.attribution.request",
    ):
        raise ApiError("channel.approval_authority_changed")
    approvals = connection.execute(
        """SELECT approver_operator_id,approver_permission_revision
             FROM attribution_change_approvals
            WHERE attribution_change_id=? AND approval_generation=?
              AND decision='approved'""",
        (change["attribution_change_id"], change["approval_generation"]),
    ).fetchall()
    if len(approvals) != int(change["required_approvals"]):
        raise ApiError("channel.approval_authority_changed")
    if any(
        not _operator_has_current_permission(
            connection,
            str(approval["approver_operator_id"]),
            int(approval["approver_permission_revision"]),
            "platform.channel.attribution.approve",
        )
        for approval in approvals
    ):
        raise ApiError("channel.approval_authority_changed")

    current = connection.execute(
        """SELECT channel_partner_id,revision FROM workspace_attributions
            WHERE target_workspace_id=? AND target_tenant_id=? AND status='active'""",
        (change["target_workspace_id"], change["target_tenant_id"]),
    ).fetchone()
    before_partner_id = change["before_partner_id"]
    before_revision = change["before_attribution_revision"]
    if before_partner_id is None:
        if current is not None:
            raise ApiError("channel.revision_conflict")
    elif (
        current is None
        or current["channel_partner_id"] != before_partner_id
        or int(current["revision"]) != int(before_revision)
    ):
        raise ApiError("channel.revision_conflict")


def _operator_has_current_permission(
    connection: sqlite3.Connection,
    operator_id: str,
    expected_revision: int,
    permission_code: str,
) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM platform_operators operator
                JOIN platform_operator_role_bindings binding
                  ON binding.operator_id=operator.operator_id AND binding.status='active'
                JOIN platform_channel_role_permissions permission
                  ON permission.role_code=binding.role_code
               WHERE operator.operator_id=? AND operator.status='active'
                 AND operator.revision=? AND permission.permission_code=? LIMIT 1""",
            (operator_id, expected_revision, permission_code),
        ).fetchone()
        is not None
    )


def _referral_id(connection: sqlite3.Connection, plaintext: str | None) -> str | None:
    if plaintext is None:
        return None
    row = connection.execute(
        """SELECT channel_referral_code_id,usage_limit,usage_count,expires_at
             FROM channel_referral_codes WHERE lookup_sha256=? AND status='active'""",
        (_sha(plaintext),),
    ).fetchone()
    if row is None:
        raise ApiError("channel.referral_not_found")
    if (row["usage_limit"] is not None and int(row["usage_count"]) >= int(row["usage_limit"])) or (
        row["expires_at"] is not None and _parse_time(row["expires_at"]) <= _utcnow()
    ):
        raise ApiError("channel.referral_not_found")
    return str(row["channel_referral_code_id"])


def _workspace_owner(connection: sqlite3.Connection, workspace_id: str | None) -> tuple[str, str]:
    row = connection.execute(
        """SELECT binding.workspace_id,binding.tenant_id
             FROM workspace_tenant_bindings binding
             JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
             JOIN workspace_operational_states state
               ON state.workspace_id=binding.workspace_id AND state.tenant_id=binding.tenant_id
            WHERE binding.workspace_id=? AND binding.status='active'
              AND workspace.status='active' AND state.status='active'""",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ApiError("channel.workspace_not_found")
    return str(row["workspace_id"]), str(row["tenant_id"])


def _partner_row(connection: sqlite3.Connection, partner_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM channel_partners WHERE channel_partner_id=?", (partner_id,)
    ).fetchone()
    if row is None:
        raise ApiError("channel.partner_not_found")
    return row


def _attribution_row(connection: sqlite3.Connection, attribution_id: str) -> sqlite3.Row:
    if not _valid_id(attribution_id, "wat_"):
        raise ApiError("channel.attribution_not_found")
    row = connection.execute(
        "SELECT * FROM workspace_attributions WHERE workspace_attribution_id=?",
        (attribution_id,),
    ).fetchone()
    if row is None:
        raise ApiError("channel.attribution_not_found")
    return row


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0131' AND status='applied'"
    ).fetchone() is None:
        raise ApiError("channel.unavailable")


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("channel.unavailable") from None


def _valid_id(value: str, prefix: str) -> bool:
    return len(value) == 36 and value.startswith(prefix) and all(
        character in "0123456789abcdef" for character in value[4:]
    )


def _id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_time(value: str | None) -> datetime | None:
    return _parse_time(value) if value else None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "apply_partner_command",
    "approve_workspace_attribution",
    "list_channel_partners",
    "list_channel_statements",
    "list_partner_workspaces",
    "register_channel_lead",
    "request_workspace_attribution",
]
