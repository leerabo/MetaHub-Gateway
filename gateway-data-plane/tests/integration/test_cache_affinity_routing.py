"""PRD-050 Prompt Cache 感知粘性路由的 Gateway 端到端测试。

测试使用真实 0001-0040 迁移、Virtual Key 认证、RoutingPolicy 事务、attempt 执行门和
Usage/RequestLog 收口，只替换最终网络发送函数。断言与诊断不得输出 Prompt 或 Provider Key。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api import openai_compatible as openai_api
from metahub_gateway.api.openai_compatible import router as openai_router
from metahub_gateway.cache_affinity.lifecycle import recover_cache_affinity_state
from metahub_gateway.providers.base import NormalizedResponse, NormalizedUsage, ProviderRequest
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_secret, secret_prefix
from metahub_shared.runtime.fastapi_app import create_service_app

# 固定测试凭据只在当前进程内构造 Authorization；数据库和断言失败消息不得包含其明文。
VIRTUAL_KEY = "mhv_prd050_virtual_key"
SECOND_VIRTUAL_KEY = "mhv_prd050_second_virtual_key"
ISOLATED_VIRTUAL_KEY = "mhv_prd050_isolated_tenant_key"
PROVIDER_KEY = "sk-prd050-provider-secret"
CACHE_SECRET = "prd050-cache-affinity-hmac-secret-0123456789abcdef"
CACHE_SECRET_B = "prd050-cache-affinity-hmac-secret-fedcba9876543210"
SECOND_VIRTUAL_KEY_ID = "vkey_prd050_second"
ISOLATED_TENANT_ID = "tenant_prd050_isolated"


class CapturingAdapter(OpenAICompatibleAdapter):
    """保留标准请求构造与 Key Header，只用内存成功响应替代真实 Provider 网络。"""

    def __init__(self) -> None:
        """初始化只含 ProviderRequest 对象的调用列表，不复制 Header 或 Payload。"""

        self.requests: list[ProviderRequest] = []

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """记录内存请求对象并返回固定 usage，模拟已经到达上游的成功终态。"""

        self.requests.append(request)
        return NormalizedResponse(
            payload={
                "id": "chatcmpl_prd050",
                "object": "chat.completion",
                "model": "local-chat-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 1, "total_tokens": 301},
            },
            usage=NormalizedUsage(input_tokens=300, output_tokens=1, total_tokens=301),
            status_code=200,
        )


class FailingNonStreamAdapter(OpenAICompatibleAdapter):
    """在 Provider 调用阶段返回固定公开错误，用于验证失败终态和租约释放。"""

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """保留真实 ProviderRequest 构造后抛统一错误，不执行任何外部网络访问。"""

        # 测试只抛公开错误码，不携带 Header、Prompt、URL 或底层异常正文。
        raise openai_api.ApiError("gateway.upstream_unavailable")


class ScriptedStream:
    """按顺序输出 SSE 字节或受控异常，并记录上游响应关闭次数。"""

    def __init__(self, steps: list[bytes | BaseException]) -> None:
        """保存单次流脚本；步骤仅存在内存且不得包含真实客户正文或 Key。"""

        self.steps = steps
        self.close_calls = 0
        self._closed = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """逐步让出 SSE 字节；异常步骤模拟已经握手后的上游读取失败。"""

        for value in self.steps:
            await asyncio.sleep(0)
            if isinstance(value, BaseException):
                # 原始异常只能触发统一 SSE 错误，业务响应和日志不得输出其正文。
                raise value
            yield value

    async def aclose(self) -> None:
        """幂等记录资源关闭，证明 completed/failed 路径均释放上游连接。"""

        if self._closed:
            return
        self._closed = True
        self.close_calls += 1


class StreamingAdapter(OpenAICompatibleAdapter):
    """返回注入的可控 SSE 流，同时保留 repr-safe ProviderRequest 供隐私断言。"""

    def __init__(self, stream: ScriptedStream) -> None:
        """保存当前测试唯一流对象和请求列表，不复制敏感 Header 或 Payload。"""

        self.stream = stream
        self.requests: list[ProviderRequest] = []

    async def send_stream(self, request: ProviderRequest) -> ScriptedStream:
        """记录请求对象并返回脚本流，不访问任何真实 Provider Endpoint。"""

        self.requests.append(request)
        # 返回对象只暴露异步字节迭代和幂等关闭接口，不外泄请求内容。
        return self.stream


class BlockingAdapter(OpenAICompatibleAdapter):
    """在 Selection 已提交后永久等待，用任务取消模拟进程崩溃窗口。"""

    def __init__(self) -> None:
        """创建进入信号和永不主动结束的等待信号，供测试精确控制取消时点。"""

        self.entered = asyncio.Event()
        self._never = asyncio.Event()

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """通知非流请求已经越过 Selection 提交点，然后等待调用任务被取消。"""

        self.entered.set()
        await self._never.wait()
        # 永久等待被意外解除代表测试替身合同损坏，只能以固定本地断言失败。
        raise AssertionError("unreachable blocking non-stream adapter")

    async def send_stream(self, request: ProviderRequest) -> ScriptedStream:
        """通知流握手已经越过 Session/Selection 提交点，然后等待任务被取消。"""

        self.entered.set()
        await self._never.wait()
        # 流式等待同样不应自然返回，固定断言不包含 Session、请求正文或上游详情。
        raise AssertionError("unreachable blocking stream adapter")


def _sqlite_url(path: Path) -> str:
    """把隔离测试数据库路径转换为 shared DB 接受的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """应用真实迁移和种子，并配置只服务当前测试租户的本地 Provider 环境 Key。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd050.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    monkeypatch.setenv("METAHUB_CACHE_AFFINITY_HMAC_SECRET", CACHE_SECRET)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    return database_url


def _enable_default_alias_affinity(database_url: str) -> tuple[str, str]:
    """为默认 dev Alias 插入一条租户内 enabled 配置，并返回 tenant/Alias ID。"""

    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    assert tenant is not None
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (tenant["tenant_id"],),
    ).fetchone()
    assert environment is not None and model is not None
    # Direct Model 按设计不得启用亲和，测试必须显式建立持久 Alias 才能进入 PRD-050 路径。
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        ) VALUES(
          'alias_prd050', ?, 'company-cache', 'dev', ?, ?, NULL, 'active',
          '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z'
        )
        """,
        (tenant["tenant_id"], environment["environment_id"], model["model_id"]),
    )
    actor = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
        (tenant["tenant_id"],),
    ).fetchone()
    assert actor is not None
    connection.execute(
        """
        INSERT INTO cache_affinities(
          cache_affinity_id, tenant_id, model_alias_id, status, data_class,
          minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
          created_at, updated_at
        ) VALUES(
          'caf_05000000000000000000000000000000', ?, ?, 'enabled', 'internal',
          256, 3600, 1, ?, ?, '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z'
        )
        """,
        (tenant["tenant_id"], "alias_prd050", actor["user_id"], actor["user_id"]),
    )
    connection.commit()
    # 只返回认证租户与固定 Alias ID，调用方不得从客户端正文补造租户边界。
    return str(tenant["tenant_id"]), "alias_prd050"


