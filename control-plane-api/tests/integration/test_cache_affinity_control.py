"""PRD-050 Control CacheAffinity 配置、权限、幂等和日志详情集成测试。

测试始终应用真实 0040 迁移，覆盖主流程、异常 wire、后端 RBAC、跨租户隐藏、三事实
事务回滚和 RequestLog Selection 脱敏；不得用临时弱约束表绕过数据库安全触发器。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.model_aliases import cache_affinity_service
from metahub_control.model_aliases.routes import router as model_aliases_router
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 三类固定测试账号用于验证后端角色门禁，不得复用生产环境或开发库真实凭据。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把临时文件转换成项目统一 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造包含认证、Alias 和 RequestLog 的最小真实 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-050 Control Test API")
    app.include_router(auth_router)
    app.include_router(model_aliases_router)
    app.include_router(request_logs_router)
    return app


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    """应用完整迁移并准备三角色及 Selection 夹具所需权威对象。"""

    database_url = _sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    monkeypatch.setenv(
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "test-cache-affinity-secret-at-least-32-characters",
    )
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    model = connection.execute(
        """
        SELECT m.model_id, m.model_alias, m.provider_id, p.provider_code
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_alias='default-chat'
        """,
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (seed.tenant_id,),
    ).fetchone()
    assert model is not None and virtual_key is not None and environment is not None
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd050_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd050_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    connection.close()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "model_id": str(model["model_id"]),
        "internal_model_id": str(model["model_alias"]),
        "provider_id": str(model["provider_id"]),
        "provider_code": str(model["provider_code"]),
        "virtual_key_id": str(virtual_key["virtual_key_id"]),
        "environment_id": str(environment["environment_id"]),
    }


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录指定角色并返回仅供本测试请求使用的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌只保留在当前测试客户端内存中，断言失败时不得输出认证响应正文。
    return str(response.json()["access_token"])


async def _create_alias(
    client: AsyncClient,
    admin_token: str,
    model_id: str,
    *,
    request_id: str,
) -> str:
    """通过真实 API 创建带持久 RoutingPolicy 的测试 Alias。"""

    response = await client.post(
        "/model-aliases",
        headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": request_id},
        json={
            "alias": request_id.replace("req_", "").replace("_", "-")[:80],
            "environment": "dev",
            "default_model_id": model_id,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["model_alias"]["model_alias_id"])


def _config_payload(*, expected_version: int, ttl_seconds: int = 3600) -> dict[str, object]:
    """返回完整 CacheAffinity 配置意图，避免测试误用部分 PATCH。"""

    return {
        "status": "enabled",
        "data_class": "internal",
        "minimum_prefix_chars": 1024,
        "ttl_seconds": ttl_seconds,
        "expected_version": expected_version,
    }


@pytest.mark.anyio
async def test_cache_affinity_flow_idempotency_get_audit_and_webhook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：create/noop/update、精确重放、GET 摘要与两类冲突均可追踪。"""

    facts = _prepare_database(monkeypatch, tmp_path)
    captured_events: list[Any] = []

    def capture_dispatch(
        _config: object,
        _tenant_id: str,
        event: object,
        **_kwargs: object,
    ) -> list[object]:
        """截获已完成共享校验的 Webhook 信封，不执行外部网络请求。"""

        captured_events.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_dispatch)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        alias_id = await _create_alias(
            client,
            admin,
            facts["model_id"],
            request_id="req_prd050_affinity_alias",
        )
        create_headers = {
            "Authorization": f"Bearer {admin}",
            "X-Request-ID": "req_prd050_affinity_create",
            "X-Trace-ID": "trc_prd050_affinity_create",
        }
        created = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers=create_headers,
            json=_config_payload(expected_version=0),
        )
        replayed = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers=create_headers,
            json=_config_payload(expected_version=0),
        )
        noop = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_affinity_noop",
            },
            json=_config_payload(expected_version=1),
        )
        updated = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_affinity_update",
            },
            json=_config_payload(expected_version=1, ttl_seconds=7200),
        )
        stale_replay = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers=create_headers,
            json=_config_payload(expected_version=0),
        )
        request_conflict = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_affinity_update",
            },
            json=_config_payload(expected_version=1, ttl_seconds=7201),
        )
        listed = await client.get(
            "/model-aliases",
            headers={
                "Authorization": f"Bearer {developer}",
                "X-Request-ID": "req_prd050_affinity_list",
            },
        )

    assert created.status_code == 200, created.text
    assert created.json()["changed"] is True
    assert created.json()["cache_affinity"]["version"] == 1
    assert replayed.status_code == 200
    assert replayed.json() == created.json()
    assert noop.status_code == 200 and noop.json()["changed"] is False
    assert noop.json()["cache_affinity"]["version"] == 1
    assert updated.status_code == 200 and updated.json()["changed"] is True
    assert updated.json()["cache_affinity"]["version"] == 2
    assert updated.json()["cache_affinity"]["ttl_seconds"] == 7200
    assert stale_replay.status_code == 409
    assert stale_replay.json()["code"] == "cache_affinity.version_conflict"
    assert request_conflict.status_code == 409
    assert request_conflict.json()["code"] == "cache_affinity.request_conflict"
    assert listed.status_code == 200
    # ModelAlias 会返回当前租户分类与治理配置；成功和冲突都必须禁止浏览器缓存。
    for response in (created, replayed, noop, updated, stale_replay, request_conflict, listed):
        assert response.headers["cache-control"] == "no-store, private"
    listed_alias = next(
        item for item in listed.json()["model_aliases"] if item["model_alias_id"] == alias_id
    )
    assert listed_alias["cache_affinity"] == updated.json()["cache_affinity"]
    for forbidden in (
        "prompt_fingerprint",
        "affinity_key_digest",
        "provider_credential_id",
        "cache_affinity_binding_id",
    ):
        assert forbidden not in listed.text.lower()

    connection = connect_database(facts["database_url"])
    events = connection.execute(
        """
        SELECT event_type, cache_affinity_version
        FROM cache_affinity_config_events
        WHERE tenant_id=? AND model_alias_id=?
        ORDER BY created_at, cache_affinity_config_event_id
        """,
        (facts["tenant_id"], alias_id),
    ).fetchall()
    audits = connection.execute(
        """
        SELECT action FROM audit_logs
        WHERE tenant_id=? AND object_type='cache_affinity'
        ORDER BY created_at, audit_log_id
        """,
        (facts["tenant_id"],),
    ).fetchall()
    connection.close()
    assert sorted((row["event_type"], row["cache_affinity_version"]) for row in events) == [
        ("created", 1),
        ("noop", 1),
        ("updated", 2),
    ]
    assert sorted(row["action"] for row in audits) == [
        "cache_affinity.created",
        "cache_affinity.updated",
    ]
    affinity_events = [
        event
        for event in captured_events
        if getattr(event, "event_type", None) == "cache.affinity_selected"
    ]
    # 重放可能再次经过幂等投递器；稳定 event_id 去重后必须只有 created 与 updated 两事实。
    unique_events = {event.event_id: event for event in affinity_events}
    assert len(unique_events) == 2
    for event in unique_events.values():
        assert event.event_id.startswith("cae_")
        assert event.object_id == event.payload["cache_affinity_id"]
        assert event.actor_id is not None and event.trace_id
        assert set(event.payload) == {
            "trigger",
            "request_id",
            "cache_affinity_id",
            "cache_affinity_version",
            "model_alias_id",
            "change_kind",
            "changed_fields",
            "status",
        }


