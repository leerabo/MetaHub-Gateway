"""PRD-059 认证快照与 permission 后端守卫专项测试。

测试只使用隔离 SQLite 文件，覆盖六角色登录、旧 Token 即时降权、矩阵损坏失败关闭和
职责分离；测试证据由调用方通过 ``--basetemp``/JUnit 写入明确的仓库内验收目录。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.auth.dependencies import (
    require_debug_console_user,
    require_model_price_writer,
    require_provider_writer,
    require_usage_reader,
)
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import AuthContext, require_permission
from metahub_control.permissions.routes import router as permissions_router
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import (
    PERMISSION_CATALOG,
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 隔离测试账号与角色邮箱只服务本地矩阵验证，密码进入数据库前必须单向哈希。
_PASSWORD = "Prd059Local!2026"
_ROLE_EMAILS = {
    "admin": "admin@metahub.local",
    "developer": "developer-prd059@example.com",
    "viewer": "viewer-prd059@example.com",
    "finance_admin": "finance-prd059@example.com",
    "security_admin": "security-prd059@example.com",
    "ops_admin": "ops-prd059@example.com",
}

# 历史升级回归只读取并复制正式迁移，先停在 0049 seed UUID 角色，再继续应用完整栈。
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
_LEGACY_DEVELOPER_ROLE_ID = "706c98f5-0edb-5e48-ab68-4e03cf91eab6"


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离路径转换为仓库数据库层接受的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """迁移并初始化六角色认证数据，返回隔离数据库 URL。

    参数：pytest 环境补丁和沙箱内临时目录。返回：仅指向当前用例数据库的 URL。
    约束：密码只进入 PBKDF2 哈希，不写日志或断言输出。
    """

    database_url = _sqlite_url(tmp_path / "prd059-auth.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd059-auth-test-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    now = "2026-07-31T05:00:00Z"
    rows = [
        (
            f"user_prd059_{role}",
            seed.tenant_id,
            email,
            role,
            hash_password(_PASSWORD),
            role,
            now,
            now,
        )
        for role, email in _ROLE_EMAILS.items()
        if role != "admin"
    ]
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        rows,
    )
    connection.commit()
    connection.close()
    return database_url


def _prepare_legacy_upgraded_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """构造从 0049 seed 后升级到当前栈顶的真实历史角色数据库。

    参数：pytest 环境补丁和当前用例独占目录。返回：数据库 URL 与保留下来的 Developer UUID。
    约束：迁移副本和 SQLite 文件均位于 ``tmp_path``，不得读取或修改当前开发数据库。
    """

    database_url = _sqlite_url(tmp_path / "prd059-legacy-upgraded.db")
    legacy_migrations = tmp_path / "migrations-through-0049"
    legacy_migrations.mkdir(parents=True, exist_ok=False)
    for source in _MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        # 只复制不高于 0049 的正式脚本，确保 seed 确实生成旧版 UUID，而非新触发器 ID。
        if version.isdigit() and int(version) <= 49:
            shutil.copy2(source, legacy_migrations / source.name)

    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd059-legacy-auth-test-secret")
    apply_migrations(database_url, migrations_dir=legacy_migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    legacy_role_id = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code='developer'",
        (seed.tenant_id,),
    ).fetchone()[0]
    connection.close()
    assert legacy_role_id == _LEGACY_DEVELOPER_ROLE_ID

    # 0050 明确保留旧角色主键；继续应用到 0058 后 API 必须能对同一 UUID 产生安全事件。
    apply_migrations(database_url)
    return database_url, legacy_role_id


def _build_app():
    """构造只包含认证和四个代表性 permission 守卫的隔离 ASGI 应用。"""

    app = create_service_app("control-plane-api", "PRD-059 Auth Permission Test API")
    app.include_router(auth_router)
    app.include_router(permissions_router)

    @app.get("/guard/provider-write")
    async def provider_write(
        context: Annotated[AuthContext, Depends(require_provider_writer)],
    ) -> dict[str, str]:
        """返回已通过 Provider 写权限校验的数据库角色。"""

        return {"role": context.user.role}

    @app.get("/guard/model-price-write")
    async def model_price_write(
        context: Annotated[AuthContext, Depends(require_model_price_writer)],
    ) -> dict[str, str]:
        """返回已通过价格写权限校验的数据库角色。"""

        return {"role": context.user.role}

    @app.get("/guard/usage-read")
    async def usage_read(
        context: Annotated[AuthContext, Depends(require_usage_reader)],
    ) -> dict[str, str]:
        """返回已通过全租户 Usage 读取校验的数据库角色。"""

        return {"role": context.user.role}

    @app.get("/guard/debug-console")
    async def debug_console(
        context: Annotated[AuthContext, Depends(require_debug_console_user)],
    ) -> dict[str, str]:
        """返回已通过调试台使用权限校验的数据库角色。"""

        return {"role": context.user.role}

    return app


async def _login(client: AsyncClient, role: str) -> Response:
    """以指定数据库角色登录并返回完整响应，便于同时断言错误和安全字段。"""

    return await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": _ROLE_EMAILS[role], "password": _PASSWORD},
        headers={"X-Request-ID": f"req_prd059_login_{role}"},
    )


@pytest.mark.anyio
async def test_six_roles_login_and_me_return_database_permission_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：六角色登录和 /auth/me 返回相同的数据库权限、revision 与指纹。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        for role in ROLE_CODES:
            login = await _login(client, role)
            assert login.status_code == 200, login.text
            payload = login.json()
            assert set(payload["permission_codes"]) == set(ROLE_GRANTABLE_PERMISSIONS[role])
            assert payload["permission_codes"] == payload["user"]["permission_codes"]
            assert payload["permission_revision"] == payload["user"]["permission_revision"] == 1
            assert (
                payload["authorization_fingerprint"]
                == payload["user"]["authorization_fingerprint"]
            )
            assert len(payload["authorization_fingerprint"]) == 64

            me = await client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            assert me.status_code == 200, me.text
            assert me.json()["user"]["role"] == role
            assert me.json()["permission_codes"] == payload["permission_codes"]
            assert me.json()["authorization_fingerprint"] == payload["authorization_fingerprint"]


@pytest.mark.anyio
async def test_existing_token_uses_new_user_role_from_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：Token 中旧 developer claim 不得覆盖数据库已变更的 viewer 角色。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "developer")
        assert login.status_code == 200
        token = login.json()["access_token"]

        connection = connect_database(database_url)
        connection.execute(
            "UPDATE users SET role='viewer',updated_at=? WHERE email=?",
            ("2026-07-31T05:01:00Z", _ROLE_EMAILS["developer"]),
        )
        connection.commit()
        connection.close()

        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200, me.text
    assert me.json()["user"]["role"] == "viewer"
    assert set(me.json()["permission_codes"]) == set(ROLE_GRANTABLE_PERMISSIONS["viewer"])


@pytest.mark.anyio
async def test_revoked_permission_takes_effect_on_next_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：同一 Session 的旧 Token 在矩阵撤权后下一请求立即被后端拒绝。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "developer")
        assert login.status_code == 200
        token = login.json()["access_token"]
        before_fingerprint = login.json()["authorization_fingerprint"]
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/guard/debug-console", headers=headers)).status_code == 200

        connection = connect_database(database_url)
        role = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE code='developer'"
        ).fetchone()
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE code='debug_console.use'"
        ).fetchone()
        assert role is not None and permission is not None
        connection.execute(
            "DELETE FROM role_permissions WHERE role_id=? AND permission_id=?",
            (role["role_id"], permission["permission_id"]),
        )
        remaining_codes = set(ROLE_GRANTABLE_PERMISSIONS["developer"]) - {"debug_console.use"}
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE role_id=?
            """,
            (
                role["permission_revision"] + 1,
                permission_matrix_sha256(remaining_codes),
                "2026-07-31T05:02:00Z",
                role["role_id"],
            ),
        )
        connection.commit()
        connection.close()

        denied = await client.get("/guard/debug-console", headers=headers)
        me = await client.get("/auth/me", headers=headers)

    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert denied.json()["request_id"] and denied.json()["trace_id"]
    assert me.status_code == 200
    assert me.json()["permission_revision"] == 2
    assert "debug_console.use" not in me.json()["permission_codes"]
    assert me.json()["authorization_fingerprint"] != before_fingerprint


