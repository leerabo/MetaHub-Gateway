"""PRD-060 用户邀请与加入流程后端专项测试。

测试只使用 pytest 隔离 SQLite，覆盖主流程、后端权限、公开错误同形、并发一次性、
事务回滚和敏感信息不落库；不调用真实邮件或外部 Webhook 服务。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.auth.routes import router as auth_router
from metahub_control.invitations import service as invitation_service
from metahub_control.invitations.routes import router as invitations_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import ROLE_CODES, ROLE_GRANTABLE_PERMISSIONS
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app


# 管理员密码只存在隔离测试进程，用于验证后端权限，不写入响应、数据库明文或测试记录。
ADMIN_PASSWORD = "Prd060Admin!2026"
# 受邀密码只用于 PBKDF2 主流程和短密码负例，任何断言都不得输出其值。
INVITED_PASSWORD = "Prd060Invited!2026"
# 固定 UTC 时间使邀请、审计、事件和过期比较可重复，不依赖运行机器时钟。
BASE_TIME = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def _sqlite_url(path: Path) -> str:
    """把沙箱内 pytest 数据库路径转换为项目 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """迁移并 seed 隔离数据库，返回仅属于当前用例的连接 URL。"""

    database_url = _sqlite_url(tmp_path / "prd060-invitations.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd060-auth-test-secret")
    apply_migrations(database_url)
    apply_seed_data(database_url)
    return database_url


def _build_app():
    """构造只包含认证和 Invitation 路由的隔离 Control API。"""

    app = create_service_app("control-plane-api", "PRD-060 Invitation Test API")
    app.include_router(auth_router)
    app.include_router(invitations_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    email: str = "admin@metahub.local",
    password: str = ADMIN_PASSWORD,
) -> Response:
    """使用固定本地账号登录，返回完整响应供调用方检查权限快照。"""

    return await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
        headers={"X-Request-ID": "req_prd060_login"},
    )


async def _create_invitation(
    client: AsyncClient,
    bearer: str,
    *,
    email: str,
    role: str = "developer",
    request_id: str = "req_prd060_create",
) -> Response:
    """以管理员权限创建固定 72 小时邀请，正文不携带 tenant 或 actor。"""

    return await client.post(
        "/invitations",
        json={"email": email, "role": role, "expires_in_hours": 72},
        headers={
            "Authorization": f"Bearer {bearer}",
            "X-Request-ID": request_id,
            "X-Trace-ID": f"trc_{request_id}",
        },
    )


def _public_error_signature(response: Response) -> tuple[int, dict[str, object], str, str]:
    """移除每请求随机链路 ID，比较公开失败的稳定状态、正文和缓存合同。"""

    payload = response.json()
    payload.pop("request_id", None)
    payload.pop("trace_id", None)
    return (
        response.status_code,
        payload,
        response.headers["cache-control"],
        response.headers["referrer-policy"],
    )


