"""PRD-053 成本异常检测与 Control API 聚焦集成测试。

覆盖检测 run 的 claim/CAS、过期接管、迟到重评、滚动冷却，管理员查询与四类处置，
后端 RBAC、跨租户隐藏、严格 raw wire、actor 级幂等、Audit 同事务及 source outbox。
数据库均位于 pytest 独占目录；不保存 Token、Prompt、Response 或原始幂等键。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.auth.routes import router as auth_router
from metahub_control.cost_anomalies import detector
from metahub_control.cost_anomalies import outbox as anomaly_outbox
from metahub_control.cost_anomalies import service as anomaly_service
from metahub_control.cost_anomalies.routes import router as anomaly_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 复用 0043 迁移专项已验证的正式 Request -> PriceSnapshot -> Usage 夹具，避免测试直写账本投影。
from test_prd053_db_migrations import _insert_usage_charge, _metadata

# 测试身份、第二租户和固定检测时钟集中定义，用于验证真实后端权限与租户隔离边界。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"
OTHER_TENANT_ID = "tenant_prd053_control_other"
OTHER_TENANT_SLUG = "prd053-control-other"
DETECTION_NOW = datetime(2026, 7, 27, 10, 7, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CostAnomalyFixture:
    """API 场景的安全资源 ID，不包含密码哈希、Session Token 或 Key 明文。"""

    database_url: str
    tenant_id: str
    environment_id: str
    virtual_key_id: str
    tenant_anomaly_id: str
    key_spike_anomaly_id: str
    key_off_hours_anomaly_id: str


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占文件转换为迁移和服务共同支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _configure_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> tuple[str, object]:
    """迁移并 seed 沙箱数据库；关闭 scheduler，检测器由测试同步驱动。"""

    database_url = _sqlite_url(tmp_path / name)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd053-control-auth-secret")
    monkeypatch.setenv("METAHUB_COST_ANOMALY_ENABLED", "false")
    apply_migrations(database_url)
    return database_url, apply_seed_data(database_url)


def _drain_detection(config: AppConfig, now: datetime) -> int:
    """在有限循环中完成全部可领取窗口，防止候选错误导致专项测试挂起。"""

    completed = 0
    for _ in range(16):
        claim = detector.claim_next_detection(
            config,
            worker_id="worker-prd053-control-drain",
            now=now,
        )
        if claim is None:
            return completed
        # 完成时刻仍在 30 秒 lease 内，验证真实持久化 CAS。
        assert detector.process_claimed_detection(
            config,
            claim,
            now=now + timedelta(seconds=1),
        )
        completed += 1
    # 有界轮询耗尽表示检测器未收敛，必须立刻失败而不是让专项测试无限挂起。
    raise AssertionError("detector did not become idle within bounded iterations")


def _insert_role_matrix(connection, tenant_id: str) -> None:
    """创建 developer、viewer 和第二租户 admin，供后端 RBAC 与真实跨租户测试。"""

    timestamp = "2026-07-27T09:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd053_control_developer",
                tenant_id,
                "developer@example.com",
                "PRD053 Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                timestamp,
                timestamp,
            ),
            (
                "user_prd053_control_viewer",
                tenant_id,
                "viewer@example.com",
                "PRD053 Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                timestamp,
                timestamp,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, ?, 'PRD053 其他租户', 'internal_only', 'active', ?, ?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(
          'user_prd053_control_other_admin', ?,
          'admin@other.example.com', 'PRD053 Other Admin', ?,
          'admin', 'active', ?, ?
        )
        """,
        (OTHER_TENANT_ID, hash_password(OTHER_ADMIN_PASSWORD), timestamp, timestamp),
    )


