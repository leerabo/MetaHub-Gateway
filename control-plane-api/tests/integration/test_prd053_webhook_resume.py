"""PRD-053 Webhook pending delivery 显式恢复模式的聚焦集成测试。

测试使用真实 SQLite 迁移和真实控制面投递事务，只替换最终 HTTP 发送原语，验证 worker
崩溃后的持久 intent、租户内订阅快照、Secret 轮换和停用闸门不会被调用方重构事件绕过。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import (
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
    secret_prefix,
)
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

# 固定 ID 和时间让持久快照断言可重复，同时避免测试输出携带任何真实租户或密钥信息。
EVENT_ID = f"caevt_{'1' * 32}"
ANOMALY_ID = f"can_{'a' * 32}"
CREATED_AT = "2026-07-27T08:00:00Z"

# 0047 会以原始 Delivery ID 的 32 位十六进制主体派生 DeadLetter ID；历史恢复
# 夹具也必须使用生产格式，才能验证终态迁移而不被测试专用短 ID 绕开约束。
DELIVERY_ID_DEFAULT = f"whd_{'0' * 32}"
DELIVERY_ID_PERSISTED = f"whd_{'1' * 32}"
DELIVERY_ID_SOURCE_BINDING = f"whd_{'2' * 32}"
DELIVERY_ID_SUCCESS = f"whd_{'3' * 32}"
DELIVERY_ID_FAILED = f"whd_{'4' * 32}"
DELIVERY_ID_DISABLED = f"whd_{'5' * 32}"
DELIVERY_ID_ROTATED = f"whd_{'6' * 32}"
DELIVERY_ID_GUARD_FIRST = f"whd_{'7' * 32}"
DELIVERY_ID_GUARD_SECOND = f"whd_{'8' * 32}"
DELIVERY_ID_GUARD_FAILURE = f"whd_{'9' * 32}"


@pytest.fixture
def webhook_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[AppConfig, str, str]:
    """创建单测试独占数据库，并返回配置、数据库 URL 与可信租户 ID。

    参数：monkeypatch 只注入测试运行配置，tmp_path 由本次 pytest basetemp 派生。
    返回：已执行全部迁移和 seed 的运行时三元组。
    约束：所有业务行都使用 seed 返回的 tenant_id，不能用调用方自造租户绕过隔离。
    """

    database_url = f"sqlite:///{(tmp_path / 'metahub.db').as_posix()}"
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd053-test-auth-secret-at-least-32-characters")
    monkeypatch.setenv(
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "prd053-test-cache-affinity-secret-at-least-32-characters",
    )
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "prd053-test-encryption-secret")
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    # 配置与 tenant_id 都来自同一临时数据库，防止 fixture 把其他租户身份传入恢复入口。
    return load_app_config("control-plane-api"), database_url, seed.tenant_id


def _cost_anomaly_event(*, current_cost_micros: str = "100000") -> WebhookEvent:
    """构造一个合同完整的成本暴涨事件，供原始 intent 与调用方重构事件对照。

    参数：current_cost_micros 必须是可触发阈值的 canonical 微元文本。
    返回：绑定固定持久 event_id、anomaly_id 和 trace_id 的 WebhookEvent。
    约束：比率按共享合同的整数公式重算，不能用浮点数制造边界歧义。
    """

    baseline_total_micros = 1_000
    current_cost = int(current_cost_micros)
    observed_ratio_millis = current_cost * 288 * 1_000 // baseline_total_micros
    return build_webhook_event(
        tenant_id="tenant_is_rebound_by_helper",
        event_type="cost.anomaly_detected",
        event_id=EVENT_ID,
        timestamp=datetime(2026, 7, 27, 8, 5, tzinfo=UTC),
        actor_id=None,
        object_id=ANOMALY_ID,
        trace_id="trc_prd053_webhook_resume",
        payload={
            "anomaly_id": ANOMALY_ID,
            "rule_type": "virtual_key_spend_spike",
            "severity": "critical",
            "status": "open",
            "environment_id": "env_prd053",
            "scope_type": "virtual_key",
            "scope_id": "vk_prd053",
            "window_start": "2026-07-27T08:00:00Z",
            "window_end": "2026-07-27T08:05:00Z",
            "current_cost_micros": current_cost_micros,
            "baseline_total_micros": str(baseline_total_micros),
            "observed_ratio_millis": str(observed_ratio_millis),
            "baseline_bucket_count": 288,
            "threshold_multiplier_millis": 5_000,
            "minimum_cost_micros": 10_000,
            "source_entry_count": 5,
            "estimated_entry_count": 1,
            "affected_virtual_key_count": 1,
            "recommended_action": "apply_temporary_rate_limit",
            "detection_version": "cost-anomaly-rules-v1",
        },
    )


def _provider_error_map_event(tenant_id: str) -> WebhookEvent:
    """构造携带非序列化 source_request_id 绑定的 PRD-056 错误映射事件。

    参数：tenant_id 来自本测试独占数据库的可信 seed。
    返回：event_id、map_id、trace_id 与 payload request_id 全部满足共享合同的事件。
    约束：source_request_id 只参与构造校验，不会成为公开信封的新字段。
    """

    provider_error_map_id = f"pem_{'2' * 32}"
    source_request_id = "request-prd056-resume"
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type="error_map.updated",
        event_id=f"pemevt_{'3' * 32}",
        timestamp=datetime(2026, 7, 29, 8, 5, tzinfo=UTC),
        actor_id="user-prd056-resume",
        object_id=provider_error_map_id,
        trace_id="trace-prd056-resume",
        source_request_id=source_request_id,
        payload={
            "schema_version": 1,
            "operation": "update",
            "provider_error_map_id": provider_error_map_id,
            "rule_version": 2,
            "head_revision": 3,
            "provider_scope": "all",
            "status": "active",
            "priority": 500,
            "output_error_code": "gateway.upstream_quota_exceeded",
            "pattern_hash": "a" * 64,
            "request_id": source_request_id,
        },
    )


def _for_tenant(event: WebhookEvent, tenant_id: str) -> WebhookEvent:
    """把测试事件重新绑定到 seed 租户，并保持其余持久信封字段不变。

    参数：event 是已经通过共享合同校验的事件，tenant_id 是数据库 seed 的可信租户。
    返回：只替换 tenant_id 的不可变 WebhookEvent。
    约束：业务代码仍会再次核对 dispatch 参数与信封租户，测试不会绕过该边界。
    """

    return WebhookEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        timestamp=event.timestamp,
        tenant_id=tenant_id,
        payload=event.payload,
        actor_id=event.actor_id,
        object_id=event.object_id,
        trace_id=event.trace_id,
    )


def _insert_endpoint(
    database_url: str,
    tenant_id: str,
    endpoint_id: str,
    *,
    secret: str,
    max_attempts: int = 3,
    status: str = "active",
    subscribed: bool = True,
    event_type: str = "cost.anomaly_detected",
) -> None:
    """插入租户内 Webhook Endpoint，并确保签名 Secret 只以密文和摘要落库。

    参数：endpoint_id 标识测试接收端；状态、订阅开关和 event_type 控制恢复分支。
    返回：无。
    约束：secret 仅在进程内加密，不会写入明文字段或测试日志。
    """

    event_types = [event_type] if subscribed else []
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status, timeout_ms,
              max_attempts, created_at, updated_at
            )
            VALUES(?, ?, ?, 'http://127.0.0.1:65530/webhook', ?, ?, ?, ?, ?, 1000, ?, ?, ?)
            """,
            (
                endpoint_id,
                tenant_id,
                f"PRD053 {endpoint_id}",
                json.dumps(event_types, separators=(",", ":")),
                encrypt_secret(secret),
                secret_prefix(secret),
                secret_fingerprint(secret),
                status,
                max_attempts,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_delivery(
    database_url: str,
    tenant_id: str,
    endpoint_id: str,
    delivery_id: str,
    event: WebhookEvent,
    *,
    status: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 3,
    secret_version: int = 1,
) -> str:
    """插入原始 delivery 快照，并返回数据库中的 canonical payload_json。

    参数：状态、attempt_count 和 max_attempts 用于模拟 worker 崩溃时已经提交的事实。
    返回：实际持久化的稳定 JSON 文本。
    约束：event_id、event_type、event_timestamp 和信封必须完全一致，恢复时会重新验证。
    """

    payload_json = json.dumps(
        event.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO webhook_deliveries(
              webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id, event_type,
              event_timestamp, payload_json, status, attempt_count, max_attempts,
              secret_version, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                tenant_id,
                endpoint_id,
                event.event_id,
                event.event_type,
                event.timestamp,
                payload_json,
                status,
                attempt_count,
                max_attempts,
                secret_version,
                CREATED_AT,
                CREATED_AT,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return payload_json


def _rows(database_url: str, sql: str, params: tuple[object, ...] = ()) -> list[Any]:
    """用短连接读取测试数据库，避免 Windows 下句柄残留影响 basetemp 回收。

    参数：sql 仅来自本测试文件中的固定只读语句，params 提供绑定变量。
    返回：已经 materialize 的 sqlite Row 列表。
    约束：函数总会关闭连接，不把连接对象泄漏到断言阶段。
    """

    connection = connect_database(database_url)
    try:
        return list(connection.execute(sql, params).fetchall())
    finally:
        connection.close()


def _success_result() -> dict[str, object]:
    """返回不含响应正文的成功发送摘要，供聚焦测试替代真实 HTTP。"""

    return {
        "status": "success",
        "http_status": 204,
        "duration_ms": 1,
        "error_code": None,
        "error_summary": None,
    }


def _failure_result() -> dict[str, object]:
    """返回可安全落库的失败发送摘要，验证持久 max_attempts 终止条件。"""

    return {
        "status": "failed",
        "http_status": 503,
        "duration_ms": 1,
        "error_code": "webhook.http_error",
        "error_summary": "Receiver is unavailable.",
    }


def test_default_mode_reads_existing_pending_without_network(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认模式命中既有 pending 时只回读，不能隐式承担 worker 的恢复职责。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    endpoint_id = "wh_prd053_default"
    delivery_id = DELIVERY_ID_DEFAULT
    _insert_endpoint(database_url, tenant_id, endpoint_id, secret="whsec_prd053_default")
    _insert_delivery(database_url, tenant_id, endpoint_id, delivery_id, event)
    network_calls: list[str] = []

    def fail_if_sent(_config: AppConfig, _endpoint: Any, _body: bytes, seen_delivery_id: str) -> dict[str, object]:
        """记录任何意外出网；默认路径一旦调用该原语，测试必须失败。"""

        network_calls.append(seen_delivery_id)
        # 默认模式触达发送原语即代表隐式恢复，必须立即失败而不能掩盖额外网络副作用。
        raise AssertionError("default dispatch must not resume an existing pending delivery")

    monkeypatch.setattr(webhook_service, "_send_once", fail_if_sent)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd053_default",
        trace_id="trc_prd053_default",
    )

    assert network_calls == []
    assert len(deliveries) == 1
    assert deliveries[0].webhook_delivery_id == delivery_id
    assert deliveries[0].status == "pending"
    assert deliveries[0].attempt_count == 0
    row = _rows(
        database_url,
        "SELECT status, attempt_count FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (delivery_id,),
    )[0]
    assert (row["status"], row["attempt_count"]) == ("pending", 0)


def test_resume_uses_persisted_payload_limit_and_snapshot_after_unsubscribe(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式恢复从 attempt_count+1 续发，并忽略调用方新正文和后续取消订阅。"""

    config, database_url, tenant_id = webhook_runtime
    persisted_event = _for_tenant(_cost_anomaly_event(current_cost_micros="100000"), tenant_id)
    rebuilt_event = _for_tenant(_cost_anomaly_event(current_cost_micros="200000"), tenant_id)
    endpoint_id = "wh_prd053_persisted"
    delivery_id = DELIVERY_ID_PERSISTED
    _insert_endpoint(
        database_url,
        tenant_id,
        endpoint_id,
        secret="whsec_prd053_persisted",
        max_attempts=3,
    )
    persisted_json = _insert_delivery(
        database_url,
        tenant_id,
        endpoint_id,
        delivery_id,
        persisted_event,
        attempt_count=1,
        max_attempts=2,
    )
    connection = connect_database(database_url)
    try:
        # 取消订阅发生在 intent 提交之后；恢复必须服从 claim 快照，不能让事实永久 pending。
        connection.execute(
            "UPDATE webhook_endpoints SET event_types_json='[]', max_attempts=3 WHERE webhook_endpoint_id=?",
            (endpoint_id,),
        )
        connection.commit()
    finally:
        connection.close()
    sent_bodies: list[dict[str, Any]] = []

    def record_failure(_config: AppConfig, _endpoint: Any, body: bytes, seen_delivery_id: str) -> dict[str, object]:
        """记录恢复实际发送的持久正文，并让第二次尝试失败以验证快照上限。"""

        assert seen_delivery_id == delivery_id
        sent_bodies.append(json.loads(body.decode("utf-8")))
        return _failure_result()

    monkeypatch.setattr(webhook_service, "_send_once", record_failure)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        rebuilt_event,
        request_id="req_prd053_persisted",
        trace_id="trc_prd053_persisted",
        resume_existing_pending=True,
    )

    assert sent_bodies == [json.loads(persisted_json)]
    assert sent_bodies[0] != rebuilt_event.to_payload()
    assert deliveries[0].status == "failed"
    assert deliveries[0].attempt_count == 2
    assert deliveries[0].max_attempts == 2
    attempts = _rows(
        database_url,
        "SELECT attempt_no, status FROM webhook_delivery_attempts WHERE webhook_delivery_id=?",
        (delivery_id,),
    )
    assert [(row["attempt_no"], row["status"]) for row in attempts] == [(2, "failed")]


def test_resume_error_map_updated_restores_source_request_binding(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-056 pending 恢复重建构造期身份，并发送未经改写的持久信封。"""

    config, database_url, tenant_id = webhook_runtime
    event = _provider_error_map_event(tenant_id)
    endpoint_id = "wh_prd056_source_binding"
    delivery_id = DELIVERY_ID_SOURCE_BINDING
    _insert_endpoint(
        database_url,
        tenant_id,
        endpoint_id,
        secret="whsec_prd056_source_binding",
        event_type="error_map.updated",
    )
    persisted_json = _insert_delivery(
        database_url,
        tenant_id,
        endpoint_id,
        delivery_id,
        event,
    )
    sent_bodies: list[dict[str, Any]] = []

    def record_success(
        _config: AppConfig,
        _endpoint: Any,
        body: bytes,
        seen_delivery_id: str,
    ) -> dict[str, object]:
        """只替换最终 HTTP 原语，保留真实重建、签名准备和持久事务。"""

        assert seen_delivery_id == delivery_id
        sent_bodies.append(json.loads(body.decode("utf-8")))
        return _success_result()

    monkeypatch.setattr(webhook_service, "_send_once", record_success)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd056_resume_worker",
        trace_id="trc_prd056_resume_worker",
        resume_existing_pending=True,
    )

    assert sent_bodies == [json.loads(persisted_json)]
    assert "source_request_id" not in sent_bodies[0]
    assert sent_bodies[0]["payload"]["request_id"] == "request-prd056-resume"
    assert deliveries[0].status == "success"
    assert deliveries[0].attempt_count == 1
    attempts = _rows(
        database_url,
        "SELECT attempt_no, status FROM webhook_delivery_attempts WHERE webhook_delivery_id=?",
        (delivery_id,),
    )
    assert [(row["attempt_no"], row["status"]) for row in attempts] == [(1, "success")]


def test_resume_terminal_deliveries_is_network_idempotent(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """success 和 failed 终态重入都只回读同一 delivery，不新增 attempt 或网络请求。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    cases = [
        ("wh_prd053_success", DELIVERY_ID_SUCCESS, "success"),
        ("wh_prd053_failed", DELIVERY_ID_FAILED, "failed"),
    ]
    for endpoint_id, delivery_id, status in cases:
        _insert_endpoint(database_url, tenant_id, endpoint_id, secret=f"whsec_{status}")
        _insert_delivery(
            database_url,
            tenant_id,
            endpoint_id,
            delivery_id,
            event,
            status=status,
            attempt_count=1,
        )

    def fail_if_sent(_config: AppConfig, _endpoint: Any, _body: bytes, _delivery_id: str) -> dict[str, object]:
        """终态恢复不允许到达发送原语。"""

        # success/failed 已是持久终态，任何再次发送都会破坏事件幂等和失败可追踪性。
        raise AssertionError("terminal delivery must not be sent again")

    monkeypatch.setattr(webhook_service, "_send_once", fail_if_sent)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd053_terminal",
        trace_id="trc_prd053_terminal",
        resume_existing_pending=True,
    )

    assert {item.status for item in deliveries} == {"success", "failed"}
    assert {item.webhook_delivery_id for item in deliveries} == {
        DELIVERY_ID_SUCCESS,
        DELIVERY_ID_FAILED,
    }
    assert _rows(
        database_url,
        """
        SELECT attempt_no FROM webhook_delivery_attempts
        WHERE webhook_delivery_id IN (?, ?)
        """,
        (DELIVERY_ID_SUCCESS, DELIVERY_ID_FAILED),
    ) == []


def test_resume_disabled_endpoint_fails_atomically_without_network(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Endpoint 停用后 pending 原子终结为明确失败，且不伪造一次 HTTP attempt。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    endpoint_id = "wh_prd053_disabled"
    delivery_id = DELIVERY_ID_DISABLED
    _insert_endpoint(
        database_url,
        tenant_id,
        endpoint_id,
        secret="whsec_prd053_disabled",
        status="disabled",
    )
    _insert_delivery(database_url, tenant_id, endpoint_id, delivery_id, event)

    def fail_if_sent(_config: AppConfig, _endpoint: Any, _body: bytes, _delivery_id: str) -> dict[str, object]:
        """disabled 闸门必须在解密 Secret 或发送 HTTP 之前生效。"""

        # 停用是明确运维闸门，触达此处说明实现错误地绕过了零网络约束。
        raise AssertionError("disabled endpoint must not access the network")

    monkeypatch.setattr(webhook_service, "_send_once", fail_if_sent)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd053_disabled",
        trace_id="trc_prd053_disabled",
        resume_existing_pending=True,
    )

    assert len(deliveries) == 1
    assert deliveries[0].status == "failed"
    assert deliveries[0].error_code == "webhook.endpoint_disabled"
    assert deliveries[0].attempt_count == 0
    delivery_row = _rows(
        database_url,
        "SELECT status, error_code, attempt_count FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (delivery_id,),
    )[0]
    assert (
        delivery_row["status"],
        delivery_row["error_code"],
        delivery_row["attempt_count"],
    ) == ("failed", "webhook.endpoint_disabled", 0)
    endpoint_row = _rows(
        database_url,
        "SELECT status, last_delivery_status FROM webhook_endpoints WHERE webhook_endpoint_id=?",
        (endpoint_id,),
    )[0]
    assert (endpoint_row["status"], endpoint_row["last_delivery_status"]) == ("disabled", "failed")
    assert _rows(
        database_url,
        "SELECT attempt_no FROM webhook_delivery_attempts WHERE webhook_delivery_id=?",
        (delivery_id,),
    ) == []


def test_resume_rotated_secret_cas_precedes_send(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次续发前重读当前 Secret，并先 CAS delivery.secret_version 再使用新密钥。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    endpoint_id = "wh_prd053_rotated"
    delivery_id = DELIVERY_ID_ROTATED
    old_secret = "whsec_prd053_old"
    new_secret = "whsec_prd053_new"
    _insert_endpoint(database_url, tenant_id, endpoint_id, secret=old_secret)
    _insert_delivery(database_url, tenant_id, endpoint_id, delivery_id, event, secret_version=1)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE webhook_endpoints
            SET encrypted_secret=?, secret_prefix=?, secret_fingerprint=?, secret_version=2,
                secret_rotated_at=?, updated_at=?
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (
                encrypt_secret(new_secret),
                secret_prefix(new_secret),
                secret_fingerprint(new_secret),
                CREATED_AT,
                CREATED_AT,
                tenant_id,
                endpoint_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    secrets_used: list[str] = []

    def record_secret(_config: AppConfig, endpoint: Any, _body: bytes, seen_delivery_id: str) -> dict[str, object]:
        """在发送边界读取当前密文，并确认 delivery 的版本 CAS 已经提交。"""

        assert seen_delivery_id == delivery_id
        version_at_send = _rows(
            database_url,
            "SELECT secret_version FROM webhook_deliveries WHERE webhook_delivery_id=?",
            (delivery_id,),
        )[0]["secret_version"]
        assert version_at_send == 2
        secrets_used.append(decrypt_secret(str(endpoint["encrypted_secret"])))
        return _success_result()

    monkeypatch.setattr(webhook_service, "_send_once", record_secret)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd053_rotated",
        trace_id="trc_prd053_rotated",
        resume_existing_pending=True,
    )

    assert secrets_used == [new_secret]
    assert old_secret not in secrets_used
    assert deliveries[0].status == "success"
    assert deliveries[0].secret_version == 2
    row = _rows(
        database_url,
        "SELECT status, secret_version, attempt_count FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (delivery_id,),
    )[0]
    assert (row["status"], row["secret_version"], row["attempt_count"]) == ("success", 2, 1)


def test_first_resume_freezes_all_subscriber_intents_before_send(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次 claim 在任何发送前冻结全部订阅者 intent，模拟崩溃后仍可完整恢复。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    _insert_endpoint(
        database_url,
        tenant_id,
        "wh_prd053_snapshot_a",
        secret="whsec_prd053_snapshot_a",
        max_attempts=1,
    )
    _insert_endpoint(
        database_url,
        tenant_id,
        "wh_prd053_snapshot_b",
        secret="whsec_prd053_snapshot_b",
        max_attempts=3,
    )
    checkpoints: list[int] = []

    def crash_after_snapshot(
        _config: AppConfig,
        _connection: Any,
        _delivery: Any,
        *,
        request_id: str,
        trace_id: str,
        source_lease_guard: object | None,
    ) -> Any:
        """在首条 pending 真正发送前模拟 worker 崩溃，并从独立连接观察已提交快照。"""

        assert request_id == "req_prd053_snapshot"
        assert trace_id == "trc_prd053_snapshot"
        # 普通 PRD-053 source 未接入文件安全 lease，兼容路径必须显式保持空 guard。
        assert source_lease_guard is None
        rows = _rows(
            database_url,
            """
            SELECT status, attempt_count, max_attempts, payload_json
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            ORDER BY max_attempts
            """,
            (tenant_id, EVENT_ID),
        )
        checkpoints.append(len(rows))
        assert [(row["status"], row["attempt_count"], row["max_attempts"]) for row in rows] == [
            ("pending", 0, 1),
            ("pending", 0, 3),
        ]
        assert all(json.loads(row["payload_json"]) == event.to_payload() for row in rows)
        # 崩溃发生在已提交快照之后，保留 pending 事实供下一次持 lease 的 worker 续发。
        raise RuntimeError("simulated_worker_crash_after_intent_commit")

    monkeypatch.setattr(webhook_service, "_resume_pending_delivery", crash_after_snapshot)
    with pytest.raises(RuntimeError, match="simulated_worker_crash_after_intent_commit"):
        webhook_service.dispatch_webhook_event(
            config,
            tenant_id,
            event,
            request_id="req_prd053_snapshot",
            trace_id="trc_prd053_snapshot",
            resume_existing_pending=True,
        )

    assert checkpoints == [2]
    rows = _rows(
        database_url,
        "SELECT webhook_endpoint_id, status, attempt_count FROM webhook_deliveries WHERE event_id=?",
        (EVENT_ID,),
    )
    assert {row["webhook_endpoint_id"] for row in rows} == {
        "wh_prd053_snapshot_a",
        "wh_prd053_snapshot_b",
    }
    assert all((row["status"], row["attempt_count"]) == ("pending", 0) for row in rows)


def test_source_lease_guard_precedes_every_real_network_attempt(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """source 恢复的每次真实重试前都必须先续租，不能只在 Endpoint 开始时检查一次。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    endpoint_id = "wh_prd073_guard_attempts"
    _insert_endpoint(
        database_url,
        tenant_id,
        endpoint_id,
        secret="whsec_prd073_guard_attempts",
        max_attempts=3,
    )
    _insert_delivery(
        database_url,
        tenant_id,
        endpoint_id,
        DELIVERY_ID_GUARD_FIRST,
        event,
        max_attempts=3,
    )
    call_order: list[str] = []

    def preserve_source_lease() -> None:
        """记录 source CAS；真实 worker 在此处执行当前 lease 值的精确续租。"""

        call_order.append("guard")

    def record_failed_attempt(
        _config: AppConfig,
        _endpoint: Any,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """模拟三次可重试失败，证明每个 HTTP attempt 都有独立 guard。"""

        assert delivery_id == DELIVERY_ID_GUARD_FIRST
        call_order.append("send")
        return _failure_result()

    monkeypatch.setattr(webhook_service, "_send_once", record_failed_attempt)
    deliveries = webhook_service.dispatch_webhook_event(
        config,
        tenant_id,
        event,
        request_id="req_prd073_guard_attempts",
        trace_id="trc_prd073_guard_attempts",
        resume_existing_pending=True,
        source_lease_guard=preserve_source_lease,
        create_snapshot_if_missing=False,
    )

    assert call_order == ["guard", "send", "guard", "send", "guard", "send"]
    assert len(deliveries) == 1
    assert (deliveries[0].status, deliveries[0].attempt_count) == ("failed", 3)


def test_source_lease_guard_failure_blocks_network_and_preserves_pending(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续租或停服检查失败时必须零网络，并保留原 delivery 供有效 owner 后续恢复。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    endpoint_id = "wh_prd073_guard_failure"
    _insert_endpoint(
        database_url,
        tenant_id,
        endpoint_id,
        secret="whsec_prd073_guard_failure",
    )
    _insert_delivery(
        database_url,
        tenant_id,
        endpoint_id,
        DELIVERY_ID_GUARD_FAILURE,
        event,
    )
    network_calls: list[str] = []

    def reject_stale_source() -> None:
        """模拟完整 CAS 未命中；异常只带固定领域码，不泄漏 owner 或数据库字段。"""

        # 测试桩只模拟固定领域拒绝，不能把租户、owner 或 lease 原值带入异常正文。
        raise ApiError("file.security_scan_unavailable")

    def record_unexpected_network(
        _config: AppConfig,
        _endpoint: Any,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """任何调用都表示失权 worker 已越过 guard，必须让测试失败。"""

        network_calls.append(delivery_id)
        return _success_result()

    monkeypatch.setattr(webhook_service, "_send_once", record_unexpected_network)
    with pytest.raises(ApiError) as rejected:
        webhook_service.dispatch_webhook_event(
            config,
            tenant_id,
            event,
            request_id="req_prd073_guard_failure",
            trace_id="trc_prd073_guard_failure",
            resume_existing_pending=True,
            source_lease_guard=reject_stale_source,
            create_snapshot_if_missing=False,
        )

    assert rejected.value.code == "file.security_scan_unavailable"
    assert network_calls == []
    row = _rows(
        database_url,
        "SELECT status,attempt_count FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (DELIVERY_ID_GUARD_FAILURE,),
    )[0]
    assert (row["status"], row["attempt_count"]) == ("pending", 0)


def test_cooperative_stop_after_blocking_attempt_skips_next_endpoint(
    webhook_runtime: tuple[AppConfig, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停服发生在同步 attempt 内时允许当前落库，但下一 Endpoint 必须在出网前停止。"""

    config, database_url, tenant_id = webhook_runtime
    event = _for_tenant(_cost_anomaly_event(), tenant_id)
    first_endpoint = "wh_prd073_stop_first"
    second_endpoint = "wh_prd073_stop_second"
    _insert_endpoint(
        database_url,
        tenant_id,
        first_endpoint,
        secret="whsec_prd073_stop_first",
        max_attempts=1,
    )
    _insert_endpoint(
        database_url,
        tenant_id,
        second_endpoint,
        secret="whsec_prd073_stop_second",
        max_attempts=1,
    )
    _insert_delivery(
        database_url,
        tenant_id,
        first_endpoint,
        DELIVERY_ID_GUARD_FIRST,
        event,
        max_attempts=1,
    )
    _insert_delivery(
        database_url,
        tenant_id,
        second_endpoint,
        DELIVERY_ID_GUARD_SECOND,
        event,
        max_attempts=1,
    )
    stopping = False
    network_calls: list[str] = []

    def stop_guard() -> None:
        """模拟 scheduler 的 threading.Event：首次发送中置位后，下一次 guard 失败关闭。"""

        if stopping:
            # stop 置位后下一 Endpoint 必须在网络原语之前收到固定领域拒绝。
            raise ApiError("file.security_scan_unavailable")

    def finish_current_attempt(
        _config: AppConfig,
        _endpoint: Any,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """同步 attempt 无法强杀；返回后置位的 stop 会阻止下一个 Endpoint。"""

        nonlocal stopping
        network_calls.append(delivery_id)
        stopping = True
        return _success_result()

    monkeypatch.setattr(webhook_service, "_send_once", finish_current_attempt)
    with pytest.raises(ApiError) as stopped:
        webhook_service.dispatch_webhook_event(
            config,
            tenant_id,
            event,
            request_id="req_prd073_cooperative_stop",
            trace_id="trc_prd073_cooperative_stop",
            resume_existing_pending=True,
            source_lease_guard=stop_guard,
            create_snapshot_if_missing=False,
        )

    assert stopped.value.code == "file.security_scan_unavailable"
    assert network_calls == [DELIVERY_ID_GUARD_FIRST]
    rows = _rows(
        database_url,
        """
        SELECT webhook_delivery_id,status,attempt_count FROM webhook_deliveries
        WHERE event_id=? ORDER BY webhook_delivery_id
        """,
        (EVENT_ID,),
    )
    assert [tuple(row) for row in rows] == [
        (DELIVERY_ID_GUARD_FIRST, "success", 1),
        (DELIVERY_ID_GUARD_SECOND, "pending", 0),
    ]
