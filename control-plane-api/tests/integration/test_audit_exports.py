"""PRD-058 审计日志不可删除与 CSV 导出集成测试。

覆盖稳定快照、幂等重放、租户隔离、后端权限、公式注入、坏存量失败关闭以及
`audit.exported` source outbox 的 lease 恢复。测试数据和 SQLite 文件仅位于 pytest 的
``D:/MetaHub/.tmp`` basetemp，不写入系统临时目录。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.audit import export_scheduler, export_service
from metahub_control.audit.export_outbox import (
    claim_next_audit_export_event,
    dispatch_claimed_audit_export_event,
)
from metahub_control.audit.routes import router as audit_router
from metahub_control.auth.routes import router as auth_router
from metahub_control.providers.routes import provider_keys_router
from metahub_control.providers.routes import router as providers_router
from metahub_control.webhooks import service as webhook_service
from metahub_control.webhooks.routes import router as webhooks_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 固定凭据只服务于临时 SQLite 租户，任何断言和证据都不得输出登录 Token。
ADMIN_PASSWORD = "LocalAdmin!2026"
SECOND_ADMIN_PASSWORD = "SecondAdminLocal!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换为应用可识别的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证、Provider 写入和审计导出的定向 Control 应用。"""

    app = create_service_app("control-plane-api", "Audit Export Test Control API")
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(providers_router)
    app.include_router(provider_keys_router)
    app.include_router(webhooks_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """初始化完整迁移、默认租户和两种无导出权限角色。"""

    database_url = _sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "audit-export-test-auth-secret")
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "audit-export-test-encryption-secret",
    )
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z')
        """,
        [
            (
                "user_prd058_developer",
                seed.tenant_id,
                "developer-prd058@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd058_viewer",
                seed.tenant_id,
                "viewer-prd058@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    connection.close()
    # 后续断言继续携带 tenant_id，防止只验证功能而遗漏租户边界。
    return database_url, seed.tenant_id


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试用户；Token 只保留在当前测试进程内存中。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只留在测试进程内存，不写入快照、日志或测试证据文件。
    return response.json()["access_token"]


def _csv_rows(content: bytes) -> list[list[str]]:
    """按 UTF-8 BOM 与标准 CSV 规则解析测试响应，不用字符串切割模拟解析器。"""

    assert content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in content
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline="")))


# 该 helper 必须走真实 Provider API，避免用直接 SQL 伪造“同事务审计已接入”的结论。
async def _write_provider_audit(
    client: AsyncClient,
    database_url: str,
    tenant_id: str,
    token: str,
    *,
    name: str = "PRD058 已审计 Provider",
    user_agent: str = "MetaHub-PRD058-Test",
) -> None:
    """通过真实管理 API 写入同事务 Provider AuditLog，验证接入而非伪造数据。"""

    connection = connect_database(database_url)
    provider_id = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant_id,),
    ).fetchone()["provider_id"]
    connection.close()
    response = await client.patch(
        f"/providers/{provider_id}",
        json={"name": name},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Request-ID": "req_prd058_provider_change",
            "User-Agent": user_agent,
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_audit_export_main_flow_snapshot_replay_and_self_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：固定 CSV、不可变复核事实、同字节重放和下一次可见自身审计同时成立。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        await _write_provider_audit(client, database_url, tenant_id, token)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": "req_prd058_export_main",
        }
        exported = await client.get("/audit-logs/export", headers=headers)
        replayed = await client.get("/audit-logs/export", headers=headers)
        conflict = await client.get("/audit-logs/export?action=provider.updated", headers=headers)
        next_export = await client.get(
            "/audit-logs/export?action=audit.exported",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_export_next",
            },
        )

    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "text/csv; charset=utf-8"
    assert exported.headers["cache-control"] == "no-store, private"
    assert exported.headers["pragma"] == "no-cache"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["etag"].startswith('"') and exported.headers["etag"].endswith('"')
    export_id = exported.headers["x-metahub-audit-export-id"]
    assert export_id.startswith("aex_") and len(export_id) == 36
    rows = _csv_rows(exported.content)
    assert len(rows[0]) == 18
    assert rows[0][0] == "audit_log_id"
    assert rows[0][-1] == "schema_version"
    assert all(row[1] == tenant_id for row in rows[1:])
    assert "audit.exported" not in {row[5] for row in rows[1:]}
    assert int(exported.headers["x-metahub-audit-export-row-count"]) == len(rows) - 1
    assert replayed.status_code == 200
    assert replayed.content == exported.content
    assert replayed.headers["x-metahub-audit-export-id"] == export_id
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "audit_export.request_conflict"
    assert conflict.json()["request_id"] == "req_prd058_export_main"

    next_rows = _csv_rows(next_export.content)
    assert next_export.status_code == 200
    assert [row[5] for row in next_rows[1:]] == ["audit.exported"]
    assert next_rows[1][7] == export_id
    connection = connect_database(database_url)
    export_row = connection.execute(
        "SELECT * FROM audit_exports WHERE tenant_id=? AND audit_export_id=?",
        (tenant_id, export_id),
    ).fetchone()
    assert export_row["row_count"] == len(rows) - 1
    assert export_row["content_size_bytes"] == len(exported.content)
    assert exported.headers["etag"] == f'"{export_row["content_sha256"]}"'
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM audit_exports WHERE tenant_id=? AND requested_by=? AND request_id=?",
        (tenant_id, export_row["requested_by"], "req_prd058_export_main"),
    ).fetchone()["count"] == 1
    self_sequence = connection.execute(
        "SELECT audit_log_sequence FROM audit_log_export_sequences WHERE tenant_id=? AND audit_log_id=?",
        (tenant_id, export_row["audit_log_id"]),
    ).fetchone()["audit_log_sequence"]
    assert self_sequence > export_row["snapshot_sequence"]
    assert connection.execute(
        "SELECT status FROM audit_export_events WHERE audit_export_event_id=?",
        (export_row["event_id"],),
    ).fetchone()["status"] == "pending"
    connection.close()


@pytest.mark.anyio
async def test_audit_export_snapshot_preserves_historical_offset_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """兼容历史 +08:00 AuditLog：快照保留原文，CSV 规范为同一时刻的 UTC。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    historical_audit_log_id = "aud_88888888888888888888888888888888"
    historical_created_at = "2026-07-31T08:30:00+08:00"
    connection = connect_database(database_url)
    actor = connection.execute(
        "SELECT user_id,email,role FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (tenant_id,),
    ).fetchone()
    assert actor is not None
    # 存量审计允许使用带偏移的合法 ISO-8601；插入触发器会在同事务生成稳定 sequence。
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.updated','provider','provider_timezone','历史时区 Provider',
          '{}','{"status":"disabled"}','["status"]',NULL,NULL,
          'req_prd058_historical_timezone','trc_prd058_historical_timezone',?)
        """,
        (
            historical_audit_log_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            historical_created_at,
        ),
    )
    snapshot_sequence = connection.execute(
        """
        SELECT audit_log_sequence
        FROM audit_log_export_sequences
        WHERE tenant_id=? AND audit_log_id=?
        """,
        (tenant_id, historical_audit_log_id),
    ).fetchone()["audit_log_sequence"]
    connection.commit()
    connection.close()

    headers = {
        "Authorization": "",
        "X-Request-ID": "req_prd058_export_historical_timezone",
        "X-Trace-ID": "trc_prd058_export_historical_timezone",
    }
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers["Authorization"] = f"Bearer {token}"
        exported = await client.get(
            "/audit-logs/export?object_id=provider_timezone",
            headers=headers,
        )
        replayed = await client.get(
            "/audit-logs/export?object_id=provider_timezone",
            headers=headers,
        )

    assert exported.status_code == 200, exported.text
    assert replayed.status_code == 200, replayed.text
    assert replayed.content == exported.content
    assert replayed.headers["x-metahub-audit-export-id"] == exported.headers[
        "x-metahub-audit-export-id"
    ]
    rows = _csv_rows(exported.content)
    assert len(rows) == 2
    assert rows[1][0] == historical_audit_log_id
    # CSV 对外合同统一 UTC；快照列则必须逐字保留历史 AuditLog 的 +08:00 文本。
    assert rows[1][16] == "2026-07-31T00:30:00Z"

    connection = connect_database(database_url)
    export_row = connection.execute(
        """
        SELECT snapshot_sequence,snapshot_audit_log_id,snapshot_created_at
        FROM audit_exports
        WHERE tenant_id=? AND audit_export_id=?
        """,
        (tenant_id, exported.headers["x-metahub-audit-export-id"]),
    ).fetchone()
    stored_created_at = connection.execute(
        "SELECT created_at FROM audit_logs WHERE tenant_id=? AND audit_log_id=?",
        (tenant_id, historical_audit_log_id),
    ).fetchone()["created_at"]
    connection.close()
    assert export_row is not None
    assert export_row["snapshot_sequence"] == snapshot_sequence
    assert export_row["snapshot_audit_log_id"] == historical_audit_log_id
    assert export_row["snapshot_created_at"] == historical_created_at
    assert stored_created_at == historical_created_at


@pytest.mark.anyio
async def test_audit_export_same_request_id_is_actor_scoped_and_survives_vacuum(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """幂等边界：第二位管理员同 request_id 独立建档，原管理员 VACUUM 后仍同字节重放。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(
          'user_prd058_second_admin',?,'second-admin-prd058@example.com','Second Admin',
          ?,'admin','active','2026-07-31T00:00:00Z','2026-07-31T00:00:00Z'
        )
        """,
        (tenant_id, hash_password(SECOND_ADMIN_PASSWORD)),
    )
    connection.commit()
    connection.close()

    operation_id = "req_prd058_actor_scoped_idempotency"
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        first_admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        second_admin = await _login(
            client,
            "second-admin-prd058@example.com",
            SECOND_ADMIN_PASSWORD,
        )
        await _write_provider_audit(client, database_url, tenant_id, first_admin)
        first = await client.get(
            "/audit-logs/export?action=provider.updated",
            headers={"Authorization": f"Bearer {first_admin}", "X-Request-ID": operation_id},
        )
        second = await client.get(
            "/audit-logs/export?action=provider.updated",
            headers={"Authorization": f"Bearer {second_admin}", "X-Request-ID": operation_id},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.content == first.content
    assert second.headers["x-metahub-audit-export-id"] != first.headers[
        "x-metahub-audit-export-id"
    ]

    # VACUUM 会重排普通 rowid；重放必须只依赖 0049 的稳定 AUTOINCREMENT sequence。
    connection = connect_database(database_url)
    connection.execute("VACUUM")
    exports = connection.execute(
        """
        SELECT requested_by,audit_export_id,audit_log_id,event_id
        FROM audit_exports
        WHERE tenant_id=? AND request_id=?
        ORDER BY requested_by
        """,
        (tenant_id, operation_id),
    ).fetchall()
    connection.close()
    assert len(exports) == 2
    assert len({row["requested_by"] for row in exports}) == 2
    assert len({row["audit_export_id"] for row in exports}) == 2
    assert len({row["audit_log_id"] for row in exports}) == 2
    assert len({row["event_id"] for row in exports}) == 2

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        replayed = await client.get(
            "/audit-logs/export?action=provider.updated",
            headers={"Authorization": f"Bearer {first_admin}", "X-Request-ID": operation_id},
        )
    assert replayed.status_code == 200, replayed.text
    assert replayed.content == first.content
    assert replayed.headers["x-metahub-audit-export-id"] == first.headers[
        "x-metahub-audit-export-id"
    ]


@pytest.mark.anyio
async def test_audit_export_permissions_strict_query_and_tenant_isolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/隔离：后端 admin 门禁先执行，未知/重复 query 失败且另一租户行不进入 CSV。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_prd058_other','prd058-other','Other','internal_only','active',?,?)
        """,
        ("2026-07-31T00:00:00Z", "2026-07-31T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at)
        VALUES('user_prd058_other','tenant_prd058_other','other@example.com','Other',?,'admin','active',?,?)
        """,
        (hash_password("OtherLocal!2026"), "2026-07-31T00:00:00Z", "2026-07-31T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.updated','provider','provider_other','Other','{}',
          '{"status":"disabled"}','["status"]',NULL,NULL,'req_other','trc_other',?)
        """,
        (
            "aud_11111111111111111111111111111111",
            "tenant_prd058_other",
            "user_prd058_other",
            "other@example.com",
            "admin",
            "2026-07-31T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer-prd058@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer-prd058@example.com", VIEWER_PASSWORD)
        developer_denied = await client.get(
            "/audit-logs/export?tenant_id=tenant_prd058_other",
            headers={"Authorization": f"Bearer {developer}"},
        )
        viewer_denied = await client.get(
            "/audit-logs/export", headers={"Authorization": f"Bearer {viewer}"}
        )
        anonymous = await client.get("/audit-logs/export")
        unknown = await client.get(
            "/audit-logs/export?tenant_id=tenant_prd058_other",
            headers={"Authorization": f"Bearer {admin}"},
        )
        duplicate = await client.get(
            "/audit-logs/export?action=provider.updated&action=provider.updated",
            headers={"Authorization": f"Bearer {admin}"},
        )
        exported = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd058_isolation",
            },
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert viewer_denied.status_code == 403
    assert anonymous.status_code == 401
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "audit_export.filter_invalid"
    assert duplicate.status_code == 422
    assert duplicate.json()["code"] == "audit_export.filter_invalid"
    rows = _csv_rows(exported.content)
    assert all(row[1] == tenant_id for row in rows[1:])
    assert "tenant_prd058_other" not in exported.text
    assert "other@example.com" not in exported.text


