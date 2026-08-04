"""PRD-062 配置导入导出 Control API 专项测试。

测试覆盖四类对象主流程、严格 JSON 与字段类型、原子回滚、后端权限、租户隔离、精确重试、
一次性 Virtual Key、审计和 Webhook；所有数据库均由 pytest 在指定沙箱证据目录内隔离创建。
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.auth.routes import router as auth_router
from metahub_control.config_transfer import service as config_transfer_service
from metahub_control.config_transfer.routes import router as config_transfer_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app


# 测试凭据只用于隔离数据库登录，断言和测试记录不得输出其明文或访问令牌。
ADMIN_PASSWORD = "Prd062Admin!2026"
VIEWER_PASSWORD = "Prd062Viewer!2026"
# 固定提交时间用于核对事实、审计和 Webhook 共用同一秒级 UTC 身份。
BASE_TIME = datetime(2026, 8, 1, 2, 3, 4, tzinfo=UTC)


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库路径转换为项目 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """应用全部迁移和 seed，并增加无迁移权限的 Viewer 测试用户。"""

    database_url = _sqlite_url(tmp_path / "prd062-config-transfer.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd062-auth-test-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'viewer', 'active', datetime('now'), datetime('now'))
        """,
        (
            "user_prd062_viewer",
            seed.tenant_id,
            "viewer.prd062@example.com",
            "PRD062 Viewer",
            hash_password(VIEWER_PASSWORD),
        ),
    )
    connection.commit()
    connection.close()
    # 测试只把隔离数据库地址和目标租户 ID 交给当前用例，避免后续断言误查其他租户事实。
    return database_url, seed.tenant_id


