"""PRD-049 Gateway Virtual Key 反馈入口的鉴权、同 Key、幂等与事件队列专项集成测试。

用例使用完整迁移和真实 Virtual Key 哈希认证；Key 明文只作为当前 HTTP Authorization
测试输入，不写数据库正文、反馈事件、结构化日志或断言输出。Gateway 不注册 summary。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.feedback.routes import router as feedback_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_secret, secret_prefix
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.webhooks import WebhookEvent

# 三把测试 Key 只用于当前进程鉴权：A/B 为 active，disabled 验证禁用状态失败关闭。
VIRTUAL_KEY_A = "mhv_prd049_feedback_key_a"
VIRTUAL_KEY_B = "mhv_prd049_feedback_key_b"
VIRTUAL_KEY_DISABLED = "mhv_prd049_feedback_key_disabled"
# 固定源调用区分同 Key、跨 Key 和跨租户三种授权结果。
OWN_REQUEST_ID = "req_prd049_gateway_own"
OTHER_KEY_REQUEST_ID = "req_prd049_gateway_other_key"
HIDDEN_REQUEST_ID = "req_prd049_gateway_hidden_tenant"


def _sqlite_url(path: Path) -> str:
    """把临时数据库路径转换为 shared 数据库层支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app(*, with_queue: bool):
    """构造只含 Feedback POST 的 Gateway，并可安装不消费事件的测试有界队列。

    ``with_queue=True`` 用于检查稳定持久 Event 入队；False/运行中移除队列用于证明 worker
    不可用只产生 best-effort 丢弃告警，不会回滚或把成功响应改成错误。
    """

    app = create_service_app("gateway-data-plane", "PRD-049 Gateway Feedback Test API")
    app.include_router(feedback_router)
    if with_queue:
        app.state.runtime_event_queue = asyncio.Queue(maxsize=8)
    return app


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, str]:
    """准备三把 Key、各自成功调用与另一租户真实调用，返回安全对象 ID。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd049-gateway.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY_A)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    key_a = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND key_hash=?",
        (seed.tenant_id, hash_secret(VIRTUAL_KEY_A)),
    ).fetchone()
    assert provider is not None and model is not None and key_a is not None
    connection.executemany(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, description, created_by, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'dev', NULL, NULL, '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z')
        """,
        [
            (
                "vk_prd049_feedback_b",
                seed.tenant_id,
                "PRD049 Feedback B",
                secret_prefix(VIRTUAL_KEY_B),
                hash_secret(VIRTUAL_KEY_B),
                "active",
            ),
            (
                "vk_prd049_feedback_disabled",
                seed.tenant_id,
                "PRD049 Feedback Disabled",
                secret_prefix(VIRTUAL_KEY_DISABLED),
                hash_secret(VIRTUAL_KEY_DISABLED),
                "disabled",
            ),
        ],
    )
    _insert_request_log(
        connection,
        tenant_id=seed.tenant_id,
        request_id=OWN_REQUEST_ID,
        virtual_key_id=str(key_a["virtual_key_id"]),
        provider_id=str(provider["provider_id"]),
        model_id=str(model["model_id"]),
    )
    _insert_request_log(
        connection,
        tenant_id=seed.tenant_id,
        request_id=OTHER_KEY_REQUEST_ID,
        virtual_key_id="vk_prd049_feedback_b",
        provider_id=str(provider["provider_id"]),
        model_id=str(model["model_id"]),
    )
    _insert_hidden_tenant_request(connection)
    connection.commit()
    connection.close()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "model_id": str(model["model_id"]),
        "key_a_id": str(key_a["virtual_key_id"]),
        "key_b_id": "vk_prd049_feedback_b",
    }


def _insert_request_log(
    connection,
    *,
    tenant_id: str,
    request_id: str,
    virtual_key_id: str | None,
    provider_id: str,
    model_id: str,
) -> None:
    """按 claim -> RequestLog 顺序写一条成功 legacy 调用，供同 Key 资格校验。"""

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
          status, endpoint, request_model, resolved_model, provider_code,
          routing_attempt_count, created_at, updated_at
        )
        SELECT ?, ?, ?, ?, ?, ?, 'success', '/v1/chat/completions',
               'company-default', m.model_alias, p.provider_code, 0,
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
            tenant_id,
            model_id,
            provider_id,
        ),
    )


