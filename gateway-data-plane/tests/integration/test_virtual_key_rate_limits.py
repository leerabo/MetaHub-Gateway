"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/integration/test_virtual_key_rate_limits.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_gateway.api.openai_compatible import router as openai_compatible_router
from metahub_gateway.rate_limits import InMemoryRateLimitStore
from metahub_gateway.rate_limits import service as rate_limit_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_secret, secret_prefix
from metahub_shared.runtime.fastapi_app import create_service_app


# 以下测试密钥只用于 Virtual Key 限流夹具，覆盖租户内不同 Key 的隔离计数。
VIRTUAL_KEY_A = "mhv_prd022_virtual_key_a"
VIRTUAL_KEY_B = "mhv_prd022_virtual_key_b"
PROVIDER_KEY = "sk-prd022-provider-secret"


def sqlite_url(path: Path) -> str:
    """生成 SQLite 测试数据库 URL，确保每个集成测试使用独立临时文件。"""

    return f"sqlite:///{path.as_posix()}"


def build_gateway_app(store: InMemoryRateLimitStore | None = None):
    """创建只包含 OpenAI-compatible 路由的测试网关应用。"""

    app = create_service_app("gateway-data-plane", "Gateway PRD-022 Test API")
    app.include_router(openai_compatible_router)
    if store is not None:
        app.state.rate_limit_store = store
    return app


@pytest.fixture()
def fake_upstream():
    """启动可观测的假上游，用于证明超限请求不会继续打 Provider。"""

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """处理测试上游请求，并记录是否真的穿透到了 Provider 适配层。"""

        def do_POST(self) -> None:  # noqa: N802
            """返回最小 OpenAI-compatible chat 响应，用于验证超限前后的请求路径。"""

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append({"path": self.path, "body": body})
            if _contains_slow_marker(body):
                time.sleep(0.25)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "chatcmpl_prd022",
                        "object": "chat.completion",
                        "model": body.get("model", "local-chat-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "pong"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args: object) -> None:
            """关闭 http.server 默认日志，避免测试输出混入请求内容。"""

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_base_url: str,
) -> str:
    """准备带两把 Virtual Key 的测试数据库，用于校验不同 Key 的限流隔离性。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY_A)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
              environment, description, created_by, created_at, updated_at
            )
            VALUES(
              'vk_prd022_b', ?, 'PRD022 B', ?, ?, 'active', 'dev', NULL, NULL,
              datetime('now'), datetime('now')
            )
            """,
            (seed.tenant_id, secret_prefix(VIRTUAL_KEY_B), hash_secret(VIRTUAL_KEY_B)),
        )
        connection.commit()
    finally:
        # Windows 会保留未关闭的 SQLite 句柄，夹具必须在 pytest 清理 basetemp 前主动释放。
        connection.close()
    return database_url


def set_limit_rule(
    database_url: str,
    *,
    virtual_key_id: str,
    rpm: int | None = None,
    tpm: int | None = None,
    concurrency: int | None = None,
) -> None:
    """直接写入测试限流规则，让用例聚焦数据面限流行为而非控制台 API。"""

    connection = connect_database(database_url)
    try:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()["tenant_id"]
        connection.execute(
            """
            INSERT INTO virtual_key_rate_limit_rules(
              rate_limit_rule_id, tenant_id, virtual_key_id, rpm_limit, tpm_limit,
              concurrency_limit, status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
            """,
            (f"rlr_{virtual_key_id}", tenant_id, virtual_key_id, rpm, tpm, concurrency),
        )
        connection.commit()
    finally:
        connection.close()


def seeded_virtual_key_id(database_url: str) -> str:
    """读取种子 Virtual Key 的 ID，供集成测试绑定默认租户下的规则。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT virtual_key_id FROM virtual_keys WHERE key_hash=?",
            (hash_secret(VIRTUAL_KEY_A),),
        ).fetchone()
        return str(row["virtual_key_id"])
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def stable_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定本文件的 RPM 窗口键，避免真实分钟边界把同一用例拆到两个计数窗口。"""

    # 仍保留 tenant、Virtual Key 和维度，测试只冻结时间分桶，不削弱隔离断言。
    monkeypatch.setattr(
        rate_limit_service,
        "_window_key",
        lambda rule, limit_type: (
            f"metahub:rl:{rule.tenant_id}:{rule.virtual_key_id}:{limit_type}:1700000000"
        ),
    )


