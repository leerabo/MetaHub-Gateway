"""PRD-050 Prompt Cache 感知路由的 Gateway 服务级边界测试。

本文件使用真实 0001-0040 SQLite 迁移、Virtual Key 鉴权、Alias RoutingPolicy、
Attempt/StreamSession 和 Usage Ledger，只替换最终 Provider 网络对象。测试中的明文 Key、
Prompt 与私有 URL 仅驻留当前进程内存，日志断言禁止输出 Key、前缀、Credential、URL 或正文。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from metahub_gateway.api import openai_compatible as openai_api
from metahub_gateway.api.openai_compatible import router as openai_router
from metahub_gateway.cache_affinity import keypool as affinity_keypool
from metahub_gateway.cache_affinity import lifecycle as affinity_lifecycle
from metahub_gateway.cache_affinity import routing as affinity_routing
from metahub_gateway.policies.runtime import PolicyExecutionResult
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    NormalizedResponse,
    NormalizedUsage,
    ProviderRequest,
    UpstreamApiError,
)
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing import attempts as routing_attempts
from metahub_gateway.routing import models as routing_models
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import (
    encrypt_provider_key,
    secret_fingerprint,
    secret_prefix,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下固定值仅服务隔离测试：明文 Key、Prompt 和私有 URL 只进入内存请求，任何日志、异常
# 或对象 repr 都必须由后续安全断言证明不包含这些值及其可识别前缀。
VIRTUAL_KEY = "mhv_prd050_boundary_virtual"
ENVIRONMENT_KEY = "sk-prd050-boundary-environment-private"
MANAGED_KEY_A = "sk-prd050-boundary-managed-a-private"
MANAGED_KEY_B = "sk-prd050-boundary-managed-b-private"
CACHE_SECRET = "prd050-boundary-cache-hmac-secret-0123456789abcdef"
ENCRYPTION_SECRET = "prd050-boundary-provider-key-secret-0123456789abcdef"
BASE_URL = "http://127.0.0.1:9/v1"
PRIVATE_CHANGED_URL = "https://endpoint-private-marker.invalid/v2"
ALIAS_ID = "alias_prd050_boundary"
ALIAS_NAME = "cache-boundary"
AFFINITY_ID = "caf_" + "b" * 32
PRIVATE_PROMPT = "企业内部缓存边界固定前缀" * 32


@dataclass(frozen=True, slots=True)
class BoundaryGraph:
    """保存单个测试独占的真实租户图，不携带任何明文 Provider Key 或 Prompt。"""

    database_url: str
    tenant_id: str
    virtual_key_id: str
    environment_id: str
    provider_id: str
    model_id: str


class RecordingAdapter(OpenAICompatibleAdapter):
    """用内存成功响应替代外部网络，并保留 repr-safe ProviderRequest 供安全断言。"""

    def __init__(self, cache_tokens: list[int] | None = None) -> None:
        """初始化请求列表和逐次 Provider 缓存 Token；列表为空时统一返回零。"""

        self.requests: list[ProviderRequest] = []
        self._cache_tokens = list(cache_tokens or [])

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """记录已经通过全部服务端闸门的请求，并返回不含业务正文的规范成功响应。"""

        self.requests.append(request)
        cache_tokens = self._cache_tokens.pop(0) if self._cache_tokens else 0
        return NormalizedResponse(
            payload={
                "id": "chatcmpl_prd050_boundary",
                "object": "chat.completion",
                "model": request.payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 1,
                    "total_tokens": 301,
                    "cache_tokens": cache_tokens,
                },
            },
            usage=NormalizedUsage(
                input_tokens=300,
                output_tokens=1,
                total_tokens=301,
                cache_tokens=cache_tokens,
                raw_usage={"cache_tokens": cache_tokens},
            ),
            status_code=200,
        )


class FallbackAdapter(RecordingAdapter):
    """让主 endpoint 返回可 Fallback 的 5xx 事实，而备用 endpoint 正常成功。"""

    def __init__(self, primary_model_id: str) -> None:
        """冻结当前测试的主模型 ID，避免依据客户端 model 文本判断执行分支。"""

        super().__init__()
        self.primary_model_id = primary_model_id

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """主模型抛标准化 5xx，备用模型复用成功实现并形成独立 Selection。"""

        self.requests.append(request)
        # 分支只比较服务端冻结的内部模型 ID，不能使用客户端 alias 或 Prompt 决定 Fallback。
        if request.model_id == self.primary_model_id:
            # 主调用使用固定安全错误元数据，确保异常正文不会进入响应、日志或回退事件。
            raise UpstreamApiError(
                NormalizedProviderError(
                    code="gateway.upstream_error",
                    message="Upstream service failed.",
                    http_status=503,
                    retryable=True,
                    provider_error_type="server_error",
                    error_category="upstream_5xx",
                )
            )
        # 父类也会记录请求，因此直接构造响应，确保每个实际调用只出现一次。
        return NormalizedResponse(
            payload={
                "id": "chatcmpl_prd050_fallback",
                "object": "chat.completion",
                "model": request.payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "fallback-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 1, "total_tokens": 301},
            },
            usage=NormalizedUsage(input_tokens=300, output_tokens=1, total_tokens=301),
            status_code=200,
        )


class HangingStream:
    """永不主动产生首块的可关闭流，用事件取消稳定驱动 watchdog 和断连路径。"""

    def __init__(self) -> None:
        """创建仅由关闭路径设置的事件和幂等关闭计数。"""

        self._closed = asyncio.Event()
        self.close_calls = 0

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """等待关闭事件且不生成数据；读取任务应由超时或客户端断开分支取消。"""

        await self._closed.wait()
        if False:  # pragma: no cover - 仅用于把异步函数声明为字节迭代器。
            yield b""

    async def aclose(self) -> None:
        """幂等释放测试流，并唤醒仍可能处于退出阶段的等待者。"""

        if self._closed.is_set():
            return
        self.close_calls += 1
        self._closed.set()

    async def wait_closed(self) -> None:
        """等待生产清理路径确认关闭，供 ASGI 断连测试同步后台 finally。"""

        await self._closed.wait()


class StreamAdapter(OpenAICompatibleAdapter):
    """返回预置流或标准握手异常，且从不访问真实 Provider Endpoint。"""

    def __init__(self, stream: HangingStream | None, error: ApiError | None = None) -> None:
        """保存单次流脚本和可选握手异常，两者都只存在于测试进程内。"""

        self.stream = stream
        self.error = error
        self.requests: list[ProviderRequest] = []

    async def send_stream(self, request: ProviderRequest) -> HangingStream:
        """记录握手请求；配置异常时抛标准错误，否则返回唯一可控流。"""

        self.requests.append(request)
        # 可选异常只用于握手失败用例；正常 watchdog/断连场景必须取得同一个可控流对象。
        if self.error is not None:
            # 异常已由测试构造为统一 ApiError，禁止再包装包含 URL 或 Provider 正文的信息。
            raise self.error
        assert self.stream is not None
        return self.stream


def _sqlite_url(path: Path) -> str:
    """把隔离数据库路径转换成 shared 数据库模块接受的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_graph(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> BoundaryGraph:
    """应用真实迁移和种子，并创建启用 CacheAffinity 的固定 Alias 租户图。"""

    database_url = _sqlite_url(tmp_path / "prd050-service-boundaries.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", BASE_URL)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", ENVIRONMENT_KEY)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", ENCRYPTION_SECRET)
    monkeypatch.setenv("METAHUB_CACHE_AFFINITY_HMAC_SECRET", CACHE_SECRET)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (seed.tenant_id,),
        ).fetchone()
        model = connection.execute(
            """
            SELECT model_id, provider_id FROM models
            WHERE tenant_id=? AND model_alias='default-chat'
            """,
            (seed.tenant_id,),
        ).fetchone()
        virtual_key = connection.execute(
            "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? AND key_prefix=?",
            (seed.tenant_id, seed.virtual_key_prefix),
        ).fetchone()
        actor = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at, user_id LIMIT 1",
            (seed.tenant_id,),
        ).fetchone()
        assert all(item is not None for item in (environment, model, virtual_key, actor))
        # 每个用例显式决定环境 Key 或托管 Key，避免种子历史行改变稳定排序。
        connection.execute(
            "DELETE FROM provider_credentials WHERE tenant_id=? AND provider_id=?",
            (seed.tenant_id, model["provider_id"]),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, fallback_model_id, status, created_at, updated_at
            ) VALUES(?, ?, ?, 'dev', ?, ?, NULL, 'active', datetime('now'), datetime('now'))
            """,
            (
                ALIAS_ID,
                seed.tenant_id,
                ALIAS_NAME,
                environment["environment_id"],
                model["model_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinities(
              cache_affinity_id, tenant_id, model_alias_id, status, data_class,
              minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
              created_at, updated_at
            ) VALUES(?, ?, ?, 'enabled', 'internal', 256, 3600, 1, ?, ?,
                     datetime('now'), datetime('now'))
            """,
            (
                AFFINITY_ID,
                seed.tenant_id,
                ALIAS_ID,
                actor["user_id"],
                actor["user_id"],
            ),
        )
        connection.commit()
        return BoundaryGraph(
            database_url=database_url,
            tenant_id=str(seed.tenant_id),
            virtual_key_id=str(virtual_key["virtual_key_id"]),
            environment_id=str(environment["environment_id"]),
            provider_id=str(model["provider_id"]),
            model_id=str(model["model_id"]),
        )
    finally:
        connection.close()