def _insert_hidden_tenant_request(connection) -> None:
    """创建另一租户真实成功调用，证明跨租户源调用不会被默认租户 Key 枚举。"""

    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(
          'tenant_prd049_gateway_hidden', 'prd049-gateway-hidden', 'Gateway Hidden',
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
          'provider_prd049_gateway_hidden', 'tenant_prd049_gateway_hidden', 'gateway_hidden',
          'Gateway Hidden Provider', 'http://127.0.0.1:9/v1', 'active',
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
          'model_prd049_gateway_hidden', 'tenant_prd049_gateway_hidden',
          'provider_prd049_gateway_hidden', 'gateway-hidden-chat', 'gateway-hidden-chat',
          '{}', 'active', '2026-07-19T09:00:00Z', '2026-07-19T09:00:00Z'
        )
        """
    )
    _insert_request_log(
        connection,
        tenant_id="tenant_prd049_gateway_hidden",
        request_id=HIDDEN_REQUEST_ID,
        virtual_key_id=None,
        provider_id="provider_prd049_gateway_hidden",
        model_id="model_prd049_gateway_hidden",
    )


def _payload(request_id: str, *, rating: int = 5) -> dict[str, object]:
    """生成规范四字段反馈正文，标签乱序用于验证 shared 固定排序。"""

    return {
        "request_id": request_id,
        "rating": rating,
        "accepted": True,
        "tags": ["helpful", "accurate"],
    }


def _headers(secret: str, operation_request_id: str, trace_id: str) -> dict[str, str]:
    """构造只在当前请求使用的 Virtual Key 鉴权与链路头。"""

    return {
        "Authorization": f"Bearer {secret}",
        "X-Request-ID": operation_request_id,
        "X-Trace-ID": trace_id,
    }


@pytest.mark.anyio
async def test_gateway_feedback_same_key_replay_and_best_effort_event_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：同 Key 创建/重放复用事件，worker 不可用仍保留第二把 Key 的成功事实。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app(with_queue=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_gateway_main", "trace_gateway_first"),
            json=_payload(OWN_REQUEST_ID),
        )
        replay = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_gateway_main", "trace_gateway_replay"),
            json=_payload(OWN_REQUEST_ID),
        )
        queue = app.state.runtime_event_queue
        first_event = queue.get_nowait()
        replay_event = queue.get_nowait()
        # 模拟 worker 尚未安装/已经退出；第二把 Key 提交仍应在数据库 commit 后返回 201。
        app.state.runtime_event_queue = None
        worker_unavailable = await client.post(
            "/feedback",
            headers=_headers(
                VIRTUAL_KEY_B,
                "feedback_gateway_no_worker",
                "trace_gateway_no_worker",
            ),
            json=_payload(OTHER_KEY_REQUEST_ID, rating=4),
        )
        no_summary = await client.get(
            "/feedback/summary",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY_A}"},
        )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert worker_unavailable.status_code == 201, worker_unavailable.text
    assert first.headers["cache-control"] == "no-store, private"
    assert first.json()["feedback"] == replay.json()["feedback"]
    assert first.json()["feedback"]["actor_type"] == "virtual_key"
    assert first.json()["feedback"]["tags"] == ["accurate", "helpful"]
    assert replay.json()["trace_id"] == "trace_gateway_replay"
    assert isinstance(first_event, WebhookEvent) and isinstance(replay_event, WebhookEvent)
    assert first_event.event_id == replay_event.event_id
    assert first_event.trace_id == replay_event.trace_id == "trace_gateway_first"
    assert first_event.actor_id == fixture["key_a_id"]
    assert first_event.payload["request_id"] == OWN_REQUEST_ID
    assert no_summary.status_code == 404
    assert no_summary.headers["cache-control"] == "no-store, private"

    connection = connect_database(fixture["database_url"])
    rows = connection.execute(
        """
        SELECT source_request_id, actor_virtual_key_id, actor_user_id
        FROM request_feedbacks WHERE tenant_id=? ORDER BY source_request_id
        """,
        (fixture["tenant_id"],),
    ).fetchall()
    assert [(row["source_request_id"], row["actor_virtual_key_id"]) for row in rows] == [
        (OTHER_KEY_REQUEST_ID, fixture["key_b_id"]),
        (OWN_REQUEST_ID, fixture["key_a_id"]),
    ]
    assert all(row["actor_user_id"] is None for row in rows)
    connection.close()


@pytest.mark.anyio
async def test_gateway_feedback_redacts_virtual_key_from_chain_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Virtual Key 出现在链路 Header 时必须先替换，不能进入响应、DB、事件队列或日志。"""

    fixture = _prepare_database(monkeypatch, tmp_path)
    app = _build_app(with_queue=True)
    caplog.set_level(logging.INFO)
    request_fragment = f"prefix_{VIRTUAL_KEY_A[:20]}"
    trace_fragment = f"trace_{VIRTUAL_KEY_A[4:24]}_suffix"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        malformed_auth = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Token {VIRTUAL_KEY_A}",
                "X-Request-ID": request_fragment,
                "X-Trace-ID": trace_fragment,
            },
            json=_payload(OWN_REQUEST_ID),
        )
        collision = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, request_fragment, trace_fragment),
            json=_payload(OWN_REQUEST_ID),
        )
        trace_collision = await client.post(
            "/feedback",
            headers=_headers(
                VIRTUAL_KEY_A,
                "feedback_gateway_safe_operation",
                trace_fragment,
            ),
            json=_payload(OWN_REQUEST_ID),
        )
        queued_event = app.state.runtime_event_queue.get_nowait()

    assert malformed_auth.status_code == 401
    assert malformed_auth.headers["X-Request-ID"] != request_fragment
    assert malformed_auth.headers["X-Trace-ID"] != trace_fragment
    assert collision.status_code == 422
    assert collision.json()["code"] == "feedback.input_invalid"
    assert collision.json()["request_id"] != request_fragment
    assert collision.json()["trace_id"] != trace_fragment
    assert trace_collision.status_code == 201, trace_collision.text
    assert trace_collision.json()["request_id"] == "feedback_gateway_safe_operation"
    assert trace_collision.json()["trace_id"] != trace_fragment
    assert isinstance(queued_event, WebhookEvent)
    assert queued_event.trace_id == trace_collision.json()["trace_id"]

    connection = connect_database(fixture["database_url"])
    feedback = connection.execute(
        """
        SELECT operation_request_id, trace_id FROM request_feedbacks
        WHERE tenant_id=? AND source_request_id=?
        """,
        (fixture["tenant_id"], OWN_REQUEST_ID),
    ).fetchone()
    event = connection.execute(
        """
        SELECT operation_request_id, trace_id FROM request_feedback_events
        WHERE tenant_id=? AND source_request_id=?
        """,
        (fixture["tenant_id"], OWN_REQUEST_ID),
    ).fetchone()
    connection.close()
    assert feedback is not None and event is not None
    assert feedback["operation_request_id"] == event["operation_request_id"] == (
        "feedback_gateway_safe_operation"
    )
    assert feedback["trace_id"] == event["trace_id"] == trace_collision.json()["trace_id"]
    sensitive_fragments = (
        VIRTUAL_KEY_A,
        request_fragment,
        trace_fragment,
        VIRTUAL_KEY_A[:20],
        VIRTUAL_KEY_A[4:24],
    )
    # 响应、队列、数据库与 LogRecord 均不得保留完整 Key 或带装饰的 Key 片段。
    public_values = (
        malformed_auth.text,
        malformed_auth.headers["X-Request-ID"],
        malformed_auth.headers["X-Trace-ID"],
        collision.text,
        trace_collision.text,
        str(queued_event.trace_id),
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
async def test_gateway_feedback_auth_same_key_enumeration_and_wire_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/异常流程：认证先于正文，同 Key 限制及跨租户同形 404，raw 头严格拒绝。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app(with_queue=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        missing_key = await client.post(
            "/feedback",
            headers={"Content-Type": "application/json"},
            content=b"x" * 5000,
        )
        invalid_key = await client.post(
            "/feedback",
            headers={
                "Authorization": "Bearer invalid-prd049-key",
                "Content-Type": "application/json",
            },
            content=b"x" * 5000,
        )
        disabled_key = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_DISABLED, "feedback_disabled", "trace_disabled"),
            json=_payload(OWN_REQUEST_ID),
        )
        other_key = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_cross_key", "trace_cross_key"),
            json=_payload(OTHER_KEY_REQUEST_ID),
        )
        hidden = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_cross_tenant", "trace_cross_tenant"),
            json=_payload(HIDDEN_REQUEST_ID),
        )
        unknown = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_unknown", "trace_unknown"),
            json=_payload("req_prd049_gateway_unknown"),
        )
        invalid_payload = await client.post(
            "/feedback",
            headers=_headers(VIRTUAL_KEY_A, "feedback_bad_payload", "trace_bad_payload"),
            json={"request_id": OWN_REQUEST_ID, "rating": 5, "accepted": None, "tags": []},
        )
        duplicate_header = await client.post(
            "/feedback",
            headers=[
                ("Authorization", f"Bearer {VIRTUAL_KEY_A}"),
                ("X-Request-ID", "feedback_duplicate_a"),
                ("X-Request-ID", "feedback_duplicate_b"),
                ("Content-Type", "application/json"),
            ],
            content=json.dumps(_payload(OWN_REQUEST_ID)).encode("utf-8"),
        )

    assert missing_key.status_code == invalid_key.status_code == disabled_key.status_code == 401
    assert missing_key.json()["code"] == "gateway.auth_missing"
    assert invalid_key.json()["code"] == disabled_key.json()["code"] == "gateway.auth_invalid"
    assert other_key.status_code == hidden.status_code == unknown.status_code == 404
    assert other_key.json()["code"] == hidden.json()["code"] == unknown.json()["code"]
    assert unknown.json()["code"] == "feedback.request_not_found"
    assert invalid_payload.status_code == duplicate_header.status_code == 422
    assert (
        invalid_payload.json()["code"]
        == duplicate_header.json()["code"]
        == "feedback.input_invalid"
    )
    for response in (
        missing_key,
        invalid_key,
        disabled_key,
        other_key,
        hidden,
        unknown,
        invalid_payload,
        duplicate_header,
    ):
        # Gateway 的认证/防枚举/校验错误同样不能被浏览器或代理缓存。
        assert response.headers["cache-control"] == "no-store, private"


