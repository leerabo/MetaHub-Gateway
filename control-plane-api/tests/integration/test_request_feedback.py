"""PRD-049 Control 反馈 API 的主流程、权限、租户、幂等与只读扩展集成测试。

测试使用完整迁移和真实 SQLite 事务，不 mock shared 领域规则。Token 与测试口令只存在
于当前请求内；断言和数据库扫描不输出 Prompt、Response、Provider Key 或 Virtual Key 明文。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth import AuthContext, require_feedback_summary_user
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import authenticate_bearer
from metahub_control.feedback.routes import router as feedback_router
from metahub_control.model_quality_scores.routes import router as quality_router
from metahub_control.request_logs.routes import router as request_logs_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试身份口令只用于临时数据库登录，不能进入反馈事实、事件 payload 或失败输出。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
# 固定源调用 ID 让 API、详情、聚合与数据库事实可以精确交叉断言。
SUCCESS_REQUEST_ID = "req_prd049_control_success"
FAILED_REQUEST_ID = "req_prd049_control_failed"
ATTRIBUTION_REQUEST_ID = "req_prd049_control_bad_attribution"
HIDDEN_REQUEST_ID = "req_prd049_hidden_success"
FALLBACK_REQUEST_ID = "req_prd049_control_fallback"


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时文件转换为项目数据库层支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证、反馈和两个只读扩展的控制面集成应用。"""

    app = create_service_app("control-plane-api", "PRD-049 Control Feedback Test API")
    app.include_router(auth_router)
    app.include_router(feedback_router)
    app.include_router(request_logs_router)
    app.include_router(quality_router)
    return app


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    """准备默认租户三角色、三类源调用和真实存在的隐藏租户调用。

    返回数据库 URL、默认 tenant/model/provider/key ID。所有 RequestLog 先写不可变 claim，
    与真实 Gateway 写路径保持一致；隐藏租户事实用于证明 404 不是随机 ID 偶然未命中。
    """

    database_url = _sqlite_url(tmp_path / "metahub-prd049-control.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd049-control-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z')
        """,
        [
            (
                "user_prd049_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd049_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None and model is not None and virtual_key is not None
    _insert_request_log(
        connection,
        tenant_id=seed.tenant_id,
        request_id=SUCCESS_REQUEST_ID,
        virtual_key_id=str(virtual_key["virtual_key_id"]),
        provider_id=str(provider["provider_id"]),
        model_id=str(model["model_id"]),
        status="success",
    )
    _insert_request_log(
        connection,
        tenant_id=seed.tenant_id,
        request_id=FAILED_REQUEST_ID,
        virtual_key_id=str(virtual_key["virtual_key_id"]),
        provider_id=str(provider["provider_id"]),
        model_id=str(model["model_id"]),
        status="failed",
    )
    _insert_request_log(
        connection,
        tenant_id=seed.tenant_id,
        request_id=ATTRIBUTION_REQUEST_ID,
        virtual_key_id=str(virtual_key["virtual_key_id"]),
        provider_id=str(provider["provider_id"]),
        model_id=str(model["model_id"]),
        status="success",
        routing_attempt_count=1,
    )
    _insert_hidden_tenant_request(connection)
    connection.commit()
    connection.close()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "provider_id": str(provider["provider_id"]),
        "model_id": str(model["model_id"]),
        "virtual_key_id": str(virtual_key["virtual_key_id"]),
    }


def _insert_request_log(
    connection,
    *,
    tenant_id: str,
    request_id: str,
    virtual_key_id: str | None,
    provider_id: str,
    model_id: str,
    status: str,
    routing_attempt_count: int = 0,
) -> None:
    """按真实 claim -> RequestLog 顺序写一条反馈资格测试源调用。

    参数均是测试数据库已存在的 tenant-qualified ID；成功零 attempt 行验证 legacy
    回退，失败行验证资格错误，count=1 且无 final 行验证归因失败关闭。
    """

    trace_id = f"trc_{request_id}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-19T10:00:00Z')
        """,
        (tenant_id, request_id, trace_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          status, error_code, endpoint, request_model, resolved_model, provider_code,
          routing_attempt_count, created_at, updated_at
        )
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, '/v1/chat/completions',
               'company-default', m.model_alias, p.provider_code, ?,
               '2026-07-19T10:00:00Z', '2026-07-19T10:00:01Z'
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_id=? AND p.provider_id=?
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            virtual_key_id,
            provider_id,
            model_id,
            status,
            None if status == "success" else "gateway.upstream_error",
            routing_attempt_count,
            tenant_id,
            model_id,
            provider_id,
        ),
    )