@pytest.mark.anyio
async def test_corrupted_matrix_hash_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常：存储 hash 与 RolePermission 集合不一致时返回统一 500，绝不回退默认角色。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "developer")
        assert login.status_code == 200
        token = login.json()["access_token"]

        connection = connect_database(database_url)
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
            WHERE code='developer'
            """,
            ("0" * 64, "2026-07-31T05:03:00Z"),
        )
        connection.commit()
        connection.close()

        failed = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert failed.status_code == 500
    assert failed.json()["code"] == "rbac.matrix_integrity_failed"
    assert failed.json()["request_id"] and failed.json()["trace_id"]
    assert token not in failed.text


@pytest.mark.anyio
async def test_separation_of_duties_is_enforced_by_backend_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：Finance、Security、Developer 的职责边界由后端 permission 守卫执行。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        responses: dict[str, Response] = {}
        for role, path in (
            ("finance_admin", "/guard/provider-write"),
            ("security_admin", "/guard/model-price-write"),
            ("developer", "/guard/usage-read"),
        ):
            login = await _login(client, role)
            assert login.status_code == 200
            responses[role] = await client.get(
                path,
                headers={"Authorization": f"Bearer {login.json()['access_token']}"},
            )

        ops_login = await _login(client, "ops_admin")
        finance_login = await _login(client, "finance_admin")
        ops_allowed = await client.get(
            "/guard/provider-write",
            headers={"Authorization": f"Bearer {ops_login.json()['access_token']}"},
        )
        finance_allowed = await client.get(
            "/guard/usage-read",
            headers={"Authorization": f"Bearer {finance_login.json()['access_token']}"},
        )

    for response in responses.values():
        assert response.status_code == 403
        assert response.json()["code"] == "auth.permission_denied"
        assert response.json()["request_id"] and response.json()["trace_id"]
    assert ops_allowed.status_code == 200
    assert finance_allowed.status_code == 200


def test_require_permission_uses_snapshot_not_role_name() -> None:
    """单元：角色名不能替代 permission set，admin 名称但空快照仍必须拒绝。"""

    context = AuthContext(
        user=AuthUser(
            user_id="user_prd059_manual",
            tenant_id="tenant_prd059_manual",
            email="manual@example.com",
            display_name="Manual",
            role="admin",
            permission_codes=(),
            permission_revision=1,
            authorization_fingerprint="f" * 64,
        ),
        tenant=AuthTenant(
            tenant_id="tenant_prd059_manual",
            slug="manual",
            name="Manual Tenant",
        ),
        session=AuthSession(
            session_id="session_prd059_manual",
            expires_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        token="test-only-token",
        token_jti="jti_prd059_manual",
    )

    with pytest.raises(ApiError) as error:
        require_permission(context, "provider.write")

    assert error.value.code == "auth.permission_denied"



def _matrix_role(payload: dict[str, object], role_code: str) -> dict[str, object]:
    """从完整矩阵响应按固定 code 取角色；缺失时让测试立即失败。"""

    roles = payload.get("roles")
    assert isinstance(roles, list)
    role = next((item for item in roles if item.get("code") == role_code), None)
    assert isinstance(role, dict)
    return role


@pytest.mark.anyio
async def test_permission_matrix_get_patch_noop_cas_and_event_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：GET、真实 PATCH、no-op、旧 revision CAS 及审计事件保持同一追踪链。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "admin")
        assert login.status_code == 200
        token = login.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}
        matrix = await client.get(
            "/permissions",
            headers={**auth_headers, "X-Request-ID": "req_prd059_matrix_get"},
        )
        assert matrix.status_code == 200, matrix.text
        matrix_payload = matrix.json()
        assert matrix_payload["request_id"] == "req_prd059_matrix_get"
        # 当前 0061 栈顶是 revision 6：在 Chat、Prompt 与策略模板后继续加入敏感意图分类权限。
        # 本用例读取运行时完整链，数量和 code 集合都必须精确等于共享权威目录。
        assert len(matrix_payload["permissions"]) == len(PERMISSION_CATALOG)
        assert {item["code"] for item in matrix_payload["permissions"]} == {
            definition.code for definition in PERMISSION_CATALOG
        }
        assert len(matrix_payload["roles"]) == 6
        assert all(
            item["tenant_id"] == login.json()["user"]["tenant_id"]
            for item in matrix_payload["permissions"] + matrix_payload["roles"]
        )

        developer = _matrix_role(matrix_payload, "developer")
        # 当前迁移栈顶是 PRD-066：除验证普通权限撤销外，再显式撤销两项配置迁移权限，
        # 保留 chat.use 以验证新增权限也参与同一租户矩阵哈希与 CAS 更新。
        requested_codes = sorted(
            set(developer["permission_codes"])
            - {
                "config.transfer.read",
                "config.transfer.write",
                "debug_console.use",
            }
        )
        changed = await client.patch(
            f"/roles/{developer['role_id']}/permissions",
            json={
                "expected_revision": developer["permission_revision"],
                "permission_codes": requested_codes,
            },
            headers={**auth_headers, "X-Request-ID": "req_prd059_matrix_patch"},
        )
        assert changed.status_code == 200, changed.text
        changed_payload = changed.json()
        assert changed_payload["changed"] is True
        assert changed_payload["request_id"] == "req_prd059_matrix_patch"
        assert changed_payload["event_id"].startswith("rpevt_")
        assert changed_payload["role"]["permission_revision"] == 2
        assert changed_payload["role"]["permission_codes"] == requested_codes

        no_op = await client.patch(
            f"/roles/{developer['role_id']}/permissions",
            json={"expected_revision": 2, "permission_codes": requested_codes},
            headers={**auth_headers, "X-Request-ID": "req_prd059_matrix_noop"},
        )
        assert no_op.status_code == 200, no_op.text
        assert no_op.json()["changed"] is False
        assert no_op.json()["event_id"] is None
        assert no_op.json()["role"]["permission_revision"] == 2

        stale = await client.patch(
            f"/roles/{developer['role_id']}/permissions",
            json={"expected_revision": 1, "permission_codes": requested_codes},
            headers={**auth_headers, "X-Request-ID": "req_prd059_matrix_stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "rbac.revision_conflict"
        assert stale.json()["request_id"] == "req_prd059_matrix_stale"

        refreshed = await client.get("/permissions", headers=auth_headers)
        assert refreshed.status_code == 200
        assert _matrix_role(refreshed.json(), "developer")["permission_codes"] == requested_codes

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='role.permission_changed'"
    ).fetchone()[0] == 1
    event = connection.execute(
        """
        SELECT request_id,trace_id,delivery_status,last_error_code,payload_json
        FROM role_permission_events
        """
    ).fetchone()
    assert event is not None
    assert event["request_id"] == "req_prd059_matrix_patch"
    assert event["delivery_status"] == "dispatched"
    assert event["last_error_code"] is None
    event_payload = json.loads(event["payload_json"])
    assert set(event_payload) == {
        "schema_version",
        "role_id",
        "role_code",
        "before_revision",
        "after_revision",
        "before_sha256",
        "after_sha256",
        "added_permissions",
        "removed_permissions",
    }
    assert set(event_payload["removed_permissions"]) == {
        "config.transfer.read",
        "config.transfer.write",
        "debug_console.use",
    }
    serialized = event["payload_json"].lower()
    assert all(
        forbidden not in serialized
        for forbidden in (
            "authorization",
            "cookie",
            "provider_key",
            "prompt",
            "response",
            "secret",
            "token",
        )
    )
    connection.close()

    # 0061 的敏感意图权限和 0060 的策略模板权限均未授予 Developer，且不存在相应业务事实，
    # 因此可按真实栈顶依次安全收缩；随后 0059 看到仍持有 Prompt 权限的自定义 Developer
    # 矩阵时，必须在首项 DDL 前失败关闭，不能因新增迁移而跳过原有 down guard。
    assert rollback_last_migration(database_url).rolled_back == "0061"
    assert rollback_last_migration(database_url).rolled_back == "0060"
    with pytest.raises(DbError) as rollback_error:
        rollback_last_migration(database_url)
    assert rollback_error.value.code == "db.migration_failed"
    assert "prd067_down_business_facts_present" in rollback_error.value.message

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0061'"
    ).fetchone()[0] == "rolled_back"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0060'"
    ).fetchone()[0] == "rolled_back"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0059'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0054'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permission_events WHERE request_id=?",
        ("req_prd059_matrix_patch",),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='role.permission_changed'"
    ).fetchone()[0] == 1
    connection.close()


@pytest.mark.anyio
async def test_permission_patch_can_revoke_new_chat_permission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """目录演进：正式 PATCH 可从 Developer 完整集合单独撤销新增的 ``chat.use``。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        login = await _login(client, "admin")
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        matrix = await client.get("/permissions", headers=headers)
        assert matrix.status_code == 200, matrix.text
        developer = _matrix_role(matrix.json(), "developer")
        assert "chat.use" in developer["permission_codes"]
        requested_codes = sorted(
            set(developer["permission_codes"]) - {"chat.use"}
        )

        changed = await client.patch(
            f"/roles/{developer['role_id']}/permissions",
            json={
                "expected_revision": developer["permission_revision"],
                "permission_codes": requested_codes,
            },
            headers={**headers, "X-Request-ID": "req_prd066_revoke_chat_use"},
        )

    assert changed.status_code == 200, changed.text
    payload = changed.json()
    assert payload["changed"] is True
    assert payload["role"]["permission_codes"] == requested_codes
    assert payload["role"]["permission_revision"] == developer["permission_revision"] + 1

    connection = connect_database(database_url)
    role = connection.execute(
        """
        SELECT permission_revision,matrix_sha256 FROM roles
        WHERE tenant_id=? AND role_id=?
        """,
        (developer["tenant_id"], developer["role_id"]),
    ).fetchone()
    event = connection.execute(
        """
        SELECT removed_permissions_json,delivery_status FROM role_permission_events
        WHERE tenant_id=? AND request_id='req_prd066_revoke_chat_use'
        """,
        (developer["tenant_id"],),
    ).fetchone()
    connection.close()
    assert tuple(role) == (
        developer["permission_revision"] + 1,
        permission_matrix_sha256(requested_codes),
    )
    assert json.loads(event["removed_permissions_json"]) == ["chat.use"]
    assert event["delivery_status"] == "dispatched"


