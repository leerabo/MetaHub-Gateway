"""PRD-139 Platform/Partner contracts, approvals, isolation, and scheduler fence."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.channel_management import routes, scheduler, service
from metahub_control.channel_management.routes import router as channel_router
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.routes import router as platform_router
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
PLATFORM_CSRF = "c" * 43
STEP_UP = "u" * 43
GLOBAL_SESSION_COOKIE = "__Host-metahub-global-session"
PARTNER_TOKEN = "p" * 43
STAMP = "2026-08-31T00:00:00.000Z"
ROLE_BY_NAME = {
    "channel": "platform_channel",
    "owner1": "platform_owner",
    "owner2": "platform_owner",
    "billing": "platform_billing",
}


@dataclass(frozen=True, slots=True)
class Runtime:
    config: AppConfig
    partner_workspace_id: str
    partner_tenant_id: str
    target_workspace_id: str
    target_tenant_id: str
    partner_account_id: str
    tokens: dict[str, str]
    contexts: dict[str, PlatformContext]
    operator_ids: dict[str, str]


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd139-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0132"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> Runtime:
    database = tmp_path / "prd139.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    tokens: dict[str, str] = {}
    contexts: dict[str, PlatformContext] = {}
    operator_ids: dict[str, str] = {}
    with closing(connect_database(config.database_url)) as connection:
        partner_owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,membership.account_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_memberships membership
                   ON membership.workspace_id=binding.workspace_id
                  AND membership.tenant_id=binding.tenant_id
                WHERE membership.role_code='owner' AND membership.status='active' LIMIT 1"""
        ).fetchone()
        assert partner_owner is not None
        target_account_id = "gac_" + "9" * 32
        connection.execute(
            """INSERT INTO global_accounts(
              account_id,email_normalized,phone_e164,display_name,status,login_enabled,
              credential_revision,session_revision,mfa_state,failed_login_count,
              locked_until,last_login_at,created_at,updated_at)
            VALUES(?,'prd139-target@example.invalid',NULL,'Target Owner','active',0,
              1,1,'not_enrolled',0,NULL,NULL,?,?)""",
            (target_account_id, STAMP, STAMP),
        )
        target_tenant_id = "ten_" + "9" * 32
        connection.execute(
            """INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,'prd139-target','PRD-139 Target','internal_only','active',?,?)""",
            (target_tenant_id, STAMP, STAMP),
        )
        target = connection.execute(
            "SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=?",
            (target_tenant_id,),
        ).fetchone()
        assert target is not None
        target_workspace_id = str(target["workspace_id"])
        connection.execute(
            """UPDATE workspaces SET status='active',profile_state='confirmed',
                 created_by_account_id=?,revision=revision+1,updated_at=?
               WHERE workspace_id=?""",
            (target_account_id, STAMP, target_workspace_id),
        )
        connection.execute(
            """INSERT INTO workspace_operational_states(
                 workspace_operational_state_id,workspace_id,tenant_id,status,
                 revision,risk_revision,source,suspended_reason_code,
                 suspended_by_operator_id,suspended_at,created_at,updated_at)
               VALUES(?,?,?,'active',1,1,'workspace_creation',NULL,NULL,NULL,?,?)
               ON CONFLICT(workspace_id) DO UPDATE SET
                 status='active',revision=workspace_operational_states.revision+1,
                 source='workspace_creation',suspended_reason_code=NULL,
                 suspended_by_operator_id=NULL,suspended_at=NULL,updated_at=excluded.updated_at""",
            (
                "wos_" + "9" * 32,
                target_workspace_id,
                target_tenant_id,
                STAMP,
                STAMP,
            ),
        )
        for index, (name, role) in enumerate(ROLE_BY_NAME.items(), start=1):
            marker = format(index, "x")
            account_id = "gac_" + marker * 32
            operator_id = "pto_" + marker * 32
            token = chr(96 + index) * 43
            connection.execute(
                """INSERT INTO global_accounts(
                  account_id,email_normalized,phone_e164,display_name,status,login_enabled,
                  credential_revision,session_revision,mfa_state,failed_login_count,
                  locked_until,last_login_at,created_at,updated_at)
                VALUES(?,?,NULL,?,'active',0,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
                (account_id, f"prd139-{name}@example.invalid", name, STAMP, STAMP),
            )
            connection.execute(
                """INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at)
                VALUES(?,?,'active',1,NULL,?,?)""",
                (operator_id, account_id, STAMP, STAMP),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,?,'active',NULL,?,NULL)""",
                (operator_id, role, STAMP),
            )
            tokens[name] = token
            operator_ids[name] = operator_id
        connection.commit()
        expires = datetime.now(UTC) + timedelta(hours=1)
        for name, token in tokens.items():
            operator_id = operator_ids[name]
            roles, permissions, _fingerprint = platform_access_service._authority(
                connection, operator_id
            )
            marker = operator_id[4]
            contexts[token] = PlatformContext(
                platform_session_id="pts_" + marker * 32,
                operator_id=operator_id,
                account_id="gac_" + marker * 32,
                global_session_id="gss_" + marker * 32,
                factor_id="mfa_" + marker * 32,
                factor_revision=1,
                operator_revision=1,
                roles=roles,
                permissions=permissions,
                expires_at=expires,
            )
    return Runtime(
        config=config,
        partner_workspace_id=str(partner_owner["workspace_id"]),
        partner_tenant_id=str(partner_owner["tenant_id"]),
        target_workspace_id=target_workspace_id,
        target_tenant_id=target_tenant_id,
        partner_account_id=str(partner_owner["account_id"]),
        tokens=tokens,
        contexts=contexts,
        operator_ids=operator_ids,
    )