@pytest.mark.anyio
async def test_audit_export_formula_protection_and_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全/空集：危险前缀只作为文本，零行文件仍提交完整导出、审计和 source 事实。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        await _write_provider_audit(
            client,
            database_url,
            tenant_id,
            token,
            name="=SUM(1,1)",
            user_agent="@cmd",
        )
        protected = await client.get(
            "/audit-logs/export?action=provider.updated",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_formula",
            },
        )
        empty = await client.get(
            "/audit-logs/export?object_id=provider_missing",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_empty",
            },
        )

    protected_rows = _csv_rows(protected.content)
    assert len(protected_rows) == 2
    assert protected_rows[1][8].startswith("'")
    assert protected_rows[1][13].startswith("'")
    for cell in protected_rows[1]:
        assert not cell.lstrip().startswith(("=", "+", "-", "@"))
    empty_rows = _csv_rows(empty.content)
    assert len(empty_rows) == 1
    assert empty.headers["x-metahub-audit-export-row-count"] == "0"
    connection = connect_database(database_url)
    empty_id = empty.headers["x-metahub-audit-export-id"]
    assert connection.execute(
        "SELECT row_count FROM audit_exports WHERE tenant_id=? AND audit_export_id=?",
        (tenant_id, empty_id),
    ).fetchone()["row_count"] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE tenant_id=? AND action='audit.exported' AND object_id=?",
        (tenant_id, empty_id),
    ).fetchone()["count"] == 1
    connection.close()