def _build_app() -> Any:
    """创建只包含真实 OpenAI-compatible Router 的测试应用，不启动后台网络 worker。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-050 Test API")
    app.include_router(openai_router)
    return app


def _long_payload(*, stream: bool = False) -> dict[str, Any]:
    """构造超过配置门限的固定长前缀请求；正文只保存在测试进程内存。"""

    return {
        "model": "company-cache",
        "messages": [
            {"role": "system", "content": "企业内部制度固定前缀" * 32},
            {"role": "user", "content": "请回答当前问题"},
        ],
        "stream": stream,
    }


def _insert_secondary_virtual_key(database_url: str, tenant_id: str) -> None:
    """为默认租户增加第二把 active Virtual Key，验证同租户 Key 维度仍严格隔离。"""

    connection = connect_database(database_url)
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant_id,),
    ).fetchone()
    assert environment is not None
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        ) VALUES(
          ?, ?, 'PRD050 Secondary', ?, ?, 'active', 'dev', ?,
          '2026-07-20T00:01:00Z', '2026-07-20T00:01:00Z'
        )
        """,
        (
            SECOND_VIRTUAL_KEY_ID,
            tenant_id,
            secret_prefix(SECOND_VIRTUAL_KEY),
            hash_secret(SECOND_VIRTUAL_KEY),
            environment["environment_id"],
        ),
    )
    connection.commit()
    connection.close()