@pytest.mark.anyio
async def test_admin_invites_anonymous_accepts_and_new_user_gets_database_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0119：创建、匿名接受、指定角色登录、强审计和 source event 全链路成功。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_login = await _login(client)
        assert admin_login.status_code == 200, admin_login.text
        admin_token = admin_login.json()["access_token"]
        created = await _create_invitation(
            client,
            admin_token,
            email="Developer.PRD060@Example.com",
        )
        assert created.status_code == 201, created.text
        raw_token = created.json()["accept_token"]
        token_fragment = raw_token[12:28]

        accepted = await client.post(
            f"/invitations/{raw_token}/accept",
            json={"display_name": "PRD060 Developer", "password": INVITED_PASSWORD},
            headers={
                "X-Request-ID": raw_token,
                "X-Trace-ID": f"trace-{token_fragment}",
                "User-Agent": f"MetaHub-Test/{raw_token}",
            },
        )
        invited_login = await _login(
            client,
            email="developer.prd060@example.com",
            password=INVITED_PASSWORD,
        )
        invited_me = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {invited_login.json()['access_token']}"},
        )

    assert created.json()["invitation"]["email"] == "developer.prd060@example.com"
    assert created.json()["invitation"]["role"] == "developer"
    assert len(raw_token) == 43
    assert accepted.status_code == 201, accepted.text
    assert accepted.headers["cache-control"] == "no-store, private"
    assert accepted.headers["referrer-policy"] == "no-referrer"
    assert accepted.headers["x-request-id"].startswith("req_")
    assert accepted.headers["x-trace-id"].startswith("trc_")
    assert raw_token not in accepted.text
    assert token_fragment not in accepted.text
    assert accepted.json()["user"]["role"] == "developer"
    assert "tenant_id" not in accepted.json()["user"]
    assert invited_login.status_code == 200, invited_login.text
    assert invited_me.status_code == 200, invited_me.text
    assert invited_me.json()["user"]["role"] == "developer"
    assert set(invited_me.json()["permission_codes"]) == set(
        ROLE_GRANTABLE_PERMISSIONS["developer"]
    )

    connection = connect_database(database_url)
    invitation = connection.execute(
        "SELECT * FROM invitations WHERE email=?",
        ("developer.prd060@example.com",),
    ).fetchone()
    assert invitation is not None
    assert invitation["status"] == "accepted"
    assert invitation["token_hash"] != raw_token
    audits = connection.execute(
        """
        SELECT action,actor_user_id,actor_email,user_agent,request_id,trace_id,before_summary_json,
               after_summary_json
        FROM audit_logs WHERE object_type='invitation' ORDER BY created_at,action
        """
    ).fetchall()
    assert {row["action"] for row in audits} == {"user.invited", "invitation.accepted"}
    accepted_audit = next(row for row in audits if row["action"] == "invitation.accepted")
    assert accepted_audit["actor_user_id"] == invitation["accepted_user_id"]
    # 匿名接受者仍由 user_id 和 Invitation 指纹追踪；个人邮箱不得复制进可查询、可导出的审计列。
    assert accepted_audit["actor_email"] == invitation_service.INVITATION_ACCEPTED_AUDIT_EMAIL
    assert accepted_audit["actor_email"] != invitation["email"]
    assert accepted_audit["user_agent"] is None
    assert raw_token not in accepted_audit["request_id"]
    assert token_fragment not in accepted_audit["trace_id"]
    events = connection.execute(
        """
        SELECT event_type,actor_user_id,request_id,trace_id,payload_json,delivery_status
        FROM invitation_events ORDER BY event_type
        """
    ).fetchall()
    assert {row["event_type"] for row in events} == {
        "user.invited",
        "invitation.accepted",
    }
    assert {row["delivery_status"] for row in events} == {"dispatched"}
    assert all("developer.prd060@example.com" not in row["payload_json"] for row in events)
    database_dump = "\n".join(connection.iterdump())
    connection.close()
    assert raw_token not in database_dump
    assert INVITED_PASSWORD not in database_dump