@pytest.mark.anyio
async def test_audit_export_bad_stored_row_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：伪造 changed_fields 不释放 CSV，也不留下 AuditExport。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    actor = connection.execute(
        "SELECT user_id,email,role FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (tenant_id,),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.updated','provider','provider_corrupt','Corrupt','{}',
          '{"status":"disabled"}','[]',NULL,NULL,'req_corrupt','trc_corrupt',?)
        """,
        (
            "aud_22222222222222222222222222222222",
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "2026-07-31T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        integrity_failed = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_integrity",
            },
        )

    assert integrity_failed.status_code == 500
    assert integrity_failed.json()["code"] == "audit_export.integrity_failed"
    assert integrity_failed.json()["request_id"] == "req_prd058_integrity"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM audit_exports WHERE tenant_id=? AND request_id=?",
        (tenant_id, "req_prd058_integrity"),
    ).fetchone()["count"] == 0
    connection.close()


@pytest.mark.anyio
async def test_audit_export_size_limits_release_no_partial_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """规模门禁：行数或真实 CSV 字节超限均返回 413，且不释放任何部分事实。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        await _write_provider_audit(client, database_url, tenant_id, token)
        # 将专项上限收紧到零，只验证同一逐行门禁和回滚路径，不制造一万条无关测试数据。
        monkeypatch.setattr(export_service, "MAX_EXPORT_ROWS", 0)
        too_large = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_too_large",
            },
        )
        # 恢复行数上限后把字节门禁收紧到表头以下，独立验证 BOM/表头也计入 16 MiB 合同。
        monkeypatch.setattr(export_service, "MAX_EXPORT_ROWS", 10_000)
        monkeypatch.setattr(export_service, "MAX_EXPORT_BYTES", 1)
        bytes_too_large = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_bytes_too_large",
            },
        )

    assert too_large.status_code == 413
    assert too_large.json()["code"] == "audit_export.too_large"
    assert too_large.json()["request_id"] == "req_prd058_too_large"
    assert bytes_too_large.status_code == 413
    assert bytes_too_large.json()["code"] == "audit_export.too_large"
    assert bytes_too_large.json()["request_id"] == "req_prd058_bytes_too_large"
    connection = connect_database(database_url)
    assert connection.execute(
        """
        SELECT COUNT(*) AS count FROM audit_exports
        WHERE tenant_id=? AND request_id IN (?, ?)
        """,
        (tenant_id, "req_prd058_too_large", "req_prd058_bytes_too_large"),
    ).fetchone()["count"] == 0
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE tenant_id=? AND action='audit.exported'",
        (tenant_id,),
    ).fetchone()["count"] == 0
    connection.close()