def _build_app():
    """构造仅含认证和配置迁移路由的隔离 Control API。"""

    app = create_service_app("control-plane-api", "PRD-062 Config Transfer Test API")
    app.include_router(auth_router)
    app.include_router(config_transfer_router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录指定测试用户并只返回后续 Authorization 头需要的会话令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
        headers={"X-Request-ID": "req_prd062_login"},
    )
    assert response.status_code == 200, response.text
    # Token 只保存在当前测试协程内，不能写入数据库快照、日志或失败消息。
    return response.json()["access_token"]


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造带固定请求与链路标识的授权头，便于核对全链路事实。"""

    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trc_{request_id}",
    }


def _manifest(*, source_tenant_id: str = "source-enterprise") -> dict[str, object]:
    """构造覆盖四类对象和 Alias/Fallback 关系的合法 v1 导入清单。

    参数允许替换来源租户标识，用于验证 request_id 重放时的意图冲突；返回内容不含任何
    Provider Key 或 Virtual Key 明文，避免测试失败输出把凭据带入日志。
    """

    return {
        "schema_version": "metahub.config/v1",
        "source_tenant_id": source_tenant_id,
        "exported_at": "2026-08-01T00:00:00Z",
        "providers": [
            {
                "provider_code": "prd062_deepseek",
                "name": "PRD062 DeepSeek",
                "provider_type": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "auth_type": "bearer",
                "description": "PRD062 migration provider",
                "maintenance_note": None,
                "status": "active",
            }
        ],
        "models": [
            {
                "internal_model_id": "prd062-chat-primary",
                "provider_code": "prd062_deepseek",
                "provider_model_id": "deepseek-chat",
                "display_name": "PRD062 Chat Primary",
                "capabilities": {"chat": True, "stream": True, "embeddings": False},
                "context_window": 64_000,
                "input_price_per_1k": 0.001,
                "output_price_per_1k": 0.002,
                "currency": "CNY",
                "price_schema_version": "2026-08",
                "description": None,
                "status": "active",
            },
            {
                "internal_model_id": "prd062-chat-fallback",
                "provider_code": "prd062_deepseek",
                "provider_model_id": "deepseek-chat-fallback",
                "display_name": "PRD062 Chat Fallback",
                "capabilities": {"chat": True, "stream": True, "embeddings": False},
                "context_window": 32_000,
                "input_price_per_1k": 0.001,
                "output_price_per_1k": 0.002,
                "currency": "CNY",
                "price_schema_version": "2026-08",
                "description": None,
                "status": "active",
            },
        ],
        "aliases": [
            {
                "alias": "prd062-chat",
                "environment": "dev",
                "default_internal_model_id": "prd062-chat-primary",
                "fallback_internal_model_id": "prd062-chat-fallback",
                "fallback_conditions": ["upstream_timeout", "upstream_5xx"],
                "routing_mode": "quality_cost",
                "quality_cost_config": {
                    "quality_weight_bps": 5_000,
                    "cost_weight_bps": 3_000,
                    "latency_weight_bps": 2_000,
                    "quality_floor_bps": 6_000,
                    "minimum_sample_count": 20,
                },
                "description": "PRD062 migrated alias",
                "status": "active",
            }
        ],
        "virtual_keys": [
            {
                "name": "PRD062 Delivery Key",
                "environment": "dev",
                "description": "PRD062 generated credential",
                "project_code": None,
                "application_code": None,
                "status": "active",
                "rpm_limit": 60,
                "tpm_limit": 120_000,
                "concurrency_limit": 8,
                "rate_limit_status": "active",
            }
        ],
    }


def _assert_error_identity(response: Response, expected_code: str) -> None:
    """断言统一错误信封包含稳定领域码以及非空 request_id/trace_id。"""

    payload = response.json()
    assert payload["code"] == expected_code
    assert payload["request_id"]
    assert payload["trace_id"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_import_export_replay_audit_webhook_and_secret_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0127：四类对象原子导入、脱敏导出、精确重试及证据链全部成立。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_prd062_other','prd062-other','PRD062 Other','internal_only','active',
               datetime('now'),datetime('now'))
        """
    )
    # 同业务键存在于其他租户时不得冲突，也不能被当前租户导入覆盖。
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,provider_type,auth_type,
          created_at,updated_at
        )
        VALUES(
          'provider_prd062_other','tenant_prd062_other','prd062_deepseek','Other Tenant Provider',
          'https://other.example.com/v1','active','openai_compatible','none',datetime('now'),datetime('now')
        )
        """
    )
    connection.commit()
    connection.close()

    captured_events = []
    original_builder = config_transfer_service.build_webhook_event

    def capture_config_event(**kwargs):
        """捕获已通过共享 SDK 严格校验的配置事件，同时保留真实构造行为。"""

        event = original_builder(**kwargs)
        captured_events.append(event)
        return event

    monkeypatch.setattr(config_transfer_service, "build_webhook_event", capture_config_event)
    monkeypatch.setattr(config_transfer_service, "_utcnow", lambda: BASE_TIME)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        document = _manifest()
        validated = await client.post(
            "/config/import/validate",
            json=document,
            headers=_headers(token, "req_prd062_validate"),
        )
        applied = await client.post(
            "/config/import",
            json=document,
            headers={
                **_headers(token, "req_prd062_apply"),
                "User-Agent": "Provider-Key-canary-must-not-enter-audit",
            },
        )
        replayed = await client.post(
            "/config/import",
            json=document,
            headers={
                **_headers(token, "req_prd062_apply"),
                "X-Trace-ID": "trc_req_prd062_replay_current",
            },
        )
        exported = await client.post(
            "/config/export",
            json={},
            headers={
                **_headers(token, "req_prd062_export"),
                "User-Agent": "Session-Token-canary-must-not-enter-audit",
            },
        )
        round_trip = await client.post(
            "/config/import/validate",
            json=exported.json()["config_export"],
            headers=_headers(token, "req_prd062_round_trip"),
        )

    assert validated.status_code == 200, validated.text
    assert validated.json()["report"]["status"] == "validated"
    assert validated.json()["report"]["counts"]["models_created"] == 2
    assert applied.status_code == 201, applied.text
    assert applied.headers["cache-control"] == "no-store"
    applied_payload = applied.json()
    assert applied_payload["report"]["status"] == "applied"
    assert applied_payload["report"]["request_id"] == "req_prd062_apply"
    assert applied_payload["report"]["trace_id"] == "trc_req_prd062_apply"
    assert len(applied_payload["generated_virtual_keys"]) == 1
    plain_value = applied_payload["generated_virtual_keys"][0]["plain_value"]
    assert plain_value.startswith("mhv_dev_")

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["report"]["status"] == "replayed"
    assert replayed.json()["trace_id"] == "trc_req_prd062_replay_current"
    assert replayed.json()["report"]["trace_id"] == "trc_req_prd062_replay_current"
    assert replayed.json()["generated_virtual_keys"] == []
    assert exported.status_code == 200, exported.text
    assert exported.headers["cache-control"] == "no-store"
    export_document = exported.json()["config_export"]
    assert export_document["source_tenant_id"] == tenant_id
    assert [row["provider_code"] for row in export_document["providers"]] == sorted(
        row["provider_code"] for row in export_document["providers"]
    )
    assert any(row["provider_code"] == "prd062_deepseek" for row in export_document["providers"])
    assert plain_value not in exported.text
    assert all(secret_field not in exported.text for secret_field in ("plain_value", "key_hash", "key_prefix"))
    assert round_trip.status_code == 200, round_trip.text
    assert round_trip.json()["report"]["status"] == "validated"
    assert round_trip.json()["report"]["counts"]["virtual_keys_unchanged"] >= 1

    connection = connect_database(database_url)
    providers = connection.execute(
        "SELECT tenant_id,name FROM providers WHERE provider_code='prd062_deepseek' ORDER BY tenant_id"
    ).fetchall()
    assert [(row["tenant_id"], row["name"]) for row in providers] == [
        (tenant_id, "PRD062 DeepSeek"),
        ("tenant_prd062_other", "Other Tenant Provider"),
    ]
    import_fact = connection.execute(
        "SELECT * FROM config_imports WHERE request_id='req_prd062_apply'"
    ).fetchone()
    assert import_fact is not None
    assert import_fact["tenant_id"] == tenant_id
    assert import_fact["models_created_count"] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM config_import_rows WHERE config_import_id=?",
        (import_fact["config_import_id"],),
    ).fetchone()[0] == 5
    policy = connection.execute(
        """
        SELECT p.routing_mode,p.version,p.quality_weight_bps,p.cost_weight_bps,p.latency_weight_bps
        FROM model_alias_routing_policies p
        JOIN model_aliases a ON a.tenant_id=p.tenant_id AND a.model_alias_id=p.model_alias_id
        WHERE a.tenant_id=? AND a.alias='prd062-chat'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(policy) == ("quality_cost", 2, 5_000, 3_000, 2_000)
    fallback = connection.execute(
        """
        SELECT f.status,f.condition_expr
        FROM fallback_policies f
        JOIN model_aliases a ON a.tenant_id=f.tenant_id AND a.model_alias_id=f.model_alias_id
        WHERE a.tenant_id=? AND a.alias='prd062-chat'
        """,
        (tenant_id,),
    ).fetchone()
    assert fallback["status"] == "enabled"
    assert json.loads(fallback["condition_expr"]) == ["upstream_timeout", "upstream_5xx"]
    virtual_key = connection.execute(
        "SELECT key_hash,key_prefix FROM virtual_keys WHERE tenant_id=? AND name='PRD062 Delivery Key'",
        (tenant_id,),
    ).fetchone()
    assert virtual_key["key_hash"] != plain_value
    assert plain_value.startswith(virtual_key["key_prefix"])
    audit_rows = connection.execute(
        "SELECT action,user_agent FROM audit_logs WHERE object_type='config_transfer'"
    ).fetchall()
    audit_actions = {
        row["action"]
        for row in audit_rows
    }
    assert audit_actions == {"config.imported", "config.exported"}
    assert all(row["user_agent"] is None for row in audit_rows)
    database_dump = "\n".join(connection.iterdump())
    connection.close()
    assert plain_value not in database_dump

    assert len(captured_events) == 2
    first_event, replay_event = captured_events
    assert first_event.event_type == "config.imported"
    assert first_event.event_id == replay_event.event_id == import_fact["event_id"]
    assert first_event.payload == replay_event.payload
    assert set(first_event.payload) == {
        "config_import_id",
        "schema_version",
        "manifest_sha256",
        "providers_created",
        "providers_unchanged",
        "models_created",
        "models_unchanged",
        "aliases_created",
        "aliases_unchanged",
        "virtual_keys_created",
        "virtual_keys_unchanged",
        "generated_virtual_keys",
        "disabled_virtual_keys",
        "applied_at",
    }
    assert plain_value not in json.dumps(first_event.to_payload(), ensure_ascii=False)


