"""PRD-075 控制面集成测试：覆盖实验 API、权限、跨租户、调度、统计和 Outbox 恢复。

PRD-075 Control API, finalizer and source-outbox integration coverage.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import authenticate_bearer, login_admin
from metahub_control.experiments import outbox as experiment_outbox
from metahub_control.experiments import scheduler as experiment_scheduler
from metahub_control.experiments import service as experiment_service
from metahub_control.experiments.outbox import (
    claim_next_experiment_event,
    dispatch_claimed_experiment_event,
    recover_experiment_completed_source_request_id,
)
from metahub_control.experiments.reaper import (
    close_expired_settlements,
    reap_expired_executions,
)
from metahub_control.experiments.routes import router as experiments_router
from metahub_control.experiments.scheduler import run_experiment_scheduler_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 该测试常量组只使用合成凭据、固定时钟和预期哈希，不得进入日志或验收证据。
ADMIN_PASSWORD = "MetaHubLocal!2026"
VIEWER_PASSWORD = "Prd075ViewerTestOnly!2026"
OTHER_PASSWORD = "Prd075OtherTestOnly!2026"
AUTH_SECRET = "prd075-auth-test-root-" + "a" * 48
FILE_SECRET = "prd075-file-test-root-" + "b" * 48
BATCH_SECRET = "prd075-batch-test-root-" + "c" * 48
PROVIDER_SECRET = "prd075-provider-test-root-" + "d" * 48
NOW = "2026-08-09T00:00:00Z"


def _url(path: Path) -> str:
    """构造独立 SQLite 测试环境和迁移序列，避免复用或污染其他测试数据库。"""
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd075_control_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    database = tmp_path_factory.mktemp("prd075-control-template") / "template.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0068"
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd075_viewer',?,'viewer@prd075.local','PRD075 Viewer',?,
                     'viewer','active',?,?)
            """,
            (seed.tenant_id, hash_password(VIEWER_PASSWORD), NOW, NOW),
        )
        for marker in ("a", "b", "c"):
            _insert_scope_graph(connection, marker)
        connection.commit()
    finally:
        connection.close()
    return database


def _insert_scope_graph(connection: sqlite3.Connection, marker: str) -> dict[str, str]:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    identity = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,environment.environment_id,environment.code,
               provider.provider_id,provider.provider_code,model.model_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id
        JOIN environments environment ON environment.tenant_id=tenant.tenant_id
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        JOIN models model ON model.tenant_id=tenant.tenant_id
          AND model.provider_id=provider.provider_id
        WHERE tenant.slug='default' AND user.role='admin' AND user.status='active'
          AND environment.code='dev' AND environment.status='active'
          AND provider.status='active' AND model.status='active'
        ORDER BY user.user_id,provider.provider_id,model.model_id LIMIT 1
        """
    ).fetchone()
    assert identity is not None
    (
        tenant_id,
        user_id,
        environment_id,
        environment_code,
        provider_id,
        provider_code,
        model_a_id,
    ) = map(str, identity)
    graph = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "environment_id": environment_id,
        "environment_code": environment_code,
        "provider_id": provider_id,
        "provider_code": provider_code,
        "model_a_id": model_a_id,
        "model_b_id": f"model_prd075_control_{marker}",
        "project_id": f"project_prd075_control_{marker}",
        "application_id": f"application_prd075_control_{marker}",
        "model_alias_id": f"alias_prd075_control_{marker}",
        "alias": f"prd075-control-alias-{marker}",
        "virtual_key_id": f"vkey_prd075_control_{marker}",
    }
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD075 control project',NULL,?,'active',?,?,?,?)
        """,
        (
            graph["project_id"],
            tenant_id,
            f"prd075-control-{marker}",
            user_id,
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO applications(
          application_id,tenant_id,project_id,application_code,name,description,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,'PRD075 control app',NULL,'active',?,?,?,?)
        """,
        (
            graph["application_id"],
            tenant_id,
            graph["project_id"],
            f"prd075-control-{marker}",
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          capabilities,status,created_at,updated_at,context_window
        ) VALUES(?,?,?,?,?,'{"chat":true,"stream":true}','active',?,?,8192)
        """,
        (
            graph["model_b_id"],
            tenant_id,
            provider_id,
            f"prd075-control-model-{marker}",
            f"prd075-control-upstream-{marker}",
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,
          fallback_model_id,status,description,created_by,disabled_at,
          disabled_by,created_at,updated_at,environment_id
        ) VALUES(?,?,?,?,?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            graph["model_alias_id"],
            tenant_id,
            graph["alias"],
            environment_code,
            model_a_id,
            user_id,
            NOW,
            NOW,
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_prd075_control',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,?,?)
        """,
        (
            graph["virtual_key_id"],
            tenant_id,
            f"PRD075 control key {marker}",
            marker * 64,
            NOW,
            NOW,
            environment_code,
            user_id,
            graph["project_id"],
            graph["application_id"],
            environment_id,
        ),
    )
    return graph


def _clone_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, dict[str, dict[str, str]]]:
    """构造独立 SQLite 测试环境和迁移序列，避免复用或污染其他测试数据库。"""
    database = tmp_path / "prd075-control.sqlite3"
    shutil.copy2(template, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_FILE_ENCRYPTION_SECRET", FILE_SECRET)
    monkeypatch.setenv("METAHUB_BATCH_ENCRYPTION_SECRET", BATCH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", PROVIDER_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_BATCH_WORKER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT project.tenant_id,project.project_code,project.project_id,
                   alias.model_alias_id,alias.alias,alias.environment,alias.environment_id,
                   alias.default_model_id,model.model_id,model.provider_id,
                   provider.provider_code
            FROM projects project
            JOIN model_aliases alias ON alias.tenant_id=project.tenant_id
            JOIN models model ON model.tenant_id=project.tenant_id
            JOIN providers provider ON provider.tenant_id=model.tenant_id
              AND provider.provider_id=model.provider_id
            WHERE project.project_code LIKE 'prd075-control-%'
              AND alias.alias=replace(project.project_code,'prd075-control-',
                'prd075-control-alias-')
              AND model.model_alias=replace(project.project_code,'prd075-control-',
                'prd075-control-model-')
            ORDER BY project.project_code
            """
        ).fetchall()
        graphs = {
            str(row["project_code"])[-1]: {
                "project_id": str(row["project_id"]),
                "tenant_id": str(row["tenant_id"]),
                "application_id": f"application_prd075_control_{str(row['project_code'])[-1]}",
                "model_alias_id": str(row["model_alias_id"]),
                "alias": str(row["alias"]),
                "environment_code": str(row["environment"]),
                "environment_id": str(row["environment_id"]),
                "model_a_id": str(row["default_model_id"]),
                "model_b_id": str(row["model_id"]),
                "provider_id": str(row["provider_id"]),
                "provider_code": str(row["provider_code"]),
                "virtual_key_id": f"vkey_prd075_control_{str(row['project_code'])[-1]}",
            }
            for row in rows
        }
    finally:
        connection.close()
    assert set(graphs) == {"a", "b", "c"}
    return database_url, graphs


def _build_app():
    """构造实验 API 的合成认证、请求或租户资源夹具，不把凭据写入断言输出。"""
    app = create_service_app("control-plane-api", "PRD-075 Control Test")
    app.include_router(auth_router)
    app.include_router(experiments_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
    tenant_slug: str = "default",
) -> str:
    """构造实验 API 的合成认证、请求或租户资源夹具，不把凭据写入断言输出。"""
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str, idempotency_key: str) -> dict[str, str]:
    """构造实验 API 的合成认证、请求或租户资源夹具，不把凭据写入断言输出。"""
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "Idempotency-Key": idempotency_key,
    }