@pytest.mark.anyio
async def test_audit_export_source_outbox_lease_success_and_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事件流程：只有 lease owner 使用恢复模式，成功终结；失败按五秒退避且不改 Export。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=_build_app()), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        await _write_provider_audit(client, database_url, tenant_id, token)
        first = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_outbox_success",
            },
        )
        second = await client.get(
            "/audit-logs/export?action=provider.updated",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_outbox_retry",
            },
        )
    assert first.status_code == 200 and second.status_code == 200
    claimed_at = datetime.now(UTC) + timedelta(seconds=2)
    success_claim = claim_next_audit_export_event(
        _build_app().state.config,
        worker_id="worker.prd058.success",
        now=claimed_at,
    )
    assert success_claim is not None
    captured: list[tuple[object, ...]] = []

    def capture_dispatch(config, tenant, event, *, request_id, trace_id, resume_existing_pending=False):
        """捕获 lease worker 调用，不执行真实网络请求。"""

        captured.append((tenant, event, request_id, trace_id, resume_existing_pending))
        return []

    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_dispatch)
    assert dispatch_claimed_audit_export_event(
        _build_app().state.config,
        success_claim,
        now=success_claim.lease_until - timedelta(seconds=5),
    )
    assert captured and captured[0][-1] is True
    assert captured[0][1].event_type == "audit.exported"

    retry_claim = claim_next_audit_export_event(
        _build_app().state.config,
        worker_id="worker.prd058.retry",
        now=claimed_at + timedelta(seconds=2),
    )
    assert retry_claim is not None

    def fail_dispatch(*_args, **_kwargs):
        """模拟 Endpoint/dispatcher 暂不可用，异常正文不得进入 source 状态。"""

        # 只抛统一错误，测试不能把 URL、响应正文或凭据写进 source 行。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    completed_at = retry_claim.lease_until - timedelta(seconds=5)
    assert dispatch_claimed_audit_export_event(
        _build_app().state.config,
        retry_claim,
        now=completed_at,
    )
    connection = connect_database(database_url)
    success = connection.execute(
        "SELECT status FROM audit_export_events WHERE audit_export_event_id=?",
        (success_claim.event_id,),
    ).fetchone()
    retry = connection.execute(
        """
        SELECT status,attempt_count,last_error_code,next_attempt_at
        FROM audit_export_events WHERE audit_export_event_id=?
        """,
        (retry_claim.event_id,),
    ).fetchone()
    assert success["status"] == "dispatched"
    assert retry["status"] == "pending"
    assert retry["attempt_count"] == 1
    assert retry["last_error_code"] == "webhook.delivery_unavailable"
    next_attempt = datetime.fromisoformat(retry["next_attempt_at"].replace("Z", "+00:00"))
    assert next_attempt == completed_at.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=5)
    connection.close()