@pytest.mark.anyio
async def test_strict_json_types_sensitive_fields_and_atomic_invalid_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0128：错误 wire 类型、秘密字段和非法引用只返回安全报告且不留下半批数据。"""

    database_url, _tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        mutations = [
            ("provider_code", 123, "providers", 0),
            ("context_window", 1.5, "models", 0),
            ("context_window", True, "models", 0),
            ("context_window", "64000", "models", 0),
            ("rpm_limit", True, "virtual_keys", 0),
        ]
        for field, value, collection, row_index in mutations:
            document = _manifest()
            document[collection][row_index][field] = value
            response = await client.post(
                "/config/import/validate",
                json=document,
                headers=_headers(token, f"req_prd062_type_{field}_{row_index}"),
            )
            assert response.status_code == 200, response.text
            report = response.json()["report"]
            assert report["status"] == "invalid"
            assert any(error["field"] == field for error in report["errors"])

        secret_document = _manifest()
        secret_document["providers"][0]["api_key"] = "must-never-be-reflected"
        secret_response = await client.post(
            "/config/import/validate",
            json=secret_document,
            headers=_headers(token, "req_prd062_sensitive"),
        )
        camel_secret_document = _manifest()
        camel_secret_document["providers"][0]["metadata"] = {
            "signingKey": "camel-secret-must-never-be-reflected"
        }
        camel_secret_response = await client.post(
            "/config/import/validate",
            json=camel_secret_document,
            headers=_headers(token, "req_prd062_camel_sensitive"),
        )
        atomic_document = _manifest()
        atomic_document["aliases"][0]["default_internal_model_id"] = "missing-model"
        atomic_response = await client.post(
            "/config/import",
            json=atomic_document,
            headers=_headers(token, "req_prd062_atomic"),
        )

    assert secret_response.status_code == 200
    secret_report = secret_response.json()["report"]
    assert secret_report["errors"] == [
        {
            "object_type": "document",
            "row": 0,
            "field": "credential",
            "code": "config_transfer.import_sensitive_field",
        }
    ]
    assert "must-never-be-reflected" not in secret_response.text
    assert camel_secret_response.status_code == 200
    assert camel_secret_response.json()["report"]["errors"][0]["code"] == (
        "config_transfer.import_sensitive_field"
    )
    assert "camel-secret-must-never-be-reflected" not in camel_secret_response.text
    assert atomic_response.status_code == 200, atomic_response.text
    assert atomic_response.json()["report"]["status"] == "invalid"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM providers WHERE provider_code='prd062_deepseek'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM models WHERE model_alias LIKE 'prd062-%'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM config_imports WHERE request_id='req_prd062_atomic'"
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_parser_rejects_duplicate_nonfinite_control_and_oversized_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0129：严格 reader 区分无效正文与容量超限，并保持统一错误信封。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {
            **_headers(token, "req_prd062_parser"),
            "Content-Type": "application/json",
        }
        duplicate = await client.post(
            "/config/import/validate",
            content='{"schema_version":"metahub.config/v1","schema_version":"duplicate"}',
            headers=headers,
        )
        nonfinite = await client.post(
            "/config/import/validate",
            content='{"value":1e999}',
            headers=headers,
        )
        c1_control = await client.post(
            "/config/import/validate",
            content='{"value":"\\u0085"}',
            headers=headers,
        )
        lone_surrogate = await client.post(
            "/config/import/validate",
            content='{"value":"\\ud800"}',
            headers=headers,
        )
        oversized = await client.post(
            "/config/import/validate",
            content=json.dumps({"value": "x" * 4_097}),
            headers=headers,
        )
        collection_overflow_document = {
            "schema_version": "metahub.config/v1",
            "source_tenant_id": "source-enterprise",
            "exported_at": "2026-08-01T00:00:00Z",
            "providers": [{} for _index in range(501)],
            "models": [],
            "aliases": [],
            "virtual_keys": [],
        }
        collection_overflow = await client.post(
            "/config/import/validate",
            json=collection_overflow_document,
            headers=_headers(token, "req_prd062_collection_overflow"),
        )
        total_overflow_document = {
            "schema_version": "metahub.config/v1",
            "source_tenant_id": "source-enterprise",
            "exported_at": "2026-08-01T00:00:00Z",
            "providers": [{} for _index in range(500)],
            "models": [{} for _index in range(1_500)],
            "aliases": [{} for _index in range(1_500)],
            "virtual_keys": [{} for _index in range(1_501)],
        }
        total_overflow = await client.post(
            "/config/import/validate",
            json=total_overflow_document,
            headers=_headers(token, "req_prd062_total_overflow"),
        )

    assert duplicate.status_code == 400
    _assert_error_identity(duplicate, "config_transfer.body_invalid")
    assert nonfinite.status_code == 400
    _assert_error_identity(nonfinite, "config_transfer.body_invalid")
    assert c1_control.status_code == 400
    _assert_error_identity(c1_control, "config_transfer.body_invalid")
    assert lone_surrogate.status_code == 400
    _assert_error_identity(lone_surrogate, "config_transfer.body_invalid")
    assert oversized.status_code == 413
    _assert_error_identity(oversized, "config_transfer.body_too_large")
    assert collection_overflow.status_code == 413
    _assert_error_identity(collection_overflow, "config_transfer.body_too_large")
    assert total_overflow.status_code == 413
    _assert_error_identity(total_overflow, "config_transfer.body_too_large")


@pytest.mark.anyio
async def test_permission_precedes_body_parsing_and_request_id_conflict_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0130：后端权限先于正文解析，异意图重用 request_id 返回稳定 409。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        viewer_token = await _login(client, "viewer.prd062@example.com", VIEWER_PASSWORD)
        malformed = '{"schema_version":'
        denied = await client.post(
            "/config/import",
            content=malformed,
            headers={
                **_headers(viewer_token, "req_prd062_denied"),
                "Content-Type": "application/json",
            },
        )
        anonymous = await client.post(
            "/config/import/validate",
            content=malformed,
            headers={"Content-Type": "application/json", "X-Request-ID": "req_prd062_anonymous"},
        )
        admin_malformed = await client.post(
            "/config/import/validate",
            content=malformed,
            headers={
                **_headers(admin_token, "req_prd062_admin_malformed"),
                "Content-Type": "application/json",
            },
        )
        applied = await client.post(
            "/config/import",
            json=_manifest(),
            headers=_headers(admin_token, "req_prd062_conflict"),
        )
        changed_intent = _manifest(source_tenant_id="different-source")
        conflict = await client.post(
            "/config/import",
            json=changed_intent,
            headers=_headers(admin_token, "req_prd062_conflict"),
        )

    assert denied.status_code == 403
    _assert_error_identity(denied, "auth.permission_denied")
    assert anonymous.status_code == 401
    _assert_error_identity(anonymous, "auth.token_missing")
    assert admin_malformed.status_code == 400
    _assert_error_identity(admin_malformed, "config_transfer.body_invalid")
    assert applied.status_code == 201, applied.text
    assert conflict.status_code == 409
    _assert_error_identity(conflict, "config_transfer.import_request_conflict")
    assert conflict.json()["request_id"] == "req_prd062_conflict"


@pytest.mark.anyio
async def test_routing_policy_schema_rejects_unrepresentable_disabled_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0131：数据库约束拒绝 v1 无法表达的停用 RoutingPolicy 存量状态。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        document = _manifest()
        applied = await client.post(
            "/config/import",
            json=document,
            headers=_headers(token, "req_prd062_policy_apply"),
        )
        assert applied.status_code == 201, applied.text

        connection = connect_database(database_url)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET status='disabled',version=version+1,updated_at=datetime('now')
                WHERE tenant_id=? AND model_alias_id=(
                  SELECT model_alias_id FROM model_aliases WHERE tenant_id=? AND alias='prd062-chat'
                )
                """,
                (tenant_id, tenant_id),
            )
        connection.rollback()
        policy_status = connection.execute(
            """
            SELECT p.status
            FROM model_alias_routing_policies p
            JOIN model_aliases a ON a.tenant_id=p.tenant_id AND a.model_alias_id=p.model_alias_id
            WHERE a.tenant_id=? AND a.alias='prd062-chat'
            """,
            (tenant_id,),
        ).fetchone()[0]
        connection.close()

    assert policy_status == "enabled"