def _insert_isolated_tenant_graph(database_url: str) -> None:
    """创建可独立调用同名 Alias 的第二租户图，验证请求无法跨租户复用 Binding。"""

    connection = connect_database(database_url)
    now = "2026-07-20T00:02:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(
          ?, 'prd050-isolated', 'PRD050 Isolated', 'internal_only', 'active', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, now, now),
    )
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (ISOLATED_TENANT_ID,),
    ).fetchone()
    assert environment is not None
    environment_id = str(environment["environment_id"])
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(
          'user_prd050_isolated', ?, 'admin@prd050.invalid', 'PRD050 Admin',
          'test-only-password-hash', 'admin', 'active', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, now, now),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        ) VALUES(
          'vkey_prd050_isolated', ?, 'PRD050 Isolated Key', ?, ?, 'active',
          'dev', ?, ?, ?
        )
        """,
        (
            ISOLATED_TENANT_ID,
            secret_prefix(ISOLATED_VIRTUAL_KEY),
            hash_secret(ISOLATED_VIRTUAL_KEY),
            environment_id,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, created_at, updated_at
        ) VALUES(
          'provider_prd050_isolated', ?, 'prd050_isolated', 'PRD050 Isolated Provider',
          'http://127.0.0.1:9/v1', 'active', 'openai_compatible', 'none', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, now, now),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        ) VALUES(
          'model_prd050_isolated', ?, 'provider_prd050_isolated',
          'prd050-isolated-chat', 'prd050-isolated-chat',
          '{"chat":true,"stream":true}', 'active', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, now, now),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, created_at, updated_at
        ) VALUES(
          'alias_prd050_isolated', ?, 'company-cache', 'dev', ?,
          'model_prd050_isolated', NULL, 'active', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, environment_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO cache_affinities(
          cache_affinity_id, tenant_id, model_alias_id, status, data_class,
          minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
          created_at, updated_at
        ) VALUES(
          'caf_99999999999999999999999999999999', ?, 'alias_prd050_isolated',
          'enabled', 'internal', 256, 3600, 1,
          'user_prd050_isolated', 'user_prd050_isolated', ?, ?
        )
        """,
        (ISOLATED_TENANT_ID, now, now),
    )
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_same_long_prompt_reuses_binding_after_success_and_stays_private(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 miss/warm、亲和事件队列降级和事件/内存表示的敏感信息边界。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    adapter = CapturingAdapter()
    events: list[Any] = []
    affinity_enqueue_results: list[bool] = []

    def capture_runtime_event(_state: object, event: Any) -> bool:
        """记录安全事件，并模拟首条亲和事件因队列满而返回 false。"""

        events.append(event)
        if event.event_type != "cache.affinity_selected":
            return True
        accepted = bool(affinity_enqueue_results)
        affinity_enqueue_results.append(accepted)
        return accepted

    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        capture_runtime_event,
    )
    app = _build_app()
    prompt = "企业内部制度固定前缀" * 32
    payload = {
        "model": "company-cache",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请回答当前问题"},
        ],
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd050_first"},
            json=payload,
        )
        second = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd050_second"},
            json=payload,
        )

    assert first.status_code == 200, first.json()
    assert second.status_code == 200, second.json()
    # 首条 Selection 已提交后即使 runtime 队列拒绝事件，也不能回滚事实或阻断 Provider。
    assert affinity_enqueue_results == [False, True]
    assert len(adapter.requests) == 2
    assert all(
        request.headers["Authorization"] == f"Bearer {PROVIDER_KEY}"
        for request in adapter.requests
    )
    # ProviderRequest 的 repr 是常见误日志入口，必须同时隐藏 Header、Payload、URL 和 Selection。
    assert prompt not in repr(adapter.requests[0])
    assert PROVIDER_KEY not in repr(adapter.requests[0])

    connection = connect_database(database_url)
    selections = connection.execute(
        """
        SELECT cache_affinity_selection_id, cache_affinity_binding_id_snapshot,
               provider_credential_id_snapshot, target_generation, selection_reason,
               estimated_hit, outcome_status
        FROM cache_affinity_selections
        WHERE tenant_id=? AND request_id IN ('req_prd050_first', 'req_prd050_second')
        ORDER BY created_at, request_id
        """,
        (tenant_id,),
    ).fetchall()
    assert len(selections) == 2
    assert (
        selections[0]["cache_affinity_binding_id_snapshot"]
        == selections[1]["cache_affinity_binding_id_snapshot"]
    )
    assert selections[0]["provider_credential_id_snapshot"] == selections[1][
        "provider_credential_id_snapshot"
    ]
    assert selections[0]["target_generation"] == selections[1]["target_generation"]
    assert [row["estimated_hit"] for row in selections] == [0, 1]
    assert [row["outcome_status"] for row in selections] == ["succeeded", "succeeded"]
    assert selections[0]["selection_reason"] == "binding_created"
    assert selections[1]["selection_reason"] == "binding_warm_reused"

    affinity_events = [event for event in events if event.event_type == "cache.affinity_selected"]
    assert [event.event_id for event in affinity_events] == [
        row["cache_affinity_selection_id"] for row in selections
    ]
    serialized_events = "\n".join(str(event.payload) for event in affinity_events)
    for forbidden in (prompt, PROVIDER_KEY, VIRTUAL_KEY, "internal", "binding_id", "digest"):
        assert forbidden not in serialized_events