@pytest.mark.anyio
async def test_legacy_uuid_role_can_revoke_chat_permission_after_full_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """历史兼容：0049 UUID 角色升级到 0058 后可通过正式 PATCH 撤销 Chat 权限。"""

    database_url, legacy_role_id = _prepare_legacy_upgraded_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        login = await _login(client, "admin")
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        matrix = await client.get("/permissions", headers=headers)
        assert matrix.status_code == 200, matrix.text
        developer = _matrix_role(matrix.json(), "developer")
        assert developer["role_id"] == legacy_role_id
        assert "chat.use" in developer["permission_codes"]
        requested_codes = sorted(set(developer["permission_codes"]) - {"chat.use"})

        changed = await client.patch(
            f"/roles/{legacy_role_id}/permissions",
            json={
                "expected_revision": developer["permission_revision"],
                "permission_codes": requested_codes,
            },
            headers={**headers, "X-Request-ID": "req_prd066_legacy_revoke_chat_use"},
        )

    assert changed.status_code == 200, changed.text
    response_role = changed.json()["role"]
    assert response_role["role_id"] == legacy_role_id
    assert response_role["permission_revision"] == developer["permission_revision"] + 1
    assert response_role["permission_codes"] == requested_codes

    connection = connect_database(database_url)
    persisted = connection.execute(
        """
        SELECT role.permission_revision,role.matrix_sha256,event.removed_permissions_json,
               event.delivery_status,event.role_id
        FROM roles role
        JOIN role_permission_events event
          ON event.tenant_id=role.tenant_id AND event.role_id=role.role_id
        WHERE role.tenant_id=? AND role.role_id=?
          AND event.request_id='req_prd066_legacy_revoke_chat_use'
        """,
        (developer["tenant_id"], legacy_role_id),
    ).fetchone()
    connection.close()
    assert persisted["permission_revision"] == developer["permission_revision"] + 1
    assert persisted["matrix_sha256"] == permission_matrix_sha256(requested_codes)
    assert json.loads(persisted["removed_permissions_json"]) == ["chat.use"]
    assert persisted["delivery_status"] == "dispatched"
    assert persisted["role_id"] == legacy_role_id