def _insert_hidden_tenant_request(connection) -> None:
    """创建另一租户真实成功调用，验证控制面跨租户 request_id 与随机 ID 同形 404。"""

    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(
          'tenant_prd049_hidden', 'prd049-hidden', 'PRD049 Hidden',
          'internal_only', 'active', '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z'
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
          'provider_prd049_hidden', 'tenant_prd049_hidden', 'prd049_hidden',
          'Hidden Provider', 'http://127.0.0.1:9/v1', 'active',
          'openai_compatible', 'none', '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z'
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
          'model_prd049_hidden', 'tenant_prd049_hidden', 'provider_prd049_hidden',
          'hidden-chat', 'hidden-chat', '{}', 'active',
          '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z'
        )
        """
    )
    _insert_request_log(
        connection,
        tenant_id="tenant_prd049_hidden",
        request_id=HIDDEN_REQUEST_ID,
        virtual_key_id=None,
        provider_id="provider_prd049_hidden",
        model_id="model_prd049_hidden",
        status="success",
    )


def _insert_fallback_request(
    database_url: str,
    *,
    tenant_id: str,
    virtual_key_id: str,
    primary_model_id: str,
    provider_id: str,
) -> str:
    """写入 primary 失败、fallback 成功的两次 attempt，并返回最终模型 ID。

    RequestLog 先按 claim 创建，再插入两条终态 attempt，最后冻结 count/final ID 与最终
    model 快照。这样反馈资格必须选择第二条 succeeded attempt，不能使用初始模型。
    """

    connection = connect_database(database_url)
    fallback_model_id = "model_prd049_feedback_fallback"
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          ?, ?, ?, 'prd049-fallback-chat', 'prd049-fallback-chat', '{}', 'active',
          '2026-07-19T09:30:00Z', '2026-07-19T09:30:00Z'
        )
        """,
        (fallback_model_id, tenant_id, provider_id),
    )
    _insert_request_log(
        connection,
        tenant_id=tenant_id,
        request_id=FALLBACK_REQUEST_ID,
        virtual_key_id=virtual_key_id,
        provider_id=provider_id,
        model_id=primary_model_id,
        status="success",
    )
    connection.executemany(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id,
          attempt_sequence, attempt_role, trigger_type, trigger_error_code,
          model_id, internal_model_id, provider_id, provider_code,
          status, error_code, error_category, retryable, latency_ms, usage_status,
          created_at, completed_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local_openai', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "rat_prd049_feedback_primary",
                tenant_id,
                FALLBACK_REQUEST_ID,
                f"trc_{FALLBACK_REQUEST_ID}",
                1,
                "primary",
                "initial",
                None,
                primary_model_id,
                "default-chat",
                provider_id,
                "failed",
                "gateway.upstream_timeout",
                "timeout",
                1,
                100,
                "unavailable",
                "2026-07-19T10:00:00Z",
                "2026-07-19T10:00:01Z",
                "2026-07-19T10:00:01Z",
            ),
            (
                "rat_prd049_feedback_fallback",
                tenant_id,
                FALLBACK_REQUEST_ID,
                f"trc_{FALLBACK_REQUEST_ID}",
                2,
                "fallback",
                "fallback",
                "gateway.upstream_timeout",
                fallback_model_id,
                "prd049-fallback-chat",
                provider_id,
                "succeeded",
                None,
                None,
                0,
                80,
                "pending",
                "2026-07-19T10:00:01Z",
                "2026-07-19T10:00:02Z",
                "2026-07-19T10:00:02Z",
            ),
        ],
    )
    connection.execute(
        """
        UPDATE request_logs
        SET model_id=?, resolved_model='prd049-fallback-chat', fallback_triggered=1,
            routing_attempt_count=2, final_routing_attempt_id='rat_prd049_feedback_fallback',
            updated_at='2026-07-19T10:00:02Z'
        WHERE tenant_id=? AND request_id=?
        """,
        (fallback_model_id, tenant_id, FALLBACK_REQUEST_ID),
    )
    connection.commit()
    connection.close()
    return fallback_model_id


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录测试角色并返回仅供当前 AsyncClient 请求头使用的 Session Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只返回给当前测试协程构造 Authorization，不写入数据库、日志或测试快照。
    return str(response.json()["access_token"])


