"""PRD-048 模型评分 API 的主流程、异常、权限、租户与 exactly-once 集成测试。

测试只保存整数统计和安全事件摘要；登录 Token 仅放在当前 HTTP 请求头，不写断言输出、
数据库事件或日志。跨租户资源统一验证为 404，不暴露隐藏租户是否真实存在。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.model_quality_scores import service as score_service
from metahub_control.model_quality_scores.routes import router as score_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 三个口令仅用于创建临时测试身份，不得写入评分事件、业务日志或失败断言正文。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把临时文件转换成仓库数据库连接支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证和评分路由的控制面，缩小集成测试故障面。"""

    app = create_service_app("control-plane-api", "Model Quality Score Test API")
    app.include_router(auth_router)
    app.include_router(score_router)
    return app


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str]:
    """准备默认租户三角色、默认模型和一个不可见的第二租户模型。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd048.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd048-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
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
                "user_prd048_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd048_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    model_id = str(
        connection.execute(
            "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
            (seed.tenant_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        )
        VALUES(
          'tenant_prd048_hidden', 'prd048-hidden', 'PRD048 Hidden',
          'internal_only', 'active', datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, created_at, updated_at
        )
        VALUES(
          'provider_prd048_hidden', 'tenant_prd048_hidden', 'prd048_hidden',
          'Hidden Provider', 'http://127.0.0.1:9/v1', 'active',
          'openai_compatible', 'none', datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd048_hidden', 'tenant_prd048_hidden', 'provider_prd048_hidden',
          'prd048-hidden-model', 'hidden-upstream', '{}', 'active',
          datetime('now'), datetime('now')
        )
        """
    )
    connection.commit()
    connection.close()
    # 返回顺序固定为数据库、归属租户和可见模型；隐藏租户模型绝不返回给后续调用方。
    return database_url, seed.tenant_id, model_id


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试角色并返回仅在当前 client 生命周期内使用的 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只驻留当前 client 生命周期，不写数据库、日志或可能输出正文的测试断言。
    return str(response.json()["access_token"])


@pytest.mark.anyio
async def test_model_quality_score_main_flow_and_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：developer 可读，admin 修改/重试，统计增长不破坏人工幂等。"""

    database_url, tenant_id, model_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        developer_read = await client.get(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd048_manual_score",
        }
        changed = await client.patch(
            "/model-quality-scores",
            headers=admin_headers,
            json={
                "model_id": model_id,
                "manual_quality_bps": 8500,
                "expected_feedback_version": 1,
            },
        )

        assert developer_read.status_code == 200, developer_read.text
        assert developer_read.headers["cache-control"] == "no-store"
        initial = next(
            item
            for item in developer_read.json()["model_quality_scores"]
            if item["model_id"] == model_id
        )
        assert initial["sample_count"] == 0
        assert initial["manual_quality_bps"] is None
        assert initial["effective_quality_bps"] == 5000
        assert initial["observation_status"] == "unobserved"
        assert changed.status_code == 200, changed.text
        assert changed.json()["changed"] is True
        assert changed.json()["model_quality_score"]["manual_quality_bps"] == 8500
        assert changed.json()["model_quality_score"]["feedback_version"] == 2

        # 模拟真实 Gateway 单样本字段转换；statistics_version 独立增长不应使原 request 冲突。
        # 观测时点复用当前 Score 的单调时间下界，避免测试时钟早于动态迁移/模型创建时间。
        observation_at = changed.json()["model_quality_score"]["updated_at"]
        connection = connect_database(database_url)
        connection.execute(
            """
            UPDATE model_quality_scores
            SET sample_count=sample_count+1, success_count=success_count+1,
                latency_sample_count=latency_sample_count+1,
                total_latency_ms=total_latency_ms+125,
                statistics_version=statistics_version+1,
                first_observed_at=COALESCE(first_observed_at, ?),
                last_observed_at=?, updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (observation_at, observation_at, observation_at, tenant_id, model_id),
        )
        connection.commit()
        connection.close()

        retried = await client.patch(
            "/model-quality-scores",
            headers=admin_headers,
            json={
                "model_id": model_id,
                "manual_quality_bps": 8500,
                "expected_feedback_version": 1,
            },
        )
        no_op = await client.patch(
            "/model-quality-scores",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd048_manual_noop",
            },
            json={
                "model_id": model_id,
                "manual_quality_bps": 8500,
                "expected_feedback_version": 2,
            },
        )

    assert retried.status_code == 200, retried.text
    assert retried.json()["changed"] is True
    assert retried.json()["model_quality_score"]["sample_count"] == 1
    assert retried.json()["model_quality_score"]["statistics_version"] == 1
    assert retried.json()["model_quality_score"]["success_rate_bps"] == 10000
    assert retried.json()["model_quality_score"]["error_rate_bps"] == 0
    assert retried.json()["model_quality_score"]["average_latency_ms"] == "125.000"
    assert no_op.status_code == 200
    assert no_op.json()["changed"] is False
    connection = connect_database(database_url)
    event = connection.execute(
        """
        SELECT event_type, actor_user_id, request_id, payload_json
        FROM model_quality_score_events
        WHERE tenant_id=? AND model_id=?
        """,
        (tenant_id, model_id),
    ).fetchall()
    audit = connection.execute(
        """
        SELECT action, object_type, request_id, before_summary_json, after_summary_json
        FROM audit_logs
        WHERE tenant_id=? AND action='model_quality_score.updated'
        """,
        (tenant_id,),
    ).fetchall()
    assert len(event) == 1
    assert len(audit) == 1
    payload = json.loads(event[0]["payload_json"])
    assert event[0]["event_type"] == "model.feedback_recorded"
    assert event[0]["request_id"] == "req_prd048_manual_score"
    assert payload == {
        "feedback_version": 2,
        "manual_quality_bps": 8500,
        "model_id": model_id,
        "model_quality_score_id": changed.json()["model_quality_score"]["model_quality_score_id"],
        "previous_manual_quality_bps": None,
        "request_id": "req_prd048_manual_score",
        "sample_count": 0,
        "statistics_version": 0,
        "trigger": "manual_score_updated",
    }
    serialized = f"{event[0]['payload_json']} {audit[0]['before_summary_json']} {audit[0]['after_summary_json']}".lower()
    for forbidden in ("prompt", "response", "provider_key", "authorization", "rating", "accepted", "tags"):
        assert forbidden not in serialized