@pytest.mark.anyio
async def test_permission_patch_rejects_before_body_for_unauthorized_and_strict_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限与异常：401/403 在读正文前返回，管理员正文必须唯一、严格且完整。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_login = await _login(client, "admin")
        viewer_login = await _login(client, "viewer")
        assert admin_login.status_code == viewer_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
        viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}
        matrix = await client.get("/permissions", headers=admin_headers)
        developer = _matrix_role(matrix.json(), "developer")
        path = f"/roles/{developer['role_id']}/permissions"

        anonymous = await client.patch(
            path,
            content=b"\xff",
            headers={"Content-Type": "application/json"},
        )
        denied = await client.patch(
            path,
            content=b"\xff",
            headers={**viewer_headers, "Content-Type": "application/json"},
        )
        assert anonymous.status_code == 401
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"

        invalid_bodies = (
            b'{"expected_revision":1,"permission_codes":[],"extra":true}',
            b'{"expected_revision":"1","permission_codes":[]}',
            b'{"expected_revision":1,"expected_revision":1,"permission_codes":[]}',
            b'{"expected_revision":1,"permission_codes":["console.basic.read","console.basic.read"]}',
        )
        for index, body in enumerate(invalid_bodies):
            request_id = f"req_prd059_invalid_{index}"
            response = await client.patch(
                path,
                content=body,
                headers={
                    **admin_headers,
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Request-ID": request_id,
                },
            )
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "rbac.payload_invalid"
            assert response.json()["request_id"] == request_id
            assert body.decode("utf-8") not in response.text