@pytest.mark.anyio
async def test_short_prompt_keeps_legacy_path_without_affinity_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """短前缀不满足门限时继续成功调用，但不得创建 Scope、Binding、Selection 或亲和事件。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    adapter = CapturingAdapter()
    events: list[Any] = []
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: events.append(event) is None,
    )
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": "req_prd050_short"},
            json={
                "model": "company-cache",
                "messages": [
                    {"role": "system", "content": "短前缀"},
                    {"role": "user", "content": "正常请求"},
                ],
            },
        )

    assert response.status_code == 200, response.json()
    connection = connect_database(database_url)
    for table in ("cache_affinity_scopes", "cache_affinity_bindings", "cache_affinity_selections"):
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?",  # noqa: S608 - 表名来自固定测试白名单。
            (tenant_id,),
        ).fetchone()[0]
        assert count == 0
    assert all(event.event_type != "cache.affinity_selected" for event in events)


@pytest.mark.anyio
async def test_non_stream_failure_releases_pointer_and_retry_stays_cold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Provider 失败必须终结 Selection、释放最后活动者，并允许后续冷重试成功。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    events: list[Any] = []
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: FailingNonStreamAdapter())
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: events.append(event) is None,
    )
    app = _build_app()
    failed_request_id = "req_prd050_failure_release"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        failed = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": failed_request_id,
            },
            json=_long_payload(),
        )

    assert failed.status_code == 503
    assert failed.json()["code"] == "gateway.upstream_unavailable"
    assert failed.json()["request_id"] == failed_request_id
    connection = connect_database(database_url)
    failed_fact = connection.execute(
        """
        SELECT selection.outcome_status, binding.active_selection_count,
               binding.last_success_at, scope.preferred_binding_id
        FROM cache_affinity_selections selection
        JOIN cache_affinity_bindings binding
          ON binding.tenant_id=selection.tenant_id
         AND binding.cache_affinity_binding_id=selection.cache_affinity_binding_id_snapshot
        JOIN cache_affinity_scopes scope
          ON scope.tenant_id=selection.tenant_id
         AND scope.cache_affinity_scope_id=selection.cache_affinity_scope_id_snapshot
        WHERE selection.tenant_id=? AND selection.request_id=?
        """,
        (tenant_id, failed_request_id),
    ).fetchone()
    assert failed_fact is not None
    assert tuple(failed_fact) == ("failed", 0, None, None)
    connection.close()

    success_adapter = CapturingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: success_adapter)
    retry_request_id = "req_prd050_failure_retry"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        retry = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": retry_request_id,
            },
            json=_long_payload(),
        )

    assert retry.status_code == 200, retry.json()
    connection = connect_database(database_url)
    retry_fact = connection.execute(
        """
        SELECT selection.estimated_hit, selection.outcome_status,
               binding.active_selection_count, binding.last_success_at
        FROM cache_affinity_selections selection
        JOIN cache_affinity_bindings binding
          ON binding.tenant_id=selection.tenant_id
         AND binding.cache_affinity_binding_id=selection.cache_affinity_binding_id_snapshot
        WHERE selection.tenant_id=? AND selection.request_id=?
        """,
        (tenant_id, retry_request_id),
    ).fetchone()
    assert retry_fact is not None
    assert retry_fact["estimated_hit"] == 0
    assert retry_fact["outcome_status"] == "succeeded"
    assert retry_fact["active_selection_count"] == 0
    assert retry_fact["last_success_at"] is not None
    connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fails_after_chunk", "expected_session", "expected_outcome"),
    [
        (False, "completed", "succeeded"),
        (True, "upstream_failed", "failed"),
    ],
    ids=("completed", "failed"),
)
async def test_stream_terminal_controls_warm_evidence_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fails_after_chunk: bool,
    expected_session: str,
    expected_outcome: str,
) -> None:
    """流式 completed 才写 warm；读取失败只返回固定 SSE 错误并释放计数和连接。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    first_chunk = (
        b'data: {"id":"chatcmpl_prd050_stream","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":300,"completion_tokens":1,"total_tokens":301}}\n\n'
    )
    steps: list[bytes | BaseException] = [first_chunk]
    if fails_after_chunk:
        steps.append(RuntimeError("private upstream read failure"))
    else:
        steps.append(b"data: [DONE]\n\n")
    stream = ScriptedStream(steps)
    adapter = StreamingAdapter(stream)
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    request_id = (
        "req_prd050_stream_failed" if fails_after_chunk else "req_prd050_stream_completed"
    )
    prompt = str(_long_payload()["messages"][0]["content"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": request_id,
            },
            json=_long_payload(stream=True),
        )

    assert response.status_code == 200
    assert stream.close_calls == 1
    assert len(adapter.requests) == 1
    assert prompt not in repr(adapter.requests[0])
    assert PROVIDER_KEY not in repr(adapter.requests[0])
    assert "private upstream read failure" not in response.text
    if fails_after_chunk:
        assert "gateway.upstream_unavailable" in response.text
    else:
        assert "[DONE]" in response.text

    connection = connect_database(database_url)
    terminal = connection.execute(
        """
        SELECT session.status AS session_status, session.error_code,
               selection.outcome_status, binding.active_selection_count,
               binding.last_success_at, scope.preferred_binding_id
        FROM cache_affinity_selections selection
        JOIN stream_sessions session
          ON session.tenant_id=selection.tenant_id
         AND session.stream_session_id=selection.stream_session_id
        JOIN cache_affinity_bindings binding
          ON binding.tenant_id=selection.tenant_id
         AND binding.cache_affinity_binding_id=selection.cache_affinity_binding_id_snapshot
        JOIN cache_affinity_scopes scope
          ON scope.tenant_id=selection.tenant_id
         AND scope.cache_affinity_scope_id=selection.cache_affinity_scope_id_snapshot
        WHERE selection.tenant_id=? AND selection.request_id=?
        """,
        (tenant_id, request_id),
    ).fetchone()
    assert terminal is not None
    assert terminal["session_status"] == expected_session
    assert terminal["outcome_status"] == expected_outcome
    assert terminal["active_selection_count"] == 0
    if fails_after_chunk:
        assert terminal["error_code"] == "gateway.upstream_stream_failed"
        assert terminal["last_success_at"] is None
        assert terminal["preferred_binding_id"] is None
    else:
        assert terminal["error_code"] is None
        assert terminal["last_success_at"] is not None
        assert terminal["preferred_binding_id"] is not None
    connection.close()