def set_temporary_override_fixture(
    database_url: str,
    *,
    identity: str,
    tenant_id: str,
    virtual_key_id: str,
    starts_at: datetime,
    expires_at: datetime,
    rpm: int | None = None,
    tpm: int | None = None,
    concurrency: int | None = None,
    status: str = "active",
) -> str:
    """写入一条已由 Control Plane 提交的临时覆盖事实，供 Gateway 黑盒请求测试消费。

    参数：测试数据库、稳定身份、租户/Key、UTC 生效区间、三个可选限制及状态。
    返回：符合正式 0043 形状的 opaque override ID。
    约束：Gateway 测试不重复构造 detector/动作/Audit 全链路，因此仅在隔离数据库中关闭
    Control 所属 owner/release trigger 和外键；正式表字段、CHECK 与 Gateway 查询保持不变。
    """

    path = database_url.removeprefix("sqlite:///")
    digest = hashlib.sha256(identity.encode("ascii")).hexdigest()[:32]
    override_id = f"trl_{digest}"
    start_text = _db_time(starts_at)
    end_text = _db_time(expires_at)
    released = status == "released"
    release_digest = hashlib.sha256(f"{identity}-release".encode("ascii")).hexdigest()[:32]
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        # 这些 trigger 验证 Control 写路径；本测试只验证 Gateway 对已提交事实的只读执行。
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_temporary_rate_limit_overrides_owner_insert_prd053"
        )
        connection.execute(
            "DROP TRIGGER IF EXISTS trg_temporary_rate_limit_overrides_release_update_prd053"
        )
        connection.execute(
            """
            INSERT INTO temporary_rate_limit_overrides(
              temporary_rate_limit_override_id, tenant_id, virtual_key_id,
              source_type, source_id, source_action_id, release_action_id,
              rpm_limit, tpm_limit, concurrency_limit, status, starts_at, expires_at,
              created_by, released_by, released_at, operation_request_id,
              operation_trace_id, created_at, updated_at
            )
            VALUES(?, ?, ?, 'cost_anomaly', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                override_id,
                tenant_id,
                virtual_key_id,
                f"coa_{digest}",
                f"caa_{digest}",
                f"caa_{release_digest}" if released else None,
                rpm,
                tpm,
                concurrency,
                status,
                start_text,
                end_text,
                "usr_prd053_gateway_fixture",
                "usr_prd053_gateway_fixture" if released else None,
                start_text if released else None,
                f"req_{identity}",
                f"trc_{identity}",
                start_text,
                start_text,
            ),
        )
        connection.commit()
        return override_id
    finally:
        connection.close()


def release_temporary_override_fixture(database_url: str, override_id: str) -> None:
    """把隔离夹具中的 active 覆盖切换为 released，模拟 Control 动作已原子提交。"""

    path = database_url.removeprefix("sqlite:///")
    released_at = _db_time(datetime.now(UTC).replace(microsecond=0))
    digest = hashlib.sha256(f"{override_id}-release".encode("ascii")).hexdigest()[:32]
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        created_by = connection.execute(
            """
            SELECT created_by FROM temporary_rate_limit_overrides
            WHERE temporary_rate_limit_override_id=?
            """,
            (override_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE temporary_rate_limit_overrides
            SET status='released', release_action_id=?, released_by=?, released_at=?, updated_at=?
            WHERE temporary_rate_limit_override_id=? AND status='active'
            """,
            (f"caa_{digest}", created_by, released_at, released_at, override_id),
        )
        connection.commit()
    finally:
        connection.close()


def default_tenant_id(database_url: str) -> str:
    """读取种子租户 ID，并在返回前关闭连接。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()
        # 返回前先物化 tenant_id，finally 才能在 Windows 上安全关闭底层文件句柄。
        return str(row["tenant_id"])
    finally:
        connection.close()


def request_log_result(database_url: str, request_id: str) -> tuple[str, str | None]:
    """读取请求日志终态和错误码，并主动释放 Windows SQLite 文件句柄。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT status, error_code FROM request_logs WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return str(row["status"]), row["error_code"]
    finally:
        connection.close()