def _app(monkeypatch: pytest.MonkeyPatch, runtime: Runtime):
    def current_context(connection, token: str) -> PlatformContext:
        context = runtime.contexts.get(token)
        if context is None:
            raise ApiError("platform.session_required")
        operator = connection.execute(
            "SELECT status,revision FROM platform_operators WHERE operator_id=?",
            (context.operator_id,),
        ).fetchone()
        if operator is None or operator["status"] != "active":
            raise ApiError("platform.session_invalid")
        roles, permissions, _fingerprint = platform_access_service._authority(
            connection, context.operator_id
        )
        return replace(
            context,
            roles=roles,
            permissions=permissions,
            operator_revision=int(operator["revision"]),
        )

    def platform_actor(connection, _config, *, session_token, **_kwargs):
        return current_context(connection, session_token)

    def route_actor(_config, session_token, *, csrf_token=None):
        del csrf_token
        with closing(connect_database(runtime.config.database_url)) as connection:
            return current_context(connection, session_token)

    monkeypatch.setattr(service, "authenticate_platform_session_in_transaction", platform_actor)
    monkeypatch.setattr(routes, "authenticate_platform_session_context", route_actor)
    monkeypatch.setattr(
        platform_access_service,
        "_authenticate_platform",
        lambda connection, _config, session_token, *, touch: current_context(
            connection, session_token
        ),
    )
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "mfa",
    )
    monkeypatch.setattr(
        service,
        "authenticate_global_session_in_transaction",
        lambda *_args, **_kwargs: (
            SimpleNamespace(account_id=runtime.partner_account_id),
            datetime.now(UTC),
        ),
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-139 Channel Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime.config
    app.include_router(platform_router)
    app.include_router(channel_router)
    return app


def _headers(token: str, key: str | None = None, revision: int = 0) -> dict[str, str]:
    headers = {"Cookie": f"{PLATFORM_SESSION_COOKIE}={token}"}
    if key is not None:
        headers = {
            "Cookie": (
                f"{PLATFORM_SESSION_COOKIE}={token}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
            ),
            "Idempotency-Key": key,
            "If-Match": f'"{revision}"',
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-site",
            "X-CSRF-Token": PLATFORM_CSRF,
            "X-Step-Up-Grant": STEP_UP,
        }
    return headers


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _partner_body(runtime: Runtime, code: str = "north.channel") -> dict[str, object]:
    return {
        "operation": "create",
        "owner_workspace_id": runtime.partner_workspace_id,
        "owner_account_id": runtime.partner_account_id,
        "partner_code": code,
        "display_name": "North Channel",
        "tier_code": "silver",
        "region_scope": "cn",
        "contract_sha256": _sha(f"contract:{code}"),
        "confirmed": True,
    }


async def _create_partner(
    client: AsyncClient,
    runtime: Runtime,
    *,
    code: str = "north.channel",
    key: str = "prd139-partner-create-0001",
) -> dict[str, object]:
    response = await client.post(
        "/platform/channel-partners",
        headers=_headers(runtime.tokens["channel"], key),
        json=_partner_body(runtime, code),
    )
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == '"1"'
    return response.json()["partner"]


def _rule_id(runtime: Runtime, partner_id: str, currency: str = "CNY") -> str:
    with closing(connect_database(runtime.config.database_url)) as connection:
        return str(
            connection.execute(
                """SELECT channel_commission_rule_snapshot_id
                     FROM channel_commission_rule_snapshots
                    WHERE channel_partner_id=? AND currency=? AND effective_until IS NULL""",
                (partner_id, currency),
            ).fetchone()[0]
        )


async def _request_attribution(
    client: AsyncClient,
    runtime: Runtime,
    partner_id: str,
    *,
    expected_revision: int,
    key: str,
    starts_at: str = "2026-09-01T00:00:00Z",
) -> dict[str, object]:
    response = await client.post(
        "/platform/workspace-attributions",
        headers=_headers(runtime.tokens["channel"], key, expected_revision),
        json={
            "channel_partner_id": partner_id,
            "target_workspace_id": runtime.target_workspace_id,
            "channel_commission_rule_snapshot_id": _rule_id(runtime, partner_id),
            "reason_code": "channel.assignment",
            "expected_attribution_revision": expected_revision,
            "starts_at": starts_at,
            "confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["attribution"]


async def _approve(
    client: AsyncClient,
    runtime: Runtime,
    attribution: dict[str, object],
    actor: str,
    key: str,
) -> dict[str, object]:
    revision = int(attribution["revision"])
    response = await client.post(
        f"/platform/workspace-attributions/{attribution['workspace_attribution_id']}/approve",
        headers=_headers(runtime.tokens[actor], key, revision),
        json={
            "expected_revision": revision,
            "decision": "approved",
            "reason_code": "owner.approved",
            "confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["attribution"]


@pytest.mark.anyio
async def test_transport_unknown_fields_idempotency_cas_and_partner_revocation(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        anonymous = await client.get("/platform/channel-partners")
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "platform.session_required"
        conflict = await client.get(
            "/platform/channel-partners",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={runtime.tokens['channel']}; "
                    f"{WORKSPACE_SESSION_COOKIE}={'w' * 43}"
                )
            },
        )
        assert conflict.json()["code"] == "platform.credential_conflict"
        bad_query = await client.get(
            "/platform/channel-partners?tenant_id=hidden",
            headers=_headers(runtime.tokens["channel"]),
        )
        assert bad_query.status_code == 422
        assert bad_query.json()["code"] == "channel.request_invalid"
        invalid = await client.post(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["channel"], "prd139-invalid-body-0001"),
            json={**_partner_body(runtime), "unknown": True},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "channel.request_invalid"

        billing_navigation = await client.get(
            "/platform/navigation",
            headers=_headers(runtime.tokens["billing"]),
        )
        assert billing_navigation.status_code == 200
        assert "channels" in {
            item["id"] for item in billing_navigation.json()["items"]
        }
        billing_directory = await client.get(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["billing"]),
        )
        assert billing_directory.status_code == 403
        assert billing_directory.json()["code"] == "platform.permission_denied"
        billing_statements = await client.get(
            "/platform/channel-statements",
            headers=_headers(runtime.tokens["billing"]),
        )
        assert billing_statements.status_code == 200

        partner = await _create_partner(client, runtime)
        assert {
            (rule["currency"], rule["version"])
            for rule in partner["commission_rules"]
        } == {("CNY", 1), ("USD", 1)}
        assert partner["pending_attributions"] == []
        replay = await client.post(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["channel"], "prd139-partner-create-0001"),
            json=_partner_body(runtime),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        changed = await client.post(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["channel"], "prd139-partner-create-0001"),
            json={**_partner_body(runtime), "display_name": "Changed"},
        )
        assert changed.status_code == 409
        assert changed.json()["code"] == "channel.idempotency_conflict"

        referral = "ReferralCodePRD139_000001"
        rotated = await client.post(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["channel"], "prd139-referral-rotate-0001", 1),
            json={
                "operation": "rotate_referral_code",
                "channel_partner_id": partner["channel_partner_id"],
                "expected_revision": 1,
                "referral_code": referral,
                "usage_limit": 10,
                "confirmed": True,
            },
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["issued_referral_code"] == referral
        with closing(connect_database(runtime.config.database_url)) as connection:
            row = connection.execute("SELECT * FROM channel_referral_codes").fetchone()
            assert row is not None
            assert row["lookup_sha256"] == _sha(referral)
            assert referral not in json.dumps(dict(row))

        lead = await client.post(
            "/platform/channel-leads",
            headers=_headers(runtime.tokens["channel"], "prd139-lead-create-0001", 2),
            json={
                "channel_partner_id": partner["channel_partner_id"],
                "expected_revision": 2,
                "lead_code": "lead.north.001",
                "referral_code": referral,
                "region_code": "cn-north-1",
                "contact_email": "lead@example.invalid",
                "source_sha256": _sha("lead-source"),
                "candidate_workspace_id": runtime.target_workspace_id,
                "confirmed": True,
            },
        )
        assert lead.status_code == 201, lead.text
        attribution = await _request_attribution(
            client,
            runtime,
            str(partner["channel_partner_id"]),
            expected_revision=0,
            key="prd139-attribution-create-0001",
        )
        assert attribution["required_approvals"] == 1
        attribution = await _approve(
            client,
            runtime,
            attribution,
            "owner1",
            "prd139-attribution-approve-0001",
        )
        assert attribution["status"] == "active"

        partner_view = await client.get(
            "/partner/workspaces",
            headers={"Cookie": f"{GLOBAL_SESSION_COOKIE}={PARTNER_TOKEN}"},
        )
        assert partner_view.status_code == 200, partner_view.text
        assert partner_view.json()["partner"]["commission_rules"] == []
        assert partner_view.json()["partner"]["pending_attributions"] == []
        assert [item["workspace_id"] for item in partner_view.json()["items"]] == [
            runtime.target_workspace_id
        ]
        assert partner_view.json()["items"][0]["statement_totals"] is None
        with closing(connect_database(runtime.config.database_url)) as connection:
            membership = connection.execute(
                """SELECT channel_membership_id FROM channel_memberships
                    WHERE account_id=? AND status='active'""",
                (runtime.partner_account_id,),
            ).fetchone()
            assert membership is not None
            connection.execute(
                """UPDATE channel_memberships SET status='revoked',revision=revision+1,
                     revoked_by_operator_id=?,revoked_at=?,updated_at=?
                   WHERE channel_membership_id=?""",
                (
                    runtime.operator_ids["owner1"],
                    STAMP,
                    STAMP,
                    membership["channel_membership_id"],
                ),
            )
            connection.commit()
        revoked = await client.get(
            "/partner/workspaces",
            headers={"Cookie": f"{GLOBAL_SESSION_COOKIE}={PARTNER_TOKEN}"},
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "partner.membership_required"


@pytest.mark.anyio
async def test_replacement_requires_two_independent_current_owners(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        first = await _create_partner(client, runtime)
        initial = await _request_attribution(
            client,
            runtime,
            str(first["channel_partner_id"]),
            expected_revision=0,
            key="prd139-initial-request-0001",
            starts_at="2026-08-31T00:00:00Z",
        )
        directory = await client.get(
            "/platform/channel-partners",
            headers=_headers(runtime.tokens["channel"]),
        )
        projected = next(
            item
            for item in directory.json()["items"]
            if item["channel_partner_id"] == first["channel_partner_id"]
        )
        assert projected["pending_attributions"][0]["workspace_attribution_id"] == (
            initial["workspace_attribution_id"]
        )
        initial = await _approve(
            client,
            runtime,
            initial,
            "owner1",
            "prd139-initial-approve-0001",
        )
        second = await _create_partner(
            client,
            runtime,
            code="south.channel",
            key="prd139-partner-create-0002",
        )
        replacement = await _request_attribution(
            client,
            runtime,
            str(second["channel_partner_id"]),
            expected_revision=int(initial["revision"]),
            key="prd139-replacement-request-0001",
        )
        assert replacement["required_approvals"] == 2
        replacement = await _approve(
            client,
            runtime,
            replacement,
            "owner1",
            "prd139-replacement-approve-0001",
        )
        assert replacement["status"] == "pending_approval"
        duplicate = await client.post(
            f"/platform/workspace-attributions/{replacement['workspace_attribution_id']}/approve",
            headers=_headers(
                runtime.tokens["owner1"],
                "prd139-replacement-duplicate-0001",
                int(replacement["revision"]),
            ),
            json={
                "expected_revision": int(replacement["revision"]),
                "decision": "approved",
                "reason_code": "owner.approved",
                "confirmed": True,
            },
        )
        assert duplicate.json()["code"] == "channel.approval_independence_required"
        replacement = await _approve(
            client,
            runtime,
            replacement,
            "owner2",
            "prd139-replacement-approve-0002",
        )
        assert replacement["status"] == "active"
        with closing(connect_database(runtime.config.database_url)) as connection:
            states = connection.execute(
                """SELECT channel_partner_id,status,starts_at,ends_at
                    FROM workspace_attributions
                    WHERE target_workspace_id=? ORDER BY created_at""",
                (runtime.target_workspace_id,),
            ).fetchall()
            assert [row["status"] for row in states] == ["superseded", "active"]
            assert states[0]["ends_at"] == states[1]["starts_at"]


@pytest.mark.anyio
async def test_final_approval_revalidates_requester_permission(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        partner = await _create_partner(client, runtime)
        attribution = await _request_attribution(
            client,
            runtime,
            str(partner["channel_partner_id"]),
            expected_revision=0,
            key="prd139-revoked-request-0001",
        )
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """UPDATE platform_operator_role_bindings
                      SET status='revoked',revoked_at=?
                    WHERE operator_id=? AND role_code='platform_channel'""",
                (STAMP, runtime.operator_ids["channel"]),
            )
            connection.execute(
                """UPDATE platform_operators SET revision=revision+1,updated_at=?
                    WHERE operator_id=?""",
                (STAMP, runtime.operator_ids["channel"]),
            )
            connection.commit()
        approval = await client.post(
            f"/platform/workspace-attributions/{attribution['workspace_attribution_id']}/approve",
            headers=_headers(
                runtime.tokens["owner1"],
                "prd139-revoked-approve-0001",
                int(attribution["revision"]),
            ),
            json={
                "expected_revision": int(attribution["revision"]),
                "decision": "approved",
                "reason_code": "owner.approved",
                "confirmed": True,
            },
        )
        assert approval.json()["code"] == "channel.approval_authority_changed"


@pytest.mark.anyio
async def test_statement_recompute_platform_read_and_scheduler_fence(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
) -> None:
    app = _app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        partner = await _create_partner(client, runtime)
        partner_id = str(partner["channel_partner_id"])
        rule_id = _rule_id(runtime, partner_id)
        statement_id = "cst_" + "1" * 32
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """INSERT INTO channel_statements(
                  channel_statement_id,channel_partner_id,owner_workspace_id,tenant_id,
                  period_start,period_end,currency,total_amount_micros,
                  total_commission_micros,line_count,reconciliation_sha256,status,
                  close_revision,closed_at,created_at,updated_at)
                VALUES(?,?,?,?,? ,?,'CNY',0,0,0,?,'open',1,NULL,?,?)""",
                (
                    statement_id,
                    partner_id,
                    runtime.partner_workspace_id,
                    runtime.partner_tenant_id,
                    "2020-01-01T00:00:00.000Z",
                    "2020-02-01T00:00:00.000Z",
                    "0" * 64,
                    STAMP,
                    STAMP,
                ),
            )
            connection.execute(
                """INSERT INTO channel_statement_lines(
                  channel_statement_line_id,channel_statement_id,channel_partner_id,
                  owner_workspace_id,tenant_id,target_workspace_id,target_tenant_id,
                  channel_commission_rule_snapshot_id,source_kind,source_reference_id,
                  amount_micros,commission_amount_micros,token_count,estimated,
                  source_created_at,created_at)
                VALUES(?,?,?,?,?,NULL,NULL,?,'ledger','entry-prd139',1234,123,10,0,?,?)""",
                (
                    "csl_" + "1" * 32,
                    statement_id,
                    partner_id,
                    runtime.partner_workspace_id,
                    runtime.partner_tenant_id,
                    rule_id,
                    STAMP,
                    STAMP,
                ),
            )
            connection.commit()
        assert scheduler.run_channel_scheduler_once(
            runtime.config,
            worker_id="prd139-scheduler-a",
        )
        with closing(connect_database(runtime.config.database_url)) as connection:
            statement = connection.execute(
                "SELECT * FROM channel_statements WHERE channel_statement_id=?",
                (statement_id,),
            ).fetchone()
            assert statement is not None
            assert (
                statement["status"],
                statement["total_amount_micros"],
                statement["total_commission_micros"],
                statement["line_count"],
            ) == ("closed", 1234, 123, 1)
            assert connection.execute(
                """SELECT COUNT(*) FROM channel_event_outbox
                    WHERE status='delivered' AND attempt_count=1"""
            ).fetchone()[0] == 2
        listed = await client.get(
            "/platform/channel-statements?currency=CNY",
            headers=_headers(runtime.tokens["billing"]),
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["total_commission_micros"] == 123

        old_lease = scheduler.acquire_channel_lease(
            runtime.config,
            worker_id="prd139-scheduler-old",
        )
        assert old_lease is not None
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                """UPDATE channel_scheduler_state
                      SET lease_expires_at='1970-01-01T00:00:00.000Z',
                          revision=revision+1,updated_at=updated_at
                    WHERE singleton_id=1"""
            )
            connection.commit()
        takeover = scheduler.acquire_channel_lease(
            runtime.config,
            worker_id="prd139-scheduler-new",
        )
        assert takeover is not None and takeover.generation > old_lease.generation
        with pytest.raises(ApiError) as fenced:
            scheduler._process_fenced_batch(runtime.config, old_lease, lambda: False)
        assert fenced.value.code == "channel.scheduler_fenced"