@pytest.mark.anyio
async def test_cache_affinity_permission_wire_and_cross_tenant_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：正文前 RBAC、严格 wire 和跨租户 Alias 均在后端拒绝。"""

    facts = _prepare_database(monkeypatch, tmp_path)
    hidden_alias_id = _insert_hidden_tenant_alias(facts["database_url"])
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        alias_id = await _create_alias(
            client,
            admin,
            facts["model_id"],
            request_id="req_prd050_wire_alias",
        )
        attack_body = b"{" + b"x" * 9000 + b"}"
        developer_denied = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {developer}",
                "Content-Type": "application/json",
            },
            content=attack_body,
        )
        viewer_denied = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {viewer}",
                "Content-Type": "application/json",
            },
            content=attack_body,
        )
        anonymous_denied = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={"Content-Type": "application/json"},
            content=attack_body,
        )
        viewer_get_denied = await client.get(
            "/model-aliases",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        cache_affinity_preflight = await client.options(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,x-request-id,x-trace-id"
                ),
            },
        )
        invalid_requests = [
            ({"status": "enabled"}, "application/json"),
            ({**_config_payload(expected_version=0), "extra": "x"}, "application/json"),
            ({**_config_payload(expected_version=0), "ttl_seconds": True}, "application/json"),
            ({**_config_payload(expected_version=0), "data_class": None}, "application/json"),
            ({**_config_payload(expected_version=0), "data_class": "Internal"}, "application/json"),
            ({**_config_payload(expected_version=0), "ttl_seconds": 59}, "application/json"),
        ]
        invalid_responses = []
        for index, (body, media_type) in enumerate(invalid_requests):
            invalid_responses.append(
                await client.patch(
                    f"/model-aliases/{alias_id}/cache-affinity",
                    headers={
                        "Authorization": f"Bearer {admin}",
                        "X-Request-ID": f"req_prd050_invalid_{index}",
                        "Content-Type": media_type,
                    },
                    content=json.dumps(body).encode("utf-8"),
                )
            )
        invalid_responses.extend(
            [
                await client.patch(
                    f"/model-aliases/{alias_id}/cache-affinity",
                    headers={
                        "Authorization": f"Bearer {admin}",
                        "X-Request-ID": "req_prd050_duplicate",
                        "Content-Type": "application/json",
                    },
                    content=(
                        b'{"status":"enabled","status":"disabled","data_class":"internal",'
                        b'"minimum_prefix_chars":1024,"ttl_seconds":3600,"expected_version":0}'
                    ),
                ),
                await client.patch(
                    f"/model-aliases/{alias_id}/cache-affinity",
                    headers={
                        "Authorization": f"Bearer {admin}",
                        "X-Request-ID": "req_prd050_media",
                        "Content-Type": "text/plain",
                    },
                    content=json.dumps(_config_payload(expected_version=0)).encode(),
                ),
                await client.patch(
                    f"/model-aliases/{alias_id}/cache-affinity",
                    headers={
                        "Authorization": f"Bearer {admin}",
                        "X-Request-ID": "req_prd050_utf8",
                        "Content-Type": "application/json",
                    },
                    content=b'\xff\xfe',
                ),
                await client.patch(
                    f"/model-aliases/{alias_id}/cache-affinity",
                    headers={
                        "Authorization": f"Bearer {admin}",
                        "X-Request-ID": "req_prd050_oversize",
                        "Content-Type": "application/json",
                    },
                    content=attack_body,
                ),
            ]
        )
        cross_tenant = await client.patch(
            f"/model-aliases/{hidden_alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_cross_tenant",
            },
            json=_config_payload(expected_version=0),
        )

    assert developer_denied.status_code == 403
    assert viewer_denied.status_code == 403
    assert anonymous_denied.status_code == 401
    assert viewer_get_denied.status_code == 403
    assert all(response.status_code == 422 for response in invalid_responses)
    assert all(
        response.json()["code"] == "cache_affinity.input_invalid" for response in invalid_responses
    )
    assert cache_affinity_preflight.status_code == 200
    assert cache_affinity_preflight.text == "OK"
    assert cache_affinity_preflight.headers["cache-control"] == "no-store, private"
    assert cache_affinity_preflight.headers["access-control-allow-origin"] == (
        "http://localhost:5173"
    )
    allowed_methods = {
        item.strip()
        for item in cache_affinity_preflight.headers[
            "access-control-allow-methods"
        ].split(",")
    }
    allowed_headers = {
        item.strip().lower()
        for item in cache_affinity_preflight.headers["access-control-allow-headers"].split(",")
    }
    assert "PATCH" in allowed_methods
    assert {"authorization", "content-type", "x-request-id", "x-trace-id"} <= allowed_headers
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "model_alias.not_found"
    # 权限、解析和防枚举错误同样携带可能敏感的领域路径，不能只保护成功响应。
    protected_responses = [
        developer_denied,
        viewer_denied,
        anonymous_denied,
        viewer_get_denied,
        *invalid_responses,
        cross_tenant,
    ]
    assert all(
        response.headers["cache-control"] == "no-store, private" for response in protected_responses
    )
    connection = connect_database(facts["database_url"])
    assert connection.execute(
        "SELECT COUNT(*) FROM cache_affinities WHERE tenant_id=? AND model_alias_id=?",
        (facts["tenant_id"], alias_id),
    ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_cache_affinity_transaction_failures_roll_back_all_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务异常：Audit 或 ConfigEvent 任一步失败都不得留下半配置或半版本。"""

    facts = _prepare_database(monkeypatch, tmp_path)
    real_record_audit = cache_affinity_service.record_audit_log
    real_insert_event = cache_affinity_service._insert_config_event
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        alias_id = await _create_alias(
            client,
            admin,
            facts["model_id"],
            request_id="req_prd050_transaction_alias",
        )
        monkeypatch.setattr(cache_affinity_service, "record_audit_log", lambda *_a, **_k: None)
        audit_failed = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_audit_failed",
            },
            json=_config_payload(expected_version=0),
        )
        monkeypatch.setattr(cache_affinity_service, "record_audit_log", real_record_audit)
        created = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_transaction_create",
            },
            json=_config_payload(expected_version=0),
        )

        def fail_event(*_args: object, **_kwargs: object) -> object:
            """在配置和 Audit 已写入当前事务后模拟 ConfigEvent 存储故障。"""

            # 注入固定数据库异常只验证回滚边界，异常文本不得包含配置或敏感字段。
            raise sqlite3.OperationalError("injected_config_event_failure")

        monkeypatch.setattr(cache_affinity_service, "_insert_config_event", fail_event)
        event_failed = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_event_failed",
            },
            json=_config_payload(expected_version=1, ttl_seconds=7200),
        )
        monkeypatch.setattr(cache_affinity_service, "_insert_config_event", real_insert_event)

    assert audit_failed.status_code == 503
    assert audit_failed.json()["code"] == "cache_affinity.update_failed"
    assert created.status_code == 200
    assert event_failed.status_code == 503
    assert event_failed.json()["code"] == "cache_affinity.update_failed"
    connection = connect_database(facts["database_url"])
    config = connection.execute(
        """
        SELECT version, ttl_seconds FROM cache_affinities
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (facts["tenant_id"], alias_id),
    ).fetchone()
    assert tuple(config) == (1, 3600)
    for failed_request_id in ("req_prd050_audit_failed", "req_prd050_event_failed"):
        assert connection.execute(
            "SELECT COUNT(*) FROM cache_affinity_config_events WHERE operation_request_id=?",
            (failed_request_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE request_id=?",
            (failed_request_id,),
        ).fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_request_log_detail_returns_tenant_safe_cache_affinity_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """日志详情只返回 Selection 白名单，不泄露 Binding、Credential 或摘要。"""

    facts = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        alias_id = await _create_alias(
            client,
            admin,
            facts["model_id"],
            request_id="req_prd050_log_alias",
        )
        configured = await client.patch(
            f"/model-aliases/{alias_id}/cache-affinity",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd050_log_config",
            },
            json=_config_payload(expected_version=0),
        )
        assert configured.status_code == 200, configured.text
        _insert_selection_graph(
            facts,
            model_alias_id=alias_id,
            cache_affinity_id=configured.json()["cache_affinity"]["cache_affinity_id"],
        )
        detail = await client.get(
            "/request-logs/req_prd050_selection_log",
            headers={
                "Authorization": f"Bearer {developer}",
                "X-Request-ID": "req_prd050_selection_detail",
            },
        )

    assert detail.status_code == 200, detail.text
    selections = detail.json()["request_log"]["cache_affinity_selections"]
    assert selections == [
        {
            "cache_affinity_selection_id": "cas_" + "3" * 32,
            "cache_affinity_id": configured.json()["cache_affinity"]["cache_affinity_id"],
            "cache_affinity_version": 1,
            "attempt_sequence": 1,
            "selection_reason": "binding_created",
            "estimated_hit": False,
            "endpoint_preference_applied": True,
            "target_kind": "none",
            "model_id": facts["model_id"],
            "provider_id": facts["provider_id"],
            "provider_code": facts["provider_code"],
            "created_at": "2026-07-20T12:00:02Z",
        }
    ]
    lowered = detail.text.lower()
    for forbidden in (
        "cafs_" + "1" * 32,
        "cab_" + "2" * 32,
        "endpoint_revision_digest_snapshot",
        "provider_credential_id_snapshot",
        "affinity_scope_digest",
        "data_class",
        '"key_prefix":',
    ):
        assert forbidden not in lowered


def _insert_hidden_tenant_alias(database_url: str) -> str:
    """创建另一租户完整 Alias 图，用于验证路径 ID 不可枚举。"""

    connection = connect_database(database_url)
    tenant_id = "tenant_prd050_hidden"
    alias_id = "alias_prd050_hidden"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(?, 'prd050-hidden', 'PRD050 Hidden', 'internal_only', 'active',
               datetime('now'), datetime('now'))
        """,
        (tenant_id,),
    )
    environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, created_at, updated_at
        )
        VALUES('provider_prd050_hidden', ?, 'prd050_hidden', 'Hidden Provider',
               'http://127.0.0.1:9/v1', 'active', 'openai_compatible', 'none',
               datetime('now'), datetime('now'))
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES('model_prd050_hidden', ?, 'provider_prd050_hidden', 'hidden-chat',
               'hidden-upstream', '{}', 'active', datetime('now'), datetime('now'))
        """,
        (tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_at, updated_at
        )
        VALUES(?, ?, 'hidden-alias', 'dev', ?, 'model_prd050_hidden', 'active',
               datetime('now'), datetime('now'))
        """,
        (alias_id, tenant_id, environment_id),
    )
    connection.commit()
    connection.close()
    return alias_id


