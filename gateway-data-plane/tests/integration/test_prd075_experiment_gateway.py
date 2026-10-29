"""贯通验证 PRD-075 Control 创建、Gateway A/B 路由与结果事实。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import metahub_gateway.api.openai_compatible as gateway_api
import metahub_gateway.experiments.repository as experiment_repository
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.experiments.routes import router as experiments_router
from metahub_control.experiments.scheduler import run_experiment_scheduler_once
from metahub_gateway.api.openai_compatible import router as gateway_router
from metahub_gateway.experiments.models import (
    ExperimentExecutionHandle,
    ExperimentTerminalIntent,
)
from metahub_gateway.experiments.repository import (
    SqliteExperimentExecutionStore,
    resolve_experiment_route,
)
from metahub_gateway.feedback.routes import router as feedback_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.experiments import build_experiment_buckets
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 该测试常量组只使用合成凭据、固定时钟和预期哈希，不得进入日志或验收证据。
VIRTUAL_KEY = "mhv_prd075_gateway_virtual_key"
PROVIDER_KEY = "sk-prd075-gateway-provider-key"
ADMIN_PASSWORD = "MetaHubLocal!2026"
AUTH_SECRET = "prd075-gateway-auth-root-" + "a" * 48
FILE_SECRET = "prd075-gateway-file-root-" + "b" * 48
BATCH_SECRET = "prd075-gateway-batch-root-" + "c" * 48
PROVIDER_SECRET = "prd075-gateway-provider-root-" + "d" * 48


def _database_url(path: Path) -> str:
    """把仓库内 pytest 临时数据库转换为 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture()