@pytest.mark.anyio
async def test_post_commit_routing_event_failure_does_not_lose_generated_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TC-0132：提交后路由事件异常不得把已生成的一次性 Key 响应变成 500。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)

    def fail_route_dispatch(_config: object, _event: object) -> None:
        """模拟提交后的路由事件投递层抛出未分类异常。"""

        # 固定哨兵只用于证明提交后异常被隔离，错误文本不得进入一次性 Key 响应或审计事实。
        raise RuntimeError("route-dispatch-canary-must-not-leak")

    monkeypatch.setattr(
        config_transfer_service,
        "_dispatch_routing_policy_event",
        fail_route_dispatch,
    )
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/config/import",
            json=_manifest(),
            headers=_headers(token, "req_prd062_route_event_failure"),
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["report"]["status"] == "applied"
    assert len(payload["generated_virtual_keys"]) == 1
    plain_value = payload["generated_virtual_keys"][0]["plain_value"]
    assert plain_value.startswith("mhv_dev_")
    assert "route-dispatch-canary-must-not-leak" not in response.text
    connection = connect_database(database_url)
    fact = connection.execute(
        "SELECT tenant_id FROM config_imports WHERE request_id='req_prd062_route_event_failure'"
    ).fetchone()
    connection.close()
    assert fact is not None and fact["tenant_id"] == tenant_id