def _prepare_api_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> CostAnomalyFixture:
    """生成低额基线与次日高额窗口，得到 tenant spike、Key spike 和 off-hours。"""

    database_url, seed = _configure_database(monkeypatch, tmp_path, name)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    _insert_role_matrix(connection, seed.tenant_id)
    _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "api_baseline",
        created_at="2026-07-26T10:00:10Z",
        amount="0.001000",
    )
    _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "api_current",
        created_at="2026-07-27T10:00:10Z",
        amount="1.000000",
    )
    connection.commit()
    connection.close()

    config = load_app_config("control-plane-api")
    assert _drain_detection(config, DETECTION_NOW) == 2
    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT cost_anomaly_id, rule_type
        FROM cost_anomalies WHERE tenant_id=? ORDER BY anomaly_sequence
        """,
        (seed.tenant_id,),
    ).fetchall()
    connection.close()
    by_rule = {str(row["rule_type"]): str(row["cost_anomaly_id"]) for row in rows}
    assert set(by_rule) == {
        "tenant_spend_spike",
        "virtual_key_spend_spike",
        "virtual_key_off_hours",
    }
    return CostAnomalyFixture(
        database_url=database_url,
        tenant_id=seed.tenant_id,
        environment_id=metadata["environment_id"],
        virtual_key_id=metadata["virtual_key_id"],
        tenant_anomaly_id=by_rule["tenant_spend_spike"],
        key_spike_anomaly_id=by_rule["virtual_key_spend_spike"],
        key_off_hours_anomaly_id=by_rule["virtual_key_off_hours"],
    )


def _build_app():
    """构造含统一中间件、认证和成本异常路由的最小真实应用。"""

    app = create_service_app(
        "control-plane-api",
        "PRD-053 Cost Anomaly Control Test",
        extra_cors_allow_headers=("Idempotency-Key",),
    )
    app.include_router(auth_router)
    app.include_router(anomaly_router)
    return app


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录隔离用户并返回仅供 Authorization Header 使用的 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 登录令牌只留在当前测试调用栈，不写入快照、数据库或诊断输出。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造认证与调用链头；Token 不进入断言快照或持久测试记录。"""

    # Authorization 仅用于真实后端鉴权，断言与测试记录不得输出令牌原文。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


async def _patch_action(
    client: AsyncClient,
    token: str,
    anomaly_id: str,
    *,
    idempotency_key: str,
    request_id: str,
    payload: dict[str, object],
) -> Response:
    """按正式 wire 提交处置；tenant 和 actor 只能来自认证上下文。"""

    return await client.patch(
        f"/cost-anomalies/{anomaly_id}/action",
        headers={
            **_headers(token, request_id),
            "Idempotency-Key": idempotency_key,
        },
        json=payload,
    )


def _assert_error(response: Response, status: int, code: str) -> None:
    """断言统一状态、领域 code 与响应头/正文一致的 request_id。"""

    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]

