"""PRD-034 管理操作审计日志集成测试。

覆盖成功管理变更、租户隔离、后端权限、敏感字段脱敏、数据库不可变约束和审计失败回滚。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.audit.routes import router as audit_router
from metahub_control.audit.service import AuditRecord, commit_audited_transaction, dispatch_audit_recorded
from metahub_control.auth.routes import router as auth_router
from metahub_control.budgets.routes import router as budgets_router
from metahub_control.dlp.routes import router as dlp_router
from metahub_control.providers.routes import provider_keys_router, router as providers_router
from metahub_control.virtual_keys.routes import router as virtual_keys_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.errors import ApiError

# 固定测试凭据仅用于临时数据库，任何断言和日志都不得输出访问令牌或真实密钥。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把临时路径转换为项目数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只包含本 PRD 验收链路的控制面应用。"""

    app = create_service_app("control-plane-api", "Audit Test Control API")
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(providers_router)
    app.include_router(provider_keys_router)
    app.include_router(virtual_keys_router)
    app.include_router(budgets_router)
    app.include_router(dlp_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """初始化租户、管理员和两个无审计读取权限的用户。"""

    database_url = _sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "audit-test-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', '2026-07-13T00:00:00Z', '2026-07-13T00:00:00Z')
        """,
        [
            (
                "user_audit_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_audit_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 返回数据库地址与租户 ID，后续用例必须继续用 tenant_id 证明隔离边界。
    return database_url, seed.tenant_id


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户并仅返回内存中的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只保留在当前测试进程内存中，不写快照或错误输出。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_management_changes_are_audited_and_sensitive_values_are_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：两类 Key、预算和 DLP 变更均生成可查询、可追踪且脱敏的审计事实。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    provider_key_id = connection.execute(
        "SELECT provider_credential_id FROM provider_credentials WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["provider_credential_id"]
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["provider_id"]
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["virtual_key_id"]
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()["environment_id"]

    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "MetaHub-Audit-Test/<script>alert(1)</script>",
            "X-Forwarded-For": "203.0.113.99",
        }
        key_change = await client.patch(
            f"/provider-keys/{provider_key_id}", json={"status": "disabled"}, headers=headers
        )
        provider_change = await client.patch(
            f"/providers/{provider_id}",
            json={"name": "本地 Provider（已审计）", "description": "provider-description-secret"},
            headers=headers,
        )
        virtual_key_change = await client.patch(
            f"/virtual-keys/{virtual_key_id}",
            json={"status": "disabled", "description": "virtual-key-description-secret"},
            headers=headers,
        )
        virtual_key_limit_change = await client.patch(
            f"/virtual-keys/{virtual_key_id}/limits",
            json={"rpm_limit": 60, "concurrency_limit": 3},
            headers=headers,
        )
        budget_change = await client.patch(
            "/budgets",
            json={
                "scope_type": "tenant",
                "environment_id": environment_id,
                "name": "租户日预算",
                "daily_budget_cny": "100",
                "exhausted_action": "block",
            },
            headers=headers,
        )
        dlp_created = await client.post(
            "/dlp/rules",
            json={
                "name": "身份证规则",
                "rule_type": "keyword",
                "pattern": "raw-secret-pattern-one",
                "action": "block",
            },
            headers=headers,
        )
        assert dlp_created.status_code == 200, dlp_created.text
        dlp_rule_id = dlp_created.json()["dlp_rule"]["dlp_rule_id"]
        dlp_changed = await client.patch(
            f"/dlp/rules/{dlp_rule_id}",
            json={
                "pattern": "raw-secret-pattern-two",
                "status": "disabled",
                "description": "dlp-description-secret",
            },
            headers=headers,
        )
        listed = await client.get("/audit-logs?limit=100", headers=headers)

    for response in (
        provider_change,
        key_change,
        virtual_key_change,
        virtual_key_limit_change,
        budget_change,
        dlp_changed,
        listed,
    ):
        assert response.status_code == 200, response.text
    body = listed.json()
    actions = [item["action"] for item in body["audit_logs"]]
    assert {
        "provider_key.updated",
        "provider.updated",
        "virtual_key.updated",
        "budget_rule.created",
        "dlp_rule.created",
        "dlp_rule.updated",
    }.issubset(actions)
    assert body["request_id"]
    assert body["trace_id"]

    serialized = json.dumps(body, ensure_ascii=False)
    assert "raw-secret-pattern-one" not in serialized
    assert "raw-secret-pattern-two" not in serialized
    assert "encrypted_key" not in serialized
    assert "key_hash" not in serialized
    assert "plain_value" not in serialized
    assert "provider-description-secret" not in serialized
    assert "virtual-key-description-secret" not in serialized
    assert "dlp-description-secret" not in serialized
    provider_key_audit = next(item for item in body["audit_logs"] if item["action"] == "provider_key.updated")
    assert provider_key_audit["changed_fields"] == ["status"]
    assert set(provider_key_audit["after_summary"]) == {"provider_id", "name", "key_prefix", "weight", "status"}
    assert provider_key_audit["request_id"] == key_change.json()["request_id"]
    assert provider_key_audit["client_ip"] == "127.0.0.1"
    assert provider_key_audit["user_agent"] == "MetaHub-Audit-Test/<script>alert(1)</script>"
    dlp_audit = next(item for item in body["audit_logs"] if item["action"] == "dlp_rule.updated")
    assert "pattern_hash" in dlp_audit["after_summary"]
    assert "pattern" not in dlp_audit["after_summary"]
    assert "description_hash" in dlp_audit["after_summary"]
    assert any(
        set(item["changed_fields"]) == {"concurrency_limit", "rate_limit_status", "rpm_limit"}
        for item in body["audit_logs"]
        if item["action"] == "virtual_key.updated"
    )


@pytest.mark.anyio
async def test_audit_query_is_admin_only_and_tenant_isolation_cannot_be_bypassed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/隔离：developer、viewer 和跨租户对象筛选都不能获得审计内容。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    # 第二租户审计行只用于证明查询始终从当前 token 的 tenant_id 开始，不接受客户端 tenant_id。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('tenant_audit_other', 'audit-other', 'Other', 'internal_only', 'active', ?, ?)
        """,
        ("2026-07-13T00:00:00Z", "2026-07-13T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO users(user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at)
        VALUES('user_audit_other', 'tenant_audit_other', 'admin@other.local', 'Other Admin', ?, 'admin', 'active', ?, ?)
        """,
        (hash_password("OtherPassword!2026"), "2026-07-13T00:00:00Z", "2026-07-13T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action,
          object_type, object_id, object_name, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        ) VALUES(
          'aud_other', 'tenant_audit_other', 'user_audit_other', 'admin@other.local', 'admin',
          'provider.updated', 'provider', 'provider_other', 'Other', '{}', '{"status":"disabled"}',
          '["status"]', 'req_other', 'trc_other', '2026-07-13T00:00:00Z'
        )
        """
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action,
              object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            ) VALUES('aud_cross_owner', ?, 'user_audit_other', 'admin@other.local', 'admin',
              'provider.updated', 'provider', 'provider_cross', '{}', '{"status":"disabled"}',
              '["status"]', 'req_cross', 'trc_cross', '2026-07-13T00:00:00Z')
            """,
            (tenant_id,),
        )
    connection.rollback()

    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_denied = await client.get(
            "/audit-logs", headers={"Authorization": f"Bearer {developer}"}
        )
        viewer_denied = await client.get("/audit-logs", headers={"Authorization": f"Bearer {viewer}"})
        cross_tenant = await client.get(
            "/audit-logs?object_id=provider_other&tenant_id=tenant_audit_other",
            headers={"Authorization": f"Bearer {admin}"},
        )
        delete_not_supported = await client.delete(
            "/audit-logs/aud_other", headers={"Authorization": f"Bearer {admin}"}
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert developer_denied.json()["request_id"]
    assert viewer_denied.status_code == 403
    assert cross_tenant.status_code == 200
    assert cross_tenant.json()["audit_logs"] == []
    assert cross_tenant.json()["total"] == 0
    assert delete_not_supported.status_code in {404, 405}
    assert tenant_id != "tenant_audit_other"


@pytest.mark.anyio
async def test_audit_insert_failure_rolls_back_business_change_and_rows_are_immutable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：审计写入失败回滚 Key 状态，已提交审计行不能更新或删除。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    virtual_key_id = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["virtual_key_id"]
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()["environment_id"]
    connection.execute(
        """
        CREATE TRIGGER audit_test_reject_insert
        BEFORE INSERT ON audit_logs
        BEGIN
          SELECT RAISE(ABORT, 'audit_test_unavailable');
        END
        """
    )
    connection.commit()

    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        failed = await client.patch(
            f"/virtual-keys/{virtual_key_id}",
            json={"status": "disabled"},
            headers={"Authorization": f"Bearer {token}"},
        )
        budget_failed = await client.patch(
            "/budgets",
            json={
                "scope_type": "tenant",
                "environment_id": environment_id,
                "name": "Should Roll Back",
                "daily_budget_cny": "50",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        dlp_failed = await client.post(
            "/dlp/rules",
            json={
                "name": "Should Roll Back",
                "rule_type": "keyword",
                "pattern": "must-not-persist",
                "action": "block",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert failed.status_code == 503
    assert failed.json()["code"] == "audit_log.record_unavailable"
    assert failed.json()["request_id"]
    assert budget_failed.status_code == 503
    assert budget_failed.json()["code"] == "audit_log.record_unavailable"
    assert dlp_failed.status_code == 503
    assert dlp_failed.json()["code"] == "audit_log.record_unavailable"
    status = connection.execute(
        "SELECT status FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        (tenant_id, virtual_key_id),
    ).fetchone()["status"]
    assert status == "active"
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM budget_rules WHERE tenant_id=? AND name='Should Roll Back'",
        (tenant_id,),
    ).fetchone()["count"] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM dlp_rules WHERE tenant_id=? AND name='Should Roll Back'",
        (tenant_id,),
    ).fetchone()["count"] == 0

    connection.execute("DROP TRIGGER audit_test_reject_insert")
    actor = connection.execute(
        "SELECT user_id, email, role FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (tenant_id,),
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action,
              object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            ) VALUES('aud_bad_json_shape', ?, ?, ?, ?, 'provider.updated', 'provider', 'provider_bad_json',
              '[]', '{"status":"disabled"}', '["status"]', 'req_bad_json', 'trc_bad_json', ?)
            """,
            (tenant_id, actor["user_id"], actor["email"], actor["role"], "2026-07-13T00:00:00Z"),
        )
    connection.rollback()
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role, action,
          object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        ) VALUES('aud_immutable', ?, ?, ?, ?, 'provider.updated', 'provider', 'provider_immutable',
          '{}', '{"status":"disabled"}', '["status"]', 'req_immutable', 'trc_immutable', ?)
        """,
        (tenant_id, actor["user_id"], actor["email"], actor["role"], "2026-07-13T00:00:00Z"),
    )
    connection.commit()
    with pytest.raises(Exception, match="audit_log_immutable"):
        connection.execute("UPDATE audit_logs SET action='provider.created' WHERE audit_log_id='aud_immutable'")
    connection.rollback()
    with pytest.raises(Exception, match="audit_log_immutable"):
        connection.execute("DELETE FROM audit_logs WHERE audit_log_id='aud_immutable'")
    connection.rollback()


@pytest.mark.anyio
async def test_audit_query_rejects_invalid_filters_and_time_range(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """边界流程：未知对象、逆序时间和越界分页均返回统一错误码与 request_id。"""

    _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        unknown_object = await client.get("/audit-logs?object_type=unknown", headers=headers)
        unknown_action = await client.get("/audit-logs?action=unknown.action", headers=headers)
        bad_time = await client.get(
            "/audit-logs?created_from=2026-07-14T00:00:00Z&created_to=2026-07-13T00:00:00Z",
            headers=headers,
        )
        bad_limit = await client.get("/audit-logs?limit=101", headers=headers)

    assert unknown_object.status_code == 422
    assert unknown_object.json()["code"] == "audit_log.filter_invalid"
    assert unknown_action.status_code == 422
    assert unknown_action.json()["code"] == "audit_log.filter_invalid"
    assert bad_time.status_code == 422
    assert bad_time.json()["code"] == "audit_log.time_range_invalid"
    assert bad_limit.status_code == 422
    assert bad_limit.json()["code"] == "audit_log.pagination_invalid"
    assert all(response.json()["request_id"] for response in (unknown_object, unknown_action, bad_time, bad_limit))


def test_audited_commit_failure_uses_retryable_unified_error() -> None:
    """数据库提交故障不能降级为 infra.unhandled，必须返回可重试审计错误。"""

    class FailingConnection:
        """最小 commit 替身，用于稳定注入 SQLite 提交异常。"""

        def commit(self) -> None:
            """模拟磁盘或锁故障导致事务无法提交。"""

            # 明确抛出 SQLite 异常，验证统一映射而不是普通 RuntimeError 分支。
            raise sqlite3.OperationalError("synthetic commit failure")

    with pytest.raises(ApiError) as exc:
        commit_audited_transaction(FailingConnection())  # type: ignore[arg-type]
    assert exc.value.code == "audit_log.record_unavailable"


def test_audit_recorded_webhook_uses_minimal_safe_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事件流程：audit.recorded 只投递最小定位字段，Endpoint 异常不回滚也不向调用方抛出。"""

    _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    captured: list[object] = []

    def capture_event(_config, tenant_id, event, *, request_id, trace_id):
        """捕获事件信封供断言，不执行真实网络请求。"""

        captured.append((tenant_id, event, request_id, trace_id))
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_event)
    record = AuditRecord(
        audit_log_id="aud_webhook",
        tenant_id="tenant_webhook",
        actor_user_id="user_webhook",
        action="provider_key.updated",
        object_type="provider_key",
        object_id="provider_key_webhook",
        changed_fields=("status",),
        trace_id="trc_webhook",
        created_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    app = _build_app()
    dispatch_audit_recorded(
        app.state.config,
        object(),
        record,
        request_id="req_webhook",
        trace_id="trc_webhook",
    )

    tenant_id, event, request_id, trace_id = captured[0]
    assert tenant_id == "tenant_webhook"
    assert event.event_type == "audit.recorded"
    assert event.event_id == "evt_aud_webhook"
    assert event.payload == {
        "audit_log_id": "aud_webhook",
        "action": "provider_key.updated",
        "object_type": "provider_key",
        "object_id": "provider_key_webhook",
        "changed_fields": ["status"],
    }
    assert "before_summary" not in event.payload
    assert "after_summary" not in event.payload
    assert request_id == "req_webhook"
    assert trace_id == "trc_webhook"

    def fail_dispatch(*_args, **_kwargs):
        """模拟 Endpoint 与投递记录同时不可用，业务调用仍不得失败。"""

        # 该异常只能被提交后投递边界吞掉，不能影响已提交管理操作。
        raise RuntimeError("synthetic webhook failure")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    dispatch_audit_recorded(
        app.state.config,
        object(),
        record,
        request_id="req_webhook_failed",
        trace_id="trc_webhook_failed",
    )