def _payload(
    graph: dict[str, str],
    *,
    name: str,
    duration: int = 60,
    feedback: bool = False,
) -> dict[str, object]:
    """构造实验 API 的合成认证、请求或租户资源夹具，不把凭据写入断言输出。"""
    return {
        "name": name,
        "project_id": graph["project_id"],
        "environment_id": graph["environment_id"],
        "model_alias_id": graph["model_alias_id"],
        "model_a_id": graph["model_a_id"],
        "model_b_id": graph["model_b_id"],
        "target_sample_count": 10,
        "traffic_sample_bps": 2500,
        "model_a_allocation_bps": 5000,
        "sampling_duration_seconds": duration,
        "feedback_collection_enabled": feedback,
        "feedback_window_seconds": 60 if feedback else 0,
    }


async def _create_completed_experiment_source(
    app,
    graph: dict[str, str],
    *,
    name: str,
    request_id: str,
    idempotency_key: str,
) -> tuple[str, datetime]:
    """通过真实 API 和 scheduler 建立一条待投递的 experiment.completed source。"""

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        created = await client.post(
            "/experiments",
            json=_payload(graph, name=name, duration=60),
            headers=_headers(token, request_id, idempotency_key),
        )
        assert created.status_code == 201, created.text
        deadline = datetime.fromisoformat(
            str(created.json()["experiment"]["sampling_deadline"]).replace("Z", "+00:00")
        )

    completed_at = deadline + timedelta(seconds=1)
    assert run_experiment_scheduler_once(app.state.config, now=completed_at) is True
    connection = connect_database(app.state.config.database_url)
    try:
        source = connection.execute(
            """
            SELECT event.experiment_event_id,outbox.dispatch_status
            FROM experiment_events event
            JOIN experiment_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.experiment_event_id=event.experiment_event_id
            JOIN model_experiments experiment
              ON experiment.tenant_id=event.tenant_id
             AND experiment.experiment_id=event.experiment_id
            WHERE event.tenant_id=? AND experiment.name=?
              AND event.event_type='experiment.completed'
            """,
            (graph["tenant_id"], name),
        ).fetchone()
        assert source is not None and source["dispatch_status"] == "pending"
        return str(source["experiment_event_id"]), completed_at
    finally:
        connection.close()


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    *,
    endpoint_id: str,
    tenant_id: str,
    user_id: str,
    event_type: str,
) -> None:
    """在完整迁移 schema 中插入只使用合成 secret 元数据的 Webhook Endpoint。"""

    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'https://example.invalid/prd075',?,
                 'synthetic-ciphertext','whsec_test','prd075-synthetic-fingerprint',
                 'active',5000,3,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"PRD075 {endpoint_id}",
            json.dumps([event_type], ensure_ascii=True, separators=(",", ":")),
            user_id,
            user_id,
            NOW,
            NOW,
        ),
    )