def test_prd053_detector_claim_cas_late_revaluation_and_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """未过期 run 不可抢占；过期接管提升 revision，旧 worker 完成 CAS 失败。

    同窗迟到 Usage 只提升水位并由冷却抑制重复证据；十分钟后的窗口继续对同规则
    同 Key 执行滚动三十分钟冷却。
    """

    database_url, seed = _configure_database(
        monkeypatch,
        tmp_path,
        "prd053-detector-control.db",
    )
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    first_ledger = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "claim_current",
        created_at="2026-07-27T10:00:10Z",
        amount="1.000000",
    )
    connection.commit()
    connection.close()
    config = load_app_config("control-plane-api")

    first = detector.claim_next_detection(
        config,
        worker_id="worker-prd053-control-old",
        now=DETECTION_NOW,
    )
    assert first is not None
    assert first.evaluation_revision == 1
    assert detector.claim_next_detection(
        config,
        worker_id="worker-prd053-control-blocked",
        now=DETECTION_NOW + timedelta(seconds=10),
    ) is None

    takeover = detector.claim_next_detection(
        config,
        worker_id="worker-prd053-control-new",
        now=DETECTION_NOW + timedelta(seconds=31),
    )
    assert takeover is not None
    assert takeover.detection_run_id == first.detection_run_id
    assert takeover.evaluation_revision == 2
    # lease 接管属于同一 detection run，必须复用首次创建的固定操作链路。
    assert takeover.operation_request_id == first.operation_request_id
    assert takeover.operation_trace_id == first.operation_trace_id
    assert not detector.process_claimed_detection(
        config,
        first,
        now=DETECTION_NOW + timedelta(seconds=32),
    )
    assert detector.process_claimed_detection(
        config,
        takeover,
        now=DETECTION_NOW + timedelta(seconds=33),
    )

    connection = connect_database(database_url)
    initial = connection.execute(
        """
        SELECT rule_type, scope_id FROM cost_anomalies
        WHERE tenant_id=? ORDER BY anomaly_sequence
        """,
        (seed.tenant_id,),
    ).fetchall()
    assert [(row["rule_type"], row["scope_id"]) for row in initial] == [
        ("virtual_key_off_hours", metadata["virtual_key_id"])
    ]
    late_ledger = _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "claim_late",
        created_at="2026-07-27T10:00:20Z",
        amount="0.500000",
    )
    connection.commit()
    connection.close()
    assert int(late_ledger["ledger_sequence"]) > int(first_ledger["ledger_sequence"])

    late = detector.claim_next_detection(
        config,
        worker_id="worker-prd053-control-late",
        now=DETECTION_NOW + timedelta(minutes=1),
    )
    assert late is not None
    assert late.detection_run_id == first.detection_run_id
    assert late.evaluation_revision == 3
    # 迟到水位重评只能提升 revision，不能让同一 anomaly/event 改换 trace。
    assert late.operation_request_id == first.operation_request_id
    assert late.operation_trace_id == first.operation_trace_id
    assert late.window.frozen_watermark == int(late_ledger["ledger_sequence"])
    assert detector.process_claimed_detection(
        config,
        late,
        now=DETECTION_NOW + timedelta(minutes=1, seconds=1),
    )

    connection = connect_database(database_url)
    reevaluated = connection.execute(
        """
        SELECT evaluation_revision, ledger_watermark, anomaly_count, suppressed_count
        FROM cost_anomaly_detection_runs
        WHERE tenant_id=? AND detection_run_id=?
        """,
        (seed.tenant_id, first.detection_run_id),
    ).fetchone()
    assert reevaluated is not None
    # 同窗迟到重评命中同一不可变异常证据时是精确 no-op，不计为跨窗口冷却抑制。
    assert tuple(reevaluated) == (
        3,
        int(late_ledger["ledger_sequence"]),
        0,
        0,
    )
    _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "cooldown_next",
        created_at="2026-07-27T10:10:10Z",
        amount="1.000000",
    )
    connection.commit()
    connection.close()

    next_window = detector.claim_next_detection(
        config,
        worker_id="worker-prd053-control-cooldown",
        now=DETECTION_NOW + timedelta(minutes=10),
    )
    assert next_window is not None
    assert next_window.window.window_start.isoformat() == "2026-07-27T10:10:00+00:00"
    assert detector.process_claimed_detection(
        config,
        next_window,
        now=DETECTION_NOW + timedelta(minutes=10, seconds=1),
    )
    connection = connect_database(database_url)
    cooldown_run = connection.execute(
        """
        SELECT anomaly_count, suppressed_count
        FROM cost_anomaly_detection_runs
        WHERE tenant_id=? AND detection_run_id=?
        """,
        (seed.tenant_id, next_window.detection_run_id),
    ).fetchone()
    off_hours_count = connection.execute(
        """
        SELECT COUNT(*) FROM cost_anomalies
        WHERE tenant_id=? AND rule_type='virtual_key_off_hours' AND scope_id=?
        """,
        (seed.tenant_id, metadata["virtual_key_id"]),
    ).fetchone()[0]
    connection.close()
    assert cooldown_run is not None
    assert cooldown_run["suppressed_count"] == 1
    assert cooldown_run["anomaly_count"] == 2
    assert off_hours_count == 1