def _db_time(value: datetime) -> str:
    """把测试时刻编码为 Gateway loader 使用的 canonical UTC 秒级文本。"""

    if value.tzinfo is None:
        # 测试事实也必须使用显式时区，避免夹具掩盖生产 loader 的失败关闭约束。
        raise ValueError("test clock must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.mark.anyio
async def test_temporary_overrides_enforce_strict_dimensions_and_release_recovers_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """PRD-053 主流程：多层规则逐维取最严，429 保留链路 ID，释放后请求立即恢复。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    virtual_key_id = seeded_virtual_key_id(database_url)
    tenant_id = default_tenant_id(database_url)
    set_limit_rule(
        database_url,
        virtual_key_id=virtual_key_id,
        rpm=5,
        tpm=100,
        concurrency=5,
    )
    now = datetime.now(UTC).replace(microsecond=0)
    strict_override_id = set_temporary_override_fixture(
        database_url,
        identity="prd053-strict-rpm",
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=9),
        rpm=1,
    )
    set_temporary_override_fixture(
        database_url,
        identity="prd053-other-dimensions",
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=9),
        rpm=10,
        tpm=50,
        concurrency=2,
    )
    effective = rate_limit_service.load_virtual_key_rate_limit_rule(
        SimpleNamespace(database_url=database_url),
        tenant_id,
        virtual_key_id,
        now=now,
    )
    assert effective is not None
    assert (effective.rpm_limit, effective.tpm_limit, effective.concurrency_limit) == (1, 50, 2)
    app = build_gateway_app(InMemoryRateLimitStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        allowed = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd053_overlay_allowed",
            trace_id="trc_prd053_overlay_allowed",
        )
        blocked = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd053_overlay_blocked",
            trace_id="trc_prd053_overlay_blocked",
        )
        release_temporary_override_fixture(database_url, strict_override_id)
        recovered = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd053_overlay_recovered",
            trace_id="trc_prd053_overlay_recovered",
        )

    after_release = rate_limit_service.load_virtual_key_rate_limit_rule(
        SimpleNamespace(database_url=database_url),
        tenant_id,
        virtual_key_id,
        now=datetime.now(UTC),
    )
    assert after_release is not None
    assert (
        after_release.rpm_limit,
        after_release.tpm_limit,
        after_release.concurrency_limit,
    ) == (5, 50, 2)
    assert [allowed.status_code, blocked.status_code, recovered.status_code] == [200, 429, 200]
    error = blocked.json()
    assert error["code"] == "gateway.rate_limit_exceeded"
    assert error["request_id"] == "req_prd053_overlay_blocked"
    assert error["trace_id"] == "trc_prd053_overlay_blocked"
    assert blocked.headers["X-Request-ID"] == error["request_id"]
    assert blocked.headers["X-Trace-ID"] == error["trace_id"]
    assert len(captured) == 2
    assert request_log_result(database_url, "req_prd053_overlay_blocked") == (
        "failed",
        "gateway.rate_limit_exceeded",
    )


@pytest.mark.anyio
async def test_released_expired_future_and_cross_tenant_overrides_do_not_protect_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """租户和时间边界：四类无效覆盖都不能触发计数器或错误地失败关闭正常请求。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    virtual_key_id = seeded_virtual_key_id(database_url)
    tenant_id = default_tenant_id(database_url)
    now = datetime.now(UTC).replace(microsecond=0)
    for identity, row_tenant, status, starts_at, expires_at in (
        (
            "prd053-released",
            tenant_id,
            "released",
            now - timedelta(minutes=1),
            now + timedelta(minutes=9),
        ),
        (
            "prd053-expired",
            tenant_id,
            "active",
            now - timedelta(minutes=20),
            now - timedelta(minutes=10),
        ),
        (
            "prd053-future",
            tenant_id,
            "active",
            now + timedelta(minutes=10),
            now + timedelta(minutes=20),
        ),
        (
            "prd053-other-tenant",
            "tenant_prd053_other",
            "active",
            now - timedelta(minutes=1),
            now + timedelta(minutes=9),
        ),
    ):
        set_temporary_override_fixture(
            database_url,
            identity=identity,
            tenant_id=row_tenant,
            virtual_key_id=virtual_key_id,
            starts_at=starts_at,
            expires_at=expires_at,
            rpm=1,
            status=status,
        )
    # 不注入 Redis/store：若任一无效覆盖被错误采纳，请求会按 fail-closed 返回 503。
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd053_inactive_overlays",
            trace_id="trc_prd053_inactive_overlays",
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_prd053_inactive_overlays"
    assert response.headers["X-Trace-ID"] == "trc_prd053_inactive_overlays"
    assert len(captured) == 1


@pytest.mark.anyio
async def test_active_temporary_override_store_failure_fails_closed_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """依赖故障：仅有临时覆盖时计数器缺失仍统一返回可追踪 503，且不产生上游成本。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    virtual_key_id = seeded_virtual_key_id(database_url)
    tenant_id = default_tenant_id(database_url)
    now = datetime.now(UTC).replace(microsecond=0)
    set_temporary_override_fixture(
        database_url,
        identity="prd053-store-unavailable",
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=9),
        rpm=1,
    )
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd053_overlay_store_unavailable",
            trace_id="trc_prd053_overlay_store_unavailable",
        )

    error = response.json()
    assert response.status_code == 503
    assert error["code"] == "gateway.rate_limit_unavailable"
    assert error["request_id"] == "req_prd053_overlay_store_unavailable"
    assert error["trace_id"] == "trc_prd053_overlay_store_unavailable"
    assert response.headers["X-Request-ID"] == error["request_id"]
    assert response.headers["X-Trace-ID"] == error["trace_id"]
    assert captured == []
    assert request_log_result(database_url, "req_prd053_overlay_store_unavailable") == (
        "failed",
        "gateway.rate_limit_unavailable",
    )


@pytest.mark.anyio
async def test_rpm_limit_blocks_only_the_exceeded_virtual_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """主流程：同一 Key 达到 RPM 后返回 429，不影响另一把 Key。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    set_limit_rule(database_url, virtual_key_id=seeded_virtual_key_id(database_url), rpm=2)
    app = build_gateway_app(InMemoryRateLimitStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        first = await _chat(client, VIRTUAL_KEY_A, "req_prd022_rpm_1")
        second = await _chat(client, VIRTUAL_KEY_A, "req_prd022_rpm_2")
        blocked = await _chat(client, VIRTUAL_KEY_A, "req_prd022_rpm_3")
        other_key = await _chat(client, VIRTUAL_KEY_B, "req_prd022_rpm_other")

    assert [first.status_code, second.status_code, blocked.status_code, other_key.status_code] == [
        200,
        200,
        429,
        200,
    ]
    assert blocked.json()["code"] == "gateway.rate_limit_exceeded"
    assert len(captured) == 3
    assert request_log_result(database_url, "req_prd022_rpm_3") == (
        "failed",
        "gateway.rate_limit_exceeded",
    )


@pytest.mark.anyio
async def test_tpm_limit_blocks_before_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常流程：TPM 超限在上游调用前拦截，避免产生 Provider 成本。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    set_limit_rule(database_url, virtual_key_id=seeded_virtual_key_id(database_url), tpm=2)
    app = build_gateway_app(InMemoryRateLimitStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(
            client,
            VIRTUAL_KEY_A,
            "req_prd022_tpm",
            content="this prompt is intentionally long",
        )

    assert response.status_code == 429
    assert response.json()["code"] == "gateway.rate_limit_exceeded"
    assert captured == []


@pytest.mark.anyio
async def test_concurrency_limit_releases_after_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """并发：并发限制拒绝同时在途请求，并在完成后释放槽位。"""

    upstream_url, _captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    set_limit_rule(database_url, virtual_key_id=seeded_virtual_key_id(database_url), concurrency=1)
    app = build_gateway_app(InMemoryRateLimitStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        responses = await asyncio.gather(
            _chat(client, VIRTUAL_KEY_A, "req_prd022_concurrency_1", content="slow"),
            _chat(client, VIRTUAL_KEY_A, "req_prd022_concurrency_2", content="slow"),
        )
        after_release = await _chat(client, VIRTUAL_KEY_A, "req_prd022_concurrency_3")

    assert sorted(response.status_code for response in responses) == [200, 429]
    assert after_release.status_code == 200


@pytest.mark.anyio
async def test_rate_limit_store_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_upstream,
) -> None:
    """异常流程：启用限流规则但计数器不可用时 fail closed，并记录失败日志。"""

    upstream_url, captured = fake_upstream
    database_url = prepare_database(monkeypatch, tmp_path, upstream_url)
    set_limit_rule(database_url, virtual_key_id=seeded_virtual_key_id(database_url), rpm=1)
    app = build_gateway_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gateway") as client:
        response = await _chat(client, VIRTUAL_KEY_A, "req_prd022_redis_missing")

    assert response.status_code == 503
    assert response.json()["code"] == "gateway.rate_limit_unavailable"
    assert captured == []
    assert request_log_result(database_url, "req_prd022_redis_missing") == (
        "failed",
        "gateway.rate_limit_unavailable",
    )


async def _chat(
    client: AsyncClient,
    virtual_key: str,
    request_id: str,
    *,
    content: str = "hi",
    trace_id: str | None = None,
):
    """发送 Chat Completions 请求，并固定可断言的 request_id/trace_id 链路头。"""

    headers = {"Authorization": f"Bearer {virtual_key}", "X-Request-ID": request_id}
    # 仅显式传入时覆盖 trace Header，其他旧用例继续验证运行时自动派生链路 ID。
    if trace_id is not None:
        # 显式 trace_id 用于验证统一错误体与响应头一致；未传时仍覆盖运行时自动派生合同。
        headers["X-Trace-ID"] = trace_id
    return await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "default-chat", "messages": [{"role": "user", "content": content}]},
    )


def _contains_slow_marker(body: dict[str, Any]) -> bool:
    """判断请求是否需要让假上游延迟返回。"""

    return "slow" in json.dumps(body, ensure_ascii=False)