def _payload(request_id: str = SUCCESS_REQUEST_ID, *, rating: int = 5) -> dict[str, object]:
    """生成只含 PRD-049 精确四字段的合法反馈请求体。"""

    return {
        "request_id": request_id,
        "rating": rating,
        "accepted": True,
        "tags": ["helpful", "accurate"],
    }


@pytest.mark.anyio
async def test_control_feedback_main_flow_summary_and_read_extensions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：创建/重放只形成一条事实，并进入报表、日志详情与独立模型统计。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "feedback_operation_control_main",
            "X-Trace-ID": "trace_feedback_control_first",
        }
        # 先通过真实 Session/RBAC 恢复可信租户，再仅隔离 Summary 请求的同步依赖调度。
        # Windows 测试沙箱偶发把线程调度延迟计入 250ms 生产硬门禁；认证锁与权限语义由
        # 相邻专项节点继续使用真实依赖验证，因此这里不能放宽生产时限或伪造 tenant_id。
        trusted_context = authenticate_bearer(
            app.state.config,
            headers["Authorization"],
        )

        async def trusted_summary_user() -> AuthContext:
            """返回经真实数据库和 RBAC 校验的上下文，仅隔离测试线程调度抖动。"""

            return trusted_context

        detail_before = await client.get(
            f"/request-logs/{SUCCESS_REQUEST_ID}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        created = await client.post("/feedback", headers=headers, json=_payload())
        replay = await client.post(
            "/feedback",
            headers={**headers, "X-Trace-ID": "trace_feedback_control_replay"},
            json=_payload(),
        )
        app.dependency_overrides[require_feedback_summary_user] = trusted_summary_user
        try:
            summary = await client.get(
                "/feedback/summary",
                headers={"Authorization": f"Bearer {admin_token}"},
                params={
                    "from_time": "2026-01-01T00:00:00Z",
                    "to_time": "2027-01-01T00:00:00Z",
                },
            )
        finally:
            # 测试应用仍会执行日志与质量查询，依赖替换必须在单次 Summary 后立即撤销。
            app.dependency_overrides.pop(require_feedback_summary_user, None)
        detail = await client.get(
            f"/request-logs/{SUCCESS_REQUEST_ID}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        logs = await client.get(
            "/request-logs",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"request_id": SUCCESS_REQUEST_ID},
        )
        quality = await client.get(
            "/model-quality-scores",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"model_id": fixture["model_id"]},
        )

    assert detail_before.status_code == 200, detail_before.text
    assert detail_before.json()["request_log"]["feedback"] is None
    assert detail_before.json()["request_log"]["feedback_eligible"] is True
    assert detail_before.json()["request_log"]["feedback_ineligible_reason"] == "eligible"
    assert created.status_code == 201, created.text
    assert replay.status_code == 200, replay.text
    assert created.headers["cache-control"] == "no-store, private"
    created_body = created.json()
    replay_body = replay.json()
    assert (
        created_body["request_id"]
        == replay_body["request_id"]
        == "feedback_operation_control_main"
    )
    assert created_body["trace_id"] == "trace_feedback_control_first"
    assert replay_body["trace_id"] == "trace_feedback_control_replay"
    assert created_body["idempotent_replay"] is False
    assert replay_body["idempotent_replay"] is True
    assert created_body["feedback"] == replay_body["feedback"]
    assert created_body["feedback"]["request_id"] == SUCCESS_REQUEST_ID
    assert created_body["feedback"]["tags"] == ["accurate", "helpful"]
    assert not (
        {"tenant_id", "provider_id", "virtual_key_id", "actor_id"}
        & set(created_body["feedback"])
    )

    assert summary.status_code == 200, summary.text
    assert summary.headers["cache-control"] == "no-store, private"
    assert summary.json()["summary"] == {
        "feedback_count": 1,
        "rating_sum": 5,
        "average_rating_milli": 5000,
        "accepted_count": 1,
        "adoption_rate_bps": 10000,
        "rating_distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1},
        "tag_counts": [{"tag": "accurate", "count": 1}, {"tag": "helpful", "count": 1}],
    }
    assert summary.json()["models"][0]["model_id"] == fixture["model_id"]
    assert detail.status_code == logs.status_code == quality.status_code == 200
    detail_body = detail.json()["request_log"]
    assert detail_body["feedback"] == created_body["feedback"]
    assert detail_body["feedback_eligible"] is False
    assert detail_body["feedback_ineligible_reason"] == "already_recorded"
    assert logs.json()["request_logs"][0]["has_feedback"] is True
    quality_item = quality.json()["model_quality_scores"][0]
    assert quality_item["request_feedback_count"] == 1
    assert quality_item["request_feedback_rating_sum"] == 5
    assert quality_item["request_feedback_average_milli"] == 5000
    assert quality_item["request_feedback_accepted_count"] == 1
    assert quality_item["request_feedback_adoption_rate_bps"] == 10000
    assert quality_item["request_feedback_statistics_version"] == 1
    assert quality_item["request_feedback_rating_distribution"]["5"] == 1
    # PRD-049 独立统计不得推进 PRD-048 的真实调用或人工先验版本。
    assert quality_item["sample_count"] == 0
    assert quality_item["statistics_version"] == 0
    assert quality_item["feedback_version"] == 1

    connection = connect_database(fixture["database_url"])
    facts = connection.execute(
        "SELECT COUNT(*) AS total FROM request_feedbacks WHERE tenant_id=?",
        (fixture["tenant_id"],),
    ).fetchone()
    events = connection.execute(
        "SELECT trace_id, payload_json FROM request_feedback_events WHERE tenant_id=?",
        (fixture["tenant_id"],),
    ).fetchall()
    assert facts["total"] == 1 and len(events) == 1
    assert events[0]["trace_id"] == "trace_feedback_control_first"
    event_payload = json.loads(events[0]["payload_json"])
    assert set(event_payload) == {
        "request_feedback_id",
        "request_id",
        "model_id",
        "actor_type",
        "rating",
        "accepted",
        "tags",
    }
    assert not ({"prompt", "response", "token", "key", "provider_id"} & set(event_payload))
    connection.close()


