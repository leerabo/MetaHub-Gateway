"""验证 PRD-102 Workspace 与 Platform 企业认证边界、加密、CAS 和审核原子性。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.business_verification import routes as verification_routes
from metahub_control.business_verification import service as verification_service
from metahub_control.business_verification.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.business_verification import evidence_scan_attestation
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

# 本地测试固定 Origin 与独立业务加密密钥，任何值都不得复用真实部署凭据。
ORIGIN = "http://localhost:5173"
BUSINESS_SECRET = "test-prd102-business-verification-secret-independent-2026"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """构造组织/个人 Workspace、reviewer Session 外键和双域动态授权夹具。"""

    database_url = f"sqlite:///{(tmp_path / 'prd102.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": "LocalAdmin!2026",
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd102-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd102-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd102-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": (
            "test-prd102-pii-secret-independent-long-2026"
        ),
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd102-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd102-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd102-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd102-mfa-secret-2026-long",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "test-prd102-recovery-secret-2026",
        "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET": BUSINESS_SECRET,
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT membership.*,account.email_normalized,account.display_name,
              tenant.slug tenant_slug,tenant.name tenant_name
            FROM workspace_memberships membership
            JOIN global_accounts account ON account.account_id=membership.account_id
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            WHERE membership.role_code='owner' AND membership.status='active'
            LIMIT 1
            """
        ).fetchone()
        assert owner is not None
        account_id = str(owner["account_id"])
        operator_id = "pto_" + "1" * 32
        factor_id = "mfa_" + "1" * 32
        global_session_id = "gss_" + "1" * 32
        platform_session_id = "pts_" + "1" * 32
        connection.execute(
            """
            INSERT INTO account_mfa_factors(
              factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
              revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
            VALUES(?,?,'totp',?,1,'active',1,0,datetime('now'),NULL,
              datetime('now'),datetime('now'))
            """,
            (factor_id, account_id, "x" * 80),
        )
        connection.execute(
            """
            INSERT INTO global_sessions(
              session_id,account_id,token_hmac,csrf_hmac,status,credential_revision,
              session_revision,device_family,client_risk_hmac,expires_at,idle_expires_at,
              last_seen_at,revoked_at,revoked_reason,created_at,updated_at)
            VALUES(?,?,?,?,'active',1,1,'prd102-test',NULL,
              datetime('now','+1 day'),datetime('now','+1 day'),datetime('now'),
              NULL,NULL,datetime('now'),datetime('now'))
            """,
            (global_session_id, account_id, "a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_verification','active',NULL,datetime('now'),NULL)
            """,
            (operator_id,),
        )
        connection.execute(
            """
            INSERT INTO platform_sessions(
              platform_session_id,operator_id,account_id,global_session_id,factor_id,
              token_hmac,csrf_hmac,status,operator_revision,factor_revision,role_fingerprint,
              last_seen_at,absolute_expires_at,idle_expires_at,revoked_at,revoked_reason,
              created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'active',1,1,?,datetime('now'),datetime('now','+1 day'),
              datetime('now','+1 day'),NULL,NULL,datetime('now'),datetime('now'))
            """,
            (
                platform_session_id,
                operator_id,
                account_id,
                global_session_id,
                factor_id,
                "c" * 64,
                "d" * 64,
                "e" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    expires_at = datetime.now(UTC) + timedelta(days=1)
    workspace_context = WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id=global_session_id,
        account_id=account_id,
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        membership_id=str(owner["workspace_membership_id"]),
        legacy_user_id=str(owner["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(owner["email_normalized"]),
        display_name=str(owner["display_name"]),
        tenant_slug=str(owner["tenant_slug"]),
        tenant_name=str(owner["tenant_name"]),
        permission_codes=(),
        permission_revision=1,
        authorization_fingerprint="f" * 64,
        expires_at=expires_at,
        token="w" * 43,
    )
    platform_context = PlatformContext(
        platform_session_id=platform_session_id,
        operator_id=operator_id,
        account_id=account_id,
        global_session_id=global_session_id,
        factor_id=factor_id,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_verification",),
        permissions=("platform.verification.read", "platform.verification.review"),
        expires_at=expires_at,
    )
    workspace_state: dict[str, object] = {
        "context": workspace_context,
        "role": "owner",
    }

    def workspace_route_auth(*_args, **_kwargs):
        """让路由层读取当前可变 Workspace 上下文，专测 owner 绑定变化。"""

        return workspace_state["context"]

    def workspace_service_auth(*_args, **_kwargs):
        """让服务层动态读取角色，验证同一会话撤权后立即失败关闭。"""

        context = workspace_state["context"]
        assert isinstance(context, WorkspaceSessionContext)
        return replace(context, membership_role=str(workspace_state["role"]))

    def platform_service_auth(connection, *_args, **_kwargs):
        """从数据库实时恢复 reviewer 角色绑定，禁止测试桩缓存旧权限。"""

        active = connection.execute(
            """
            SELECT 1 FROM platform_operator_role_bindings
            WHERE operator_id=? AND role_code='platform_verification' AND status='active'
            """,
            (operator_id,),
        ).fetchone()
        permissions = platform_context.permissions if active is not None else ()
        # 返回权限集合来自当前数据库绑定，确保撤权测试不会沿用旧 Platform 上下文。
        return replace(platform_context, permissions=permissions)

    monkeypatch.setattr(
        verification_routes,
        "authenticate_workspace_session_context",
        workspace_route_auth,
    )
    monkeypatch.setattr(
        verification_service,
        "authenticate_workspace_session_in_transaction",
        workspace_service_auth,
    )
    monkeypatch.setattr(
        verification_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform_context,
    )
    monkeypatch.setattr(
        verification_service,
        "authenticate_platform_session_in_transaction",
        platform_service_auth,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-102 Business Verification Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(router)
    return database_url, app, workspace_state, workspace_context, operator_id


def _workspace_headers(key: str) -> dict[str, str]:
    """构造 Workspace 写请求的 Origin、CSRF 和幂等头，不携带其他凭据域。"""

    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": "w" * 43,
        "Idempotency-Key": key,
    }


def _platform_headers(key: str, revision: int) -> dict[str, str]:
    """构造 Platform 审核写请求头，并显式携带强 CAS revision。"""

    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": "p" * 43,
        "Idempotency-Key": key,
        "If-Match": f'"{revision}"',
    }


def _submission(workspace_id: str, *, blocked: bool = False) -> dict[str, object]:
    """构造与 Workspace owner 签名绑定的证据提交，可切换恶意扫描阻断分支。"""

    evidence = {
        "evidence_type": "business_license",
        "object_reference": (
            "obj_prd102_blocked_evidence" if blocked else "obj_prd102_sensitive_evidence"
        ),
        "content_sha256": ("b" if blocked else "a") * 64,
        "mime_type": "application/pdf",
        "size_bytes": 4096,
        "kms_key_version": 7,
        "malware_status": "blocked" if blocked else "passed",
        "dlp_status": "passed",
    }
    return {
        "country_code": "CN",
        "entity_type": "company",
        "legal_name": "PRD102 Sensitive Legal Canary",
        "registration_number": "REG-PRD102-99887766",
        "evidence": [
            {
                **evidence,
                "scan_attestation": evidence_scan_attestation(
                    secret=BUSINESS_SECRET,
                    workspace_id=workspace_id,
                    **evidence,
                ),
            }
        ],
        "reason_code": "resubmission" if blocked else "initial_submission",
        "confirmed": True,
    }


def _assert_canaries_absent(database_url: str, canaries: tuple[str, ...]) -> None:
    """扫描敏感域及平台审计全部文本列，证明明文未落库。"""

    tables = (
        "legal_entity_profiles",
        "business_verification_intents",
        "verification_evidence",
        "workspace_billing_profiles",
        "verification_reviews",
        "platform_audit_logs",
    )
    connection = connect_database(database_url)
    try:
        for table in tables:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            for column in columns:
                name = str(column["name"])
                for canary in canaries:
                    count = connection.execute(
                        f'SELECT COUNT(*) FROM "{table}" '
                        f'WHERE instr(CAST("{name}" AS TEXT),?)>0',
                        (canary,),
                    ).fetchone()[0]
                    assert count == 0, (table, name)
    finally:
        connection.close()


@pytest.mark.anyio
async def test_workspace_submit_billing_platform_review_and_sensitive_zero_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """贯穿双会话域，覆盖重放、CAS、动态撤权、下载授权、审核和敏感零泄漏。"""

    (
        database_url,
        app,
        workspace_state,
        workspace_context,
        operator_id,
    ) = _prepare(monkeypatch, tmp_path)
    workspace_id = workspace_context.workspace_id
    payload = _submission(workspace_id)
    billing_payload = {
        "invoice_title": "PRD102 Invoice Canary",
        "tax_number": "TAX-PRD102-55667788",
        "address": "PRD102 Address Canary 101",
        "phone": "+86-010-55667788",
        "bank_name": "PRD102 Bank Canary",
        "bank_account": "6222020202021234567",
        "confirmed": True,
    }
    canaries = (
        str(payload["legal_name"]),
        str(payload["registration_number"]),
        str(payload["evidence"][0]["object_reference"]),
        str(billing_payload["invoice_title"]),
        str(billing_payload["tax_number"]),
        str(billing_payload["address"]),
        str(billing_payload["phone"]),
        str(billing_payload["bank_name"]),
        str(billing_payload["bank_account"]),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(WORKSPACE_SESSION_COOKIE, "w" * 43)
        client.cookies.set(WORKSPACE_CSRF_COOKIE, "w" * 43)
        forged = _submission(workspace_id)
        forged["evidence"][0]["scan_attestation"] = "0" * 64
        invalid_scan = await client.post(
            f"/workspaces/{workspace_id}/business-verifications",
            json=forged,
            headers=_workspace_headers("prd102-invalid-scan-0001"),
        )
        assert invalid_scan.status_code == 422
        assert invalid_scan.json()["code"] == "business_verification.evidence_invalid"

        created = await client.post(
            f"/workspaces/{workspace_id}/business-verifications",
            json=payload,
            headers=_workspace_headers("prd102-verification-submit-0001"),
        )
        assert created.status_code == 201, created.text
        assert created.headers["etag"] == '"1"'
        assert created.json()["verification"]["status"] == "submitted"
        assert all(canary not in created.text for canary in canaries)
        intent_id = created.json()["verification"]["business_verification_intent_id"]

        replay = await client.post(
            f"/workspaces/{workspace_id}/business-verifications",
            json=payload,
            headers=_workspace_headers("prd102-verification-submit-0001"),
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True

        billing = await client.patch(
            f"/workspaces/{workspace_id}/billing-profile",
            json=billing_payload,
            headers={
                **_workspace_headers("prd102-billing-update-0001"),
                "If-Match": '"0"',
            },
        )
        assert billing.status_code == 200, billing.text
        assert billing.headers["etag"] == '"1"'
        assert billing.json()["billing_profile"]["tax_number_masked"] == "****7788"
        assert billing.json()["billing_profile"]["bank_account_masked"] == "****4567"
        assert all(canary not in billing.text for canary in canaries)
        billing_replay = await client.patch(
            f"/workspaces/{workspace_id}/billing-profile",
            json=billing_payload,
            headers={
                **_workspace_headers("prd102-billing-update-0001"),
                "If-Match": '"0"',
            },
        )
        assert billing_replay.status_code == 200
        assert billing_replay.json()["idempotent_replay"] is True
        stale_billing = await client.patch(
            f"/workspaces/{workspace_id}/billing-profile",
            json=billing_payload,
            headers={
                **_workspace_headers("prd102-billing-update-0002"),
                "If-Match": '"0"',
            },
        )
        assert stale_billing.status_code == 412

        workspace_state["role"] = "developer"
        revoked = await client.get(f"/workspaces/{workspace_id}/business-verification")
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "workspace_session.permission_denied"
        workspace_state["role"] = "owner"

    connection = connect_database(database_url)
    try:
        evidence_id = str(
            connection.execute(
                """
                SELECT verification_evidence_id FROM verification_evidence
                WHERE business_verification_intent_id=?
                """,
                (intent_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()

    async with AsyncClient(transport=transport, base_url="https://testserver") as reviewer:
        reviewer.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        reviewer.cookies.set(PLATFORM_CSRF_COOKIE, "p" * 43)
        queue = await reviewer.get("/platform/business-verifications?status=submitted")
        assert queue.status_code == 200, queue.text
        assert queue.json()["total"] == 1
        assert queue.json()["cases"][0]["business_verification_intent_id"] == intent_id
        assert "workspace_id" not in queue.text and "tenant_id" not in queue.text
        assert all(canary not in queue.text for canary in canaries)

        token = await reviewer.post(
            f"/platform/business-verifications/{intent_id}/evidence/{evidence_id}/download-token",
            json={},
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": "p" * 43,
            },
        )
        assert token.status_code == 200, token.text
        assert token.headers["etag"] == '"2"'
        raw_download_token = token.json()["download_token"]

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE platform_operator_role_bindings
                SET status='revoked',revoked_at=datetime('now')
                WHERE operator_id=? AND role_code='platform_verification'
                """,
                (operator_id,),
            )
            connection.commit()
        finally:
            connection.close()
        dynamically_revoked = await reviewer.post(
            f"/platform/business-verifications/{intent_id}/review",
            json={"decision": "approve", "reason_code": "verified", "confirmed": True},
            headers=_platform_headers("prd102-review-approve-0001", 2),
        )
        assert dynamically_revoked.status_code == 403

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE platform_operator_role_bindings
                SET status='active',revoked_at=NULL
                WHERE operator_id=? AND role_code='platform_verification'
                """,
                (operator_id,),
            )
            connection.commit()
        finally:
            connection.close()
        approved = await reviewer.post(
            f"/platform/business-verifications/{intent_id}/review",
            json={"decision": "approve", "reason_code": "verified", "confirmed": True},
            headers=_platform_headers("prd102-review-approve-0001", 2),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["case"]["status"] == "approved"
        assert approved.json()["contract"]["signing_status"] == "verified"
        assert all(canary not in approved.text for canary in canaries)
        review_replay = await reviewer.post(
            f"/platform/business-verifications/{intent_id}/review",
            json={"decision": "approve", "reason_code": "verified", "confirmed": True},
            headers=_platform_headers("prd102-review-approve-0001", 2),
        )
        assert review_replay.status_code == 200
        assert review_replay.json()["idempotent_replay"] is True
        concurrent = await reviewer.post(
            f"/platform/business-verifications/{intent_id}/review",
            json={"decision": "approve", "reason_code": "verified", "confirmed": True},
            headers=_platform_headers("prd102-review-approve-0002", 2),
        )
        assert concurrent.status_code == 409
        assert concurrent.json()["code"] == "business_verification.review_conflict"

    connection = connect_database(database_url)
    try:
        token_row = connection.execute(
            "SELECT token_hmac,status FROM verification_evidence_download_tokens"
        ).fetchone()
        assert token_row is not None
        assert token_row["token_hmac"] != raw_download_token
        assert token_row["status"] == "active"
    finally:
        connection.close()

    blocked_payload = _submission(workspace_id, blocked=True)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(WORKSPACE_SESSION_COOKIE, "w" * 43)
        client.cookies.set(WORKSPACE_CSRF_COOKIE, "w" * 43)
        blocked = await client.post(
            f"/workspaces/{workspace_id}/business-verifications",
            json=blocked_payload,
            headers=_workspace_headers("prd102-verification-submit-0002"),
        )
        assert blocked.status_code == 201, blocked.text
        blocked_id = blocked.json()["verification"]["business_verification_intent_id"]
        assert blocked.json()["billing_profile"]["verification_status"] == "stale"

    async with AsyncClient(transport=transport, base_url="https://testserver") as reviewer:
        reviewer.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        reviewer.cookies.set(PLATFORM_CSRF_COOKIE, "p" * 43)
        failed_approval = await reviewer.post(
            f"/platform/business-verifications/{blocked_id}/review",
            json={"decision": "approve", "reason_code": "verified", "confirmed": True},
            headers=_platform_headers("prd102-review-blocked-0001", 1),
        )
        assert failed_approval.status_code == 409
        assert failed_approval.json()["code"] == "business_verification.evidence_not_ready"
        rejected = await reviewer.post(
            f"/platform/business-verifications/{blocked_id}/review",
            json={
                "decision": "reject",
                "reason_code": "scan_failed",
                "confirmed": True,
            },
            headers=_platform_headers("prd102-review-blocked-0002", 1),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["case"]["status"] == "rejected"

    _assert_canaries_absent(database_url, canaries)
    assert all(canary not in caplog.text for canary in canaries)
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