def fake_provider():
    """启动回环 OpenAI-compatible Provider，并只保存模型与认证摘要。"""

    captured: list[dict[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        """返回固定安全响应，不记录 Prompt、Response 或 Token 正文。"""

        def do_POST(self) -> None:  # noqa: N802
            """读取测试请求并按模型名返回统一成功响应。"""

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            model = str(body.get("model") or "")
            captured.append(
                {
                    "model": model,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if body.get("stream") is True:
                frames = (
                    b'data: {"choices":[{"delta":{"content":"synthetic-stream"}}]}\n\n'
                    b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                    b'"completion_tokens":5,"total_tokens":8}}\n\n'
                    b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(frames)))
                self.end_headers()
                self.wfile.write(frames)
                return
            payload = {
                "id": "chatcmpl_prd075_gateway",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "synthetic-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            }
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            """关闭标准库访问日志，避免测试凭据或路径进入验收输出。"""

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


def _configure_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_url: str,
    provider_base_url: str,
) -> None:
    """设置两服务共同使用的测试数据库、根密钥和回环 Provider。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_VIRTUAL_KEY", VIRTUAL_KEY)
    monkeypatch.setenv("METAHUB_SEED_PROVIDER_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", provider_base_url)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_FILE_ENCRYPTION_SECRET", FILE_SECRET)
    monkeypatch.setenv("METAHUB_BATCH_ENCRYPTION_SECRET", BATCH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", PROVIDER_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_BATCH_WORKER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")


def _prepare_scope(database_url: str) -> dict[str, str]:
    """构造实验 API 的合成认证、请求或租户资源夹具，不把凭据写入断言输出。

    创建 Project、canonical Alias、第二实验臂并绑定 Virtual Key scope。
    """

    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT user.user_id,environment.environment_id,environment.code,
                   provider.provider_id,provider.provider_code,model.model_id,
                   virtual_key.virtual_key_id
            FROM users user
            JOIN environments environment ON environment.tenant_id=user.tenant_id
            JOIN providers provider ON provider.tenant_id=user.tenant_id
            JOIN models model ON model.tenant_id=user.tenant_id
              AND model.provider_id=provider.provider_id
            JOIN virtual_keys virtual_key ON virtual_key.tenant_id=user.tenant_id
            WHERE user.tenant_id=? AND user.role='admin' AND user.status='active'
              AND environment.code='dev' AND provider.provider_code='local_openai'
              AND model.model_alias='default-chat'
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        graph = {
            "tenant_id": seed.tenant_id,
            "user_id": str(owner["user_id"]),
            "environment_id": str(owner["environment_id"]),
            "environment_code": str(owner["code"]),
            "provider_id": str(owner["provider_id"]),
            "provider_code": str(owner["provider_code"]),
            "model_a_id": str(owner["model_id"]),
            "model_b_id": "model_prd075_gateway_b",
            "project_id": "project_prd075_gateway",
            "model_alias_id": "alias_prd075_gateway",
            "alias": "prd075-live-ab",
            "virtual_key_id": str(owner["virtual_key_id"]),
        }
        now = "2026-08-09T00:00:00Z"
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,description,owner_user_id,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'prd075-gateway','PRD075 Gateway',NULL,?,'active',?,?,?,?)
            """,
            (
                graph["project_id"],
                graph["tenant_id"],
                graph["user_id"],
                graph["user_id"],
                graph["user_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,context_window,
              input_price_per_1k,output_price_per_1k,currency,price_schema_version,
              status,created_at,updated_at
            ) VALUES(?,?,?,'prd075-arm-b','prd075-upstream-b','prd075-upstream-b',
              'PRD075 arm B','{"chat":true,"stream":true}',8192,
              0.20,0.40,'CNY','prd075-v1','active',?,?)
            """,
            (
                graph["model_b_id"],
                graph["tenant_id"],
                graph["provider_id"],
                now,
                now,
            ),
        )
        # 两臂使用非零 CNY 价格，实际请求必须形成可归属的成本 settlement。
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k=0.10,output_price_per_1k=0.30,
                currency='CNY',price_schema_version='prd075-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, graph["tenant_id"], graph["model_a_id"]),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,
              fallback_model_id,status,description,created_by,disabled_at,
              disabled_by,created_at,updated_at,environment_id
            ) VALUES(?,?,?,?,?,NULL,'active','PRD075 exact experiment alias',?,
                     NULL,NULL,?,?,?)
            """,
            (
                graph["model_alias_id"],
                graph["tenant_id"],
                graph["alias"],
                graph["environment_code"],
                graph["model_a_id"],
                graph["user_id"],
                now,
                now,
                graph["environment_id"],
            ),
        )
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (
                graph["project_id"],
                now,
                graph["tenant_id"],
                graph["virtual_key_id"],
            ),
        )
        connection.execute(
            """
            UPDATE providers
            SET health_status='healthy',
                health_updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (now, graph["tenant_id"], graph["provider_id"]),
        )
        connection.commit()
        return graph
    finally:
        connection.close()

def _terminal_failure_intent() -> ExperimentTerminalIntent:
    """构造不含正文或凭据的 Provider 前 Gateway 失败终态。"""

    return ExperimentTerminalIntent(
        gateway_outcome="failed",
        exposure="not_exposed",
        outcome_reason="gateway_failed",
        error_code="gateway.experiment_unavailable",
        routing_attempt_id=None,
        stream_session_id=None,
        quality_eligible=False,
        quality_succeeded=None,
        latency_ms=None,
        settlement_disposition="not_exposed",
    )


def _db_time(value: datetime) -> str:
    """把时区感知时间冻结为迁移使用的秒级 UTC Z 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _database_clock(database_url: str) -> tuple[datetime, str]:
    """读取 SQLite 权威时钟，避免测试使用宿主机锁前时间。"""

    connection = connect_database(database_url)
    try:
        text = str(
            connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return datetime.fromisoformat(text.replace("Z", "+00:00")), text


async def _open_unfinished_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
    *,
    marker: str,
    ordinal: int,
) -> tuple[
    str,
    dict[str, str],
    ExperimentExecutionHandle,
    SqliteExperimentExecutionStore,
]:
    """走真实 0068、Control 创建和 Gateway 分配事务，停在 Provider 前。"""

    provider_base_url, _captured = fake_provider
    database_url = _database_url(tmp_path / f"prd075-fencing-{marker}.sqlite3")
    _configure_environment(
        monkeypatch,
        database_url=database_url,
        provider_base_url=provider_base_url,
    )
    migration_result = apply_migrations(database_url)
    assert "0068" in migration_result.applied
    assert migration_result.applied[-1] == "0072"
    graph = _prepare_scope(database_url)
    experiment_id, _token = await _create_experiment(
        graph,
        request_suffix=f"fencing-{marker}",
        idempotency_key=f"75000000-0000-4000-8000-{ordinal:012d}",
    )
    request_id = f"req-prd075-fencing-{marker}"
    trace_id = f"trace-prd075-fencing-{marker}"
    connection = connect_database(database_url)
    try:
        application_id = connection.execute(
            """
            SELECT application_id FROM virtual_keys
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (graph["tenant_id"], graph["virtual_key_id"]),
        ).fetchone()[0]
        now_text = str(
            connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at
            ) VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (
                graph["tenant_id"],
                request_id,
                trace_id,
                graph["virtual_key_id"],
                now_text,
            ),
        )
        connection.execute(
            """
            INSERT INTO gateway_request_scope_claims(
              tenant_id,request_id,trace_id,virtual_key_id,project_id,application_id,
              environment_id,environment_code,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                graph["tenant_id"],
                request_id,
                trace_id,
                graph["virtual_key_id"],
                graph["project_id"],
                application_id,
                graph["environment_id"],
                graph["environment_code"],
                now_text,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    config = load_app_config("gateway-data-plane")
    resolution = resolve_experiment_route(
        config,
        tenant_id=graph["tenant_id"],
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=graph["virtual_key_id"],
        project_id=graph["project_id"],
        application_id=application_id,
        environment_id=graph["environment_id"],
        environment_code=graph["environment_code"],
        requested_model=graph["alias"],
        streaming=False,
    )
    assert resolution.matched is True
    assert resolution.execution is not None
    assert resolution.execution.experiment_id == experiment_id
    return (
        database_url,
        graph,
        resolution.execution,
        SqliteExperimentExecutionStore(config),
    )


def _insert_direct_outcome(
    database_url: str,
    handle: ExperimentExecutionHandle,
    *,
    ordinal: int,
    gateway_status: str,
    exposure_status: str,
    outcome_reason: str,
    error_code: str | None,
    quality_eligible: int = 0,
    quality_succeeded: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """尝试直接写 Outcome，用于证明 0068 不能被绕过。"""

    connection = connect_database(database_url)
    try:
        assignment = connection.execute(
            """
            SELECT experiment_id,variant,model_id,provider_id
            FROM experiment_assignments
            WHERE tenant_id=? AND assignment_id=?
            """,
            (handle.tenant_id, handle.assignment_id),
        ).fetchone()
        assert assignment is not None
        now_text = str(
            connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO experiment_request_outcomes(
              outcome_id,tenant_id,experiment_id,assignment_id,execution_id,request_id,
              variant,model_id,provider_id,gateway_status,exposure_status,outcome_reason,
              error_code,routing_attempt_id,stream_session_id,quality_eligible,
              quality_succeeded,latency_ms,recovery_generation,facts_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?)
            """,
            (
                "exoc_" + format(ordinal, "x") * 32,
                handle.tenant_id,
                assignment["experiment_id"],
                handle.assignment_id,
                handle.execution_id,
                handle.request_id,
                assignment["variant"],
                assignment["model_id"],
                assignment["provider_id"],
                gateway_status,
                exposure_status,
                outcome_reason,
                error_code,
                quality_eligible,
                quality_succeeded,
                latency_ms,
                handle.generation,
                now_text,
                now_text,
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize("expiry", ("lease", "absolute_deadline"))
async def test_expired_old_owner_cannot_append_outcome_or_terminalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
    expiry: str,
) -> None:
    """lease 或绝对截止到期后，旧 owner 的完整 handle 也必须失败关闭。"""

    ordinal = 751 if expiry == "lease" else 752
    database_url, _graph, handle, store = await _open_unfinished_execution(
        monkeypatch,
        tmp_path,
        fake_provider,
        marker=expiry,
        ordinal=ordinal,
    )
    if expiry == "lease":
        now, now_text = _database_clock(database_url)
        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE experiment_request_executions
                SET lease_expires_at=?,updated_at=?
                WHERE tenant_id=? AND execution_id=?
                """,
                (
                    _db_time(now - timedelta(seconds=1)),
                    now_text,
                    handle.tenant_id,
                    handle.execution_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
    else:
        expired_now = handle.absolute_deadline + timedelta(seconds=1)
        monkeypatch.setattr(
            experiment_repository,
            "_database_now",
            lambda _connection: (expired_now, _db_time(expired_now)),
        )

    assert store.finalize_execution(handle, _terminal_failure_intent()) is False
    connection = connect_database(database_url)
    try:
        execution = connection.execute(
            """
            SELECT status,generation FROM experiment_request_executions
            WHERE tenant_id=? AND execution_id=?
            """,
            (handle.tenant_id, handle.execution_id),
        ).fetchone()
        assert tuple(execution) == ("assigned", 1)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM experiment_request_outcomes
            WHERE tenant_id=? AND execution_id=?
            """,
            (handle.tenant_id, handle.execution_id),
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_generation_takeover_fences_stale_generation_owner_and_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
) -> None:
    """generation+1 接管后，旧 generation、owner 或 token 均不能冻结事实。"""

    database_url, _graph, handle, store = await _open_unfinished_execution(
        monkeypatch,
        tmp_path,
        fake_provider,
        marker="takeover",
        ordinal=753,
    )
    now, now_text = _database_clock(database_url)
    new_owner = "gateway-prd075-takeover"
    new_token = "prd075-fencing-takeover-token"
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE experiment_request_executions
            SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND execution_id=?
            """,
            (
                _db_time(now - timedelta(seconds=1)),
                now_text,
                handle.tenant_id,
                handle.execution_id,
            ),
        )
        connection.execute(
            """
            UPDATE experiment_request_executions
            SET generation=2,lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND execution_id=? AND generation=1
            """,
            (
                new_owner,
                new_token,
                _db_time(now + timedelta(seconds=60)),
                now_text,
                handle.tenant_id,
                handle.execution_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    stale_handles = (
        handle,
        replace(handle, generation=2, lease_token=new_token),
        replace(handle, generation=2, lease_owner_id=new_owner),
    )
    for stale in stale_handles:
        assert store.finalize_execution(stale, _terminal_failure_intent()) is False

    current = replace(
        handle,
        generation=2,
        lease_owner_id=new_owner,
        lease_token=new_token,
    )
    assert store.finalize_execution(current, _terminal_failure_intent()) is True
    connection = connect_database(database_url)
    try:
        execution = connection.execute(
            """
            SELECT status,generation,lease_owner_id
            FROM experiment_request_executions
            WHERE tenant_id=? AND execution_id=?
            """,
            (handle.tenant_id, handle.execution_id),
        ).fetchone()
        assert tuple(execution) == ("terminal", 2, new_owner)
        outcome = connection.execute(
            """
            SELECT recovery_generation,gateway_status,exposure_status,outcome_reason
            FROM experiment_request_outcomes
            WHERE tenant_id=? AND execution_id=?
            """,
            (handle.tenant_id, handle.execution_id),
        ).fetchone()
        assert tuple(outcome) == (2, "failed", "not_exposed", "gateway_failed")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_direct_sql_rejects_invalid_outcome_state_reason_exposure_quality_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
) -> None:
    """直接 SQL 也不能绕过 active/recovery 状态和 Outcome 枚举矩阵。"""

    database_url, _graph, handle, store = await _open_unfinished_execution(
        monkeypatch,
        tmp_path,
        fake_provider,
        marker="direct-matrix",
        ordinal=754,
    )
    invalid = (
        ("success", "not_exposed", "completed", None, 0, None, None),
        (
            "failed",
            "exposed",
            "variant_unavailable",
            "gateway.experiment_variant_unavailable",
            0,
            None,
            None,
        ),
        (
            "unresolved",
            "not_exposed",
            "abandoned_before_provider",
            "gateway.experiment_unavailable",
            0,
            None,
            None,
        ),
        (
            "unresolved",
            "unknown",
            "provider_outcome_unknown",
            "gateway.experiment_unavailable",
            0,
            None,
            None,
        ),
        (
            "failed",
            "unknown",
            "gateway_failed",
            "gateway.experiment_unavailable",
            1,
            0,
            10,
        ),
    )
    for ordinal, values in enumerate(invalid, start=1):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_direct_outcome(
                database_url,
                handle,
                ordinal=ordinal,
                gateway_status=values[0],
                exposure_status=values[1],
                outcome_reason=values[2],
                error_code=values[3],
                quality_eligible=values[4],
                quality_succeeded=values[5],
                latency_ms=values[6],
            )

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM experiment_request_outcomes
            WHERE tenant_id=? AND execution_id=?
            """,
            (handle.tenant_id, handle.execution_id),
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert store.finalize_execution(handle, _terminal_failure_intent()) is True


def _control_app():
    """构造只含认证和实验 API 的真实 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-075 Control Gateway Test")
    app.include_router(auth_router)
    app.include_router(experiments_router)
    return app


def _gateway_app():
    """构造包含公开 Chat 与 Feedback 路由的真实 Gateway 应用。"""

    app = create_service_app("gateway-data-plane", "PRD-075 Gateway Test")
    app.include_router(gateway_router)
    app.include_router(feedback_router)
    app.state.runtime_event_queue = asyncio.Queue(maxsize=8)
    return app


async def _create_experiment(
    graph: dict[str, str],
    *,
    target_sample_count: int = 4,
    traffic_sample_bps: int = 10_000,
    feedback_collection_enabled: bool = False,
    feedback_window_seconds: int = 0,
    request_suffix: str = "gateway",
    idempotency_key: str = "75000000-0000-4000-8000-000000000075",
) -> tuple[str, str]:
    """通过后端 RBAC API 创建实验，并返回实验 ID 与管理员 Token。"""

    app = _control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        login = await client.post(
            "/auth/login",
            json={
                "tenant_slug": "default",
                "email": "admin@metahub.local",
                "password": ADMIN_PASSWORD,
            },
        )
        assert login.status_code == 200, login.text
        token = str(login.json()["access_token"])
        created = await client.post(
            "/experiments",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": f"req-prd075-create-{request_suffix}",
                "Idempotency-Key": idempotency_key,
            },
            json={
                "name": f"PRD075 live A/B {request_suffix}",
                "project_id": graph["project_id"],
                "environment_id": graph["environment_id"],
                "model_alias_id": graph["model_alias_id"],
                "model_a_id": graph["model_a_id"],
                "model_b_id": graph["model_b_id"],
                "target_sample_count": target_sample_count,
                "traffic_sample_bps": traffic_sample_bps,
                "model_a_allocation_bps": 5_000,
                "sampling_duration_seconds": 60,
                "feedback_collection_enabled": feedback_collection_enabled,
                "feedback_window_seconds": feedback_window_seconds,
            },
        )
        assert created.status_code == 201, created.text
        # 测试辅助分支仅接受合成且受控的实验事实，异常输入必须关闭而不泄露敏感值。
        return str(created.json()["experiment"]["experiment_id"]), token


def _request_for_sampling_result(
    *,
    tenant_id: str,
    experiment_id: str,
    traffic_sample_bps: int,
    sampled: bool,
) -> tuple[str, int]:
    """从冻结 HMAC 合同寻找一个确定命中或旁路的公开 request_id。"""

    for ordinal in range(200_000):
        request_id = f"req-prd075-sampling-{traffic_sample_bps:04d}-{int(sampled)}-{ordinal:06d}"
        bucket = build_experiment_buckets(
            AUTH_SECRET,
            tenant_id=tenant_id,
            experiment_id=experiment_id,
            request_id=request_id,
        ).sample_bucket
        if (bucket < traffic_sample_bps) is sampled:
            return request_id, bucket
    # 有界空间内找不到目标 bucket 表示冻结 HMAC 合同或测试前提漂移，不能改用伪造分桶。
    raise AssertionError("unable to locate deterministic sampling request")


@pytest.mark.anyio
@pytest.mark.parametrize("traffic_sample_bps", (1, 9_999))
async def test_partial_traffic_sampling_uses_real_gateway_and_bypasses_unselected_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
    traffic_sample_bps: int,
) -> None:
    """真实 Gateway 在非满量采样边界只为命中请求创建实验 Assignment。"""

    provider_base_url, captured = fake_provider
    database_url = _database_url(tmp_path / f"prd075-sampling-{traffic_sample_bps}.sqlite3")
    _configure_environment(
        monkeypatch,
        database_url=database_url,
        provider_base_url=provider_base_url,
    )
    migration_result = apply_migrations(database_url)
    assert "0068" in migration_result.applied
    assert migration_result.applied[-1] == "0072"
    graph = _prepare_scope(database_url)
    experiment_id, _token = await _create_experiment(
        graph,
        target_sample_count=4,
        traffic_sample_bps=traffic_sample_bps,
        request_suffix=f"sampling-{traffic_sample_bps}",
        idempotency_key=f"75000000-0000-4000-8000-{traffic_sample_bps:012d}",
    )
    sampled_id, sampled_bucket = _request_for_sampling_result(
        tenant_id=graph["tenant_id"],
        experiment_id=experiment_id,
        traffic_sample_bps=traffic_sample_bps,
        sampled=True,
    )
    bypass_id, bypass_bucket = _request_for_sampling_result(
        tenant_id=graph["tenant_id"],
        experiment_id=experiment_id,
        traffic_sample_bps=traffic_sample_bps,
        sampled=False,
    )

    gateway = _gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=gateway),
        base_url="http://gateway",
    ) as client:
        responses = []
        for request_id in (sampled_id, bypass_id):
            responses.append(
                await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {VIRTUAL_KEY}",
                        "X-Request-ID": request_id,
                    },
                    json={
                        "model": graph["alias"],
                        "messages": [{"role": "user", "content": "synthetic-sampling"}],
                    },
                )
            )
    assert [response.status_code for response in responses] == [200, 200]
    assert len(captured) == 2
    assert sampled_bucket < traffic_sample_bps <= bypass_bucket

    connection = connect_database(database_url)
    try:
        assignments = connection.execute(
            """
            SELECT request_id,sample_bucket FROM experiment_assignments
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchall()
        assert [tuple(row) for row in assignments] == [(sampled_id, sampled_bucket)]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM request_logs
            WHERE tenant_id=? AND request_id IN (?,?) AND status='success'
            """,
            (graph["tenant_id"], sampled_id, bypass_id),
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT assigned_count FROM model_experiments
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_feedback_attribution_closes_at_fence_and_final_statistics_do_not_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
) -> None:
    """真实反馈写入只归因 fence 前事实，晚反馈全局成功但冻结结果不漂移。"""

    provider_base_url, _captured = fake_provider
    database_url = _database_url(tmp_path / "prd075-feedback-fence.sqlite3")
    _configure_environment(
        monkeypatch,
        database_url=database_url,
        provider_base_url=provider_base_url,
    )
    migration_result = apply_migrations(database_url)
    assert "0068" in migration_result.applied
    assert migration_result.applied[-1] == "0072"
    graph = _prepare_scope(database_url)
    experiment_id, admin_token = await _create_experiment(
        graph,
        target_sample_count=2,
        feedback_collection_enabled=True,
        feedback_window_seconds=60,
        request_suffix="feedback-fence",
        idempotency_key="75000000-0000-4000-8000-000000000753",
    )
    source_request_ids = ("req-prd075-feedback-early", "req-prd075-feedback-late")
    gateway = _gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=gateway),
        base_url="http://gateway",
    ) as client:
        for request_id in source_request_ids:
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": request_id,
                },
                json={
                    "model": graph["alias"],
                    "messages": [{"role": "user", "content": "synthetic-feedback"}],
                },
            )
            assert response.status_code == 200, response.text

        connection = connect_database(database_url)
        try:
            before_feedback = connection.execute(
                """
                SELECT facts_version,feedback_deadline_at FROM model_experiments
                WHERE tenant_id=? AND experiment_id=?
                """,
                (graph["tenant_id"], experiment_id),
            ).fetchone()
            assert before_feedback is not None
        finally:
            connection.close()

        early = await client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "feedback-prd075-early-operation",
                "X-Trace-ID": "trace-prd075-feedback-early",
            },
            json={
                "request_id": source_request_ids[0],
                "rating": 5,
                "accepted": True,
                "tags": ["accurate"],
            },
        )
        assert early.status_code == 201, early.text

        connection = connect_database(database_url)
        try:
            after_early = connection.execute(
                """
                SELECT facts_version FROM model_experiments
                WHERE tenant_id=? AND experiment_id=?
                """,
                (graph["tenant_id"], experiment_id),
            ).fetchone()[0]
            attribution = connection.execute(
                """
                SELECT attribution.source_request_id,attribution.rating,attribution.accepted,
                       attribution.attributed_at,assignment.variant
                FROM experiment_feedback_attributions attribution
                JOIN experiment_assignments assignment
                  ON assignment.tenant_id=attribution.tenant_id
                 AND assignment.assignment_id=attribution.assignment_id
                WHERE attribution.tenant_id=? AND attribution.experiment_id=?
                """,
                (graph["tenant_id"], experiment_id),
            ).fetchone()
            assert attribution is not None
            assert tuple(attribution)[:3] == (source_request_ids[0], 5, 1)
            assert attribution["variant"] in {"a", "b"}
            assert int(after_early) == int(before_feedback["facts_version"]) + 1
            feedback_deadline = datetime.fromisoformat(
                str(before_feedback["feedback_deadline_at"]).replace("Z", "+00:00")
            )
            attributed_at = datetime.fromisoformat(
                str(attribution["attributed_at"]).replace("Z", "+00:00")
            )
            assert attributed_at < feedback_deadline
        finally:
            connection.close()

        assert run_experiment_scheduler_once(
            gateway.state.config,
            now=feedback_deadline + timedelta(seconds=1),
        ) is True

        control = _control_app()
        async with AsyncClient(
            transport=ASGITransport(app=control),
            base_url="http://control",
        ) as control_client:
            result_headers = {
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req-prd075-feedback-results",
            }
            result_before_late = await control_client.get(
                f"/experiments/{experiment_id}/results",
                headers=result_headers,
            )
            assert result_before_late.status_code == 200, result_before_late.text
            assert result_before_late.json()["is_final"] is True

            connection = connect_database(database_url)
            try:
                finalized_facts = connection.execute(
                    """
                    SELECT facts_version,feedback_fence_closed_at
                    FROM model_experiments WHERE tenant_id=? AND experiment_id=?
                    """,
                    (graph["tenant_id"], experiment_id),
                ).fetchone()
                assert finalized_facts is not None
                assert finalized_facts["feedback_fence_closed_at"] is not None
            finally:
                connection.close()

            late = await client.post(
                "/feedback",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "feedback-prd075-late-operation",
                    "X-Trace-ID": "trace-prd075-feedback-late",
                },
                json={
                    "request_id": source_request_ids[1],
                    "rating": 2,
                    "accepted": False,
                    "tags": ["inaccurate"],
                },
            )
            assert late.status_code == 201, late.text
            result_after_late = await control_client.get(
                f"/experiments/{experiment_id}/results",
                headers=result_headers,
            )
            assert result_after_late.status_code == 200, result_after_late.text
            assert result_after_late.json()["results"] == result_before_late.json()["results"]

    connection = connect_database(database_url)
    try:
        experiment = connection.execute(
            """
            SELECT status,facts_version,feedback_fence_closed_at
            FROM model_experiments WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchone()
        assert experiment is not None and experiment["status"] == "completed"
        assert experiment["feedback_fence_closed_at"] is not None
        assert int(experiment["facts_version"]) == int(finalized_facts["facts_version"])
        assert connection.execute(
            "SELECT COUNT(*) FROM request_feedbacks WHERE tenant_id=?",
            (graph["tenant_id"],),
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM experiment_feedback_attributions
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchone()[0] == 1
        frozen = connection.execute(
            """
            SELECT SUM(feedback_count),SUM(rating_sum),SUM(accepted_count),
                   COUNT(DISTINCT facts_version)
            FROM experiment_results WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchone()
        assert tuple(frozen) == (1, 5, 1, 1)
        assert int(experiment["facts_version"]) > int(after_early)
    finally:
        connection.close()


@pytest.mark.anyio
async def test_live_requests_fill_exact_ab_quota_and_publish_traceable_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_provider,
) -> None:
    """四个真实请求形成 2/2 A/B 样本、单候选路由、Outcome、Usage 和成本。"""

    provider_base_url, captured = fake_provider
    database_url = _database_url(tmp_path / "prd075-gateway.sqlite3")
    _configure_environment(
        monkeypatch,
        database_url=database_url,
        provider_base_url=provider_base_url,
    )
    migration_result = apply_migrations(database_url)
    assert "0068" in migration_result.applied
    assert migration_result.applied[-1] == "0072"
    graph = _prepare_scope(database_url)
    experiment_id, admin_token = await _create_experiment(graph)
    gateway = _gateway_app()

    async with AsyncClient(
        transport=ASGITransport(app=gateway),
        base_url="http://gateway",
    ) as client:
        responses = []
        for sequence in range(1, 5):
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": f"req-prd075-live-{sequence:03d}",
                },
                json={
                    "model": graph["alias"],
                    "messages": [{"role": "user", "content": "synthetic-input"}],
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["x-request-id"] == f"req-prd075-live-{sequence:03d}"
            responses.append(response)

    assert len(responses) == 4
    assert [item["model"] for item in captured].count("local-chat-model") == 2
    assert [item["model"] for item in captured].count("prd075-upstream-b") == 2
    assert all(item["authorization"] == f"Bearer {PROVIDER_KEY}" for item in captured)

    connection = connect_database(database_url)
    try:
        experiment = connection.execute(
            """
            SELECT status,assigned_count,model_a_assigned_count,model_b_assigned_count
            FROM model_experiments WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchone()
        assert tuple(experiment) == ("collecting_feedback", 4, 2, 2)
        assignments = connection.execute(
            """
            SELECT assignment_sequence,variant,model_id,provider_id
            FROM experiment_assignments
            WHERE tenant_id=? AND experiment_id=? ORDER BY assignment_sequence
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchall()
        assert [row["assignment_sequence"] for row in assignments] == [1, 2, 3, 4]
        assert [row["variant"] for row in assignments].count("a") == 2
        assert [row["variant"] for row in assignments].count("b") == 2
        outcomes = connection.execute(
            """
            SELECT gateway_status,exposure_status,quality_eligible,quality_succeeded,
                   routing_attempt_id,stream_session_id
            FROM experiment_request_outcomes
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchall()
        assert len(outcomes) == 4
        assert all(
            tuple(row)
            == ("success", "exposed", 1, 1, row["routing_attempt_id"], None)
            and row["routing_attempt_id"]
            for row in outcomes
        )
        attempts = connection.execute(
            """
            SELECT attempt_sequence,attempt_role,status FROM routing_attempts
            WHERE tenant_id=? AND request_id LIKE 'req-prd075-live-%'
            ORDER BY request_id,attempt_sequence
            """,
            (graph["tenant_id"],),
        ).fetchall()
        assert [tuple(row) for row in attempts] == [(1, "primary", "succeeded")] * 4
        settlements = connection.execute(
            """
            SELECT status,gross_cny_micros,usage_ledger_id,finops_ledger_entry_id
            FROM experiment_usage_settlements
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], experiment_id),
        ).fetchall()
        assert len(settlements) == 4
        assert all(
            row["status"] == "recorded"
            and row["gross_cny_micros"] > 0
            and row["usage_ledger_id"]
            and row["finops_ledger_entry_id"]
            for row in settlements
        )
        persisted = "\n".join(
            str(value)
            for table in (
                "experiment_assignments",
                "experiment_request_outcomes",
                "request_logs",
                "usage_ledger",
                "finops_ledger_entries",
            )
            for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in tuple(row)
        )
        assert "synthetic-input" not in persisted
        assert VIRTUAL_KEY not in persisted
        assert PROVIDER_KEY not in persisted
    finally:
        connection.close()

    control = _control_app()
    async with AsyncClient(
        transport=ASGITransport(app=control),
        base_url="http://control",
    ) as client:
        results = await client.get(
            f"/experiments/{experiment_id}/results",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req-prd075-results-gateway",
            },
        )
    assert results.status_code == 200, results.text
    body = results.json()
    assert body["status"] == "collecting_feedback"
    assert body["is_final"] is False
    assert sum(item["assigned_count"] for item in body["results"]) == 4
    assert sum(item["gateway_success_count"] for item in body["results"]) == 4
    assert sum(item["cost_known_count"] for item in body["results"]) == 4

    # 同一 scope 的上一实验已结束采样后可以创建下一实验；direct model 在 active
    # 实验期间仍须权威旁路，不能被误分配到 canonical Alias 的 A/B 样本。
    concurrent_experiment_id, _token = await _create_experiment(
        graph,
        target_sample_count=13,
        request_suffix="concurrent",
        idempotency_key="75000000-0000-4000-8000-000000000076",
    )
    captured_before_second = len(captured)
    gateway = _gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=gateway),
        base_url="http://gateway",
    ) as client:
        direct = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req-prd075-direct-bypass",
            },
            json={
                "model": "prd075-arm-b",
                "messages": [{"role": "user", "content": "synthetic-direct"}],
            },
        )
        assert direct.status_code == 200, direct.text

        # 健康漂移不改变冻结 route fingerprint，但必须使被选中的 variant 在
        # Provider 前失败关闭，并保留 not_exposed Outcome 与零次网络调用。
        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE providers
                SET health_status='unhealthy',
                    health_updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE tenant_id=? AND provider_id=?
                """,
                (graph["tenant_id"], graph["provider_id"]),
            )
            connection.commit()
        finally:
            connection.close()

        unavailable = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {VIRTUAL_KEY}",
                "X-Request-ID": "req-prd075-variant-unavailable",
            },
            json={
                "model": graph["alias"],
                "messages": [{"role": "user", "content": "synthetic-unavailable"}],
            },
        )
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["code"] == "gateway.experiment_variant_unavailable"
        assert unavailable.json()["request_id"] == "req-prd075-variant-unavailable"
        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE providers
                SET health_status='healthy',
                    health_updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE tenant_id=? AND provider_id=?
                """,
                (graph["tenant_id"], graph["provider_id"]),
            )
            connection.commit()
        finally:
            connection.close()

        def fail_usage(*_args, **_kwargs):
            """在 Provider 成功后模拟 Usage 主库与安全 spool 都不可用。"""

            # 测试辅助分支仅接受合成且受控的实验事实，异常输入必须关闭而不泄露敏感值。
            raise ApiError("finops.usage_record_unavailable")

        with monkeypatch.context() as usage_failure:
            usage_failure.setattr(gateway_api, "safe_record_usage_ledger", fail_usage)
            durability_failure = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": "req-prd075-usage-unavailable",
                },
                json={
                    "model": graph["alias"],
                    "messages": [{"role": "user", "content": "synthetic-usage"}],
                },
            )
        assert durability_failure.status_code == 503, durability_failure.text
        assert durability_failure.json()["code"] == "finops.usage_record_unavailable"

        async def invoke(sequence: int):
            """并发发送唯一 request_id，验证 SQLite 配额线性化。"""

            return await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": f"req-prd075-concurrent-{sequence:03d}",
                },
                json={
                    "model": graph["alias"],
                    "messages": [{"role": "user", "content": "synthetic-concurrent"}],
                },
            )

        concurrent = await asyncio.gather(*(invoke(sequence) for sequence in range(1, 12)))
        assert all(response.status_code == 200 for response in concurrent), [
            (response.status_code, response.text) for response in concurrent
        ]

    # direct、Usage 失败和 11 个可用实验请求调用了 Provider；variant unavailable 严格零次调用。
    assert len(captured) - captured_before_second == 13
    connection = connect_database(database_url)
    try:
        direct_assignment = connection.execute(
            """
            SELECT COUNT(*) FROM experiment_assignments
            WHERE tenant_id=? AND request_id='req-prd075-direct-bypass'
            """,
            (graph["tenant_id"],),
        ).fetchone()[0]
        assert direct_assignment == 0
        experiment = connection.execute(
            """
            SELECT status,assigned_count,model_a_assigned_count,model_b_assigned_count
            FROM model_experiments WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], concurrent_experiment_id),
        ).fetchone()
        assert tuple(experiment) == ("collecting_feedback", 13, 7, 6)
        outcomes = connection.execute(
            """
            SELECT request_id,gateway_status,exposure_status,outcome_reason,error_code,
                   routing_attempt_id,quality_eligible,quality_succeeded
            FROM experiment_request_outcomes
            WHERE tenant_id=? AND experiment_id=?
            ORDER BY request_id
            """,
            (graph["tenant_id"], concurrent_experiment_id),
        ).fetchall()
        assert len(outcomes) == 13
        assert sum(row["gateway_status"] == "success" for row in outcomes) == 11
        failed = [row for row in outcomes if row["gateway_status"] == "failed"]
        assert len(failed) == 2
        unavailable_outcome = next(
            row for row in failed if row["request_id"] == "req-prd075-variant-unavailable"
        )
        assert tuple(unavailable_outcome) == (
            "req-prd075-variant-unavailable",
            "failed",
            "not_exposed",
            "variant_unavailable",
            "gateway.experiment_variant_unavailable",
            None,
            0,
            None,
        )
        usage_outcome = next(
            row for row in failed if row["request_id"] == "req-prd075-usage-unavailable"
        )
        assert tuple(usage_outcome) == (
            "req-prd075-usage-unavailable",
            "failed",
            "exposed",
            "gateway_failed",
            "finops.usage_record_unavailable",
            usage_outcome["routing_attempt_id"],
            1,
            1,
        )
        assert usage_outcome["routing_attempt_id"]
        sequences = connection.execute(
            """
            SELECT assignment_sequence FROM experiment_assignments
            WHERE tenant_id=? AND experiment_id=? ORDER BY assignment_sequence
            """,
            (graph["tenant_id"], concurrent_experiment_id),
        ).fetchall()
        assert [row[0] for row in sequences] == list(range(1, 14))
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    streaming_experiment_id, _token = await _create_experiment(
        graph,
        target_sample_count=2,
        request_suffix="streaming",
        idempotency_key="75000000-0000-4000-8000-000000000077",
    )
    gateway = _gateway_app()
    async with AsyncClient(
        transport=ASGITransport(app=gateway),
        base_url="http://gateway",
    ) as client:
        streaming_responses = []
        for sequence in range(1, 3):
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {VIRTUAL_KEY}",
                    "X-Request-ID": f"req-prd075-stream-live-{sequence:03d}",
                },
                json={
                    "model": graph["alias"],
                    "messages": [{"role": "user", "content": "synthetic-stream-input"}],
                    "stream": True,
                },
            )
            assert response.status_code == 200, response.text
            assert response.content.endswith(b"data: [DONE]\n\n")
            assert b"event: error" not in response.content
            streaming_responses.append(response)
    assert len(streaming_responses) == 2
    connection = connect_database(database_url)
    try:
        stream_outcomes = connection.execute(
            """
            SELECT outcome.gateway_status,outcome.exposure_status,
                   outcome.quality_eligible,outcome.quality_succeeded,
                   outcome.stream_session_id,session.status,settlement.status
            FROM experiment_request_outcomes outcome
            JOIN stream_sessions session
              ON session.tenant_id=outcome.tenant_id
             AND session.stream_session_id=outcome.stream_session_id
            JOIN experiment_usage_settlements settlement
              ON settlement.tenant_id=outcome.tenant_id
             AND settlement.assignment_id=outcome.assignment_id
            WHERE outcome.tenant_id=? AND outcome.experiment_id=?
            ORDER BY outcome.request_id
            """,
            (graph["tenant_id"], streaming_experiment_id),
        ).fetchall()
        assert len(stream_outcomes) == 2
        assert all(
            tuple(row)
            == (
                "success",
                "exposed",
                1,
                1,
                row["stream_session_id"],
                "completed",
                "recorded",
            )
            and row["stream_session_id"]
            for row in stream_outcomes
        )
        stream_experiment = connection.execute(
            """
            SELECT status,assigned_count,model_a_assigned_count,model_b_assigned_count
            FROM model_experiments WHERE tenant_id=? AND experiment_id=?
            """,
            (graph["tenant_id"], streaming_experiment_id),
        ).fetchone()
        assert tuple(stream_experiment) == ("collecting_feedback", 2, 1, 1)
    finally:
        connection.close()