@pytest.mark.anyio
async def test_control_feedback_redacts_session_token_from_chain_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Session Token 即使被复制到链路 Header，也不能进入响应、反馈事实、事件或结构化日志。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    caplog.set_level(logging.INFO)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        request_fragment = f"prefix_{admin_token[:110]}"
        trace_fragment = f"trace_{admin_token[24:88]}_suffix"
        malformed_auth = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Token {admin_token}",
                "X-Request-ID": request_fragment,
                "X-Trace-ID": trace_fragment,
            },
            json=_payload(),
        )
        collision = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": request_fragment,
                "X-Trace-ID": trace_fragment,
            },
            json=_payload(),
        )
        trace_collision = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "feedback_control_safe_operation",
                "X-Trace-ID": trace_fragment,
            },
            json=_payload(),
        )

    assert malformed_auth.status_code == 401
    assert malformed_auth.headers["X-Request-ID"] != request_fragment
    assert malformed_auth.headers["X-Trace-ID"] != trace_fragment
    assert collision.status_code == 422
    assert collision.json()["code"] == "feedback.input_invalid"
    assert collision.json()["request_id"] != request_fragment
    assert collision.json()["trace_id"] != trace_fragment
    assert trace_collision.status_code == 201, trace_collision.text
    assert trace_collision.json()["request_id"] == "feedback_control_safe_operation"
    assert trace_collision.json()["trace_id"] != trace_fragment

    connection = connect_database(fixture["database_url"])
    feedback = connection.execute(
        """
        SELECT operation_request_id, trace_id FROM request_feedbacks
        WHERE tenant_id=? AND source_request_id=?
        """,
        (fixture["tenant_id"], SUCCESS_REQUEST_ID),
    ).fetchone()
    event = connection.execute(
        """
        SELECT operation_request_id, trace_id FROM request_feedback_events
        WHERE tenant_id=? AND source_request_id=?
        """,
        (fixture["tenant_id"], SUCCESS_REQUEST_ID),
    ).fetchone()
    connection.close()
    assert feedback is not None and event is not None
    assert feedback["operation_request_id"] == event["operation_request_id"] == (
        "feedback_control_safe_operation"
    )
    assert feedback["trace_id"] == event["trace_id"] == trace_collision.json()["trace_id"]
    sensitive_fragments = (
        admin_token,
        request_fragment,
        trace_fragment,
        admin_token[:110],
        admin_token[24:88],
    )
    # 响应、持久事实和 LogRecord 都不能保留带装饰的 Token 前缀或中段片段。
    public_values = (
        malformed_auth.text,
        malformed_auth.headers["X-Request-ID"],
        malformed_auth.headers["X-Trace-ID"],
        collision.text,
        trace_collision.text,
        str(feedback["operation_request_id"]),
        str(feedback["trace_id"]),
        str(event["operation_request_id"]),
        str(event["trace_id"]),
    )
    assert not any(fragment in value for fragment in sensitive_fragments for value in public_values)
    assert not any(
        fragment in str(value)
        for fragment in sensitive_fragments
        for record in caplog.records
        for value in vars(record).values()
    )