@pytest.mark.anyio
async def test_model_quality_score_permissions_tenant_and_strict_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/异常：viewer 无读、developer 无写，租户与严格 wire 均由后端阻断。"""

    _, _, model_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        viewer_read = await client.get(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        # 未授权 developer 的超限正文必须先返回 403，而不是观察到正文解析错误。
        developer_write = await client.request(
            "PATCH",
            "/model-quality-scores",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "Content-Type": "application/json",
            },
            content=b"{" + b"x" * 9000 + b"}",
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_token}",
            "Content-Type": "application/json",
        }
        cross_read = await client.get(
            "/model-quality-scores?model_id=model_prd048_hidden",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        cross_write = await client.patch(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "model_id": "model_prd048_hidden",
                "manual_quality_bps": 9000,
                "expected_feedback_version": 1,
            },
        )
        invalid_queries = [
            await client.get(
                "/model-quality-scores?tenant_id=default",
                headers={"Authorization": f"Bearer {admin_token}"},
            ),
            await client.get(
                "/model-quality-scores?limit=1&limit=2",
                headers={"Authorization": f"Bearer {admin_token}"},
            ),
            await client.get(
                "/model-quality-scores?offset=-1",
                headers={"Authorization": f"Bearer {admin_token}"},
            ),
        ]
        duplicate = await client.request(
            "PATCH",
            "/model-quality-scores",
            headers=admin_headers,
            content=(
                '{"model_id":"%s","manual_quality_bps":8000,'
                '"manual_quality_bps":9000,"expected_feedback_version":1}' % model_id
            ).encode(),
        )
        extra = await client.patch(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "model_id": model_id,
                "manual_quality_bps": 8000,
                "expected_feedback_version": 1,
                "tenant_id": "default",
            },
        )
        bool_score = await client.patch(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "model_id": model_id,
                "manual_quality_bps": True,
                "expected_feedback_version": 1,
            },
        )
        bool_version = await client.patch(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "model_id": model_id,
                "manual_quality_bps": 8000,
                "expected_feedback_version": True,
            },
        )

    assert viewer_read.status_code == 403
    assert viewer_read.json()["code"] == "auth.permission_denied"
    assert developer_write.status_code == 403
    assert developer_write.json()["code"] == "auth.permission_denied"
    assert cross_read.status_code == 404
    assert cross_write.status_code == 404
    assert cross_read.json()["code"] == cross_write.json()["code"] == "model_quality_score.not_found"
    assert all(response.status_code == 422 for response in invalid_queries)
    assert all(response.json()["code"] == "model_quality_score.query_invalid" for response in invalid_queries)
    assert duplicate.status_code == extra.status_code == bool_score.status_code == 422
    assert duplicate.json()["code"] == extra.json()["code"] == bool_score.json()["code"] == "model_quality_score.input_invalid"
    assert bool_version.status_code == 422
    assert bool_version.json()["code"] == "model_quality_score.version_invalid"


@pytest.mark.anyio
async def test_model_quality_score_conflicts_and_audit_failure_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """冲突/事务：版本和 request_id 冲突稳定，审计失败不留下半评分或事件。"""

    database_url, tenant_id, model_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd048_conflict",
        }
        first = await client.patch(
            "/model-quality-scores",
            headers=headers,
            json={
                "model_id": model_id,
                "manual_quality_bps": 7000,
                "expected_feedback_version": 1,
            },
        )
        reused = await client.patch(
            "/model-quality-scores",
            headers=headers,
            json={
                "model_id": model_id,
                "manual_quality_bps": 7100,
                "expected_feedback_version": 1,
            },
        )
        stale = await client.patch(
            "/model-quality-scores",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd048_stale",
            },
            json={
                "model_id": model_id,
                "manual_quality_bps": 7200,
                "expected_feedback_version": 1,
            },
        )
        monkeypatch.setattr(score_service, "record_audit_log", lambda *_args, **_kwargs: None)
        audit_failed = await client.patch(
            "/model-quality-scores",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd048_audit_failure",
            },
            json={
                "model_id": model_id,
                "manual_quality_bps": 9000,
                "expected_feedback_version": 2,
            },
        )

    assert first.status_code == 200
    assert reused.status_code == 409
    assert reused.json()["code"] == "model_quality_score.request_conflict"
    assert stale.status_code == 409
    assert stale.json()["code"] == "model_quality_score.version_conflict"
    assert audit_failed.status_code == 503
    assert audit_failed.json()["code"] == "model_quality_score.update_failed"
    connection = connect_database(database_url)
    score = connection.execute(
        """
        SELECT manual_quality_bps, feedback_version
        FROM model_quality_scores WHERE tenant_id=? AND model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    events = connection.execute(
        "SELECT COUNT(*) FROM model_quality_score_events WHERE tenant_id=? AND model_id=?",
        (tenant_id, model_id),
    ).fetchone()[0]
    assert score["manual_quality_bps"] == 7000
    assert score["feedback_version"] == 2
    assert events == 1