@pytest.mark.anyio
async def test_create_invitation_requires_backend_user_manage_before_body_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限流程：匿名 401、五种非管理角色 403，畸形正文也不能绕过后端守卫。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    tenant_id = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()[0]
    now = "2026-07-31T12:00:00Z"
    for role in ROLE_CODES:
        # admin 已由 seed 创建；其余五角色逐一建号才能证明前端隐藏按钮不是权限边界。
        if role == "admin":
            continue
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                f"user_prd060_{role}",
                tenant_id,
                f"{role.replace('_', '-')}@prd060.example.com",
                role,
                hash_password(ADMIN_PASSWORD),
                role,
                now,
                now,
            ),
        )
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        anonymous = await client.post(
            "/invitations",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )
        denied: list[Response] = []
        for role in ROLE_CODES:
            # 管理员是唯一成功基线，本循环只收集其他固定角色的真实后端 403。
            if role == "admin":
                continue
            login = await _login(
                client,
                email=f"{role.replace('_', '-')}@prd060.example.com",
            )
            assert login.status_code == 200, login.text
            denied.append(
                await client.post(
                    "/invitations",
                    content=b"{",
                    headers={
                        "Authorization": f"Bearer {login.json()['access_token']}",
                        "Content-Type": "application/json",
                    },
                )
            )

    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "auth.token_missing"
    assert {response.status_code for response in denied} == {403}
    assert {response.json()["code"] for response in denied} == {"auth.permission_denied"}
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM invitations").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_public_unavailable_states_are_same_shape_and_token_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0120：随机、过期、重放、邮箱竞争和邀请人失权全部返回同形 410。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_login = await _login(client)
        admin_token = admin_login.json()["access_token"]

        replay_created = await _create_invitation(
            client,
            admin_token,
            email="replay-prd060@example.com",
            request_id="req_prd060_replay_create",
        )
        replay_token = replay_created.json()["accept_token"]
        first_accept = await client.post(
            f"/invitations/{replay_token}/accept",
            json={"display_name": "Replay User", "password": INVITED_PASSWORD},
        )
        replay = await client.post(
            f"/invitations/{replay_token}/accept",
            json={"display_name": "Replay User", "password": INVITED_PASSWORD},
        )

        expired_created = await _create_invitation(
            client,
            admin_token,
            email="expired-prd060@example.com",
            request_id="req_prd060_expired_create",
        )
        expired_token = expired_created.json()["accept_token"]
        monkeypatch.setattr(
            invitation_service,
            "_utcnow",
            lambda: BASE_TIME + timedelta(hours=80),
        )
        expired = await client.post(
            f"/invitations/{expired_token}/accept",
            json={"display_name": "Expired User", "password": INVITED_PASSWORD},
        )
        monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)

        conflict_created = await _create_invitation(
            client,
            admin_token,
            email="conflict-prd060@example.com",
            request_id="req_prd060_conflict_create",
        )
        conflict_token = conflict_created.json()["accept_token"]
        connection = connect_database(database_url)
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                "user_prd060_conflict",
                tenant_id,
                "conflict-prd060@example.com",
                "Conflict User",
                hash_password(INVITED_PASSWORD),
                "developer",
                "2026-07-31T12:00:00Z",
                "2026-07-31T12:00:00Z",
            ),
        )
        connection.commit()
        connection.close()
        conflict = await client.post(
            f"/invitations/{conflict_token}/accept",
            json={"display_name": "Conflict User", "password": INVITED_PASSWORD},
        )

        revoked_created = await _create_invitation(
            client,
            admin_token,
            email="revoked-prd060@example.com",
            request_id="req_prd060_revoked_create",
        )
        revoked_token = revoked_created.json()["accept_token"]
        connection = connect_database(database_url)
        connection.execute(
            "UPDATE users SET status='disabled',updated_at=? WHERE email='admin@metahub.local'",
            ("2026-07-31T12:01:00Z",),
        )
        connection.commit()
        connection.close()
        revoked = await client.post(
            f"/invitations/{revoked_token}/accept",
            json={"display_name": "Revoked User", "password": INVITED_PASSWORD},
        )
        random = await client.post(
            f"/invitations/{'R' * 43}/accept",
            json={"display_name": "Random User", "password": INVITED_PASSWORD},
        )

    assert first_accept.status_code == 201
    signatures = {
        json.dumps(_public_error_signature(response), ensure_ascii=True, sort_keys=True)
        for response in (replay, expired, conflict, revoked, random)
    }
    assert len(signatures) == 1
    assert replay.json()["code"] == "invitation.not_available"