@pytest.mark.anyio
async def test_control_feedback_permissions_enumeration_validation_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限流程：RBAC 前置，跨租户同形 404，资格与幂等冲突使用稳定错误码。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        anonymous_post = await client.post(
            "/feedback",
            headers={"Content-Type": "application/json"},
            content=b"x" * 5000,
        )
        anonymous_summary = await client.get("/feedback/summary?limit=bad&tenant_id=hidden")
        viewer_post = await client.post(
            "/feedback",
            headers={"Authorization": f"Bearer {viewer_token}", "Content-Type": "application/json"},
            content=b"x" * 5000,
        )
        viewer_summary = await client.get(
            "/feedback/summary?tenant_id=hidden&limit=bad",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        hidden = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback_hidden",
            },
            json=_payload(HIDDEN_REQUEST_ID),
        )
        unknown = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback_unknown",
            },
            json=_payload("req_prd049_unknown"),
        )
        failed = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback_failed",
            },
            json=_payload(FAILED_REQUEST_ID),
        )
        attribution = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback_attribution",
            },
            json=_payload(ATTRIBUTION_REQUEST_ID),
        )
        invalid = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback_invalid",
            },
            json={"request_id": SUCCESS_REQUEST_ID, "rating": True, "accepted": 1, "tags": []},
        )
        duplicate_header = await client.post(
            "/feedback",
            headers=[
                ("Authorization", f"Bearer {developer_token}"),
                ("X-Request-ID", "feedback_duplicate_a"),
                ("X-Request-ID", "feedback_duplicate_b"),
                ("Content-Type", "application/json"),
            ],
            content=json.dumps(_payload()).encode("utf-8"),
        )
        sanitized_header = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {developer_token}",
                "X-Request-ID": "feedback?sanitized-collision",
            },
            json=_payload(),
        )
        first = await client.post(
            "/feedback",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "feedback_conflict"},
            json=_payload(),
        )
        request_conflict = await client.post(
            "/feedback",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "feedback_conflict"},
            json=_payload(rating=4),
        )
        already_exists = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "feedback_second_operation",
            },
            json=_payload(),
        )
        bad_query = await client.get(
            "/feedback/summary?limit=10&limit=20",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert anonymous_post.status_code == anonymous_summary.status_code == 401
    assert anonymous_post.json()["code"] == anonymous_summary.json()["code"] == "auth.token_missing"
    assert viewer_post.status_code == viewer_summary.status_code == 403
    assert viewer_post.json()["code"] == viewer_summary.json()["code"] == "auth.permission_denied"
    for response in (
        anonymous_post,
        anonymous_summary,
        viewer_post,
        viewer_summary,
        hidden,
        unknown,
        failed,
        attribution,
        invalid,
        duplicate_header,
        sanitized_header,
        already_exists,
        bad_query,
    ):
        # 权限 dependency 和异常转换可能先于路由返回，中间件仍必须统一禁止缓存。
        assert response.headers["cache-control"] == "no-store, private"
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json()["code"] == unknown.json()["code"] == "feedback.request_not_found"
    assert failed.status_code == attribution.status_code == 409
    assert (
        failed.json()["code"]
        == attribution.json()["code"]
        == "feedback.request_not_feedbackable"
    )
    assert (
        invalid.status_code
        == duplicate_header.status_code
        == sanitized_header.status_code
        == 422
    )
    assert invalid.json()["code"] == duplicate_header.json()["code"] == "feedback.input_invalid"
    assert sanitized_header.json()["code"] == "feedback.input_invalid"
    assert bad_query.status_code == 422
    assert bad_query.json()["code"] == "feedback.query_invalid"
    assert first.status_code == 201
    assert request_conflict.status_code == already_exists.status_code == 409
    assert request_conflict.json()["code"] == "feedback.request_conflict"
    assert already_exists.json()["code"] == "feedback.already_exists"


@pytest.mark.anyio
async def test_control_feedback_uses_final_succeeded_fallback_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fallback 流程：反馈与详情必须冻结第二条最终成功 attempt 的模型，而非 primary。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    fallback_model_id = _insert_fallback_request(
        fixture["database_url"],
        tenant_id=fixture["tenant_id"],
        virtual_key_id=fixture["virtual_key_id"],
        primary_model_id=fixture["model_id"],
        provider_id=fixture["provider_id"],
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "feedback_fallback_attribution",
            },
            json=_payload(FALLBACK_REQUEST_ID),
        )
        detail = await client.get(
            f"/request-logs/{FALLBACK_REQUEST_ID}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert created.status_code == 201, created.text
    assert created.json()["feedback"]["model_id"] == fallback_model_id
    assert detail.status_code == 200, detail.text
    assert detail.json()["request_log"]["feedback"]["model_id"] == fallback_model_id
    connection = connect_database(fixture["database_url"])
    row = connection.execute(
        """
        SELECT model_id, provider_id FROM request_feedbacks
        WHERE tenant_id=? AND source_request_id=?
        """,
        (fixture["tenant_id"], FALLBACK_REQUEST_ID),
    ).fetchone()
    assert row["model_id"] == fallback_model_id
    assert row["provider_id"] == fixture["provider_id"]
    connection.close()


@pytest.mark.parametrize(
    ("path", "method", "expected_allow"),
    (
        ("/feedback", "GET", "POST"),
        ("/feedback", "PUT", "POST"),
        ("/feedback", "PATCH", "POST"),
        ("/feedback", "DELETE", "POST"),
        ("/feedback/summary", "POST", "GET"),
        ("/feedback/summary", "PATCH", "GET"),
        ("/feedback/summary", "DELETE", "GET"),
    ),
)
@pytest.mark.anyio
async def test_control_feedback_unsupported_method_matrix_is_unified(
    path: str,
    method: str,
    expected_allow: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常/CORS 流程：Control 两条 Feedback 路径只公开设计冻结的方法集合。"""

    allowed_origin = "https://console.prd049.example"
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", allowed_origin)
    route_name = "summary" if path.endswith("/summary") else "create"
    operation_request_id = f"feedback_control_405_{route_name}_{method.lower()}"
    trace_id = f"trace_control_405_{route_name}_{method.lower()}"
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        response = await client.request(
            method,
            path,
            headers={
                "Origin": allowed_origin,
                "X-Request-ID": operation_request_id,
                "X-Trace-ID": trace_id,
            },
        )

    payload = response.json()
    exposed = {
        item.strip().lower()
        for item in response.headers["access-control-expose-headers"].split(",")
    }
    varied = {item.strip().lower() for item in response.headers["vary"].split(",")}
    assert response.status_code == 405
    assert response.headers["allow"] == expected_allow
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["access-control-allow-origin"] == allowed_origin
    assert "origin" in varied
    assert {"x-request-id", "x-trace-id"} <= exposed
    # Allow 仅保留在线路响应，不能扩大所有 API 的浏览器可读头集合。
    assert "allow" not in exposed
    assert payload["code"] == "infra.method_not_allowed"
    assert payload["request_id"] == response.headers["x-request-id"] == operation_request_id
    assert payload["trace_id"] == response.headers["x-trace-id"] == trace_id
    assert payload["diagnosis"]["rule_id"] == "infra_method_not_allowed"
    assert "detail" not in payload