@pytest.mark.anyio
async def test_startup_recovery_interrupts_pending_attempt_and_stream_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """取消两个已提交 Selection 的调用后，启动恢复必须失败化父门、对账并重算计数。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    # 模拟进程在 Selection 提交后无法持久化任何父/亲和终态；启动恢复仍调用真实数据库实现。
    monkeypatch.setattr(
        openai_api,
        "safe_complete_routing_attempt",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        openai_api,
        "safe_finish_stream_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        openai_api,
        "safe_finalize_cache_affinity_selection",
        lambda *_args, **_kwargs: False,
    )
    app = _build_app()

    attempt_adapter = BlockingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: attempt_adapter)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        attempt_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "req_prd050_recover_attempt",
                },
                json=_long_payload(),
            )
        )
        await asyncio.wait_for(attempt_adapter.entered.wait(), timeout=5)
        attempt_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt_task

    stream_adapter = BlockingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: stream_adapter)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        stream_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "req_prd050_recover_stream",
                },
                json=_long_payload(stream=True),
            )
        )
        await asyncio.wait_for(stream_adapter.entered.wait(), timeout=5)
        stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream_task

    connection = connect_database(database_url)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM cache_affinity_selections
        WHERE tenant_id=? AND outcome_status='pending'
        """,
        (tenant_id,),
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT active_selection_count FROM cache_affinity_bindings WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == 2
    connection.close()

    caplog.set_level(logging.INFO, logger="metahub.gateway.cache_affinity")
    summary = recover_cache_affinity_state(app.state.config)
    assert summary == {
        "reconciled": 2,
        "interrupted_attempts": 1,
        "interrupted_streams": 1,
        "bindings_rebuilt": 1,
    }
    connection = connect_database(database_url)
    selections = connection.execute(
        """
        SELECT request_id, outcome_status FROM cache_affinity_selections
        WHERE tenant_id=? ORDER BY request_id
        """,
        (tenant_id,),
    ).fetchall()
    assert [(row["request_id"], row["outcome_status"]) for row in selections] == [
        ("req_prd050_recover_attempt", "failed"),
        ("req_prd050_recover_stream", "failed"),
    ]
    attempt = connection.execute(
        """
        SELECT status, error_code, usage_status FROM routing_attempts
        WHERE tenant_id=? AND request_id='req_prd050_recover_attempt'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(attempt) == ("failed", "gateway.execution_interrupted", "not_applicable")
    session = connection.execute(
        """
        SELECT status, error_code FROM stream_sessions
        WHERE tenant_id=? AND request_id='req_prd050_recover_stream'
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(session) == ("upstream_failed", "gateway.execution_interrupted")
    binding = connection.execute(
        """
        SELECT active_selection_count, last_success_at
        FROM cache_affinity_bindings WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchone()
    assert tuple(binding) == (0, None)
    scope = connection.execute(
        "SELECT preferred_binding_id FROM cache_affinity_scopes WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    assert scope["preferred_binding_id"] is None
    connection.close()
    for forbidden in (
        PROVIDER_KEY,
        VIRTUAL_KEY,
        CACHE_SECRET,
        str(_long_payload()["messages"][0]["content"]),
    ):
        assert forbidden not in caplog.text


@pytest.mark.anyio
async def test_tenant_virtual_key_and_secret_domains_never_cross_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同 Prompt 在 tenant、Virtual Key 或 HMAC Secret 任一变化时必须 miss，回滚 Secret 可恢复。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    _insert_secondary_virtual_key(database_url, tenant_id)
    _insert_isolated_tenant_graph(database_url)
    adapter = CapturingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    payload = _long_payload()

    app_a = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_a), base_url="http://testserver"
    ) as client:
        first = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_domain_first",
            },
            json=payload,
        )
        second_key = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {SECOND_VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_domain_second_key",
            },
            json=payload,
        )

    monkeypatch.setenv("METAHUB_CACHE_AFFINITY_HMAC_SECRET", CACHE_SECRET_B)
    app_b = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_b), base_url="http://testserver"
    ) as client:
        second_secret = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_domain_second_secret",
            },
            json=payload,
        )

    monkeypatch.setenv("METAHUB_CACHE_AFFINITY_HMAC_SECRET", CACHE_SECRET)
    app_a_restored = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_a_restored), base_url="http://testserver"
    ) as client:
        restored = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_domain_restored",
            },
            json=payload,
        )
        other_tenant = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ISOLATED_VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_domain_other_tenant",
            },
            json=payload,
        )

    for response in (first, second_key, second_secret, restored, other_tenant):
        assert response.status_code == 200, response.json()

    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT selection.request_id, selection.tenant_id,
               selection.cache_affinity_binding_id_snapshot AS binding_id,
               selection.estimated_hit, scope.virtual_key_id,
               scope.affinity_scope_digest
        FROM cache_affinity_selections selection
        JOIN cache_affinity_scopes scope
          ON scope.tenant_id=selection.tenant_id
         AND scope.cache_affinity_scope_id=selection.cache_affinity_scope_id_snapshot
        WHERE selection.request_id LIKE 'req_prd050_domain_%'
        """
    ).fetchall()
    facts = {str(row["request_id"]): row for row in rows}
    assert set(facts) == {
        "req_prd050_domain_first",
        "req_prd050_domain_second_key",
        "req_prd050_domain_second_secret",
        "req_prd050_domain_restored",
        "req_prd050_domain_other_tenant",
    }
    first_fact = facts["req_prd050_domain_first"]
    key_fact = facts["req_prd050_domain_second_key"]
    secret_fact = facts["req_prd050_domain_second_secret"]
    restored_fact = facts["req_prd050_domain_restored"]
    tenant_fact = facts["req_prd050_domain_other_tenant"]
    assert [
        first_fact["estimated_hit"],
        key_fact["estimated_hit"],
        secret_fact["estimated_hit"],
        restored_fact["estimated_hit"],
        tenant_fact["estimated_hit"],
    ] == [0, 0, 0, 1, 0]
    assert first_fact["binding_id"] == restored_fact["binding_id"]
    assert first_fact["affinity_scope_digest"] == restored_fact["affinity_scope_digest"]
    assert key_fact["virtual_key_id"] == SECOND_VIRTUAL_KEY_ID
    assert key_fact["affinity_scope_digest"] != first_fact["affinity_scope_digest"]
    assert secret_fact["affinity_scope_digest"] != first_fact["affinity_scope_digest"]
    assert tenant_fact["tenant_id"] == ISOLATED_TENANT_ID
    assert tenant_fact["affinity_scope_digest"] != first_fact["affinity_scope_digest"]
    connection.close()

    rendered_requests = "\n".join(repr(request) for request in adapter.requests)
    for forbidden in (
        PROVIDER_KEY,
        VIRTUAL_KEY,
        SECOND_VIRTUAL_KEY,
        ISOLATED_VIRTUAL_KEY,
        CACHE_SECRET,
        CACHE_SECRET_B,
        str(payload["messages"][0]["content"]),
    ):
        assert forbidden not in rendered_requests


@pytest.mark.anyio
async def test_lazy_reconcile_never_reads_or_writes_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """租户 B 的亲和请求不能收口或被租户 A 的损坏 pending 事实拖成失败。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    tenant_id, _alias_id = _enable_default_alias_affinity(database_url)
    _insert_isolated_tenant_graph(database_url)
    adapter = CapturingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()

    # 先让租户 A 的 Provider 成功而亲和 finalizer 受控降级，形成父终态已提交的 pending。
    # 随后把活动计数改成不一致值，模拟仅应由启动恢复阻断 ready 的外域损坏事实。
    with monkeypatch.context() as scoped:
        scoped.setattr(
            openai_api,
            "safe_finalize_cache_affinity_selection",
            lambda *_args, **_kwargs: False,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            tenant_a = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "req_prd050_reconcile_tenant_a",
                },
                json=_long_payload(),
            )
    assert tenant_a.status_code == 200, tenant_a.json()
    connection = connect_database(database_url)
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=0
        WHERE tenant_id=?
          AND cache_affinity_binding_id=(
            SELECT cache_affinity_binding_id_snapshot
            FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=?
          )
        """,
        (
            tenant_id,
            tenant_id,
            "req_prd050_reconcile_tenant_a",
        ),
    )
    connection.commit()
    connection.close()

    # 旧的全表惰性恢复会先处理租户 A 并因计数下溢让本请求返回 503；修复后 SQL 只看
    # tenant B + 当前 Alias，因此 Provider 主流程成功，A 的 Selection 仍保持原样。
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        tenant_b = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {ISOLATED_VIRTUAL_KEY}",
                "X-Request-ID": "req_prd050_reconcile_tenant_b",
            },
            json=_long_payload(),
        )
    assert tenant_b.status_code == 200, tenant_b.json()
    connection = connect_database(database_url)
    try:
        tenant_a_outcome = connection.execute(
            """
            SELECT outcome_status FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, "req_prd050_reconcile_tenant_a"),
        ).fetchone()
        tenant_b_outcome = connection.execute(
            """
            SELECT outcome_status FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=?
            """,
            (ISOLATED_TENANT_ID, "req_prd050_reconcile_tenant_b"),
        ).fetchone()
        assert tenant_a_outcome is not None and tenant_a_outcome[0] == "pending"
        assert tenant_b_outcome is not None and tenant_b_outcome[0] == "succeeded"
    finally:
        connection.close()