@pytest.mark.anyio
async def test_model_quality_score_corrupt_facts_map_read_and_write_errors_separately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """损坏 Score/Event 在 GET 返回 unavailable，在 PATCH/幂等恢复返回 update_failed。

    参数：真实迁移库、管理员身份和两类内部解析故障注入。返回值：无。关键约束：写路径
    必须整体回滚且不能沿用只读错误码；已提交事件损坏时也不能重派伪响应或重复改评分。
    """

    database_url, tenant_id, model_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

        def fail_count(_value: object) -> int:
            """模拟持久计数类型损坏，避免直接破坏受 CHECK 保护的真实表。"""

            # 固定内部文本只验证错误转换，不能把真实数据库值或异常正文带入响应。
            raise ValueError("private corrupt count")

        with monkeypatch.context() as scoped:
            scoped.setattr(score_service, "_bounded_count", fail_count)
            read_failed = await client.get(
                f"/model-quality-scores?model_id={model_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            write_failed = await client.patch(
                "/model-quality-scores",
                headers={
                    "Authorization": f"Bearer {admin_token}",
                    "X-Request-ID": "req_prd048_corrupt_score",
                },
                json={
                    "model_id": model_id,
                    "manual_quality_bps": 7500,
                    "expected_feedback_version": 1,
                },
            )

        recovery_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd048_corrupt_event",
        }
        recovery_payload = {
            "model_id": model_id,
            "manual_quality_bps": 7500,
            "expected_feedback_version": 1,
        }
        committed = await client.patch(
            "/model-quality-scores",
            headers=recovery_headers,
            json=recovery_payload,
        )

        def fail_bps(_value: object) -> int | None:
            """模拟不可变反馈事件中的人工基点字段损坏。"""

            # 故障注入不接触真实事件正文，只证明幂等恢复会转换为写路径统一错误码。
            raise ValueError("private corrupt feedback")

        with monkeypatch.context() as scoped:
            scoped.setattr(score_service, "_optional_bps", fail_bps)
            recovery_failed = await client.patch(
                "/model-quality-scores",
                headers=recovery_headers,
                json=recovery_payload,
            )

    assert read_failed.status_code == 503
    assert read_failed.json()["code"] == "model_quality_score.unavailable"
    assert write_failed.status_code == 503
    assert write_failed.json()["code"] == "model_quality_score.update_failed"
    assert committed.status_code == 200, committed.text
    assert recovery_failed.status_code == 503
    assert recovery_failed.json()["code"] == "model_quality_score.update_failed"
    connection = connect_database(database_url)
    score = connection.execute(
        """
        SELECT manual_quality_bps, feedback_version
        FROM model_quality_scores WHERE tenant_id=? AND model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    events = connection.execute(
        "SELECT COUNT(*) FROM model_quality_score_events WHERE tenant_id=? AND model_id=?",
        (tenant_id, model_id),
    ).fetchone()[0]
    assert tuple(score) == (7500, 2)
    assert events == 1