def test_prd053_detector_insert_same_second_outbox_drain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 detector 写入异常后在同一注入秒完成 outbox claim 与 source finish。"""

    database_url, seed = _configure_database(
        monkeypatch,
        tmp_path,
        "prd053-same-second-outbox.db",
    )
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    _insert_usage_charge(
        connection,
        seed.tenant_id,
        metadata,
        "same_second_outbox",
        created_at="2026-07-27T10:00:10Z",
        amount="1.000000",
    )
    connection.commit()
    connection.close()

    config = load_app_config("control-plane-api")
    claim = detector.claim_next_detection(
        config,
        worker_id="worker-prd053-same-second-detector",
        now=DETECTION_NOW,
    )
    assert claim is not None
    completed_at = DETECTION_NOW + timedelta(seconds=1)
    assert detector.process_claimed_detection(config, claim, now=completed_at)
    # detector 插入与 outbox drain 共用同一秒，行版本仍须连续推进并完成投递。
    assert anomaly_outbox.drain_cost_anomaly_event_once(
        config,
        worker_id="worker-prd053-same-second-outbox",
        now=completed_at,
    )

    connection = connect_database(database_url)
    source = connection.execute(
        """
        SELECT event_dispatch_status, event_dispatched_at, updated_at
        FROM cost_anomalies WHERE tenant_id=?
        """,
        (seed.tenant_id,),
    ).fetchone()
    connection.close()
    assert source is not None
    assert source["event_dispatch_status"] == "dispatched"
    assert source["event_dispatched_at"] == "2026-07-27T10:07:03Z"
    assert source["updated_at"] == source["event_dispatched_at"]


@pytest.mark.anyio
async def test_prd053_admin_actions_idempotency_audit_and_source_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证管理员四类处置、同秒状态迁移、幂等审计及 source outbox 的真实 CAS。"""

    fixture = _prepare_api_fixture(
        monkeypatch,
        tmp_path,
        "prd053-control-actions.db",
    )
    clock = {"now": datetime(2026, 7, 27, 10, 9, 0, tzinfo=UTC)}
    monkeypatch.setattr(anomaly_service, "_service_now", lambda _value=None: clock["now"])
    audit_dispatches: list[Any] = []
    monkeypatch.setattr(
        anomaly_service,
        "dispatch_audit_recorded",
        lambda _config, _context, record, **_kwargs: audit_dispatches.append(record),
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        listed = await client.get(
            "/cost-anomalies?limit=2",
            headers=_headers(admin, "req_prd053_control_list"),
        )
        assert listed.status_code == 200, listed.text
        listed_body = listed.json()
        assert listed_body["total"] == 3
        assert len(listed_body["cost_anomalies"]) == 2
        assert listed_body["has_more"] is True
        assert listed_body["next_cursor"]
        assert listed_body["summary"]["detector_status"] == "healthy"
        assert listed_body["request_id"] == "req_prd053_control_list"
        assert listed.headers["X-Request-ID"] == listed_body["request_id"]
        assert "no-store" in listed.headers["Cache-Control"]

        # 首屏恰好是两个 Key 级异常时不会读取 tenant_name；扩大分页必须实际物化租户级
        # 异常，防止列表 SQL 漏联 tenants 后仅在真实数据顺序下触发 500。
        tenant_listed = await client.get(
            "/cost-anomalies?limit=100",
            headers=_headers(admin, "req_prd053_control_tenant_list"),
        )
        assert tenant_listed.status_code == 200, tenant_listed.text
        tenant_items = {
            item["cost_anomaly_id"]: item
            for item in tenant_listed.json()["cost_anomalies"]
        }
        assert tenant_items[fixture.tenant_anomaly_id]["scope_type"] == "tenant"
        assert tenant_items[fixture.tenant_anomaly_id]["scope_label"]

        clock["now"] = datetime(2026, 7, 27, 10, 10, 0, tzinfo=UTC)
        acknowledged = await _patch_action(
            client,
            admin,
            fixture.tenant_anomaly_id,
            idempotency_key="idem-prd053-ack-main",
            request_id="req_prd053_control_ack",
            payload={"action": "acknowledge"},
        )
        replay = await _patch_action(
            client,
            admin,
            fixture.tenant_anomaly_id,
            idempotency_key="idem-prd053-ack-main",
            request_id="req_prd053_control_ack_replay",
            payload={"action": "acknowledge"},
        )
        conflict = await _patch_action(
            client,
            admin,
            fixture.tenant_anomaly_id,
            idempotency_key="idem-prd053-ack-main",
            request_id="req_prd053_control_ack_conflict",
            payload={"action": "dismiss", "dismiss_reason": "expected_activity"},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["cost_anomaly"]["status"] == "acknowledged"
        assert acknowledged.json()["idempotent_replay"] is False
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["request_id"] == "req_prd053_control_ack_replay"
        assert (
            replay.json()["action"]["cost_anomaly_action_id"]
            == acknowledged.json()["action"]["cost_anomaly_action_id"]
        )
        _assert_error(conflict, 409, "cost_anomaly.idempotency_conflict")

        # 模拟管理员在同一秒连续确认并关闭异常；updated_at 必须由服务层单调推进，
        # 不能依赖墙钟自然跨秒，也不能把动作时间伪造到缓解/到期边界之外。
        dismissed = await _patch_action(
            client,
            admin,
            fixture.tenant_anomaly_id,
            idempotency_key="idem-prd053-dismiss-main",
            request_id="req_prd053_control_dismiss",
            payload={"action": "dismiss", "dismiss_reason": "expected_activity"},
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["cost_anomaly"]["status"] == "dismissed"

        clock["now"] = datetime(2026, 7, 27, 10, 12, 0, tzinfo=UTC)
        applied = await _patch_action(
            client,
            admin,
            fixture.key_spike_anomaly_id,
            idempotency_key="idem-prd053-apply-main",
            request_id="req_prd053_control_apply",
            payload={
                "action": "apply_temporary_rate_limit",
                "duration_minutes": 30,
                "rpm_limit": 10,
            },
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        assert applied_body["cost_anomaly"]["status"] == "mitigated"
        assert applied_body["temporary_rate_limit_override"]["effective_status"] == "active"
        assert applied_body["action"]["effective_rpm_after"] == 10
        override_id = applied_body["temporary_rate_limit_override"][
            "temporary_rate_limit_override_id"
        ]

        clock["now"] = datetime(2026, 7, 27, 10, 13, 0, tzinfo=UTC)
        released = await _patch_action(
            client,
            admin,
            fixture.key_spike_anomaly_id,
            idempotency_key="idem-prd053-release-main",
            request_id="req_prd053_control_release",
            payload={"action": "release_temporary_rate_limit"},
        )
        assert released.status_code == 200, released.text
        released_body = released.json()
        assert released_body["cost_anomaly"]["status"] == "mitigated"
        assert released_body["temporary_rate_limit_override"]["effective_status"] == "released"
        assert released_body["action"]["target_override_id"] == override_id

    connection = connect_database(fixture.database_url)
    action_rows = connection.execute(
        """
        SELECT action_type, idempotency_key_sha256
        FROM cost_anomaly_actions
        WHERE tenant_id=? ORDER BY rowid
        """,
        (fixture.tenant_id,),
    ).fetchall()
    audit_rows = connection.execute(
        """
        SELECT action, object_id, request_id
        FROM audit_logs
        WHERE tenant_id=? AND object_type='cost_anomaly'
        ORDER BY request_id
        """,
        (fixture.tenant_id,),
    ).fetchall()
    override = connection.execute(
        """
        SELECT status, source_id, release_action_id, released_by
        FROM temporary_rate_limit_overrides
        WHERE tenant_id=? AND temporary_rate_limit_override_id=?
        """,
        (fixture.tenant_id, override_id),
    ).fetchone()
    anomaly_timestamp = connection.execute(
        """
        SELECT updated_at FROM cost_anomalies
        WHERE tenant_id=? AND cost_anomaly_id=?
        """,
        (fixture.tenant_id, fixture.tenant_anomaly_id),
    ).fetchone()
    connection.close()
    assert [row["action_type"] for row in action_rows] == [
        "acknowledge",
        "dismiss",
        "apply_temporary_rate_limit",
        "release_temporary_rate_limit",
    ]
    assert all(len(str(row["idempotency_key_sha256"])) == 64 for row in action_rows)
    assert {row["action"] for row in audit_rows} == {
        "cost_anomaly.acknowledged",
        "cost_anomaly.dismissed",
        "cost_anomaly.temporary_rate_limit_applied",
        "cost_anomaly.temporary_rate_limit_released",
    }
    assert len(audit_rows) == 4
    assert len(audit_dispatches) == 4
    assert anomaly_timestamp is not None
    assert anomaly_timestamp["updated_at"] == "2026-07-27T10:10:01Z"
    assert override is not None
    assert override["status"] == "released"
    assert override["source_id"] == fixture.key_spike_anomaly_id
    assert override["release_action_id"]
    assert override["released_by"]

    # 无订阅路径不发网络请求，但必须在 claim 同一注入秒完成 source 状态机。
    config = app.state.config
    first_claim = anomaly_outbox.claim_next_cost_anomaly_event(
        config,
        worker_id="worker-prd053-outbox-first",
        now=datetime(2026, 7, 27, 10, 20, 0, tzinfo=UTC),
    )
    assert first_claim is not None
    assert anomaly_outbox.dispatch_claimed_cost_anomaly_event(
        config,
        first_claim,
        now=datetime(2026, 7, 27, 10, 20, 0, tzinfo=UTC),
    )

    old_claim = anomaly_outbox.claim_next_cost_anomaly_event(
        config,
        worker_id="worker-prd053-outbox-old",
        now=datetime(2026, 7, 27, 10, 21, 0, tzinfo=UTC),
    )
    assert old_claim is not None
    takeover = anomaly_outbox.claim_next_cost_anomaly_event(
        config,
        worker_id="worker-prd053-outbox-new",
        now=datetime(2026, 7, 27, 10, 21, 31, tzinfo=UTC),
    )
    assert takeover is not None
    assert takeover.cost_anomaly_id == old_claim.cost_anomaly_id
    assert takeover.event_dispatch_attempts == 2
    assert not anomaly_outbox._finish_success(
        config,
        old_claim,
        datetime(2026, 7, 27, 10, 21, 31, tzinfo=UTC),
    )
    assert anomaly_outbox.dispatch_claimed_cost_anomaly_event(
        config,
        takeover,
        now=datetime(2026, 7, 27, 10, 21, 31, tzinfo=UTC),
    )
    connection = connect_database(fixture.database_url)
    event_rows = connection.execute(
        """
        SELECT cost_anomaly_id, event_dispatch_status, event_dispatch_attempts,
               event_claim_token, event_lease_owner_id, event_last_error_code
        FROM cost_anomalies
        WHERE tenant_id=? AND cost_anomaly_id IN (?, ?)
        ORDER BY anomaly_sequence
        """,
        (
            fixture.tenant_id,
            first_claim.cost_anomaly_id,
            takeover.cost_anomaly_id,
        ),
    ).fetchall()
    connection.close()
    assert [tuple(row) for row in event_rows] == [
        (first_claim.cost_anomaly_id, "dispatched", 1, None, None, None),
        (takeover.cost_anomaly_id, "dispatched", 2, None, None, None),
    ]

@pytest.mark.anyio
async def test_prd053_rbac_cross_tenant_and_strict_raw_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """认证/RBAC 先于解析；admin 仍受唯一 Header、8 KiB、UTF-8 和 StrictInt 约束。"""

    fixture = _prepare_api_fixture(
        monkeypatch,
        tmp_path,
        "prd053-control-wire.db",
    )
    monkeypatch.setattr(
        anomaly_service,
        "_service_now",
        lambda _value=None: datetime(2026, 7, 27, 10, 12, 0, tzinfo=UTC),
    )
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        other_admin = await _login(
            client,
            "admin@other.example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )

        # 畸形输入不能让匿名、developer 或 viewer 探测管理员接口的解析合同。
        anonymous = await client.get(
            "/cost-anomalies?unknown=value",
            headers={"X-Request-ID": "req_prd053_wire_anonymous"},
        )
        developer_denied = await client.get(
            "/cost-anomalies?unknown=value",
            headers=_headers(developer, "req_prd053_wire_developer"),
        )
        viewer_denied = await client.patch(
            f"/cost-anomalies/{fixture.tenant_anomaly_id}/action?unknown=value",
            headers=_headers(viewer, "req_prd053_wire_viewer"),
            content=b"{",
        )
        # 第二租户 admin 访问默认租户真实资源，资源 ID 与不存在场景保持同形 404。
        cross_tenant_action = await _patch_action(
            client,
            other_admin,
            fixture.tenant_anomaly_id,
            idempotency_key="idem-prd053-cross-tenant",
            request_id="req_prd053_wire_cross_action",
            payload={"action": "acknowledge"},
        )
        cross_tenant_filter = await client.get(
            f"/cost-anomalies?environment_id={fixture.environment_id}",
            headers=_headers(other_admin, "req_prd053_wire_cross_filter"),
        )
        unknown_query = await client.get(
            "/cost-anomalies?tenant_id=other",
            headers=_headers(admin, "req_prd053_wire_unknown_query"),
        )
        duplicate_query = await client.get(
            "/cost-anomalies?status=open&status=dismissed",
            headers=_headers(admin, "req_prd053_wire_duplicate_query"),
        )

        path = f"/cost-anomalies/{fixture.key_off_hours_anomaly_id}/action"
        authorization = ("Authorization", f"Bearer {admin}")
        duplicate_content_type = await client.patch(
            path,
            headers=[
                authorization,
                ("X-Request-ID", "req_prd053_wire_duplicate_content_type"),
                ("Content-Type", "application/json"),
                ("Content-Type", "application/json; charset=utf-8"),
                ("Idempotency-Key", "idem-prd053-dup-content-type"),
            ],
            content=b'{"action":"acknowledge"}',
        )
        invalid_content_types: list[Response] = []
        for suffix, content_type in (
            ("open-quote", 'application/json; charset="utf-8'),
            ("close-quote", 'application/json; charset=utf-8"'),
        ):
            invalid_content_types.append(
                await client.patch(
                    path,
                    headers={
                        **_headers(admin, f"req_prd053_wire_charset_{suffix}"),
                        "Content-Type": content_type,
                        "Idempotency-Key": f"idem-prd053-charset-{suffix}",
                    },
                    content=b'{"action":"acknowledge"}',
                )
            )
        duplicate_idempotency = await client.patch(
            path,
            headers=[
                authorization,
                ("X-Request-ID", "req_prd053_wire_duplicate_idempotency"),
                ("Content-Type", "application/json"),
                ("Idempotency-Key", "idem-prd053-dup-one"),
                ("Idempotency-Key", "idem-prd053-dup-two"),
            ],
            content=b'{"action":"acknowledge"}',
        )

        malformed_cases: list[Response] = []
        malformed_cases.append(
            await client.patch(
                path,
                headers={
                    **_headers(admin, "req_prd053_wire_duplicate_json"),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "idem-prd053-duplicate-json",
                },
                content=b'{"action":"acknowledge","action":"dismiss"}',
            )
        )
        malformed_cases.append(
            await client.patch(
                path,
                headers={
                    **_headers(admin, "req_prd053_wire_invalid_utf8"),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "idem-prd053-invalid-utf8",
                },
                content=b'{"action":"acknowledge","note":"\xff"}',
            )
        )
        malformed_cases.append(
            await client.patch(
                path,
                headers={
                    **_headers(admin, "req_prd053_wire_oversized"),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "idem-prd053-oversized",
                },
                content=b"{" + b"x" * (8 * 1024),
            )
        )
        malformed_cases.append(
            await client.patch(
                path,
                headers={
                    **_headers(admin, "req_prd053_wire_length_mismatch"),
                    "Content-Type": "application/json",
                    "Content-Length": "999",
                    "Idempotency-Key": "idem-prd053-length-mismatch",
                },
                content=b'{"action":"acknowledge"}',
            )
        )
        malformed_cases.append(
            await client.patch(
                path,
                headers={
                    **_headers(admin, "req_prd053_wire_float"),
                    "Content-Type": "application/json",
                    "Idempotency-Key": "idem-prd053-float",
                },
                content=(
                    b'{"action":"apply_temporary_rate_limit",'
                    b'"duration_minutes":5.0,"rpm_limit":10}'
                ),
            )
        )
        for suffix, value in (("string", '"10"'), ("boolean", "true")):
            malformed_cases.append(
                await client.patch(
                    path,
                    headers={
                        **_headers(admin, f"req_prd053_wire_{suffix}"),
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"idem-prd053-{suffix}-integer",
                    },
                    content=(
                        '{"action":"apply_temporary_rate_limit",'
                        f'"duration_minutes":5,"rpm_limit":{value}'
                        "}"
                    ).encode("ascii"),
                )
            )
        # 非 apply 动作的无关显式 null 必须拒绝；省略与 null 不能被 Pydantic 混为一谈。
        for suffix, body in (
            (
                "ack-null",
                b'{"action":"acknowledge","dismiss_reason":null}',
            ),
            (
                "dismiss-null",
                b'{"action":"dismiss","dismiss_reason":"other","rpm_limit":null}',
            ),
            (
                "release-null",
                b'{"action":"release_temporary_rate_limit","duration_minutes":null}',
            ),
        ):
            malformed_cases.append(
                await client.patch(
                    path,
                    headers={
                        **_headers(admin, f"req_prd053_wire_{suffix}"),
                        "Content-Type": "application/json",
                        "Idempotency-Key": f"idem-prd053-{suffix}",
                    },
                    content=body,
                )
            )
        # apply 的可选限流字段允许显式 null，只要至少一个维度真正收紧。
        valid_apply_null = await client.patch(
            path,
            headers={
                **_headers(admin, "req_prd053_wire_apply_null"),
                "Content-Type": "application/json",
                "Idempotency-Key": "idem-prd053-apply-null",
            },
            content=(
                b'{"action":"apply_temporary_rate_limit","duration_minutes":5,'
                b'"rpm_limit":10,"tpm_limit":null}'
            ),
        )

    _assert_error(anonymous, 401, "auth.token_missing")
    _assert_error(developer_denied, 403, "auth.permission_denied")
    _assert_error(viewer_denied, 403, "auth.permission_denied")
    _assert_error(cross_tenant_action, 404, "cost_anomaly.not_found")
    _assert_error(cross_tenant_filter, 404, "cost_anomaly.not_found")
    _assert_error(unknown_query, 422, "cost_anomaly.query_invalid")
    _assert_error(duplicate_query, 422, "cost_anomaly.query_invalid")
    _assert_error(duplicate_content_type, 415, "cost_anomaly.media_type_invalid")
    for response in invalid_content_types:
        _assert_error(response, 415, "cost_anomaly.media_type_invalid")
    _assert_error(duplicate_idempotency, 422, "cost_anomaly.action_invalid")
    for response in malformed_cases:
        _assert_error(response, 422, "cost_anomaly.action_invalid")
    assert valid_apply_null.status_code == 200, valid_apply_null.text
    assert valid_apply_null.json()["temporary_rate_limit_override"]["rpm_limit"] == 10
    assert valid_apply_null.json()["temporary_rate_limit_override"]["tpm_limit"] is None


@pytest.mark.parametrize(
    "value",
    [None, "", "short", "幂等键必须是ASCII", "abcdefgh ", object()],
)
def test_prd053_service_rejects_invalid_direct_idempotency_key(value: object) -> None:
    """非 HTTP 调用仍把非法幂等键折叠为统一领域错误。"""

    with pytest.raises(ApiError) as captured:
        anomaly_service._normalize_idempotency_key(value)
    assert captured.value.code == "cost_anomaly.action_invalid"


def test_prd053_service_accepts_bounded_ascii_idempotency_key() -> None:
    """服务层接受与 raw wire 一致的有界 ASCII 幂等键。"""

    value = "idem-prd053-service-valid"
    assert anomaly_service._normalize_idempotency_key(value) == value