def _insert_selection_graph(
    facts: dict[str, str],
    *,
    model_alias_id: str,
    cache_affinity_id: str,
) -> None:
    """通过 0040 全部图触发器插入一条可供 RequestLog 查询的 Selection。"""

    connection = connect_database(facts["database_url"])
    request_id = "req_prd050_selection_log"
    trace_id = "trc_prd050_selection_log"
    scope_id = "cafs_" + "1" * 32
    binding_id = "cab_" + "2" * 32
    attempt_id = "rta_" + "4" * 32
    selection_id = "cas_" + "3" * 32
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-20T12:00:00Z')
        """,
        (facts["tenant_id"], request_id, trace_id, facts["virtual_key_id"]),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, environment_id,
          environment_code, provider_id, model_id, endpoint, request_model,
          resolved_model, provider_code, status, latency_ms, input_tokens,
          output_tokens, total_tokens, estimated_cost_amount, currency,
          price_schema_version, routing_attempt_count, final_routing_attempt_id,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'dev', ?, ?, '/v1/chat/completions', 'affinity-log',
               ?, ?, 'success', 20, 10, 5, 15, 0, 'CNY', 'test-v1', 0, NULL,
               '2026-07-20T12:00:00Z', '2026-07-20T12:00:03Z')
        """,
        (
            request_id,
            facts["tenant_id"],
            trace_id,
            facts["virtual_key_id"],
            facts["environment_id"],
            facts["provider_id"],
            facts["model_id"],
            facts["internal_model_id"],
            facts["provider_code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_scopes(
          cache_affinity_scope_id, tenant_id, virtual_key_id, digest_schema_version,
          affinity_scope_digest, preferred_binding_id, preferred_lease_generation,
          last_selected_at, created_at, updated_at
        )
        VALUES(?, ?, ?, 1, ?, NULL, NULL, '2026-07-20T12:00:01Z',
               '2026-07-20T12:00:01Z', '2026-07-20T12:00:01Z')
        """,
        (scope_id, facts["tenant_id"], facts["virtual_key_id"], "a" * 64),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_bindings(
          cache_affinity_binding_id, tenant_id, cache_affinity_scope_id,
          affinity_key_digest, model_endpoint_id, endpoint_revision_digest,
          provider_id, provider_credential_id, target_kind, target_generation,
          pending_lease_generation, pending_lease_started_at, pending_lease_expires_at,
          active_selection_count, last_success_at, selection_count,
          estimated_hit_count, last_selected_at, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, 'none', 1, 1,
               '2026-07-20T12:00:01Z', '2026-07-20T12:00:31Z', 1, NULL, 1, 0,
               '2026-07-20T12:00:02Z', '2026-07-20T12:00:01Z',
               '2026-07-20T12:00:02Z')
        """,
        (
            binding_id,
            facts["tenant_id"],
            scope_id,
            "b" * 64,
            facts["model_id"],
            "c" * 64,
            facts["provider_id"],
        ),
    )
    connection.execute(
        """
        UPDATE cache_affinity_scopes
        SET preferred_binding_id=?, preferred_lease_generation=1,
            updated_at='2026-07-20T12:00:02Z'
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        """,
        (binding_id, facts["tenant_id"], scope_id),
    )
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
          attempt_sequence, attempt_role, trigger_type, model_id, internal_model_id,
          provider_id, provider_code, status, retryable, usage_status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?,
               'started', 0, 'pending', '2026-07-20T12:00:02Z', '2026-07-20T12:00:02Z')
        """,
        (
            attempt_id,
            facts["tenant_id"],
            request_id,
            trace_id,
            model_alias_id,
            facts["model_id"],
            facts["internal_model_id"],
            facts["provider_id"],
            facts["provider_code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_selections(
          cache_affinity_selection_id, tenant_id, request_id, trace_id,
          attempt_sequence, routing_attempt_id, stream_session_id,
          cache_affinity_id, cache_affinity_version,
          cache_affinity_scope_id_snapshot, cache_affinity_binding_id_snapshot,
          model_alias_id, model_endpoint_id, endpoint_revision_digest_snapshot,
          provider_id, provider_code_snapshot, provider_credential_id_snapshot,
          target_kind, target_generation, pending_lease_generation_snapshot,
          ttl_seconds_snapshot, warm_evidence_at_snapshot,
          endpoint_preference_applied, endpoint_preference_state,
          binding_state_before, selection_reason, estimated_hit,
          outcome_status, outcome_at, created_at
        )
        VALUES(?, ?, ?, ?, 1, ?, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL,
               'none', 1, 1, 3600, NULL, 1, 'pending', 'missing',
               'binding_created', 0, 'pending', NULL, '2026-07-20T12:00:02Z')
        """,
        (
            selection_id,
            facts["tenant_id"],
            request_id,
            trace_id,
            attempt_id,
            cache_affinity_id,
            scope_id,
            binding_id,
            model_alias_id,
            facts["model_id"],
            "c" * 64,
            facts["provider_id"],
            facts["provider_code"],
        ),
    )
    connection.commit()
    connection.close()