@pytest.mark.anyio
async def test_permission_patch_hides_cross_tenant_role_and_enforces_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """租户与职责：跨租户 role_id 等同不存在，角色上限和 admin 锁定均由后端执行。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES(
          'tenant_prd059_other','prd059-other','PRD-059 Other',
          'internal_only','active','2026-07-31T06:00:00Z','2026-07-31T06:00:00Z'
        )
        """
    )
    other_role_id = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id='tenant_prd059_other' AND code='developer'"
    ).fetchone()[0]
    connection.commit()
    connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "admin")
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        matrix = (await client.get("/permissions", headers=headers)).json()
        finance = _matrix_role(matrix, "finance_admin")
        admin = _matrix_role(matrix, "admin")

        cross_tenant = await client.patch(
            f"/roles/{other_role_id}/permissions",
            json={"expected_revision": 1, "permission_codes": []},
            headers={**headers, "X-Request-ID": "req_prd059_cross_role"},
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["code"] == "rbac.role_not_found"

        assert "provider.write" not in finance["grantable_permission_codes"]
        outside_ceiling = await client.patch(
            f"/roles/{finance['role_id']}/permissions",
            json={
                "expected_revision": finance["permission_revision"],
                "permission_codes": sorted(
                    set(finance["permission_codes"]) | {"provider.write"}
                ),
            },
            headers={**headers, "X-Request-ID": "req_prd059_ceiling"},
        )
        assert outside_ceiling.status_code == 422
        assert outside_ceiling.json()["code"] == "rbac.permission_not_grantable"

        immutable = await client.patch(
            f"/roles/{admin['role_id']}/permissions",
            json={
                "expected_revision": admin["permission_revision"],
                "permission_codes": admin["permission_codes"],
            },
            headers={**headers, "X-Request-ID": "req_prd059_admin_immutable"},
        )
        assert immutable.status_code == 409
        assert immutable.json()["code"] == "rbac.role_immutable"

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='role.permission_changed'"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM role_permission_events").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_permission_patch_audit_failure_rolls_back_matrix_and_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务异常：审计写入失败必须回滚 RolePermission、revision、hash 和 source event。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await _login(client, "admin")
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        matrix = (await client.get("/permissions", headers=headers)).json()
        developer = _matrix_role(matrix, "developer")
        before_codes = list(developer["permission_codes"])
        requested_codes = sorted(set(before_codes) - {"debug_console.use"})

        connection = connect_database(database_url)
        before_count = connection.execute(
            "SELECT COUNT(*) FROM role_permissions WHERE role_id=?",
            (developer["role_id"],),
        ).fetchone()[0]
        connection.execute(
            """
            CREATE TRIGGER trg_prd059_test_audit_failure
            BEFORE INSERT ON audit_logs FOR EACH ROW
            WHEN NEW.action='role.permission_changed'
            BEGIN SELECT RAISE(ABORT,'prd059_test_audit_failure'); END
            """
        )
        connection.commit()
        connection.close()

        failed = await client.patch(
            f"/roles/{developer['role_id']}/permissions",
            json={
                "expected_revision": developer["permission_revision"],
                "permission_codes": requested_codes,
            },
            headers={**headers, "X-Request-ID": "req_prd059_audit_failure"},
        )
        assert failed.status_code == 503
        assert failed.json()["code"] == "rbac.unavailable"
        assert failed.json()["request_id"] == "req_prd059_audit_failure"
        assert "prd059_test_audit_failure" not in failed.text

    connection = connect_database(database_url)
    role = connection.execute(
        "SELECT permission_revision,matrix_sha256 FROM roles WHERE role_id=?",
        (developer["role_id"],),
    ).fetchone()
    assert role["permission_revision"] == developer["permission_revision"]
    assert role["matrix_sha256"] == developer["matrix_sha256"]
    assert connection.execute(
        "SELECT COUNT(*) FROM role_permissions WHERE role_id=?",
        (developer["role_id"],),
    ).fetchone()[0] == before_count
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='role.permission_changed'"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM role_permission_events").fetchone()[0] == 0
    connection.close()