@pytest.mark.anyio
async def test_audit_export_pending_delivery_restores_authoritative_request_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实恢复：权威 request_id 可续发，actor 错配的持久 intent 必须零出网失败关闭。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        endpoint_response = await client.post(
            "/webhooks",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_resume_endpoint",
            },
            json={
                "name": "PRD058 audit export resume",
                "endpoint_url": "http://127.0.0.1:65530/audit-exported",
                "event_types": ["audit.exported"],
                "timeout_ms": 1000,
            },
        )
        assert endpoint_response.status_code == 200, endpoint_response.text
        endpoint_id = endpoint_response.json()["webhook"]["webhook_endpoint_id"]
        first_export = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_resume_success",
            },
        )
        assert first_export.status_code == 200, first_export.text

    sent_bodies: list[dict[str, object]] = []

    def record_success(
        _config: object,
        _endpoint: object,
        body: bytes,
        _delivery_id: str,
    ) -> dict[str, object]:
        """只替换最终 HTTP 原语，保留权威回读、信封重建和投递事务。"""

        sent_bodies.append(json.loads(body.decode("utf-8")))
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    monkeypatch.setattr(webhook_service, "_send_once", record_success)
    first_claim = claim_next_audit_export_event(
        app.state.config,
        worker_id="worker.prd058.authoritative-success",
        now=datetime.now(UTC) + timedelta(seconds=2),
    )
    assert first_claim is not None
    assert dispatch_claimed_audit_export_event(
        app.state.config,
        first_claim,
        now=first_claim.lease_until - timedelta(seconds=5),
    )
    connection = connect_database(database_url)
    first_delivery = connection.execute(
        """
        SELECT delivery.status,delivery.attempt_count,delivery.payload_json,
               source.status AS source_status,export.request_id
        FROM audit_exports export
        JOIN audit_export_events source
          ON source.tenant_id=export.tenant_id AND source.audit_export_id=export.audit_export_id
        JOIN webhook_deliveries delivery
          ON delivery.tenant_id=source.tenant_id AND delivery.event_id=source.audit_export_event_id
        WHERE export.tenant_id=? AND export.request_id='req_prd058_resume_success'
        """,
        (tenant_id,),
    ).fetchone()
    first_attempt_count = connection.execute(
        """
        SELECT COUNT(*) FROM webhook_delivery_attempts attempt
        JOIN webhook_deliveries delivery
          ON delivery.webhook_delivery_id=attempt.webhook_delivery_id
        JOIN audit_exports export
          ON export.tenant_id=delivery.tenant_id AND export.event_id=delivery.event_id
        WHERE export.tenant_id=? AND export.request_id='req_prd058_resume_success'
        """,
        (tenant_id,),
    ).fetchone()[0]
    connection.close()
    assert first_delivery is not None
    assert tuple(first_delivery[key] for key in ("status", "attempt_count", "source_status")) == (
        "success",
        1,
        "dispatched",
    )
    assert first_delivery["request_id"] == "req_prd058_resume_success"
    assert first_attempt_count == 1
    assert sent_bodies == [json.loads(first_delivery["payload_json"])]
    assert "source_request_id" not in sent_bodies[0]
    assert "source_request_id" not in sent_bodies[0]["payload"]

    # 第二次真实导出后预置一条 actor 错配 intent；恢复只能依据不可变 AuditExport，
    # 不能因为 delivery 其余字段看似合法就使用 worker request_id 继续出网。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        second_export = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_resume_actor_mismatch",
            },
        )
        assert second_export.status_code == 200, second_export.text
    connection = connect_database(database_url)
    second = connection.execute(
        """
        SELECT audit_export_id,event_id,event_payload_json,trace_id,created_at
        FROM audit_exports
        WHERE tenant_id=? AND request_id='req_prd058_resume_actor_mismatch'
        """,
        (tenant_id,),
    ).fetchone()
    assert second is not None
    mismatched_envelope = {
        "event_id": second["event_id"],
        "event_type": "audit.exported",
        "timestamp": second["created_at"],
        "tenant_id": tenant_id,
        "actor_id": "user_prd058_developer",
        "object_id": second["audit_export_id"],
        "trace_id": second["trace_id"],
        "payload": json.loads(second["event_payload_json"]),
    }
    mismatch_delivery_id = f"whd_{'9' * 32}"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,secret_version,
          created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'pending',0,3,1,?,?)
        """,
        (
            mismatch_delivery_id,
            tenant_id,
            endpoint_id,
            second["event_id"],
            "audit.exported",
            second["created_at"],
            json.dumps(
                mismatched_envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            second["created_at"],
            second["created_at"],
        ),
    )
    connection.commit()
    connection.close()

    mismatch_claim = claim_next_audit_export_event(
        app.state.config,
        worker_id="worker.prd058.authoritative-mismatch",
        now=datetime.now(UTC) + timedelta(seconds=2),
    )
    assert mismatch_claim is not None
    assert dispatch_claimed_audit_export_event(
        app.state.config,
        mismatch_claim,
        now=mismatch_claim.lease_until - timedelta(seconds=5),
    )
    connection = connect_database(database_url)
    mismatch_source = connection.execute(
        """
        SELECT status,attempt_count,last_error_code,claim_token,claim_owner,lease_until
        FROM audit_export_events WHERE audit_export_event_id=? AND tenant_id=?
        """,
        (second["event_id"], tenant_id),
    ).fetchone()
    mismatch_delivery = connection.execute(
        """
        SELECT status,attempt_count FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_delivery_id=?
        """,
        (tenant_id, mismatch_delivery_id),
    ).fetchone()
    connection.close()
    assert tuple(mismatch_source) == (
        "pending",
        1,
        "webhook.delivery_unavailable",
        None,
        None,
        None,
    )
    assert tuple(mismatch_delivery) == ("pending", 0)
    assert len(sent_bodies) == 1


@pytest.mark.anyio
async def test_audit_export_expired_lease_takeover_and_late_cas_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复流程：过期 lease 可接管，旧 worker 的迟到 CAS 与再次出网都被拒绝。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        exported = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_takeover",
            },
        )
    assert exported.status_code == 200
    config = app.state.config
    claimed_at = datetime.now(UTC) + timedelta(seconds=2)
    first_claim = claim_next_audit_export_event(
        config,
        worker_id="worker.prd058.first",
        now=claimed_at,
    )
    assert first_claim is not None
    assert claim_next_audit_export_event(
        config,
        worker_id="worker.prd058.early",
        now=first_claim.lease_until - timedelta(seconds=1),
    ) is None

    from metahub_control.webhooks import service as webhook_service

    takeover_claims = []
    outbound_calls: list[bool] = []

    def takeover_during_dispatch(
        _config,
        _tenant,
        _event,
        *,
        request_id,
        trace_id,
        resume_existing_pending=False,
    ):
        """模拟旧 worker 出网后暂停到 lease 到期，第二 worker 在终态 CAS 前接管。"""

        assert request_id.startswith("req_aexevt_") and trace_id
        outbound_calls.append(resume_existing_pending)
        takeover_claims.append(
            claim_next_audit_export_event(
                config,
                worker_id="worker.prd058.takeover",
                now=first_claim.lease_until,
            )
        )
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", takeover_during_dispatch)
    # 预检时旧 lease 仍有效；dispatcher 返回前已被接管，因此旧 token 的成功 CAS 必须零命中。
    assert dispatch_claimed_audit_export_event(
        config,
        first_claim,
        now=first_claim.lease_until - timedelta(seconds=1),
    ) is False
    assert outbound_calls == [True]
    takeover_claim = takeover_claims[0]
    assert takeover_claim is not None
    assert takeover_claim.attempt_count == 2
    assert takeover_claim.claim_token != first_claim.claim_token
    assert takeover_claim.claim_owner != first_claim.claim_owner

    def capture_current_owner(
        _config,
        _tenant,
        _event,
        *,
        request_id,
        trace_id,
        resume_existing_pending=False,
    ):
        """记录当前 lease owner 的唯一恢复调用，不执行真实 Endpoint 网络请求。"""

        assert request_id.startswith("req_aexevt_") and trace_id
        outbound_calls.append(resume_existing_pending)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_current_owner)
    # 接管完成后旧 claim 连恢复模式都不能再次进入；调用次数必须保持不变。
    assert dispatch_claimed_audit_export_event(
        config,
        first_claim,
        now=first_claim.lease_until + timedelta(seconds=1),
    ) is False
    assert outbound_calls == [True]
    assert dispatch_claimed_audit_export_event(
        config,
        takeover_claim,
        now=takeover_claim.lease_until - timedelta(seconds=5),
    ) is True
    assert outbound_calls == [True, True]

    connection = connect_database(database_url)
    row = connection.execute(
        """
        SELECT status,attempt_count,claim_token,claim_owner,lease_until
        FROM audit_export_events WHERE audit_export_event_id=? AND tenant_id=?
        """,
        (first_claim.event_id, tenant_id),
    ).fetchone()
    connection.close()
    assert tuple(row) == ("dispatched", 2, None, None, None)


@pytest.mark.anyio
async def test_audit_export_scheduler_recovers_pending_source_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """启动恢复：无需客户端重试或显式 wake，lifespan 启动即派发已提交 pending source。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        exported = await client.get(
            "/audit-logs/export",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd058_startup_recovery",
            },
        )
    assert exported.status_code == 200
    connection = connect_database(database_url)
    source = connection.execute(
        """
        SELECT event.audit_export_event_id,event.status
        FROM audit_export_events event
        JOIN audit_exports export
          ON export.tenant_id=event.tenant_id AND export.audit_export_id=event.audit_export_id
        WHERE export.tenant_id=? AND export.request_id='req_prd058_startup_recovery'
        """,
        (tenant_id,),
    ).fetchone()
    connection.close()
    assert tuple(source) == (source["audit_export_event_id"], "pending")

    from metahub_control.webhooks import service as webhook_service

    outbound_calls: list[tuple[str, bool]] = []

    def capture_startup_dispatch(
        _config,
        tenant,
        event,
        *,
        request_id,
        trace_id,
        resume_existing_pending=False,
    ):
        """捕获 lifespan 首轮恢复；只记录安全租户 ID 和恢复布尔值。"""

        assert tenant == tenant_id and request_id.startswith("req_aexevt_") and trace_id
        outbound_calls.append((event.event_id, resume_existing_pending))
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_startup_dispatch)
    # 安装发生在 source 提交之后，因此没有 route wake 可依赖，成功只能来自启动首轮 drain。
    export_scheduler.install_audit_export_scheduler(app)
    manager = app.router.lifespan_context(app)
    await manager.__aenter__()
    try:
        status = "pending"
        for _ in range(200):
            connection = connect_database(database_url)
            status = connection.execute(
                "SELECT status FROM audit_export_events WHERE audit_export_event_id=?",
                (source["audit_export_event_id"],),
            ).fetchone()["status"]
            connection.close()
            if status == "dispatched":
                break
            await asyncio.sleep(0.01)
        assert status == "dispatched"
        assert outbound_calls == [(source["audit_export_event_id"], True)]
    finally:
        await manager.__aexit__(None, None, None)