@pytest.mark.anyio
async def test_create_and_accept_reject_ambiguous_or_injected_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：严格 JSON 拒绝额外租户字段、重复 key、宽松数字和接受身份注入。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_token = (await _login(client)).json()["access_token"]
        create_headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        invalid_create_bodies = (
            b'{"email":"wire-prd060@example.com","role":"developer","tenant_id":"other"}',
            b'{"email":"wire-prd060@example.com","email":"other@example.com","role":"developer"}',
            b'{"email":"not-an-email","role":"developer"}',
            b'{"email":"wire-prd060@example.com","role":"developer","expires_in_hours":true}',
            b'{"email":"wire-prd060@example.com","role":"developer","expires_in_hours":"72"}',
            b'{"email":"wire-prd060@example.com","role":"developer","expires_in_hours":7.2e1}',
            b'{"email":"wire-low-prd060@example.com","role":"developer","expires_in_hours":0}',
            b'{"email":"wire-high-prd060@example.com","role":"developer","expires_in_hours":169}',
        )
        invalid_creates = [
            await client.post("/invitations", content=body, headers=create_headers)
            for body in invalid_create_bodies
        ]
        created = await _create_invitation(
            client,
            admin_token,
            email="wire-prd060@example.com",
        )
        raw_token = created.json()["accept_token"]
        invalid_accept_bodies = (
            b'{"display_name":"Wire User","password":"Prd060Wire!2026","tenant_id":"other"}',
            b'{"display_name":"Wire User","password":"Prd060Wire!2026","email":"other@example.com"}',
            b'{"display_name":"Wire User","password":"Prd060Wire!2026","role":"admin"}',
            b'{"display_name":"Wire User","password":"first-value","password":"second-value"}',
            b'{"display_name":"Wire User","password":"short"}',
            b'{"display_name":"","password":"Prd060Wire!2026"}',
            b'{"display_name":"   ","password":"Prd060Wire!2026"}',
        )
        invalid_accepts = [
            await client.post(
                f"/invitations/{raw_token}/accept",
                content=body,
                headers={"Content-Type": "application/json"},
            )
            for body in invalid_accept_bodies
        ]
        invalid_role = await _create_invitation(
            client,
            admin_token,
            email="invalid-role-prd060@example.com",
            role="unknown-role",
        )
        existing_user = await _create_invitation(
            client,
            admin_token,
            email="admin@metahub.local",
        )
        duplicate_pending = await _create_invitation(
            client,
            admin_token,
            email="wire-prd060@example.com",
        )
        connection = connect_database(database_url)
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()[0]
        # 固定角色目录本身不可变；这里只在隔离测试库构造外部损坏状态，验证创建仍按 active 失败关闭。
        connection.execute("DROP TRIGGER trg_roles_matrix_update_prd059")
        connection.execute(
            "UPDATE roles SET status='disabled' WHERE tenant_id=? AND code='viewer'",
            (tenant_id,),
        )
        connection.commit()
        connection.close()
        disabled_role = await _create_invitation(
            client,
            admin_token,
            email="disabled-role-prd060@example.com",
            role="viewer",
        )
        malformed_token = await client.post(
            "/invitations/not-a-token/accept",
            json={"display_name": "Malformed Token", "password": INVITED_PASSWORD},
        )

    assert created.status_code == 201, created.text
    assert {response.status_code for response in invalid_creates} == {422}
    assert {response.json()["code"] for response in invalid_creates} == {
        "invitation.create_invalid"
    }
    assert {response.status_code for response in invalid_accepts} == {422}
    assert {response.json()["code"] for response in invalid_accepts} == {
        "invitation.accept_invalid"
    }
    assert (invalid_role.status_code, invalid_role.json()["code"]) == (
        422,
        "invitation.role_invalid",
    )
    assert (disabled_role.status_code, disabled_role.json()["code"]) == (
        422,
        "invitation.role_invalid",
    )
    assert (existing_user.status_code, existing_user.json()["code"]) == (
        409,
        "invitation.user_exists",
    )
    assert (duplicate_pending.status_code, duplicate_pending.json()["code"]) == (
        409,
        "invitation.pending_exists",
    )
    assert (malformed_token.status_code, malformed_token.json()["code"]) == (
        410,
        "invitation.not_available",
    )
    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT status,accepted_user_id FROM invitations WHERE email='wire-prd060@example.com'"
    ).fetchone()
    assert tuple(row) == ("pending", None)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE email='wire-prd060@example.com'"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_tenant_role_and_inviter_permission_drift_share_public_410(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限流程：目标角色、租户或邀请人实时授权漂移均不得继续建号或暴露差异。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_token = (await _login(client)).json()["access_token"]
        role_token = (await _create_invitation(
            client,
            admin_token,
            email="role-drift-prd060@example.com",
            role="viewer",
            request_id="req_prd060_role_drift_create",
        )).json()["accept_token"]
        tenant_token = (await _create_invitation(
            client,
            admin_token,
            email="tenant-drift-prd060@example.com",
            request_id="req_prd060_tenant_drift_create",
        )).json()["accept_token"]
        inviter_token = (await _create_invitation(
            client,
            admin_token,
            email="inviter-drift-prd060@example.com",
            request_id="req_prd060_inviter_drift_create",
        )).json()["accept_token"]

        connection = connect_database(database_url)
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()[0]
        # PRD-059 正常路径禁止停用固定角色；这里只在一次性测试库移除更新 guard，
        # 注入外部损坏目录，证明 Invitation 服务和 0051 接受 trigger 仍会失败关闭。
        connection.execute("DROP TRIGGER trg_roles_matrix_update_prd059")
        connection.execute(
            "UPDATE roles SET status='disabled' WHERE tenant_id=? AND code='viewer'",
            (tenant_id,),
        )
        connection.commit()
        connection.close()
        role_drift = await client.post(
            f"/invitations/{role_token}/accept",
            json={"display_name": "Role Drift", "password": INVITED_PASSWORD},
        )

        connection = connect_database(database_url)
        connection.execute(
            "UPDATE roles SET status='active' WHERE tenant_id=? AND code='viewer'",
            (tenant_id,),
        )
        connection.execute(
            "UPDATE tenants SET status='disabled',updated_at=? WHERE tenant_id=?",
            ("2026-07-31T12:02:00Z", tenant_id),
        )
        connection.commit()
        connection.close()
        tenant_drift = await client.post(
            f"/invitations/{tenant_token}/accept",
            json={"display_name": "Tenant Drift", "password": INVITED_PASSWORD},
        )

        connection = connect_database(database_url)
        connection.execute(
            "UPDATE tenants SET status='active',updated_at=? WHERE tenant_id=?",
            ("2026-07-31T12:03:00Z", tenant_id),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                "user_prd060_backup_admin",
                tenant_id,
                "backup-admin-prd060@example.com",
                "Backup Admin",
                hash_password(ADMIN_PASSWORD),
                "admin",
                "2026-07-31T12:03:00Z",
                "2026-07-31T12:03:00Z",
            ),
        )
        connection.execute(
            "UPDATE users SET role='developer',updated_at=? WHERE email='admin@metahub.local'",
            ("2026-07-31T12:04:00Z",),
        )
        connection.commit()
        connection.close()
        inviter_drift = await client.post(
            f"/invitations/{inviter_token}/accept",
            json={"display_name": "Inviter Drift", "password": INVITED_PASSWORD},
        )
        random = await client.post(
            f"/invitations/{'D' * 43}/accept",
            json={"display_name": "Random Drift", "password": INVITED_PASSWORD},
        )

    signatures = {
        json.dumps(_public_error_signature(response), ensure_ascii=True, sort_keys=True)
        for response in (role_drift, tenant_drift, inviter_drift, random)
    }
    assert len(signatures) == 1
    assert random.status_code == 410
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE email LIKE '%-drift-prd060@example.com'"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_same_email_is_tenant_isolated_and_cross_tenant_inviter_relation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """租户流程：另一租户同邮箱不阻断邀请，复合关系禁止借用另一租户邀请人。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    default_tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0]
    occurred_at = "2026-07-31T12:00:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (
            "tenant_prd060_other",
            "prd060-other",
            "PRD-060 Other",
            occurred_at,
            occurred_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        (
            "user_prd060_other_admin",
            "tenant_prd060_other",
            "shared-prd060@example.com",
            "Other Admin",
            hash_password(ADMIN_PASSWORD),
            "admin",
            occurred_at,
            occurred_at,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO invitations(
              invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
              invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'pending',?,NULL,?,NULL,?,?)
            """,
            (
                "inv_prd060_cross_tenant",
                default_tenant_id,
                "relation-prd060@example.com",
                "b" * 64,
                "developer",
                "a" * 64,
                "user_prd060_other_admin",
                "2026-08-03T12:00:00Z",
                occurred_at,
                occurred_at,
            ),
        )
    connection.rollback()
    connection.close()

    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_token = (await _login(client)).json()["access_token"]
        created = await _create_invitation(
            client,
            admin_token,
            email="shared-prd060@example.com",
        )

    assert created.status_code == 201, created.text
    assert created.json()["invitation"]["tenant_id"] == default_tenant_id
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE tenant_id='tenant_prd060_other' AND email='shared-prd060@example.com'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM invitations WHERE tenant_id=? AND email='shared-prd060@example.com'",
        (default_tenant_id,),
    ).fetchone()[0] == 1
    connection.close()


@pytest.mark.anyio
async def test_create_strong_facts_roll_back_and_webhook_failure_preserves_business_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务流程：创建审计/source 失败全回滚；提交后 Webhook 失败只标记事件失败。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    original_audit = invitation_service.record_audit_log
    original_insert_event = invitation_service._insert_source_event
    original_commit = invitation_service.commit_audited_transaction
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_token = (await _login(client)).json()["access_token"]

        def fail_audit(*args: object, **kwargs: object) -> None:
            """模拟提交前强审计失败，不携带业务正文或数据库细节。"""

            # 固定内部码触发事务回滚，避免把夹具参数中的邮箱或 Token 放进异常。
            raise ApiError("audit_log.record_unavailable")

        monkeypatch.setattr(invitation_service, "record_audit_log", fail_audit)
        audit_failed = await _create_invitation(
            client,
            admin_token,
            email="create-audit-rollback-prd060@example.com",
        )
        monkeypatch.setattr(invitation_service, "record_audit_log", original_audit)

        def fail_source_event(*args: object, **kwargs: object) -> None:
            """模拟提交前不可变 source event 写入失败。"""

            # 固定 guard 名只表示 source 写失败，不包含 SQL、邮箱或凭据。
            raise sqlite3.IntegrityError("invitation_event_guard")

        monkeypatch.setattr(invitation_service, "_insert_source_event", fail_source_event)
        source_failed = await _create_invitation(
            client,
            admin_token,
            email="create-source-rollback-prd060@example.com",
        )
        monkeypatch.setattr(
            invitation_service,
            "_insert_source_event",
            original_insert_event,
        )

        def fail_create_commit(*args: object, **kwargs: object) -> None:
            """模拟创建事务 commit 失败，验证 pending Invitation 和强事实均不会残留。"""

            # 固定 DbError 不携带连接、邮箱或 SQL，服务必须折叠为 invitation.unavailable。
            raise invitation_service.DbError(
                "db.commit_failed",
                "fixed create commit failure",
                "retry with a new request id",
            )

        monkeypatch.setattr(
            invitation_service,
            "commit_audited_transaction",
            fail_create_commit,
        )
        commit_failed = await _create_invitation(
            client,
            admin_token,
            email="create-commit-rollback-prd060@example.com",
            request_id="req_prd060_create_commit_failure",
        )
        monkeypatch.setattr(
            invitation_service,
            "commit_audited_transaction",
            original_commit,
        )

        def fail_webhook(*args: object, **kwargs: object) -> None:
            """模拟提交后外部投递失败；服务只能保存固定失败码，不能撤销业务事实。"""

            # 固定异常证明投递失败隔离；不使用 Endpoint URL、响应正文或事件 payload。
            raise RuntimeError("fixed webhook failure")

        monkeypatch.setattr(invitation_service, "dispatch_webhook_event", fail_webhook)
        created = await _create_invitation(
            client,
            admin_token,
            email="webhook-failed-prd060@example.com",
        )
        accepted = await client.post(
            f"/invitations/{created.json()['accept_token']}/accept",
            json={"display_name": "Webhook Failed", "password": INVITED_PASSWORD},
        )

    assert audit_failed.status_code == 503
    assert source_failed.status_code == 503
    assert commit_failed.status_code == 503
    assert audit_failed.json()["code"] == "invitation.unavailable"
    assert source_failed.json()["code"] == "invitation.unavailable"
    assert commit_failed.json()["code"] == "invitation.unavailable"
    assert created.status_code == 201, created.text
    assert accepted.status_code == 201, accepted.text
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM invitations WHERE email LIKE 'create-%-rollback-prd060@example.com'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd060_create_commit_failure'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM invitation_events WHERE request_id='req_prd060_create_commit_failure'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT status FROM invitations WHERE email='webhook-failed-prd060@example.com'"
    ).fetchone()[0] == "accepted"
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE email='webhook-failed-prd060@example.com'"
    ).fetchone()[0] == 1
    event_rows = connection.execute(
        "SELECT event_type,delivery_status,last_error_code FROM invitation_events ORDER BY event_type"
    ).fetchall()
    connection.close()
    assert {row["event_type"] for row in event_rows} == {
        "user.invited",
        "invitation.accepted",
    }
    assert {row["delivery_status"] for row in event_rows} == {"failed"}
    assert {row["last_error_code"] for row in event_rows} == {
        "webhook.delivery_unavailable"
    }


@pytest.mark.anyio
async def test_accept_source_event_and_commit_failures_roll_back_every_strong_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接受事务：source event 或 commit 失败时不消费邀请，也不留下用户和接受审计。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    original_insert_event = invitation_service._insert_source_event
    original_commit = invitation_service.commit_audited_transaction
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_token = (await _login(client)).json()["access_token"]
        source_invitation = await _create_invitation(
            client,
            admin_token,
            email="accept-source-rollback-prd060@example.com",
            request_id="req_prd060_accept_source_create",
        )

        def fail_accept_source(*args: object, **kwargs: object) -> None:
            """模拟接受阶段不可变 source event 写入失败，不保留调用参数。"""

            # 创建阶段已经完成，当前补丁只作用于 accept；固定异常文本不包含邮箱或 Token。
            raise sqlite3.IntegrityError("invitation_accept_event_guard")

        monkeypatch.setattr(invitation_service, "_insert_source_event", fail_accept_source)
        source_failed = await client.post(
            f"/invitations/{source_invitation.json()['accept_token']}/accept",
            json={"display_name": "Source Rollback", "password": INVITED_PASSWORD},
        )
        monkeypatch.setattr(invitation_service, "_insert_source_event", original_insert_event)

        commit_invitation = await _create_invitation(
            client,
            admin_token,
            email="accept-commit-rollback-prd060@example.com",
            request_id="req_prd060_accept_commit_create",
        )

        def fail_accept_commit(*args: object, **kwargs: object) -> None:
            """模拟接受事务提交失败，由 service 统一 rollback 并转换为公开 503。"""

            # DbError 使用固定安全字段，验证 commit 失败不会透出连接或 SQL 细节。
            raise invitation_service.DbError(
                "db.commit_failed",
                "fixed commit failure",
                "retry with a new request id",
            )

        monkeypatch.setattr(
            invitation_service,
            "commit_audited_transaction",
            fail_accept_commit,
        )
        commit_failed = await client.post(
            f"/invitations/{commit_invitation.json()['accept_token']}/accept",
            json={"display_name": "Commit Rollback", "password": INVITED_PASSWORD},
        )
        monkeypatch.setattr(
            invitation_service,
            "commit_audited_transaction",
            original_commit,
        )

    assert source_failed.status_code == 503
    assert commit_failed.status_code == 503
    assert source_failed.json()["code"] == "invitation.unavailable"
    assert commit_failed.json()["code"] == "invitation.unavailable"
    connection = connect_database(database_url)
    rollback_rows = connection.execute(
        """
        SELECT email,status,accepted_user_id FROM invitations
        WHERE email IN (?,?) ORDER BY email
        """,
        (
            "accept-source-rollback-prd060@example.com",
            "accept-commit-rollback-prd060@example.com",
        ),
    ).fetchall()
    assert len(rollback_rows) == 2
    assert all(row["status"] == "pending" for row in rollback_rows)
    assert all(row["accepted_user_id"] is None for row in rollback_rows)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE email IN (?,?)
        """,
        (
            "accept-source-rollback-prd060@example.com",
            "accept-commit-rollback-prd060@example.com",
        ),
    ).fetchone()[0] == 0
    assert connection.execute(
        """
        SELECT COUNT(*) FROM audit_logs audit
        JOIN invitations invitation
          ON invitation.tenant_id=audit.tenant_id AND invitation.invitation_id=audit.object_id
        WHERE audit.action='invitation.accepted' AND invitation.email IN (?,?)
        """,
        (
            "accept-source-rollback-prd060@example.com",
            "accept-commit-rollback-prd060@example.com",
        ),
    ).fetchone()[0] == 0
    assert connection.execute(
        """
        SELECT COUNT(*) FROM invitation_events event
        JOIN invitations invitation
          ON invitation.tenant_id=event.tenant_id
         AND invitation.invitation_id=event.invitation_id
        WHERE event.event_type='invitation.accepted' AND invitation.email IN (?,?)
        """,
        (
            "accept-source-rollback-prd060@example.com",
            "accept-commit-rollback-prd060@example.com",
        ),
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_concurrent_accept_has_one_winner_and_audit_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """并发/原子性：同 Token 只创建一个用户；强审计失败时用户和消费状态全部回滚。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(invitation_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin_login = await _login(client)
        admin_token = admin_login.json()["access_token"]
        concurrent_created = await _create_invitation(
            client,
            admin_token,
            email="concurrent-prd060@example.com",
            request_id="req_prd060_concurrent_create",
        )
        concurrent_token = concurrent_created.json()["accept_token"]

        # 两个线程各自进入 service 并创建独立 SQLite 连接；Barrier 只对齐起跑点，真正的
        # BEGIN IMMEDIATE/CAS 竞争由数据库决定，避免同一事件循环顺序执行形成伪并发证据。
        start_barrier = threading.Barrier(2)

        def accept_once(request_id: str) -> tuple[str, str]:
            """在独立线程接受同一 Token，返回固定结果分类和非敏感 ID/错误码。"""

            start_barrier.wait(timeout=10)
            try:
                result = invitation_service.accept_invitation(
                    app.state.config,
                    concurrent_token,
                    invitation_service.AcceptInvitationRequest(
                        display_name="Concurrent User",
                        password=INVITED_PASSWORD,
                    ),
                    request_meta=invitation_service.AuditRequestMeta(
                        client_ip=None,
                        user_agent=None,
                    ),
                    request_id=request_id,
                    trace_id=f"trc_{request_id}",
                )
            # 并发败者只能返回公开同形领域码，不把 SQLite 锁或 Token 写进结果。
            except ApiError as error:
                return "error", error.code
            return "accepted", result.user.user_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = [
                future.result(timeout=20)
                for future in (
                    executor.submit(accept_once, "req_prd060_concurrent_a"),
                    executor.submit(accept_once, "req_prd060_concurrent_b"),
                )
            ]

        rollback_created = await _create_invitation(
            client,
            admin_token,
            email="rollback-prd060@example.com",
            request_id="req_prd060_rollback_create",
        )
        rollback_token = rollback_created.json()["accept_token"]

        def fail_audit(*args: object, **kwargs: object) -> None:
            """模拟强审计不可用；异常不含数据库、邮箱、密码或 Token。"""

            # 接受事务必须把该内部码折叠为 invitation.unavailable 并回滚用户。
            raise ApiError("audit_log.record_unavailable")

        monkeypatch.setattr(invitation_service, "record_audit_log_for_actor", fail_audit)
        rolled_back = await client.post(
            f"/invitations/{rollback_token}/accept",
            json={"display_name": "Rollback User", "password": INVITED_PASSWORD},
        )

    assert [kind for kind, _ in concurrent_results].count("accepted") == 1
    assert concurrent_results.count(("error", "invitation.not_available")) == 1
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE email='concurrent-prd060@example.com'"
    ).fetchone()[0] == 1
    assert connection.execute(
        """
        SELECT COUNT(*) FROM audit_logs
        WHERE action='invitation.accepted' AND object_id=(
          SELECT invitation_id FROM invitations WHERE email='concurrent-prd060@example.com'
        )
        """
    ).fetchone()[0] == 1
    assert rolled_back.status_code == 503
    assert rolled_back.json()["code"] == "invitation.unavailable"
    rollback_row = connection.execute(
        "SELECT status,accepted_user_id FROM invitations WHERE email='rollback-prd060@example.com'"
    ).fetchone()
    assert rollback_row["status"] == "pending"
    assert rollback_row["accepted_user_id"] is None
    assert connection.execute(
        "SELECT COUNT(*) FROM users WHERE email='rollback-prd060@example.com'"
    ).fetchone()[0] == 0
    connection.close()