@pytest.mark.parametrize("method", ("GET", "PUT", "PATCH", "DELETE"))
@pytest.mark.anyio
async def test_gateway_feedback_unsupported_method_matrix_is_unified(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：Gateway Feedback 仅支持 POST，其他业务方法统一返回 405。"""

    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", "https://console.prd049.example")
    operation_request_id = f"feedback_gateway_405_{method.lower()}"
    trace_id = f"trace_gateway_405_{method.lower()}"
    app = _build_app(with_queue=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.request(
            method,
            "/feedback",
            headers={
                "X-Request-ID": operation_request_id,
                "X-Trace-ID": trace_id,
            },
        )

    payload = response.json()
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["cache-control"] == "no-store, private"
    assert payload["code"] == "infra.method_not_allowed"
    assert payload["request_id"] == response.headers["x-request-id"] == operation_request_id
    assert payload["trace_id"] == response.headers["x-trace-id"] == trace_id
    assert payload["diagnosis"]["rule_id"] == "infra_method_not_allowed"
    assert "detail" not in payload


@pytest.mark.parametrize("method", ("GET", "POST"))
@pytest.mark.anyio
async def test_gateway_feedback_summary_path_remains_not_found(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容流程：Gateway 未注册 summary，任一方法都不能被 405 处理器误报为存在。"""

    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", "https://console.prd049.example")
    operation_request_id = f"feedback_gateway_summary_missing_{method.lower()}"
    app = _build_app(with_queue=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.request(
            method,
            "/feedback/summary",
            headers={"X-Request-ID": operation_request_id},
        )

    payload = response.json()
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store, private"
    assert "allow" not in response.headers
    assert payload["code"] == "infra.not_found"
    assert payload["request_id"] == response.headers["x-request-id"] == operation_request_id
    assert "detail" not in payload


@pytest.mark.anyio
async def test_gateway_feedback_405_cors_and_options_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORS/OPTIONS：405 来源矩阵与合法、非法和普通 OPTIONS 必须保持协议边界。"""

    allowed_origin = "https://console.prd049.example"
    denied_origin = "https://attacker.invalid"
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", allowed_origin)
    app = _build_app(with_queue=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        allowed = await client.get(
            "/feedback",
            headers={
                "Origin": allowed_origin,
                "X-Request-ID": "feedback_gateway_cors_allowed",
                "X-Trace-ID": "trace_gateway_cors_allowed",
            },
        )
        denied = await client.get(
            "/feedback",
            headers={
                "Origin": denied_origin,
                "X-Request-ID": "feedback_gateway_cors_denied",
            },
        )
        no_origin = await client.get(
            "/feedback",
            headers={"X-Request-ID": "feedback_gateway_cors_none"},
        )
        legal_preflight = await client.options(
            "/feedback",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,x-request-id,x-trace-id"
                ),
                "X-Request-ID": "feedback_gateway_preflight_post",
            },
        )
        illegal_preflight = await client.options(
            "/feedback",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "PUT",
                "X-Request-ID": "feedback_gateway_preflight_put",
            },
        )
        plain_options = await client.options(
            "/feedback",
            headers={
                "X-Request-ID": "feedback_gateway_plain_options",
                "X-Trace-ID": "trace_gateway_plain_options",
            },
        )

    assert allowed.status_code == denied.status_code == no_origin.status_code == 405
    assert allowed.headers["access-control-allow-origin"] == allowed_origin
    assert "origin" in allowed.headers["vary"].lower()
    exposed = {
        item.strip().lower()
        for item in allowed.headers["access-control-expose-headers"].split(",")
    }
    assert {"x-request-id", "x-trace-id"} <= exposed
    assert "allow" not in exposed
    # 拒绝来源可以收到普通 HTTP 响应，但不能得到 ACAO，服务也不能反射攻击者 Origin。
    assert "access-control-allow-origin" not in denied.headers
    assert denied_origin not in str(denied.headers)
    assert "access-control-allow-origin" not in no_origin.headers
    assert "access-control-expose-headers" not in no_origin.headers
    assert legal_preflight.status_code == 200
    assert legal_preflight.text == "OK"
    assert legal_preflight.headers["access-control-allow-origin"] == allowed_origin
    assert "POST" in legal_preflight.headers["access-control-allow-methods"]
    assert legal_preflight.headers["cache-control"] == "no-store, private"
    assert illegal_preflight.status_code == 400
    assert "Disallowed CORS method" in illegal_preflight.text
    assert "infra.method_not_allowed" not in illegal_preflight.text
    assert "allow" not in illegal_preflight.headers
    assert illegal_preflight.headers["cache-control"] == "no-store, private"
    assert plain_options.status_code == 405
    assert plain_options.headers["allow"] == "POST"
    assert plain_options.headers["cache-control"] == "no-store, private"
    assert plain_options.json()["code"] == "infra.method_not_allowed"
    assert plain_options.json()["request_id"] == "feedback_gateway_plain_options"
    assert plain_options.json()["trace_id"] == "trace_gateway_plain_options"

    # 通配来源单独构造应用，确认关闭 credentials 后保持星号且不引入 Vary。
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", "*")
    wildcard_app = _build_app(with_queue=False)
    async with AsyncClient(
        transport=ASGITransport(app=wildcard_app),
        base_url="http://gateway",
    ) as wildcard_client:
        wildcard = await wildcard_client.get(
            "/feedback",
            headers={
                "Origin": "https://another-console.example",
                "Cookie": "opaque=session",
                "X-Request-ID": "feedback_gateway_cors_wildcard",
            },
        )

    assert wildcard.status_code == 405
    assert wildcard.headers["access-control-allow-origin"] == "*"
    assert "vary" not in wildcard.headers
    wildcard_exposed = {
        item.strip().lower()
        for item in wildcard.headers["access-control-expose-headers"].split(",")
    }
    assert "allow" not in wildcard_exposed