def _build_app() -> Any:
    """构建只包含真实 OpenAI-compatible 路由的 Gateway 测试应用。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-050 Boundary API")
    app.include_router(openai_router)
    return app


def _payload(*, model: str = ALIAS_NAME, stream: bool = False) -> dict[str, Any]:
    """构造达到亲和门限的固定 Prompt；正文只进入请求内存，不写测试诊断。"""

    return {
        "model": model,
        "messages": [
            {"role": "system", "content": PRIVATE_PROMPT},
            {"role": "user", "content": "请回答当前边界问题"},
        ],
        "stream": stream,
    }


async def _post(
    client: AsyncClient,
    request_id: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """携带固定 Virtual Key 和唯一 request_id 调用真实 Gateway ASGI 入口。

    附加 Header 只用于构造客户端不可信输入；Authorization 与 X-Request-ID 会按大小写
    过滤后由测试夹具重写，避免调用方绕过鉴权或把事实写到另一个请求标识下。
    """

    headers = {
        name: value
        for name, value in (extra_headers or {}).items()
        if name.lower() not in {"authorization", "x-request-id"}
    }
    headers.update(
        {"Authorization": f"Bearer {VIRTUAL_KEY}", "X-Request-ID": request_id}
    )

    return await client.post(
        "/v1/chat/completions",
        headers=headers,
        json=payload or _payload(),
    )


def _insert_managed_key(
    graph: BoundaryGraph,
    *,
    credential_id: str,
    secret: str,
    status: str = "active",
    provider_id: str | None = None,
) -> None:
    """按真实加密合同写入托管 Key；明文仅传给进程内加密函数。"""

    connection = connect_database(graph.database_url)
    try:
        connection.execute(
            """
            INSERT INTO provider_credentials(
              provider_credential_id, tenant_id, provider_id, key_fingerprint,
              encrypted_key, name, key_prefix, weight, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'PRD050 Boundary Key', ?, 100, ?,
                     datetime('now'), datetime('now'))
            """,
            (
                credential_id,
                graph.tenant_id,
                provider_id or graph.provider_id,
                secret_fingerprint(secret),
                encrypt_provider_key(secret),
                secret_prefix(secret),
                status,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _selection_row(graph: BoundaryGraph, request_id: str) -> sqlite3.Row:
    """按 tenant 和 request 双键读取唯一 Selection，防止测试掩盖跨租户命中。"""

    connection = connect_database(graph.database_url)
    try:
        rows = connection.execute(
            """
            SELECT provider_credential_id_snapshot, target_generation, selection_reason,
                   estimated_hit, outcome_status, cache_affinity_scope_id_snapshot,
                   cache_affinity_binding_id_snapshot, endpoint_revision_digest_snapshot,
                   model_alias_id, model_endpoint_id, ttl_seconds_snapshot,
                   warm_evidence_at_snapshot, binding_state_before
            FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=? ORDER BY attempt_sequence
            """,
            (graph.tenant_id, request_id),
        ).fetchall()
        assert len(rows) == 1
        return rows[0]
    finally:
        connection.close()


def _assert_private_values_absent(text: str, *extra: str) -> None:
    """统一断言日志或 repr 不含明文 Key、前缀、Credential、URL 与 Prompt。"""

    forbidden = (
        VIRTUAL_KEY,
        ENVIRONMENT_KEY,
        MANAGED_KEY_A,
        MANAGED_KEY_B,
        secret_prefix(MANAGED_KEY_A),
        secret_prefix(MANAGED_KEY_B),
        "credential_boundary_a",
        "credential_boundary_b",
        BASE_URL,
        PRIVATE_CHANGED_URL,
        PRIVATE_PROMPT,
        *extra,
    )
    for value in forbidden:
        assert value not in text


@pytest.mark.anyio
async def test_managed_binding_rebinds_across_all_hard_ineligibility_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """绑定 Key 连续 disabled/冷却/新鲜异常/坏 tag/删除时切到下一把并推进 generation。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    _insert_managed_key(
        graph,
        credential_id="credential_boundary_a",
        secret=MANAGED_KEY_A,
    )
    _insert_managed_key(
        graph,
        credential_id="credential_boundary_b",
        secret=MANAGED_KEY_B,
    )
    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    caplog.set_level(logging.WARNING)
    app = _build_app()

    expected_credentials = [
        "credential_boundary_a",
        "credential_boundary_b",
        "credential_boundary_a",
        "credential_boundary_b",
        "credential_boundary_a",
        "credential_boundary_b",
    ]
    generations: list[int] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await _post(client, "req_prd050_key_initial")
        assert first.status_code == 200, first.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_initial")["target_generation"])
        )

        connection = connect_database(graph.database_url)
        connection.execute(
            """
            UPDATE provider_credentials SET status='disabled'
            WHERE provider_credential_id='credential_boundary_a'
            """
        )
        connection.commit()
        disabled = await _post(client, "req_prd050_key_disabled")
        assert disabled.status_code == 200, disabled.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_disabled")["target_generation"])
        )
        connection.execute(
            """
            UPDATE provider_credentials SET status='active'
            WHERE provider_credential_id='credential_boundary_a'
            """
        )
        connection.execute(
            """
            UPDATE provider_credentials
            SET status='rate_limited', rate_limited_until=datetime('now', '+10 minutes')
            WHERE provider_credential_id='credential_boundary_b'
            """
        )
        connection.commit()
        limited = await _post(client, "req_prd050_key_rate_limited")
        assert limited.status_code == 200, limited.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_rate_limited")["target_generation"])
        )
        connection.execute(
            """
            UPDATE provider_credentials SET status='active', rate_limited_until=NULL
            WHERE provider_credential_id='credential_boundary_b'
            """
        )
        connection.execute(
            """
            UPDATE provider_credentials
            SET probe_health_status='unhealthy', probe_last_tested_at=datetime('now')
            WHERE provider_credential_id='credential_boundary_a'
            """
        )
        connection.commit()
        unhealthy = await _post(client, "req_prd050_key_unhealthy")
        assert unhealthy.status_code == 200, unhealthy.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_unhealthy")["target_generation"])
        )
        connection.execute(
            """
            UPDATE provider_credentials SET probe_health_status='unknown', probe_last_tested_at=NULL
            WHERE provider_credential_id='credential_boundary_a'
            """
        )
        connection.execute(
            """
            UPDATE provider_credentials SET encrypted_key='aesgcm-v1:private-bad-tag-marker'
            WHERE provider_credential_id='credential_boundary_b'
            """
        )
        connection.commit()
        corrupt = await _post(client, "req_prd050_key_bad_tag")
        assert corrupt.status_code == 200, corrupt.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_bad_tag")["target_generation"])
        )
        connection.execute(
            """
            UPDATE provider_credentials SET encrypted_key=?
            WHERE provider_credential_id='credential_boundary_b'
            """,
            (encrypt_provider_key(MANAGED_KEY_B),),
        )
        connection.execute(
            "DELETE FROM provider_credentials WHERE provider_credential_id='credential_boundary_a'"
        )
        connection.commit()
        connection.close()
        deleted = await _post(client, "req_prd050_key_deleted")
        assert deleted.status_code == 200, deleted.text
        generations.append(
            int(_selection_row(graph, "req_prd050_key_deleted")["target_generation"])
        )

    rows = [
        _selection_row(graph, request_id)
        for request_id in (
            "req_prd050_key_initial",
            "req_prd050_key_disabled",
            "req_prd050_key_rate_limited",
            "req_prd050_key_unhealthy",
            "req_prd050_key_bad_tag",
            "req_prd050_key_deleted",
        )
    ]
    assert [row["provider_credential_id_snapshot"] for row in rows] == expected_credentials
    assert all(
        later > earlier for earlier, later in zip(generations, generations[1:], strict=False)
    )
    assert [row["selection_reason"] for row in rows[1:]] == ["binding_rebound"] * 5
    assert [row["estimated_hit"] for row in rows] == [0] * 6
    assert [row["outcome_status"] for row in rows] == ["succeeded"] * 6
    assert [request.headers["Authorization"] for request in adapter.requests] == [
        f"Bearer {MANAGED_KEY_A}",
        f"Bearer {MANAGED_KEY_B}",
        f"Bearer {MANAGED_KEY_A}",
        f"Bearer {MANAGED_KEY_B}",
        f"Bearer {MANAGED_KEY_A}",
        f"Bearer {MANAGED_KEY_B}",
    ]
    _assert_private_values_absent(caplog.text, "private-bad-tag-marker")
    _assert_private_values_absent("\n".join(repr(request) for request in adapter.requests))