def _insert_pending_webhook_delivery(
    connection: sqlite3.Connection,
    *,
    delivery_id: str,
    tenant_id: str,
    endpoint_id: str,
    event_id: str,
    event_type: str,
    event_timestamp: str,
    replayed_from: str | None = None,
    replayed_by: str | None = None,
) -> None:
    """插入真实约束下的 original 或 replay pending delivery 控制样本。"""

    delivery_kind = "replay" if replayed_from is not None else "original"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,
          delivery_kind,replayed_from_delivery_id,replay_idempotency_key,
          replayed_by,replayed_at,secret_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'{}','pending',0,3,?,?,?,?,?,1,?,?)
        """,
        (
            delivery_id,
            tenant_id,
            endpoint_id,
            event_id,
            event_type,
            event_timestamp,
            delivery_kind,
            replayed_from,
            f"prd075-{delivery_id}" if replayed_from is not None else None,
            replayed_by,
            event_timestamp if replayed_from is not None else None,
            event_timestamp,
            event_timestamp,
        ),
    )


def test_concurrent_same_scope_create_returns_exactly_one_created_and_one_conflict(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两个独立 Control 实例并发创建同 scope 时只允许一个 active 实验。"""

    database_url, graphs = _clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    first_app = _build_app()
    second_app = _build_app()
    barrier = threading.Barrier(2)

    with TestClient(first_app) as first_client, TestClient(second_app) as second_client:
        login = first_client.post(
            "/auth/login",
            json={
                "tenant_slug": "default",
                "email": "admin@metahub.local",
                "password": ADMIN_PASSWORD,
            },
        )
        assert login.status_code == 200, login.text
        token = str(login.json()["access_token"])

        def create(client: TestClient, ordinal: int):
            """在同一 barrier 后发起独立 HTTP 创建，保留各自 request_id。"""

            barrier.wait(timeout=5)
            return client.post(
                "/experiments",
                json=_payload(
                    graphs["a"],
                    name=f"PRD075 concurrent scope {ordinal}",
                ),
                headers=_headers(
                    token,
                    f"req-prd075-concurrent-create-{ordinal}",
                    f"75000000-0000-4000-8000-{754 + ordinal:012d}",
                ),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(create, first_client, 1),
                executor.submit(create, second_client, 2),
            )
            responses = [future.result(timeout=15) for future in futures]

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["code"] == "experiment.scope_conflict"
    assert conflict.json()["request_id"] in {
        "req-prd075-concurrent-create-1",
        "req-prd075-concurrent-create-2",
    }
    created = next(response for response in responses if response.status_code == 201)
    assert created.json()["request_id"] in {
        "req-prd075-concurrent-create-1",
        "req-prd075-concurrent-create-2",
    }

    connection = connect_database(database_url)
    try:
        active = connection.execute(
            """
            SELECT COUNT(*),COUNT(DISTINCT experiment_id)
            FROM model_experiments
            WHERE tenant_id=? AND project_id=? AND environment_id=?
              AND model_alias_id=? AND status='active'
            """,
            (
                graphs["a"]["tenant_id"],
                graphs["a"]["project_id"],
                graphs["a"]["environment_id"],
                graphs["a"]["model_alias_id"],
            ),
        ).fetchone()
        assert tuple(active) == (1, 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_experiment_api_idempotency_cursor_results_and_source_owner(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证实验完成事件源、Outbox 租约、恢复上限和 Webhook 外部事实边界。"""
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        payload_a = _payload(graphs["a"], name="PRD075 stable source")
        idempotency_key = "75000000-0000-4000-8000-000000000001"
        created = await client.post(
            "/experiments",
            json=payload_a,
            headers=_headers(token, "prd075-create-first", idempotency_key),
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store, private"
        body = created.json()
        experiment_id = body["experiment"]["experiment_id"]
        assert body["idempotent_replay"] is False
        assert "name" in body["experiment"]
        assert not ({"sampling_key_fingerprint", "request_fingerprint"} & set(body["experiment"]))

        replay = await client.post(
            "/experiments",
            json=payload_a,
            headers=_headers(token, "prd075-create-replay", idempotency_key),
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["experiment"]["experiment_id"] == experiment_id
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["request_id"] != body["request_id"]

        changed = dict(payload_a, name="PRD075 conflicting source")
        conflict = await client.post(
            "/experiments",
            json=changed,
            headers=_headers(token, "prd075-create-conflict", idempotency_key),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "experiment.idempotency_conflict"

        second = await client.post(
            "/experiments",
            json=_payload(graphs["b"], name="PRD075 second scope"),
            headers=_headers(
                token,
                "prd075-create-second",
                "75000000-0000-4000-8000-000000000002",
            ),
        )
        assert second.status_code == 201, second.text
        page = await client.get(
            "/experiments?limit=1",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "prd075-list"},
        )
        assert page.status_code == 200, page.text
        cursor = page.json()["next_cursor"]
        assert isinstance(cursor, str) and cursor
        wrong_owner = await client.get(
            f"/experiments?status=active&limit=1&cursor={cursor}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert wrong_owner.status_code == 422
        assert wrong_owner.json()["code"] == "experiment.query_invalid"

        detail = await client.get(
            f"/experiments/{experiment_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        results = await client.get(
            f"/experiments/{experiment_id}/results",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert detail.status_code == 200, detail.text
        assert results.status_code == 200, results.text
        assert results.json()["is_final"] is False
        assert [arm["variant"] for arm in results.json()["results"]] == ["a", "b"]

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT operation_request_id,operation_trace_id,idempotency_key_hash,
                   request_fingerprint FROM model_experiments
            WHERE experiment_id=?
            """,
            (experiment_id,),
        ).fetchone()
        assert source is not None
        assert source["operation_request_id"] == "prd075-create-first"
        assert source["operation_trace_id"]
        assert source["idempotency_key_hash"] and source["request_fingerprint"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_experiment_auth_strict_inputs_transaction_recheck_and_tenant_404(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证实验 API 后端权限、撤权复核和跨租户隐藏边界，前端状态不能补权。"""
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.post("/experiments", content=b"{")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["cache-control"] == "no-store, private"

        viewer = await _login(client, email="viewer@prd075.local", password=VIEWER_PASSWORD)
        denied = await client.post(
            "/experiments",
            content=b"{",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"
        assert denied.headers["cache-control"] == "no-store, private"

        admin = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        invalid_uuid = await client.post(
            "/experiments",
            json=_payload(graphs["a"], name="PRD075 invalid UUID"),
            headers=_headers(admin, "prd075-invalid-uuid", "75000000-0000-1000-8000-000000000001"),
        )
        assert invalid_uuid.status_code == 422
        assert invalid_uuid.json()["code"] == "experiment.input_invalid"

        duplicate_json = await client.post(
            "/experiments",
            content=b'{"name":"first","name":"second"}',
            headers={
                **_headers(
                    admin,
                    "prd075-duplicate-json",
                    "75000000-0000-4000-8000-000000000011",
                ),
                "Content-Type": "application/json",
            },
        )
        assert duplicate_json.status_code == 422
        assert duplicate_json.json()["code"] == "experiment.input_invalid"
        invalid_query = await client.get(
            "/experiments?limit=1&limit=2",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert invalid_query.status_code == 422
        get_body = await client.request(
            "GET",
            "/experiments",
            content=b"{}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert get_body.status_code == 422

        seen: list[str] = []
        original_guard = experiment_service.require_transaction_permission

        def revoked_guard(
            connection: sqlite3.Connection,
            context,
            permission_code: str,
            *,
            unavailable_error_code: str,
        ):
            """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
            seen.append(permission_code)
            # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
            if permission_code == "model.read":
                # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
                raise ApiError(unavailable_error_code)
            return original_guard(
                connection,
                context,
                permission_code,
                unavailable_error_code=unavailable_error_code,
            )

        monkeypatch.setattr(experiment_service, "require_transaction_permission", revoked_guard)
        revoked = await client.post(
            "/experiments",
            json=_payload(graphs["a"], name="PRD075 revoked in transaction"),
            headers=_headers(
                admin,
                "prd075-revoked-transaction",
                "75000000-0000-4000-8000-000000000012",
            ),
        )
        assert revoked.status_code == 503
        assert revoked.json()["code"] == "experiment.unavailable"
        assert seen == [
            "experiment.write",
            "project.read",
            "environment.read",
            "routing.read",
            "model.read",
        ]
        monkeypatch.setattr(experiment_service, "require_transaction_permission", original_guard)

        created = await client.post(
            "/experiments",
            json=_payload(graphs["a"], name="PRD075 tenant owner"),
            headers=_headers(
                admin,
                "prd075-tenant-owner",
                "75000000-0000-4000-8000-000000000013",
            ),
        )
        assert created.status_code == 201, created.text
        experiment_id = created.json()["experiment"]["experiment_id"]
        _insert_other_tenant(database_url)
        other = await _login(
            client,
            tenant_slug="prd075-other",
            email="admin@prd075-other.local",
            password=OTHER_PASSWORD,
        )
        hidden = await client.get(
            f"/experiments/{experiment_id}",
            headers={"Authorization": f"Bearer {other}"},
        )
        missing = await client.get(
            "/experiments/exp_ffffffffffffffffffffffffffffffff",
            headers={"Authorization": f"Bearer {other}"},
        )
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["code"] == missing.json()["code"] == "experiment.not_found"

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM model_experiments WHERE name='PRD075 revoked in transaction'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def _insert_other_tenant(database_url: str) -> None:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd075_other','prd075-other','PRD075 Other','internal_only',
                   'active',?,?)
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd075_other','tenant_prd075_other','admin@prd075-other.local',
                     'PRD075 Other Admin',?,'admin','active',?,?)
            """,
            (hash_password(OTHER_PASSWORD), NOW, NOW),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_expired_assignment(
    connection: sqlite3.Connection,
    graph: dict[str, str],
    experiment_id: str,
    *,
    lease_seconds: int = 1,
) -> tuple[str, datetime]:
    """向隔离测试数据库写入一组租户限定的合成事实，用于验证约束与回滚守卫。"""
    experiment = connection.execute(
        "SELECT started_at,execution_max_seconds FROM model_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    assert experiment is not None
    started = datetime.fromisoformat(str(experiment["started_at"]).replace("Z", "+00:00"))
    created_at = _db_time(started)
    lease_expires_at = _db_time(started + timedelta(seconds=lease_seconds))
    absolute_deadline_at = started + timedelta(seconds=int(experiment["execution_max_seconds"]))
    request_id = "req_prd075_reaper"
    trace_id = "trace_prd075_reaper"
    assignment_id = "exas_" + "a" * 32
    execution_id = "exrn_" + "a" * 32
    settlement_id = "exst_" + "a" * 32
    decision_id = "rdec_prd075_reaper"
    summary = json.dumps(
        {
            "algorithm_version": 1,
            "assignment_id": assignment_id,
            "assignment_reason": "hmac_preferred",
            "experiment_id": experiment_id,
            "variant": "a",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute("BEGIN IMMEDIATE")
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
            created_at,
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
            graph["application_id"],
            graph["environment_id"],
            graph["environment_code"],
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id,tenant_id,request_id,trace_id,virtual_key_id,
          model_alias_id,requested_model,environment,selected_model_id,
          selected_internal_model_id,provider_id,provider_code,decision_status,
          decision_reason,candidate_summary,error_code,created_at,environment_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'selected','experiment.variant_a',?,NULL,?,?)
        """,
        (
            decision_id,
            graph["tenant_id"],
            request_id,
            trace_id,
            graph["virtual_key_id"],
            graph["model_alias_id"],
            graph["alias"],
            graph["environment_code"],
            graph["model_a_id"],
            graph["model_a_id"],
            graph["provider_id"],
            graph["provider_code"],
            summary,
            created_at,
            graph["environment_id"],
        ),
    )
    connection.execute(
        """
        INSERT INTO experiment_assignments(
          assignment_id,tenant_id,experiment_id,request_id,trace_id,virtual_key_id,
          routing_decision_id,execution_id,usage_settlement_id,assignment_sequence,
          variant,model_id,provider_id,provider_code,sample_bucket,variant_bucket,
          assignment_reason,algorithm_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,1,'a',?,?,?,0,0,'hmac_preferred',1,?)
        """,
        (
            assignment_id,
            graph["tenant_id"],
            experiment_id,
            request_id,
            trace_id,
            graph["virtual_key_id"],
            decision_id,
            execution_id,
            settlement_id,
            graph["model_a_id"],
            graph["provider_id"],
            graph["provider_code"],
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO experiment_request_executions(
          execution_id,tenant_id,experiment_id,assignment_id,request_id,status,
          generation,lease_owner_id,lease_token,lease_expires_at,
          absolute_deadline_at,send_fence_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,'assigned',1,'gateway-prd075-reaper',
                 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',?,?,NULL,?,?)
        """,
        (
            execution_id,
            graph["tenant_id"],
            experiment_id,
            assignment_id,
            request_id,
            lease_expires_at,
            _db_time(absolute_deadline_at),
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO experiment_usage_settlements(
          usage_settlement_id,tenant_id,experiment_id,assignment_id,request_id,
          variant,model_id,provider_id,status,normalization_status,
          settlement_reason,settlement_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,'a',?,?,'pending','pending','awaiting_usage',1,?,?)
        """,
        (
            settlement_id,
            graph["tenant_id"],
            experiment_id,
            assignment_id,
            request_id,
            graph["model_a_id"],
            graph["provider_id"],
            created_at,
            created_at,
        ),
    )
    connection.commit()
    if lease_seconds == 1:
        for _ in range(50):
            expired = connection.execute(
                "SELECT unixepoch(?)<=unixepoch('now')",
                (lease_expires_at,),
            ).fetchone()[0]
            if expired:
                break
            time.sleep(0.05)
        else:
            # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
            raise AssertionError("SQLite execution lease did not expire")
    return execution_id, absolute_deadline_at + timedelta(seconds=1)


def _db_time(value: datetime) -> str:
    """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.mark.anyio
async def test_reaper_closes_expired_assigned_execution_without_inventing_provider_fact(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        created = await client.post(
            "/experiments",
            json=_payload(
                graphs["c"],
                name="PRD075 reaper owner",
                duration=604_800,
            ),
            headers=_headers(
                token,
                "prd075-reaper-create",
                "75000000-0000-4000-8000-000000000031",
            ),
        )
        assert created.status_code == 201, created.text
        experiment_id = str(created.json()["experiment"]["experiment_id"])

    connection = connect_database(database_url)
    try:
        execution_id, reap_at = _insert_expired_assignment(
            connection,
            graphs["c"],
            experiment_id,
        )
        connection.execute("BEGIN IMMEDIATE")
        assert (
            reap_expired_executions(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=reap_at,
            )
            == 1
        )
        connection.commit()
        execution = connection.execute(
            "SELECT status,generation FROM experiment_request_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        outcome = connection.execute(
            """
            SELECT gateway_status,exposure_status,outcome_reason,error_code,recovery_generation
            FROM experiment_request_outcomes WHERE execution_id=?
            """,
            (execution_id,),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT status,normalization_status,settlement_reason
            FROM experiment_usage_settlements WHERE experiment_id=?
            """,
            (experiment_id,),
        ).fetchone()
        assert execution is not None
        assert tuple(execution) == ("terminal", 2)
        assert outcome is not None
        assert tuple(outcome) == (
            "unresolved",
            "not_exposed",
            "abandoned_before_provider",
            "gateway.experiment_unavailable",
            2,
        )
        assert settlement is not None
        assert tuple(settlement) == ("not_exposed", "not_applicable", "not_exposed")
        connection.execute("BEGIN IMMEDIATE")
        assert (
            reap_expired_executions(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=reap_at + timedelta(seconds=1),
            )
            == 0
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_settlement_hard_deadline_preserves_pending_then_freezes_unavailable(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        created = await client.post(
            "/experiments",
            json=_payload(graphs["c"], name="PRD075 settlement deadline", duration=60),
            headers=_headers(
                token,
                "prd075-settlement-create",
                "75000000-0000-4000-8000-000000000032",
            ),
        )
        assert created.status_code == 201, created.text
        experiment = created.json()["experiment"]
        experiment_id = str(experiment["experiment_id"])
        sampling_deadline = datetime.fromisoformat(
            str(experiment["sampling_deadline"]).replace("Z", "+00:00")
        )

    connection = connect_database(database_url)
    try:
        _insert_expired_assignment(
            connection,
            graphs["c"],
            experiment_id,
            lease_seconds=120,
        )
    finally:
        connection.close()
    assert (
        run_experiment_scheduler_once(
            app.state.config,
            now=sampling_deadline + timedelta(seconds=1),
        )
        is True
    )

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT status,settlement_deadline_at FROM model_experiments
            WHERE tenant_id=? AND experiment_id=?
            """,
            (graphs["c"]["tenant_id"], experiment_id),
        ).fetchone()
        assert row is not None and row["status"] == "collecting_feedback"
        deadline = datetime.fromisoformat(str(row["settlement_deadline_at"]).replace("Z", "+00:00"))
        connection.execute("BEGIN IMMEDIATE")
        assert (
            close_expired_settlements(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=deadline - timedelta(seconds=1),
            )
            == 0
        )
        connection.commit()
        assert (
            connection.execute(
                "SELECT status FROM experiment_usage_settlements WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()[0]
            == "pending"
        )

        connection.execute("BEGIN IMMEDIATE")
        assert (
            close_expired_settlements(
                connection,
                tenant_id=graphs["c"]["tenant_id"],
                experiment_id=experiment_id,
                now=deadline + timedelta(seconds=1),
            )
            == 1
        )
        connection.commit()
        settlement = connection.execute(
            """
            SELECT status,normalization_status,settlement_reason
            FROM experiment_usage_settlements WHERE experiment_id=?
            """,
            (experiment_id,),
        ).fetchone()
        assert settlement is not None
        assert tuple(settlement) == ("unavailable", "unavailable", "hard_deadline")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_scheduler_skips_not_due_oldest_and_recovers_source_outbox_generation(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    scheduler_times: list[bool] = []
    outbox_times: list[bool] = []
    original_scheduler_time = experiment_scheduler._transaction_now
    original_outbox_time = experiment_outbox._transaction_now

    def checked_scheduler_time(connection: sqlite3.Connection, value):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        scheduler_times.append(connection.in_transaction)
        return original_scheduler_time(connection, value)

    def checked_outbox_time(connection: sqlite3.Connection, value):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        outbox_times.append(connection.in_transaction)
        return original_outbox_time(connection, value)

    monkeypatch.setattr(experiment_scheduler, "_transaction_now", checked_scheduler_time)
    monkeypatch.setattr(experiment_outbox, "_transaction_now", checked_outbox_time)
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        old = await client.post(
            "/experiments",
            json=_payload(
                graphs["a"],
                name="PRD075 oldest not due",
                duration=604_800,
            ),
            headers=_headers(
                token,
                "prd075-old-not-due",
                "75000000-0000-4000-8000-000000000021",
            ),
        )
        due = await client.post(
            "/experiments",
            json=_payload(graphs["b"], name="PRD075 newer due", duration=60),
            headers=_headers(
                token,
                "prd075-newer-due",
                "75000000-0000-4000-8000-000000000022",
            ),
        )
        assert old.status_code == due.status_code == 201
        old_id = old.json()["experiment"]["experiment_id"]
        due_id = due.json()["experiment"]["experiment_id"]
        due_deadline = datetime.fromisoformat(
            due.json()["experiment"]["sampling_deadline"].replace("Z", "+00:00")
        )

    scheduler_now = due_deadline + timedelta(seconds=1)
    assert run_experiment_scheduler_once(app.state.config, now=scheduler_now) is True
    connection = connect_database(database_url)
    try:
        statuses = {
            str(row["experiment_id"]): str(row["status"])
            for row in connection.execute(
                "SELECT experiment_id,status FROM model_experiments"
            ).fetchall()
        }
        assert statuses[old_id] == "active"
        assert statuses[due_id] == "completed"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM experiment_results WHERE experiment_id=?",
                (due_id,),
            ).fetchone()[0]
            == 2
        )
        event = connection.execute(
            """
            SELECT event.*,experiment.operation_request_id
            FROM experiment_events event
            JOIN model_experiments experiment
              ON experiment.tenant_id=event.tenant_id
             AND experiment.experiment_id=event.experiment_id
            WHERE event.experiment_id=?
            """,
            (due_id,),
        ).fetchone()
        assert event is not None
    finally:
        connection.close()

    first = claim_next_experiment_event(
        app.state.config,
        worker_id="prd075-outbox-first",
        now=scheduler_now,
    )
    assert first is not None and first.experiment_id == due_id
    connection = connect_database(database_url)
    try:
        assert (
            recover_experiment_completed_source_request_id(
                connection,
                first.event.to_payload(),
            )
            == "prd075-newer-due"
        )
    finally:
        connection.close()
    assert scheduler_times and all(scheduler_times)
    assert outbox_times and all(outbox_times)

    takeover_at = first.lease_expires_at + timedelta(seconds=1)
    second = claim_next_experiment_event(
        app.state.config,
        worker_id="prd075-outbox-second",
        now=takeover_at,
    )
    assert second is not None
    assert second.event_id == first.event_id
    assert second.dispatch_attempts == first.dispatch_attempts + 1
    assert second.lease_generation == first.lease_generation + 1
    assert (
        dispatch_claimed_experiment_event(
            app.state.config,
            first,
            now=takeover_at,
        )
        is False
    )
    assert (
        dispatch_claimed_experiment_event(
            app.state.config,
            second,
            now=takeover_at,
        )
        is True
    )
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT dispatch_status,lease_generation FROM experiment_event_outbox"
        ).fetchone()
        assert row is not None
        assert row["dispatch_status"] == "dispatched"
        assert row["lease_generation"] == second.lease_generation
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_exhausted_outbox_recovery_is_bounded_to_one_source_per_claim(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证实验完成事件源、Outbox 租约、恢复上限和 Webhook 外部事实边界。"""
    database_url, graphs = _clone_environment(prd075_control_template, monkeypatch, tmp_path)
    app = _build_app()
    deadlines: list[datetime] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, email="admin@metahub.local", password=ADMIN_PASSWORD)
        for marker, suffix in (("a", "41"), ("b", "42")):
            created = await client.post(
                "/experiments",
                json=_payload(
                    graphs[marker],
                    name=f"PRD075 bounded outbox {marker}",
                    duration=60,
                ),
                headers=_headers(
                    token,
                    f"prd075-bounded-outbox-{marker}",
                    f"75000000-0000-4000-8000-0000000000{suffix}",
                ),
            )
            assert created.status_code == 201, created.text
            deadlines.append(
                datetime.fromisoformat(
                    str(created.json()["experiment"]["sampling_deadline"]).replace("Z", "+00:00")
                )
            )

    current = max(deadlines) + timedelta(seconds=1)
    assert run_experiment_scheduler_once(app.state.config, now=current) is True
    current += timedelta(seconds=6)
    assert run_experiment_scheduler_once(app.state.config, now=current) is True
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM experiment_event_outbox WHERE dispatch_status='pending'"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()

    event_ids: set[str] | None = None
    for attempt in range(1, 11):
        first = claim_next_experiment_event(
            app.state.config,
            worker_id=f"prd075-exhausted-a-{attempt}",
            now=current,
        )
        second = claim_next_experiment_event(
            app.state.config,
            worker_id=f"prd075-exhausted-b-{attempt}",
            now=current,
        )
        assert first is not None and second is not None
        current_ids = {first.event_id, second.event_id}
        event_ids = current_ids if event_ids is None else event_ids
        assert current_ids == event_ids
        assert first.dispatch_attempts == second.dispatch_attempts == attempt
        assert first.lease_expires_at is not None and second.lease_expires_at is not None
        current = max(first.lease_expires_at, second.lease_expires_at) + timedelta(seconds=1)

    assert (
        claim_next_experiment_event(
            app.state.config,
            worker_id="prd075-exhausted-reaper-first",
            now=current,
        )
        is None
    )
    connection = connect_database(database_url)
    try:
        counts = {
            str(row["dispatch_status"]): int(row["total"])
            for row in connection.execute(
                """
                SELECT dispatch_status,COUNT(*) AS total
                FROM experiment_event_outbox GROUP BY dispatch_status
                """
            ).fetchall()
        }
        assert counts == {"failed": 1, "processing": 1}
    finally:
        connection.close()

    assert (
        claim_next_experiment_event(
            app.state.config,
            worker_id="prd075-exhausted-reaper-second",
            now=current,
        )
        is None
    )
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM experiment_event_outbox WHERE dispatch_status='failed'"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_experiment_source_failure_atomically_fails_real_pending_deliveries(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 source 失败必须精确终结 original intent，并由迁移 trigger 建立死信。"""

    database_url, graphs = _clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()
    event_id, source_time = await _create_completed_experiment_source(
        app,
        graphs["a"],
        name="PRD075 source delivery transaction",
        request_id="prd075-source-delivery-transaction",
        idempotency_key="75000000-0000-4000-8000-000000000051",
    )
    target_endpoints = (
        "wh_prd075_experiment_target_a",
        "wh_prd075_experiment_target_b",
    )
    connection = connect_database(database_url)
    try:
        actor_id = str(
            connection.execute(
                """
                SELECT user_id FROM users
                WHERE tenant_id=? AND role='admin' AND status='active'
                ORDER BY user_id LIMIT 1
                """,
                (graphs["a"]["tenant_id"],),
            ).fetchone()[0]
        )
        for endpoint_id in target_endpoints:
            _insert_webhook_endpoint(
                connection,
                endpoint_id=endpoint_id,
                tenant_id=graphs["a"]["tenant_id"],
                user_id=actor_id,
                event_type="experiment.completed",
            )
        connection.commit()
    finally:
        connection.close()

    claim = claim_next_experiment_event(
        app.state.config,
        worker_id="prd075-source-atomic-1",
        now=source_time + timedelta(seconds=1),
    )
    assert claim is not None and claim.event_id == event_id
    assert claim.dispatch_attempts == 1
    experiment_outbox._freeze_snapshot(
        app.state.config,
        claim,
        current=source_time + timedelta(seconds=1),
    )
    _insert_other_tenant(database_url)

    other_type_delivery_id = "whd_75000000000000000000000000000003"
    other_tenant_delivery_id = "whd_75000000000000000000000000000004"
    replay_delivery_id = "whd_75000000000000000000000000000005"
    event_timestamp = _db_time(source_time)
    connection = connect_database(database_url)
    try:
        target_rows = connection.execute(
            """
            SELECT webhook_delivery_id,webhook_endpoint_id
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND event_type='experiment.completed'
              AND delivery_kind='original' AND status='pending'
            ORDER BY webhook_endpoint_id
            """,
            (graphs["a"]["tenant_id"], event_id),
        ).fetchall()
        assert len(target_rows) == 2
        _insert_webhook_endpoint(
            connection,
            endpoint_id="wh_prd075_other_event",
            tenant_id=graphs["a"]["tenant_id"],
            user_id=actor_id,
            event_type="file.prompt_injection_detected",
        )
        _insert_webhook_endpoint(
            connection,
            endpoint_id="wh_prd075_other_tenant",
            tenant_id="tenant_prd075_other",
            user_id="user_prd075_other",
            event_type="experiment.completed",
        )
        _insert_pending_webhook_delivery(
            connection,
            delivery_id=other_type_delivery_id,
            tenant_id=graphs["a"]["tenant_id"],
            endpoint_id="wh_prd075_other_event",
            event_id=event_id,
            event_type="file.prompt_injection_detected",
            event_timestamp=event_timestamp,
        )
        _insert_pending_webhook_delivery(
            connection,
            delivery_id=other_tenant_delivery_id,
            tenant_id="tenant_prd075_other",
            endpoint_id="wh_prd075_other_tenant",
            event_id=event_id,
            event_type="experiment.completed",
            event_timestamp=event_timestamp,
        )
        _insert_pending_webhook_delivery(
            connection,
            delivery_id=replay_delivery_id,
            tenant_id=graphs["a"]["tenant_id"],
            endpoint_id=str(target_rows[0]["webhook_endpoint_id"]),
            event_id=event_id,
            event_type="experiment.completed",
            event_timestamp=event_timestamp,
            replayed_from=str(target_rows[0]["webhook_delivery_id"]),
            replayed_by=actor_id,
        )
        connection.commit()
        target_delivery_ids = {str(row["webhook_delivery_id"]) for row in target_rows}
    finally:
        connection.close()

    for expected_attempt in range(2, 11):
        assert claim.lease_expires_at is not None
        takeover_at = claim.lease_expires_at + timedelta(seconds=1)
        claim = claim_next_experiment_event(
            app.state.config,
            worker_id=f"prd075-source-atomic-{expected_attempt}",
            now=takeover_at,
        )
        assert claim is not None and claim.event_id == event_id
        assert claim.dispatch_attempts == expected_attempt

    assert claim.lease_expires_at is not None
    exhausted_at = claim.lease_expires_at + timedelta(seconds=1)
    assert (
        claim_next_experiment_event(
            app.state.config,
            worker_id="prd075-source-atomic-exhausted",
            now=exhausted_at,
        )
        is None
    )

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT dispatch_status,dispatch_attempts,last_error_code
            FROM experiment_event_outbox
            WHERE tenant_id=? AND experiment_event_id=?
            """,
            (graphs["a"]["tenant_id"], event_id),
        ).fetchone()
        assert source is not None
        assert tuple(source) == ("failed", 10, "webhook.delivery_unavailable")
        rows = {
            str(row["webhook_delivery_id"]): row
            for row in connection.execute(
                """
                SELECT webhook_delivery_id,status,error_code,error_summary,
                       next_retry_at,updated_at
                FROM webhook_deliveries WHERE event_id=?
                """,
                (event_id,),
            ).fetchall()
        }
        for delivery_id in target_delivery_ids:
            assert rows[delivery_id]["status"] == "failed"
            assert rows[delivery_id]["error_code"] == "webhook.delivery_unavailable"
            assert rows[delivery_id]["error_summary"] == ("Source delivery attempts are exhausted.")
            assert rows[delivery_id]["next_retry_at"] is None
            assert rows[delivery_id]["updated_at"] == _db_time(exhausted_at)
        for delivery_id in (
            other_type_delivery_id,
            other_tenant_delivery_id,
            replay_delivery_id,
        ):
            assert rows[delivery_id]["status"] == "pending"
            assert rows[delivery_id]["error_code"] is None

        dead_letters = connection.execute(
            """
            SELECT webhook_delivery_id,event_type,status,failure_error_code,failed_at
            FROM webhook_dead_letter_events
            WHERE event_id=? ORDER BY webhook_delivery_id
            """,
            (event_id,),
        ).fetchall()
        assert {str(row["webhook_delivery_id"]) for row in dead_letters} == (target_delivery_ids)
        assert all(
            row["event_type"] == "experiment.completed"
            and row["status"] == "dead_letter"
            and row["failure_error_code"] == "webhook.delivery_unavailable"
            and row["failed_at"] == _db_time(exhausted_at)
            for row in dead_letters
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_experiment_source_failure_rolls_back_deliveries_for_stale_owner(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """terminal cleanup 先执行但 source CAS 丢失时，delivery 与死信必须全部回滚。"""

    database_url, graphs = _clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()
    event_id, source_time = await _create_completed_experiment_source(
        app,
        graphs["b"],
        name="PRD075 stale source rollback",
        request_id="prd075-stale-source-rollback",
        idempotency_key="75000000-0000-4000-8000-000000000052",
    )
    connection = connect_database(database_url)
    try:
        actor_id = str(
            connection.execute(
                """
                SELECT user_id FROM users
                WHERE tenant_id=? AND role='admin' AND status='active'
                ORDER BY user_id LIMIT 1
                """,
                (graphs["b"]["tenant_id"],),
            ).fetchone()[0]
        )
        _insert_webhook_endpoint(
            connection,
            endpoint_id="wh_prd075_stale_source",
            tenant_id=graphs["b"]["tenant_id"],
            user_id=actor_id,
            event_type="experiment.completed",
        )
        connection.commit()
    finally:
        connection.close()

    first = claim_next_experiment_event(
        app.state.config,
        worker_id="prd075-stale-source-first",
        now=source_time + timedelta(seconds=1),
    )
    assert first is not None and first.event_id == event_id
    experiment_outbox._freeze_snapshot(
        app.state.config,
        first,
        current=source_time + timedelta(seconds=1),
    )
    assert first.lease_expires_at is not None
    takeover_at = first.lease_expires_at + timedelta(seconds=1)
    second = claim_next_experiment_event(
        app.state.config,
        worker_id="prd075-stale-source-second",
        now=takeover_at,
    )
    assert second is not None and second.event_id == event_id
    assert second.dispatch_attempts == 2

    assert (
        experiment_outbox._finish(
            app.state.config,
            first,
            error_code="webhook.payload_invalid",
            now=takeover_at,
        )
        is False
    )

    connection = connect_database(database_url)
    try:
        source = connection.execute(
            """
            SELECT dispatch_status,dispatch_attempts,lease_generation,
                   lease_owner_id,last_error_code
            FROM experiment_event_outbox
            WHERE tenant_id=? AND experiment_event_id=?
            """,
            (graphs["b"]["tenant_id"], event_id),
        ).fetchone()
        assert source is not None
        assert tuple(source) == (
            "processing",
            2,
            second.lease_generation,
            second.lease_owner_id,
            None,
        )
        deliveries = connection.execute(
            """
            SELECT status,error_code,error_summary,next_retry_at
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND event_type='experiment.completed'
              AND delivery_kind='original'
            """,
            (graphs["b"]["tenant_id"], event_id),
        ).fetchall()
        assert len(deliveries) == 1
        assert tuple(deliveries[0]) == ("pending", None, None, None)
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM webhook_dead_letter_events
            WHERE tenant_id=? AND event_id=?
            """,
                (graphs["b"]["tenant_id"], event_id),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_statistics_lane_does_not_depend_on_shared_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认线程池不可用时，专用统计 lane 仍应在自己的 250ms 预算内返回。"""

    expected = object()

    async def shared_pool_unavailable(*_args, **_kwargs):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
        raise AssertionError("statistics_must_not_use_shared_to_thread")

    def completed(*_args, **_kwargs):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        return expected

    monkeypatch.setattr(asyncio, "to_thread", shared_pool_unavailable)
    monkeypatch.setattr(experiment_service, "_get_results_sync", completed)
    result = await experiment_service.get_experiment_results(
        object(),
        object(),
        "exp_" + "c" * 32,
        request_id="prd075-statistics-dedicated",
        trace_id="trace-prd075-statistics-dedicated",
    )
    assert result is expected


@pytest.mark.anyio
async def test_statistics_worker_starts_its_own_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """线程真正运行时才取得 250ms SQLite 预算，调度包络不能改写查询期限。"""

    expected = object()
    observed_remaining: list[float] = []

    def completed(*args, **_kwargs):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        observed_remaining.append(args[-1] - time.monotonic())
        return expected

    monkeypatch.setattr(experiment_service, "_get_results_sync", completed)
    result = await experiment_service.get_experiment_results(
        object(),
        object(),
        "exp_" + "d" * 32,
        request_id="prd075-statistics-query-deadline",
        trace_id="trace-prd075-statistics-query-deadline",
    )
    assert result is expected
    assert len(observed_remaining) == 1
    assert 0.20 <= observed_remaining[0] <= 0.25


@pytest.mark.anyio
async def test_statistics_deadline_keeps_single_flight_gate_until_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证数据库时钟、租约接管、硬截止和回收并发只产生一个权威终态。"""
    started = threading.Event()
    release = threading.Event()

    def blocked(*_args, **_kwargs):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        started.set()
        release.wait(timeout=2)
        # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
        raise ApiError("experiment.statistics_unavailable")

    monkeypatch.setattr(experiment_service, "_get_results_sync", blocked)
    monkeypatch.setattr(experiment_service, "_STATISTICS_RESPONSE_DEADLINE_SECONDS", 0.05)
    first = asyncio.create_task(
        experiment_service.get_experiment_results(
            object(),
            object(),
            "exp_" + "a" * 32,
            request_id="prd075-statistics-first",
            trace_id="trace-prd075-statistics-first",
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    with pytest.raises(ApiError) as first_error:
        await first
    assert first_error.value.code == "experiment.statistics_unavailable"
    assert experiment_service._STATISTICS_GATE.locked()
    with pytest.raises(ApiError) as second_error:
        await experiment_service.get_experiment_results(
            object(),
            object(),
            "exp_" + "b" * 32,
            request_id="prd075-statistics-second",
            trace_id="trace-prd075-statistics-second",
        )
    assert second_error.value.code == "experiment.statistics_unavailable"
    release.set()
    for _ in range(100):
        if not experiment_service._STATISTICS_GATE.locked():
            break
        await asyncio.sleep(0.01)
    assert not experiment_service._STATISTICS_GATE.locked()


def test_statistics_connection_failure_uses_statistics_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证实验结果、质量资格与失败事实只来自权威持久记录，不推断缺失响应。"""

    def unavailable(_config):
        """构造 PRD-075 隔离测试辅助步骤，所有标识和敏感值均为合成且不写日志。"""
        # 测试回调只允许预期的租户、权限与并发路径，意外调用必须立即暴露。
        raise ApiError("experiment.unavailable")

    monkeypatch.setattr(experiment_service, "_connect", unavailable)
    with pytest.raises(ApiError) as failure:
        experiment_service._get_results_sync(
            object(),
            object(),
            "exp_" + "a" * 32,
            "prd075-statistics-connect",
            "trace-prd075-statistics-connect",
            float("inf"),
        )
    assert failure.value.code == "experiment.statistics_unavailable"


def test_statistics_transaction_fingerprint_rejects_role_change(
    prd075_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """路由鉴权后发生换角色时，结果事务必须拒绝旧 authorization fingerprint。"""

    database_url, _graphs = _clone_environment(
        prd075_control_template,
        monkeypatch,
        tmp_path,
    )
    app = _build_app()
    login = login_admin(
        app.state.config,
        tenant_slug="default",
        email="admin@metahub.local",
        password=ADMIN_PASSWORD,
        request_id="prd075-statistics-fingerprint-login",
        trace_id="trace-prd075-statistics-fingerprint-login",
        user_agent=None,
        client_host=None,
    )
    context = authenticate_bearer(
        app.state.config,
        f"Bearer {login.access_token}",
    )
    connection = connect_database(database_url)
    try:
        connection.execute("BEGIN")
        assert experiment_service._require_statistics_permission(connection, context) is context
        connection.rollback()
        connection.execute(
            "UPDATE users SET role='viewer',updated_at=? WHERE tenant_id=? AND user_id=?",
            (NOW, context.user.tenant_id, context.user.user_id),
        )
        connection.commit()
        connection.execute("BEGIN")
        with pytest.raises(ApiError) as revoked:
            experiment_service._require_statistics_permission(connection, context)
        assert revoked.value.code == "auth.permission_denied"
        connection.rollback()
    finally:
        connection.close()