@pytest.mark.anyio
async def test_managed_key_presence_never_falls_back_to_environment_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """两把托管 Key 均不可用时必须失败关闭，不能绕过治理边界读取环境 Key。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    _insert_managed_key(
        graph,
        credential_id="credential_boundary_a",
        secret=MANAGED_KEY_A,
        status="disabled",
    )
    _insert_managed_key(
        graph,
        credential_id="credential_boundary_b",
        secret=MANAGED_KEY_B,
        status="disabled",
    )
    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    caplog.set_level(logging.WARNING)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _post(client, "req_prd050_no_environment_fallback")

    assert response.status_code >= 400
    assert ENVIRONMENT_KEY not in response.text
    assert adapter.requests == []
    connection = connect_database(graph.database_url)
    try:
        selection_count = connection.execute(
            "SELECT COUNT(*) FROM cache_affinity_selections WHERE tenant_id=? AND request_id=?",
            (graph.tenant_id, "req_prd050_no_environment_fallback"),
        ).fetchone()[0]
        assert selection_count == 0
    finally:
        connection.close()
    _assert_private_values_absent(caplog.text)


@pytest.mark.anyio
async def test_fallback_uses_independent_endpoint_binding_in_same_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主 5xx 后备用 attempt 共享五维 Scope，但按自身 endpoint revision 创建 Binding。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    fallback_model_id = "model_prd050_boundary_fallback"
    connection = connect_database(graph.database_url)
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        ) VALUES(?, ?, ?, 'cache-boundary-fallback', 'cache-boundary-fallback-upstream',
                 '{"chat":true,"stream":true}', 'active', datetime('now'), datetime('now'))
        """,
        (fallback_model_id, graph.tenant_id, graph.provider_id),
    )
    connection.execute(
        """
        UPDATE model_aliases SET fallback_model_id=?, updated_at=datetime('now')
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (fallback_model_id, graph.tenant_id, ALIAS_ID),
    )
    connection.execute(
        """
        INSERT INTO fallback_policies(
          fallback_policy_id, tenant_id, model_alias_id, status, version, priority,
          condition_expr, action, created_at, updated_at
        ) VALUES('fallback_prd050_boundary', ?, ?, 'enabled', 1, 100,
                 '["upstream_timeout","upstream_rate_limited","upstream_5xx"]',
                 '{"type":"use_alias_fallback","max_attempts":1}',
                 datetime('now'), datetime('now'))
        """,
        (graph.tenant_id, ALIAS_ID),
    )
    connection.commit()
    connection.close()
    adapter = FallbackAdapter(graph.model_id)
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _post(client, "req_prd050_fallback_binding")

    assert response.status_code == 200, response.text
    connection = connect_database(graph.database_url)
    try:
        selections = connection.execute(
            """
            SELECT attempt_sequence, model_endpoint_id, cache_affinity_scope_id_snapshot,
                   cache_affinity_binding_id_snapshot, outcome_status
            FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=? ORDER BY attempt_sequence
            """,
            (graph.tenant_id, "req_prd050_fallback_binding"),
        ).fetchall()
        assert [row["attempt_sequence"] for row in selections] == [1, 2]
        assert [row["model_endpoint_id"] for row in selections] == [
            graph.model_id,
            fallback_model_id,
        ]
        assert len({row["cache_affinity_scope_id_snapshot"] for row in selections}) == 1
        assert len({row["cache_affinity_binding_id_snapshot"] for row in selections}) == 2
        assert [row["outcome_status"] for row in selections] == ["failed", "succeeded"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_two_aliases_with_same_domain_share_scope_and_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证同域跨 Alias 共享、请求 TTL 解释和 Scope 级惰性对账。

    参数：隔离 SQLite 目录和网络边界补丁。返回值：无。关键约束：客户端伪造的 data_class
    不参与摘要；短 TTL Alias 的失败 Selection 留在 pending 后，只靠长 TTL Alias 流量也必须
    按同一 Scope 收口。两者对同一 Binding/成功时间分别判断 expired 与 warm。
    """

    graph = _prepare_graph(monkeypatch, tmp_path)
    second_alias_id = "alias_prd050_boundary_second"
    second_alias = "cache-boundary-second"
    second_affinity_id = "caf_" + "c" * 32
    short_ttl_seconds = 60
    long_ttl_seconds = 3600
    seed_request_id = "req_prd050_alias_shared_seed"
    pending_request_id = "req_prd050_alias_shared_a"
    healthy_request_id = "req_prd050_alias_shared_b"
    connection = connect_database(graph.database_url)
    try:
        actor = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at, user_id LIMIT 1",
            (graph.tenant_id,),
        ).fetchone()
        assert actor is not None
        connection.execute(
            """
            UPDATE cache_affinities
            SET ttl_seconds=?, version=version+1, updated_at=datetime('now')
            WHERE tenant_id=? AND cache_affinity_id=?
            """,
            (short_ttl_seconds, graph.tenant_id, AFFINITY_ID),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, fallback_model_id, status, created_at, updated_at
            ) VALUES(?, ?, ?, 'dev', ?, ?, NULL, 'active', datetime('now'), datetime('now'))
            """,
            (
                second_alias_id,
                graph.tenant_id,
                second_alias,
                graph.environment_id,
                graph.model_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinities(
              cache_affinity_id, tenant_id, model_alias_id, status, data_class,
              minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
              created_at, updated_at
            ) VALUES(?, ?, ?, 'enabled', 'internal', 256, ?, 1, ?, ?,
                     datetime('now'), datetime('now'))
            """,
            (
                second_affinity_id,
                graph.tenant_id,
                second_alias_id,
                long_ttl_seconds,
                actor["user_id"],
                actor["user_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    seed_payload = _payload(model=second_alias)
    seed_payload["metadata"] = {"data_class": "client-forged-seed"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        seed = await _post(
            client,
            seed_request_id,
            payload=seed_payload,
            extra_headers={
                "X-Data-Class": "client-forged-confidential",
                "authorization": "Bearer client-forged-key",
                "x-request-id": "client-forged-request-id",
            },
        )
    assert seed.status_code == 200, seed.text
    seed_row = _selection_row(graph, seed_request_id)
    shared_binding_id = str(seed_row["cache_affinity_binding_id_snapshot"])

    # 读取唯一真实成功证据；后续通过统一请求时钟解释 TTL，绝不倒退数据库单调时间。
    # 这也证明共享行不需要 Alias 级 expires_at，长短 TTL 都只读同一个 last_success_at。
    connection = connect_database(graph.database_url)
    try:
        aged = connection.execute(
            """
            SELECT last_success_at FROM cache_affinity_bindings
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (graph.tenant_id, shared_binding_id),
        ).fetchone()
        assert aged is not None and aged["last_success_at"] is not None
        aged_success_at = str(aged["last_success_at"])
    finally:
        connection.close()

    # Routing、父 Attempt、KeyPool 与生命周期必须共享一个未来时刻，确保 Selection、父终态
    # 和 Binding 单调更新时间一致；五分钟恰好落在 60 秒过期、3600 秒仍 warm 的交集。
    request_as_of = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(routing_models, "_utcnow", lambda: request_as_of)
    monkeypatch.setattr(routing_attempts, "_utcnow", lambda: request_as_of)
    monkeypatch.setattr(affinity_keypool, "_utcnow", lambda: request_as_of)
    monkeypatch.setattr(affinity_lifecycle, "_utcnow", lambda: request_as_of)

    observed_states: list[tuple[str, int, str, str]] = []
    original_binding_state = affinity_routing._binding_state_for_request

    def record_binding_state(
        config: Any,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        candidate: object,
        ttl_seconds: int,
        as_of: Any,
    ) -> str:
        """记录服务端 TTL 对同一成功证据的派生状态，并继续执行真实资格校验。"""

        state = original_binding_state(
            config,
            connection,
            row,
            candidate=candidate,
            ttl_seconds=ttl_seconds,
            as_of=as_of,
        )
        if str(row["cache_affinity_binding_id"]) == shared_binding_id:
            observed_states.append(
                (shared_binding_id, ttl_seconds, state, str(row["last_success_at"] or ""))
            )
        return state

    monkeypatch.setattr(affinity_routing, "_binding_state_for_request", record_binding_state)
    original_send_non_stream = adapter.send_non_stream

    async def fail_short_ttl_request(request: ProviderRequest) -> NormalizedResponse:
        """让短 TTL Alias 的父 Attempt 失败，并保留安全、无上游正文的终态。"""

        adapter.requests.append(request)
        # 固定规范错误不携带正文，既能形成父失败终态，也不会污染日志安全断言。
        raise UpstreamApiError(
            NormalizedProviderError(
                code="gateway.upstream_error",
                message="Upstream service failed.",
                http_status=503,
                retryable=False,
                provider_error_type="server_error",
                error_category="upstream_5xx",
            )
        )

    short_payload = _payload()
    short_payload["metadata"] = {"data_class": "client-forged-restricted"}
    with monkeypatch.context() as scoped:
        scoped.setattr(adapter, "send_non_stream", fail_short_ttl_request)
        scoped.setattr(
            openai_api,
            "safe_finalize_cache_affinity_selection",
            lambda *_args, **_kwargs: False,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            pending_response = await _post(
                client,
                pending_request_id,
                payload=short_payload,
                extra_headers={"X-Data-Class": "client-forged-public"},
            )
    assert pending_response.status_code >= 400
    # context 恢复后显式锁定原成功实现，避免实例级异步方法补丁残留到长 TTL 请求。
    assert adapter.send_non_stream == original_send_non_stream

    connection = connect_database(graph.database_url)
    try:
        pending_fact = connection.execute(
            """
            SELECT selection.outcome_status, attempt.status AS attempt_status,
                   binding.active_selection_count
            FROM cache_affinity_selections selection
            JOIN routing_attempts attempt
              ON attempt.tenant_id=selection.tenant_id
             AND attempt.routing_attempt_id=selection.routing_attempt_id
            JOIN cache_affinity_bindings binding
              ON binding.tenant_id=selection.tenant_id
             AND binding.cache_affinity_binding_id=
                 selection.cache_affinity_binding_id_snapshot
            WHERE selection.tenant_id=? AND selection.request_id=?
            """,
            (graph.tenant_id, pending_request_id),
        ).fetchone()
        assert pending_fact is not None
        assert tuple(pending_fact) == ("pending", "failed", 1)
    finally:
        connection.close()

    routing_calls: list[tuple[str | None, str | None]] = []
    keypool_calls: list[tuple[str | None, str | None]] = []
    original_routing_reconcile = affinity_routing.reconcile_pending_cache_affinity_selections
    original_keypool_reconcile = affinity_keypool.reconcile_pending_cache_affinity_selections

    def record_routing_reconcile(
        connection: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        cache_affinity_scope_id_snapshot: str | None = None,
        limit: int = affinity_lifecycle.RECONCILE_BATCH_SIZE,
    ) -> int:
        """记录 Routing 的租户/Scope 所有权边界，并执行真实对账。"""

        routing_calls.append((tenant_id, cache_affinity_scope_id_snapshot))
        return original_routing_reconcile(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=cache_affinity_scope_id_snapshot,
            limit=limit,
        )

    def record_keypool_reconcile(
        connection: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        cache_affinity_scope_id_snapshot: str | None = None,
        limit: int = affinity_lifecycle.RECONCILE_BATCH_SIZE,
    ) -> int:
        """记录 KeyPool 的租户/Scope 所有权边界，并执行真实对账。"""

        keypool_calls.append((tenant_id, cache_affinity_scope_id_snapshot))
        return original_keypool_reconcile(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=cache_affinity_scope_id_snapshot,
            limit=limit,
        )

    monkeypatch.setattr(
        affinity_routing,
        "reconcile_pending_cache_affinity_selections",
        record_routing_reconcile,
    )
    monkeypatch.setattr(
        affinity_keypool,
        "reconcile_pending_cache_affinity_selections",
        record_keypool_reconcile,
    )
    long_payload = _payload(model=second_alias)
    long_payload["metadata"] = {"data_class": "client-forged-secret"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        healthy_response = await _post(
            client,
            healthy_request_id,
            payload=long_payload,
            extra_headers={"X-Data-Class": "client-forged-external"},
        )

    assert healthy_response.status_code == 200, healthy_response.text
    shared_scope_id = str(seed_row["cache_affinity_scope_id_snapshot"])
    assert routing_calls == [(graph.tenant_id, shared_scope_id)]
    assert keypool_calls == [(graph.tenant_id, shared_scope_id)]
    first_row = _selection_row(graph, pending_request_id)
    second_row = _selection_row(graph, healthy_request_id)
    assert first_row["model_alias_id"] == ALIAS_ID
    assert second_row["model_alias_id"] == second_alias_id
    assert {
        seed_row["cache_affinity_scope_id_snapshot"],
        first_row["cache_affinity_scope_id_snapshot"],
        second_row["cache_affinity_scope_id_snapshot"],
    } == {shared_scope_id}
    assert {
        seed_row["cache_affinity_binding_id_snapshot"],
        first_row["cache_affinity_binding_id_snapshot"],
        second_row["cache_affinity_binding_id_snapshot"],
    } == {shared_binding_id}
    assert [first_row["ttl_seconds_snapshot"], second_row["ttl_seconds_snapshot"]] == [
        short_ttl_seconds,
        long_ttl_seconds,
    ]
    assert [first_row["estimated_hit"], second_row["estimated_hit"]] == [0, 1]
    assert [
        first_row["warm_evidence_at_snapshot"],
        second_row["warm_evidence_at_snapshot"],
    ] == [aged_success_at, aged_success_at]
    assert (shared_binding_id, short_ttl_seconds, "expired", aged_success_at) in observed_states
    assert (shared_binding_id, long_ttl_seconds, "warm", aged_success_at) in observed_states
    assert [first_row["outcome_status"], second_row["outcome_status"]] == [
        "failed",
        "succeeded",
    ]
    connection = connect_database(graph.database_url)
    try:
        active_count = connection.execute(
            """
            SELECT active_selection_count FROM cache_affinity_bindings
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (graph.tenant_id, shared_binding_id),
        ).fetchone()
        assert active_count is not None and active_count["active_selection_count"] == 0
    finally:
        connection.close()
    assert [request.request_id for request in adapter.requests] == [
        seed_request_id,
        pending_request_id,
        healthy_request_id,
    ]


@pytest.mark.anyio
async def test_lazy_reconcile_scopes_routing_and_keypool_to_same_tenant_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同租户不同 data_class 的 Scope 不能读取或改写彼此的损坏事实。

    参数：隔离 SQLite 目录和网络边界补丁。返回值：无。关键约束：Alias A 保留父 Attempt
    已成功但 Selection pending、活动计数错误的故障图；Alias B 使用独立 data_class，Routing
    在 Scope 尚不存在时不得对账，KeyPool 建 Scope 后只能按 ``tenant_id + 当前五维 Scope``
    对账并成功形成自己的终态事实。函数名保留以维持既有测试节点的稳定性。
    """

    graph = _prepare_graph(monkeypatch, tmp_path)
    second_alias_id = "alias_prd050_reconcile_second"
    second_alias = "cache-reconcile-second"
    second_affinity_id = "caf_" + "d" * 32
    pending_request_id = "req_prd050_reconcile_alias_a"
    healthy_request_id = "req_prd050_reconcile_alias_b"
    connection = connect_database(graph.database_url)
    try:
        actor = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at, user_id LIMIT 1",
            (graph.tenant_id,),
        ).fetchone()
        assert actor is not None
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id,
              default_model_id, fallback_model_id, status, created_at, updated_at
            ) VALUES(?, ?, ?, 'dev', ?, ?, NULL, 'active', datetime('now'), datetime('now'))
            """,
            (
                second_alias_id,
                graph.tenant_id,
                second_alias,
                graph.environment_id,
                graph.model_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinities(
              cache_affinity_id, tenant_id, model_alias_id, status, data_class,
              minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
              created_at, updated_at
            ) VALUES(?, ?, ?, 'enabled', 'confidential', 256, 3600, 1, ?, ?,
                     datetime('now'), datetime('now'))
            """,
            (
                second_affinity_id,
                graph.tenant_id,
                second_alias_id,
                actor["user_id"],
                actor["user_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()

    # 先让 Alias A 的 Provider 成功、最终化受控降级，得到可由惰性恢复处理的真实 pending；
    # 再破坏活动计数，使任何越界对账都在 Provider 前失败，而不是仅靠调用参数断言放过错误 SQL。
    with monkeypatch.context() as scoped:
        scoped.setattr(
            openai_api,
            "safe_finalize_cache_affinity_selection",
            lambda *_args, **_kwargs: False,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            pending_response = await _post(client, pending_request_id)
    assert pending_response.status_code == 200, pending_response.text

    connection = connect_database(graph.database_url)
    try:
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
            (graph.tenant_id, graph.tenant_id, pending_request_id),
        )
        connection.commit()
        alias_a_before = connection.execute(
            """
            SELECT selection.outcome_status, attempt.status AS attempt_status,
                   attempt.error_code, binding.active_selection_count,
                   selection.cache_affinity_binding_id_snapshot,
                   selection.cache_affinity_scope_id_snapshot
            FROM cache_affinity_selections selection
            JOIN routing_attempts attempt
              ON attempt.tenant_id=selection.tenant_id
             AND attempt.routing_attempt_id=selection.routing_attempt_id
            JOIN cache_affinity_bindings binding
              ON binding.tenant_id=selection.tenant_id
             AND binding.cache_affinity_binding_id=
                 selection.cache_affinity_binding_id_snapshot
            WHERE selection.tenant_id=? AND selection.request_id=?
            """,
            (graph.tenant_id, pending_request_id),
        ).fetchone()
        assert alias_a_before is not None
        assert tuple(alias_a_before)[:4] == ("pending", "succeeded", None, 0)
    finally:
        connection.close()

    routing_calls: list[tuple[str | None, str | None]] = []
    keypool_calls: list[tuple[str | None, str | None]] = []
    original_routing_reconcile = affinity_routing.reconcile_pending_cache_affinity_selections
    original_keypool_reconcile = affinity_keypool.reconcile_pending_cache_affinity_selections

    def record_routing_reconcile(
        connection: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        cache_affinity_scope_id_snapshot: str | None = None,
        limit: int = affinity_lifecycle.RECONCILE_BATCH_SIZE,
    ) -> int:
        """记录 Routing 的租户/Scope 范围，并继续执行真实限定 SQL。"""

        routing_calls.append((tenant_id, cache_affinity_scope_id_snapshot))
        return original_routing_reconcile(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=cache_affinity_scope_id_snapshot,
            limit=limit,
        )

    def record_keypool_reconcile(
        connection: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        cache_affinity_scope_id_snapshot: str | None = None,
        limit: int = affinity_lifecycle.RECONCILE_BATCH_SIZE,
    ) -> int:
        """记录 KeyPool 的租户/Scope 范围，并继续执行真实限定 SQL。"""

        keypool_calls.append((tenant_id, cache_affinity_scope_id_snapshot))
        return original_keypool_reconcile(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=cache_affinity_scope_id_snapshot,
            limit=limit,
        )

    monkeypatch.setattr(
        affinity_routing,
        "reconcile_pending_cache_affinity_selections",
        record_routing_reconcile,
    )
    monkeypatch.setattr(
        affinity_keypool,
        "reconcile_pending_cache_affinity_selections",
        record_keypool_reconcile,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        healthy_response = await _post(
            client,
            healthy_request_id,
            payload=_payload(model=second_alias),
        )

    assert healthy_response.status_code == 200, healthy_response.text
    # B 的 Scope 在 Routing 读取时尚不存在，因此该阶段必须零对账；KeyPool 只能收口刚由
    # Routing 创建的 B Scope，绝不能退回 Alias、租户全域或 A Scope。
    assert routing_calls == []
    connection = connect_database(graph.database_url)
    try:
        alias_a_after = connection.execute(
            """
            SELECT selection.outcome_status, attempt.status AS attempt_status,
                   attempt.error_code, binding.active_selection_count,
                   selection.cache_affinity_binding_id_snapshot,
                   selection.cache_affinity_scope_id_snapshot
            FROM cache_affinity_selections selection
            JOIN routing_attempts attempt
              ON attempt.tenant_id=selection.tenant_id
             AND attempt.routing_attempt_id=selection.routing_attempt_id
            JOIN cache_affinity_bindings binding
              ON binding.tenant_id=selection.tenant_id
             AND binding.cache_affinity_binding_id=
                 selection.cache_affinity_binding_id_snapshot
            WHERE selection.tenant_id=? AND selection.request_id=?
            """,
            (graph.tenant_id, pending_request_id),
        ).fetchone()
        alias_b = connection.execute(
            """
            SELECT outcome_status, model_alias_id, cache_affinity_binding_id_snapshot,
                   cache_affinity_scope_id_snapshot
            FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, healthy_request_id),
        ).fetchone()
        # A 的损坏事实逐字段保持不变；B 必须提交自己的成功 Selection，且不能复用 A 的 Binding。
        assert alias_a_after is not None and tuple(alias_a_after) == tuple(alias_a_before)
        assert alias_b is not None
        assert tuple(alias_b)[:2] == ("succeeded", second_alias_id)
        assert alias_b["cache_affinity_binding_id_snapshot"] != alias_a_before[
            "cache_affinity_binding_id_snapshot"
        ]
        assert alias_b["cache_affinity_scope_id_snapshot"] != alias_a_before[
            "cache_affinity_scope_id_snapshot"
        ]
        assert keypool_calls == [
            (graph.tenant_id, alias_b["cache_affinity_scope_id_snapshot"])
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_endpoint_identity_mutations_create_distinct_revisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 Provider endpoint 身份字段原地变化均创建独立 revision Binding。

    参数：隔离 SQLite 目录和环境配置补丁。返回值：无。关键约束：provider_model、base_url、
    auth 与 provider_id 每次只改变一项；旧 warm 证据不能跨 revision 复用或产生预计命中。
    """

    graph = _prepare_graph(monkeypatch, tmp_path)
    _insert_managed_key(
        graph,
        credential_id="credential_boundary_a",
        secret=MANAGED_KEY_A,
    )
    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    request_ids = [
        "req_prd050_revision_initial",
        "req_prd050_revision_provider_model",
        "req_prd050_revision_base_url",
        "req_prd050_revision_auth",
        "req_prd050_revision_provider_id",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        initial = await _post(client, request_ids[0])
        assert initial.status_code == 200, initial.text
        connection = connect_database(graph.database_url)
        connection.execute(
            """
            UPDATE models SET provider_model_id='provider-model-revision-v2',
                              updated_at=datetime('now')
            WHERE tenant_id=? AND model_id=?
            """,
            (graph.tenant_id, graph.model_id),
        )
        connection.commit()
        provider_model = await _post(client, request_ids[1])
        assert provider_model.status_code == 200, provider_model.text
        connection.execute(
            """
            UPDATE providers SET base_url=?, updated_at=datetime('now')
            WHERE tenant_id=? AND provider_id=?
            """,
            (PRIVATE_CHANGED_URL, graph.tenant_id, graph.provider_id),
        )
        connection.commit()
        base_url = await _post(client, request_ids[2])
        assert base_url.status_code == 200, base_url.text
        connection.execute(
            """
            UPDATE providers SET auth_type='none', updated_at=datetime('now')
            WHERE tenant_id=? AND provider_id=?
            """,
            (graph.tenant_id, graph.provider_id),
        )
        connection.commit()
        auth = await _post(client, request_ids[3])
        assert auth.status_code == 200, auth.text
        new_provider_id = "provider_prd050_revision_new"
        connection.execute(
            """
            INSERT INTO providers(
              provider_id, tenant_id, provider_code, name, base_url, status,
              provider_type, auth_type, health_status, created_at, updated_at
            ) VALUES(?, ?, 'prd050_revision_new', 'PRD050 Revision Provider', ?, 'active',
                     'openai_compatible', 'none', 'unknown', datetime('now'), datetime('now'))
            """,
            (new_provider_id, graph.tenant_id, PRIVATE_CHANGED_URL),
        )
        connection.execute(
            """
            UPDATE models SET provider_id=?, updated_at=datetime('now')
            WHERE tenant_id=? AND model_id=?
            """,
            (new_provider_id, graph.tenant_id, graph.model_id),
        )
        connection.commit()
        connection.close()
        provider_id = await _post(client, request_ids[4])
        assert provider_id.status_code == 200, provider_id.text

    rows = [_selection_row(graph, request_id) for request_id in request_ids]
    assert len({row["cache_affinity_scope_id_snapshot"] for row in rows}) == 1
    assert len({row["cache_affinity_binding_id_snapshot"] for row in rows}) == 5
    assert len({row["endpoint_revision_digest_snapshot"] for row in rows}) == 5
    assert [row["estimated_hit"] for row in rows] == [0] * 5


async def _raise_rate_limit(*_args: Any, **_kwargs: Any) -> None:
    """模拟并发限流在执行门前拒绝，且不创建可释放的租约对象。"""

    # 统一限流码模拟真实守卫拒绝，不能把 Redis 键或 Virtual Key 明文带入异常。
    raise ApiError("gateway.rate_limit_exceeded")


def _raise_dlp(*_args: Any, **_kwargs: Any) -> None:
    """模拟 DLP 在 Provider 前拒绝；异常只使用统一公开错误码。"""

    # 测试只验证 Selection 时序，不构造可能包含命中文本的 DLP 私有异常。
    raise ApiError("gateway.dlp_blocked")


def _raise_budget(*_args: Any, **_kwargs: Any) -> None:
    """模拟预算守卫在 Attempt/StreamSession 前拒绝且不产生预留。"""

    # 固定预算码证明 Provider 前失败，不允许测试异常携带金额、Scope ID 或正文。
    raise ApiError("gateway.budget_exhausted")


def _raise_attempt_gate(*_args: Any, **_kwargs: Any) -> None:
    """模拟非流 Attempt 创建失败，证明 Selection 必须位于该强制闸门之后。"""

    # Attempt 未提交时不能创建无父执行事实的亲和 Selection，故直接返回审计门错误。
    raise ApiError("gateway.routing_attempt_unavailable")


def _raise_stream_gate(*_args: Any, **_kwargs: Any) -> None:
    """模拟流 Session 创建失败，证明握手 Selection 必须绑定已提交 Session。"""

    # Session 未提交时 Selection 没有合法父门，必须使用固定错误在 Provider 前失败关闭。
    raise ApiError("gateway.stream_session_unavailable")


@pytest.mark.anyio
async def test_all_pre_provider_rejections_leave_zero_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Provider 前配置漂移、存储故障及既有治理闸门均不创建 Selection。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    adapter = RecordingAdapter()
    events: list[Any] = []
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: events.append(event) is None,
    )
    reconcile_calls: list[tuple[str | None, str | None]] = []
    original_reconcile = affinity_routing.reconcile_pending_cache_affinity_selections

    def record_reconcile_scope(
        connection: sqlite3.Connection,
        *,
        tenant_id: str | None = None,
        cache_affinity_scope_id_snapshot: str | None = None,
        limit: int = affinity_lifecycle.RECONCILE_BATCH_SIZE,
    ) -> int:
        """记录请求级惰性恢复范围，并继续执行真实租户/Scope 限定 SQL。"""

        reconcile_calls.append((tenant_id, cache_affinity_scope_id_snapshot))
        return original_reconcile(
            connection,
            tenant_id=tenant_id,
            cache_affinity_scope_id_snapshot=cache_affinity_scope_id_snapshot,
            limit=limit,
        )

    monkeypatch.setattr(
        affinity_routing,
        "reconcile_pending_cache_affinity_selections",
        record_reconcile_scope,
    )
    app = _build_app()
    request_ids: list[str] = []

    def assert_cache_affinity_failure(response: Response, request_id: str) -> None:
        """断言统一亲和错误体和响应头保留当前 request_id，且不泄漏底层异常。"""

        payload = response.json()
        assert response.status_code == 503
        assert response.headers.get("x-request-id") == request_id
        assert set(payload) == {
            "code",
            "message",
            "request_id",
            "trace_id",
            "suggestion",
            "diagnosis",
        }
        assert payload["code"] == "gateway.cache_affinity_unavailable"
        assert payload["request_id"] == request_id
        assert payload["trace_id"] == f"trc_{request_id.removeprefix('req_')}"
        assert isinstance(payload["message"], str) and payload["message"]
        assert isinstance(payload["suggestion"], str) and payload["suggestion"]
        assert isinstance(payload["diagnosis"], dict)

    def selection_count_for(request_id: str) -> int:
        """按租户和请求读取 Selection 数量，确保每个失败场景立即零副作用。"""

        connection = connect_database(graph.database_url)
        try:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM cache_affinity_selections
                    WHERE tenant_id=? AND request_id=?
                    """,
                    (graph.tenant_id, request_id),
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def binding_counter_snapshot() -> tuple[tuple[object, ...], ...]:
        """冻结租户内 Binding 活动、选择和预计命中计数，验证 KeyPool 事务完整回滚。"""

        connection = connect_database(graph.database_url)
        try:
            rows = connection.execute(
                """
                SELECT cache_affinity_binding_id, active_selection_count,
                       selection_count, estimated_hit_count
                FROM cache_affinity_bindings
                WHERE tenant_id=? ORDER BY cache_affinity_binding_id
                """,
                (graph.tenant_id,),
            ).fetchall()
            return tuple(tuple(row) for row in rows)
        finally:
            connection.close()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        direct_id = "req_prd050_gate_direct"
        request_ids.append(direct_id)
        assert (
            await _post(client, direct_id, payload=_payload(model="default-chat"))
        ).status_code == 200

        connection = connect_database(graph.database_url)
        connection.execute(
            """
            UPDATE cache_affinities
            SET status='disabled', version=version+1, updated_at=datetime('now')
            WHERE tenant_id=? AND cache_affinity_id=?
            """,
            (graph.tenant_id, AFFINITY_ID),
        )
        connection.commit()
        disabled_id = "req_prd050_gate_config_disabled"
        request_ids.append(disabled_id)
        assert (await _post(client, disabled_id)).status_code == 200
        connection.execute(
            """
            UPDATE cache_affinities
            SET status='enabled', version=version+1, updated_at=datetime('now')
            WHERE tenant_id=? AND cache_affinity_id=?
            """,
            (graph.tenant_id, AFFINITY_ID),
        )
        connection.commit()

        short_id = "req_prd050_gate_short_prompt"
        request_ids.append(short_id)
        short_payload = {
            "model": ALIAS_NAME,
            "messages": [
                {"role": "system", "content": "短前缀"},
                {"role": "user", "content": "普通问题"},
            ],
        }
        assert (await _post(client, short_id, payload=short_payload)).status_code == 200
        # Direct、disabled 和短 Prompt 都不属于亲和域，连惰性恢复也不得触发；这比只断言
        # “没有新 Selection”更严格，可防止请求暗中改写其他租户的既有 pending 事实。
        assert reconcile_calls == []

        invalid_id = "req_prd050_gate_invalid_prompt"
        request_ids.append(invalid_id)
        invalid = await _post(
            client,
            invalid_id,
            payload={"model": ALIAS_NAME, "messages": [{"content": PRIVATE_PROMPT}]},
        )
        assert invalid.status_code == 422

        route_to_id = "req_prd050_gate_route_to"
        request_ids.append(route_to_id)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                openai_api,
                "evaluate_policy_snapshot",
                lambda *_args, **_kwargs: PolicyExecutionResult(
                    decision="route_to",
                    reason_code="policy.rule_matched",
                    policy_id="policy_prd050_route_to",
                    action_type="route_to",
                    target_model_id=graph.model_id,
                ),
            )
            assert (await _post(client, route_to_id)).status_code == 200

        capability_id = "req_prd050_gate_capability"
        request_ids.append(capability_id)
        connection.execute(
            "UPDATE models SET capabilities=? WHERE tenant_id=? AND model_id=?",
            (json.dumps({"chat": True, "stream": False}), graph.tenant_id, graph.model_id),
        )
        connection.commit()
        capability = await _post(client, capability_id, payload=_payload(stream=True))
        assert capability.status_code == 422
        connection.execute(
            "UPDATE models SET capabilities=? WHERE tenant_id=? AND model_id=?",
            (json.dumps({"chat": True, "stream": True}), graph.tenant_id, graph.model_id),
        )
        connection.commit()
        connection.close()

        config_drift_id = "req_prd050_gate_config_drift"
        request_ids.append(config_drift_id)
        provider_calls_before = len(adapter.requests)
        original_select_provider_access = openai_api.select_provider_access

        def mutate_config_before_keypool(*args: Any, **kwargs: Any) -> Any:
            """在 Route 已冻结旧版本后推进配置，再调用真实 KeyPool 重验。"""

            drift_connection = connect_database(graph.database_url)
            try:
                drift_connection.execute(
                    """
                    UPDATE cache_affinities
                    SET ttl_seconds=ttl_seconds+1, version=version+1,
                        updated_at=datetime('now')
                    WHERE tenant_id=? AND cache_affinity_id=?
                    """,
                    (graph.tenant_id, AFFINITY_ID),
                )
                drift_connection.commit()
            finally:
                drift_connection.close()
            return original_select_provider_access(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                openai_api,
                "select_provider_access",
                mutate_config_before_keypool,
            )
            config_drift = await _post(client, config_drift_id)
        assert_cache_affinity_failure(config_drift, config_drift_id)
        assert len(adapter.requests) == provider_calls_before
        assert selection_count_for(config_drift_id) == 0

        scope_storage_id = "req_prd050_gate_scope_storage"
        request_ids.append(scope_storage_id)
        provider_calls_before = len(adapter.requests)

        def fail_scope_reservation(*_args: Any, **_kwargs: Any) -> None:
            """在 Routing Scope helper 注入固定存储失败。

            参数：透传真实 helper 的位置和关键字参数。返回值：无，始终抛数据库异常。
            关键约束：异常只能用于验证统一错误折叠，不得在 Provider 或 Selection 后触发。
            """

            # 该异常必须由 Routing 折叠为统一亲和错误，不能携带 SQL 或数据库路径。
            raise sqlite3.OperationalError("injected scope reservation failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(affinity_routing, "_ensure_scope", fail_scope_reservation)
            scope_storage = await _post(client, scope_storage_id)
        assert_cache_affinity_failure(scope_storage, scope_storage_id)
        assert len(adapter.requests) == provider_calls_before
        assert selection_count_for(scope_storage_id) == 0

        selection_storage_id = "req_prd050_gate_selection_storage"
        request_ids.append(selection_storage_id)
        provider_calls_before = len(adapter.requests)
        counters_before = binding_counter_snapshot()

        def fail_selection_effects(*_args: Any, **_kwargs: Any) -> None:
            """在 Selection INSERT 后阻断计数更新，验证同一 KeyPool 事务回滚。

            参数：透传计数 helper 的数据库与事实参数。返回值：无，始终抛数据库异常。
            关键约束：故障位于同一写事务内，INSERT、目标变化和计数必须一起不可见。
            """

            # INSERT、目标变更和活动计数必须一起回滚，不能留下半条可见 Selection。
            raise sqlite3.OperationalError("injected selection effects failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(
                affinity_keypool,
                "_record_selection_effects",
                fail_selection_effects,
            )
            selection_storage = await _post(client, selection_storage_id)
        assert_cache_affinity_failure(selection_storage, selection_storage_id)
        assert len(adapter.requests) == provider_calls_before
        assert selection_count_for(selection_storage_id) == 0
        assert binding_counter_snapshot() == counters_before

        gate_cases: tuple[tuple[str, str, Callable[..., Any], bool], ...] = (
            ("req_prd050_gate_dlp", "enforce_dlp_guard", _raise_dlp, False),
            (
                "req_prd050_gate_rate_limit",
                "enforce_virtual_key_rate_limit",
                _raise_rate_limit,
                False,
            ),
            ("req_prd050_gate_budget", "enforce_budget_guard", _raise_budget, False),
            (
                "req_prd050_gate_attempt",
                "create_routing_attempt",
                _raise_attempt_gate,
                False,
            ),
            (
                "req_prd050_gate_stream_session",
                "create_stream_session",
                _raise_stream_gate,
                True,
            ),
        )
        for request_id, attribute, replacement, stream in gate_cases:
            request_ids.append(request_id)
            with monkeypatch.context() as scoped:
                scoped.setattr(openai_api, attribute, replacement)
                response = await _post(client, request_id, payload=_payload(stream=stream))
            assert response.status_code >= 400

    connection = connect_database(graph.database_url)
    try:
        placeholders = ",".join("?" for _item in request_ids)
        selection_count = connection.execute(
            f"""
            SELECT COUNT(*) FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id IN ({placeholders})
            """,  # noqa: S608 - 占位符数量来自测试内固定 request_id 列表。
            (graph.tenant_id, *request_ids),
        ).fetchone()[0]
        assert selection_count == 0
    finally:
        connection.close()
    assert all(event.event_type != "cache.affinity_selected" for event in events)
    assert {request.request_id for request in adapter.requests} == {
        "req_prd050_gate_direct",
        "req_prd050_gate_config_disabled",
        "req_prd050_gate_short_prompt",
        "req_prd050_gate_route_to",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("failure_mode", ["false", "storage_error"])
async def test_warm_finalize_degradation_never_changes_provider_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure_mode: str,
) -> None:
    """Provider 和父 Attempt 成功后，warm CAS 返回 false 或存储异常仍必须返回 HTTP 200。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    adapter = RecordingAdapter()
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    if failure_mode == "false":
        monkeypatch.setattr(
            openai_api,
            "safe_finalize_cache_affinity_selection",
            lambda *_args, **_kwargs: False,
        )
    else:

        def fail_lifecycle_storage(_database_url: str) -> None:
            """只破坏亲和终态连接，父 Attempt、RequestLog 和 Usage 仍使用真实数据库。"""

            # 异常正文刻意包含私有描述；safe finalizer 必须吞掉正文并仅记录固定阶段摘要。
            raise sqlite3.OperationalError("private affinity storage path")

        monkeypatch.setattr(affinity_lifecycle, "connect_database", fail_lifecycle_storage)
    caplog.set_level(logging.WARNING)
    request_id = f"req_prd050_finalize_{failure_mode}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _post(client, request_id)

    assert response.status_code == 200, response.text
    connection = connect_database(graph.database_url)
    try:
        facts = connection.execute(
            """
            SELECT selection.outcome_status, attempt.status, request_log.status,
                   binding.last_success_at
            FROM cache_affinity_selections selection
            JOIN routing_attempts attempt
              ON attempt.tenant_id=selection.tenant_id
             AND attempt.routing_attempt_id=selection.routing_attempt_id
            JOIN request_logs request_log
              ON request_log.tenant_id=selection.tenant_id
             AND request_log.request_id=selection.request_id
            JOIN cache_affinity_bindings binding
              ON binding.tenant_id=selection.tenant_id
             AND binding.cache_affinity_binding_id=selection.cache_affinity_binding_id_snapshot
            WHERE selection.tenant_id=? AND selection.request_id=?
            """,
            (graph.tenant_id, request_id),
        ).fetchone()
        assert tuple(facts) == ("pending", "succeeded", "success", None)
    finally:
        connection.close()
    _assert_private_values_absent(caplog.text, "private affinity storage path")


def _handshake_error() -> UpstreamApiError:
    """构造不携带上游正文的标准超时错误，供真实流 Session 归因。"""

    return UpstreamApiError(
        NormalizedProviderError(
            code="gateway.upstream_timeout",
            message="Upstream request timed out.",
            http_status=None,
            retryable=True,
            provider_error_type="timeout",
            error_category="timeout",
        )
    )


def _stream_terminal(graph: BoundaryGraph, request_id: str) -> sqlite3.Row:
    """读取真实 Session、Selection 与 Binding 联合终态，限定当前租户和请求。"""

    connection = connect_database(graph.database_url)
    try:
        rows = connection.execute(
            """
            SELECT session.status AS stream_status, session.error_code,
                   selection.outcome_status, binding.active_selection_count,
                   binding.last_success_at
            FROM cache_affinity_selections selection
            JOIN stream_sessions session
              ON session.tenant_id=selection.tenant_id
             AND session.stream_session_id=selection.stream_session_id
            JOIN cache_affinity_bindings binding
              ON binding.tenant_id=selection.tenant_id
             AND binding.cache_affinity_binding_id=selection.cache_affinity_binding_id_snapshot
            WHERE selection.tenant_id=? AND selection.request_id=?
            """,
            (graph.tenant_id, request_id),
        ).fetchall()
        assert len(rows) == 1
        return rows[0]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_affinity_stream_handshake_failure_reaches_real_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """握手标准上游失败把 starting Session 和 Selection 一并失败化且不产生 warm。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    adapter = StreamAdapter(None, _handshake_error())
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    caplog.set_level(logging.WARNING)
    app = _build_app()
    request_id = "req_prd050_stream_handshake_boundary"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _post(client, request_id, payload=_payload(stream=True))

    assert response.status_code == 504
    terminal = _stream_terminal(graph, request_id)
    assert tuple(terminal) == (
        "upstream_failed",
        "gateway.upstream_stream_failed",
        "failed",
        0,
        None,
    )
    _assert_private_values_absent(caplog.text)


@pytest.mark.anyio
async def test_affinity_stream_watchdog_timeout_reaches_real_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """无首块流由 watchdog 确定性超时，提交 timed_out/failed 并释放 Binding 活动计数。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    stream = HangingStream()
    adapter = StreamAdapter(stream)
    events: list[Any] = []
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: events.append(event) is None,
    )
    caplog.set_level(logging.WARNING)
    app = _build_app()
    # Pydantic model_copy 不重跑环境下限校验，仅缩短测试 watchdog；生产配置仍保持 1-600 秒。
    app.state.config = app.state.config.model_copy(
        update={"stream_inter_token_timeout_seconds": 0.01}
    )
    request_id = "req_prd050_stream_watchdog_boundary"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _post(client, request_id, payload=_payload(stream=True))

    assert response.status_code == 200
    assert "gateway.upstream_stream_timeout" in response.text
    assert stream.close_calls == 1
    terminal = _stream_terminal(graph, request_id)
    assert tuple(terminal) == (
        "timed_out",
        "gateway.upstream_stream_timeout",
        "failed",
        0,
        None,
    )
    assert [event.event_type for event in events].count("stream.timeout") == 1
    _assert_private_values_absent(caplog.text)


async def _invoke_with_disconnect(app: Any, request_id: str) -> tuple[int, bytes]:
    """直接驱动真实 ASGI app，在响应开始后发送 http.disconnect，避免基于 sleep 的竞态。"""

    encoded = json.dumps(_payload(stream=True), ensure_ascii=False).encode("utf-8")
    request_delivered = False
    response_started = asyncio.Event()
    status_code = 0
    bodies: list[bytes] = []

    async def receive() -> dict[str, Any]:
        """首次返回完整请求体，随后等待响应头并发送唯一客户端断开信号。"""

        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": encoded, "more_body": False}
        await response_started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        """记录响应状态和已产生的安全 SSE 字节，不解释任何业务正文。"""

        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            response_started.set()
        elif message["type"] == "http.response.body":
            bodies.append(bytes(message.get("body", b"")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"gateway"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(encoded)).encode("ascii")),
            (b"authorization", f"Bearer {VIRTUAL_KEY}".encode("ascii")),
            (b"x-request-id", request_id.encode("ascii")),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("gateway", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return status_code, b"".join(bodies)


@pytest.mark.anyio
async def test_affinity_stream_client_disconnect_reaches_real_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """响应头后断连提交 client_disconnected/failed，且不误写 timeout、warm 或响应正文。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    stream = HangingStream()
    adapter = StreamAdapter(stream)
    events: list[Any] = []
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(
        openai_api,
        "enqueue_runtime_event",
        lambda _state, event: events.append(event) is None,
    )
    caplog.set_level(logging.WARNING)
    app = _build_app()
    request_id = "req_prd050_stream_disconnect_boundary"
    status_code, body = await _invoke_with_disconnect(app, request_id)
    await asyncio.wait_for(stream.wait_closed(), timeout=5)

    assert status_code == 200
    assert b"gateway.upstream_stream_timeout" not in body
    assert stream.close_calls == 1
    terminal = _stream_terminal(graph, request_id)
    assert tuple(terminal) == (
        "client_disconnected",
        "gateway.client_disconnected",
        "failed",
        0,
        None,
    )
    assert all(event.event_type != "stream.timeout" for event in events)
    _assert_private_values_absent(caplog.text)


@pytest.mark.anyio
async def test_provider_cache_tokens_and_estimated_hit_remain_independent_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Provider cache_tokens 原样入账；首 miss 可实报缓存，预计 hit 也可实报零。"""

    graph = _prepare_graph(monkeypatch, tmp_path)
    adapter = RecordingAdapter(cache_tokens=[77, 0])
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: adapter)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", lambda _state, _event: True)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await _post(client, "req_prd050_usage_provider_cache")
        second = await _post(client, "req_prd050_usage_estimated_hit")

    assert [first.status_code, second.status_code] == [200, 200]
    connection = connect_database(graph.database_url)
    try:
        rows = connection.execute(
            """
            SELECT usage.request_id, usage.cache_tokens, usage.raw_usage,
                   selection.estimated_hit, selection.outcome_status
            FROM usage_ledger usage
            JOIN cache_affinity_selections selection
              ON selection.tenant_id=usage.tenant_id
             AND selection.request_id=usage.request_id
             AND selection.routing_attempt_id=usage.routing_attempt_id
            WHERE usage.tenant_id=? AND usage.request_id IN (?, ?)
            ORDER BY usage.request_id
            """,
            (
                graph.tenant_id,
                "req_prd050_usage_provider_cache",
                "req_prd050_usage_estimated_hit",
            ),
        ).fetchall()
        assert [(row["request_id"], row["cache_tokens"], row["estimated_hit"]) for row in rows] == [
            ("req_prd050_usage_estimated_hit", 0, 1),
            ("req_prd050_usage_provider_cache", 77, 0),
        ]
        assert [row["outcome_status"] for row in rows] == ["succeeded", "succeeded"]
        assert [json.loads(row["raw_usage"])["cache_tokens"] for row in rows] == [0, 77]
    finally:
        connection.close()
