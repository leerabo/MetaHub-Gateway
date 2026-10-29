"""PRD-036 路由尝试执行门、唯一性和最大次数单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_gateway.providers.base import NormalizedProviderError
from metahub_gateway.routing import attempts as routing_attempts
from metahub_gateway.routing.attempts import (
    create_routing_attempt,
    safe_complete_routing_attempt,
)
from metahub_gateway.routing.models import GatewayRoutePlan, resolve_model_route
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError


def _sqlite_url(path: Path) -> str:
    """把临时文件转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def test_attempt_sequence_unique_key_blocks_duplicate_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同一 request_id 的第二次尝试只能创建一次，第三次尝试在网络调用前被拒绝。"""

    database_url = _sqlite_url(tmp_path / "attempts.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    route = resolve_model_route(
        config,
        tenant_id=seed.tenant_id,
        model_alias="default-chat",
        request_id="req_prd036_gate",
        trace_id="trc_prd036_gate",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary", fallback_route=route)
    created = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd036_gate",
        trace_id="trc_prd036_gate",
        attempt_sequence=2,
        attempt_role="fallback",
        trigger_error_code="gateway.upstream_error",
    )

    with pytest.raises(ApiError) as duplicate:
        create_routing_attempt(
            config,
            plan,
            route,
            request_id="req_prd036_gate",
            trace_id="trc_prd036_gate",
            attempt_sequence=2,
            attempt_role="fallback",
            trigger_error_code="gateway.upstream_error",
        )
    with pytest.raises(ApiError) as third:
        create_routing_attempt(
            config,
            plan,
            route,
            request_id="req_prd036_gate",
            trace_id="trc_prd036_gate",
            attempt_sequence=3,
            attempt_role="fallback",
        )

    assert created.attempt_sequence == 2
    assert duplicate.value.code == "gateway.fallback_unavailable"
    assert third.value.code == "gateway.routing_attempt_unavailable"
    connection = connect_database(database_url)
    count = connection.execute(
        "SELECT COUNT(*) AS count FROM routing_attempts WHERE request_id='req_prd036_gate'"
    ).fetchone()
    assert count["count"] == 1


def test_attempt_quality_observation_is_atomic_exactly_once_and_excludes_local_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实终态与 Score 单事务提交，重复完成和本地失败都不得重复或错误计分。

    参数：真实迁移库和默认模型路由。返回值：无。关键约束：第一次上游成功增加一个样本；
    同一 attempt 重复完成不增加；显式 ``quality_observation_eligible=False`` 的本地失败只写
    attempt 终态；模拟 Score 写失败时 attempt 更新也必须回滚为 started。
    """

    database_url = _sqlite_url(tmp_path / "attempt-quality.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    route = resolve_model_route(
        config,
        tenant_id=seed.tenant_id,
        model_alias="default-chat",
        request_id="req_prd048_attempt_success",
        trace_id="trc_prd048_attempt_success",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    succeeded_attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd048_attempt_success",
        trace_id="trc_prd048_attempt_success",
        attempt_sequence=1,
        attempt_role="primary",
    )
    safe_complete_routing_attempt(
        config,
        succeeded_attempt,
        succeeded=True,
        latency_ms=123,
        quality_observation_eligible=True,
    )
    # 重复清理命不中 started 条件，函数只告警且不能再次推进 Score。
    safe_complete_routing_attempt(
        config,
        succeeded_attempt,
        succeeded=True,
        latency_ms=999,
        quality_observation_eligible=True,
    )
    local_attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd048_attempt_local",
        trace_id="trc_prd048_attempt_local",
        attempt_sequence=1,
        attempt_role="primary",
    )
    safe_complete_routing_attempt(
        config,
        local_attempt,
        succeeded=False,
        latency_ms=7,
        error=NormalizedProviderError(
            code="provider_key.unavailable",
            message="",
            http_status=None,
            retryable=False,
            provider_error_type="gateway",
            error_category="other",
        ),
        usage_status="not_applicable",
        quality_observation_eligible=False,
    )
    atomic_attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd048_attempt_atomic",
        trace_id="trc_prd048_attempt_atomic",
        attempt_sequence=1,
        attempt_role="primary",
    )

    def fail_quality_observation(*_args, **_kwargs) -> None:
        """模拟 Score 一致性失败，验证调用方会回滚同事务 attempt 终态。"""

        # 私有数据库异常用于验证事务整体回滚，同时不得穿透安全 warning 或统一响应。
        raise routing_attempts.sqlite3.IntegrityError("private score failure")

    monkeypatch.setattr(
        routing_attempts,
        "record_model_quality_observation",
        fail_quality_observation,
    )
    safe_complete_routing_attempt(
        config,
        atomic_attempt,
        succeeded=False,
        latency_ms=33,
        error=NormalizedProviderError(
            code="gateway.upstream_error",
            message="",
            http_status=503,
            retryable=True,
            provider_error_type="http_status",
            error_category="upstream_5xx",
        ),
        usage_status="unavailable",
        quality_observation_eligible=True,
    )

    connection = connect_database(database_url)
    score = connection.execute(
        """
        SELECT sample_count, success_count, error_count, latency_sample_count,
               total_latency_ms, statistics_version
        FROM model_quality_scores WHERE tenant_id=? AND model_id=?
        """,
        (seed.tenant_id, route.model_id),
    ).fetchone()
    assert tuple(score) == (1, 1, 0, 1, 123, 1)
    states = connection.execute(
        """
        SELECT request_id, status, usage_status FROM routing_attempts
        WHERE request_id LIKE 'req_prd048_attempt_%' ORDER BY request_id
        """
    ).fetchall()
    assert [tuple(row) for row in states] == [
        ("req_prd048_attempt_atomic", "started", "pending"),
        ("req_prd048_attempt_local", "failed", "not_applicable"),
        ("req_prd048_attempt_success", "succeeded", "pending"),
    ]


def test_attempt_terminal_and_quality_observation_share_one_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非流式终态和质量时间线复用同一 UTC 时刻，避免 down/up 后跨秒漂移。

    参数：会在第二次调用时跨秒的时钟替身。返回值：无。关键约束：完成路径只读取一次
    时钟，attempt.completed_at、Score first/last/updated_at 必须逐字节相同。
    """

    database_url = _sqlite_url(tmp_path / "attempt-quality-time.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    route = resolve_model_route(
        config,
        tenant_id=seed.tenant_id,
        model_alias="default-chat",
        request_id="req_prd048_attempt_time",
        trace_id="trc_prd048_attempt_time",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd048_attempt_time",
        trace_id="trc_prd048_attempt_time",
        attempt_sequence=1,
        attempt_role="primary",
    )
    connection = connect_database(database_url)
    score_before = connection.execute(
        "SELECT updated_at FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, route.model_id),
    ).fetchone()
    connection.close()
    baseline = datetime.fromisoformat(str(score_before["updated_at"]).replace("Z", "+00:00"))
    if baseline.tzinfo is None:
        # Seed 使用 SQLite UTC 文本；恢复时区后把测试终态放到基线下一秒。
        baseline = baseline.replace(tzinfo=UTC)
    first = baseline.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)
    first_text = first.isoformat().replace("+00:00", "Z")
    calls = 0

    def advancing_now() -> datetime:
        """第一次返回终态秒，意外的第二次读取会跨到下一秒并暴露测试失败。"""

        nonlocal calls
        calls += 1
        return first + timedelta(seconds=calls - 1)

    monkeypatch.setattr(routing_attempts, "_utcnow", advancing_now)
    safe_complete_routing_attempt(
        config,
        attempt,
        succeeded=True,
        latency_ms=5,
        quality_observation_eligible=True,
    )

    connection = connect_database(database_url)
    terminal = connection.execute(
        "SELECT completed_at FROM routing_attempts WHERE routing_attempt_id=?",
        (attempt.routing_attempt_id,),
    ).fetchone()
    score = connection.execute(
        "SELECT first_observed_at, last_observed_at, updated_at "
        "FROM model_quality_scores WHERE tenant_id=? AND model_id=?",
        (seed.tenant_id, route.model_id),
    ).fetchone()
    assert calls == 1
    assert terminal["completed_at"] == first_text
    assert tuple(score) == (terminal["completed_at"],) * 3


def test_attempt_quality_contract_rejects_false_or_ambiguous_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功、错误白名单、usage_status 和显式资格必须组成唯一可重建矩阵。

    参数：一个尚未完成的 attempt 及四组不一致调用。返回值：无。关键约束：本地同名
    upstream_unavailable 不能碰撞为 transport，任一拒绝都不得改变 started 源事实。
    """

    database_url = _sqlite_url(tmp_path / "attempt-quality-contract.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    route = resolve_model_route(
        config,
        tenant_id=seed.tenant_id,
        model_alias="default-chat",
        request_id="req_prd048_attempt_contract",
        trace_id="trc_prd048_attempt_contract",
    )
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd048_attempt_contract",
        trace_id="trc_prd048_attempt_contract",
        attempt_sequence=1,
        attempt_role="primary",
    )
    local_collision = NormalizedProviderError(
        code="gateway.upstream_unavailable",
        message="",
        http_status=None,
        retryable=False,
        provider_error_type="gateway",
        error_category="other",
    )
    upstream_5xx = NormalizedProviderError(
        code="gateway.upstream_error",
        message="",
        http_status=503,
        retryable=True,
        provider_error_type="http_status",
        error_category="upstream_5xx",
    )
    invalid_calls = (
        dict(succeeded=True, error=None, usage_status="pending", eligible=False),
        dict(succeeded=False, error=upstream_5xx, usage_status="unavailable", eligible=False),
        dict(succeeded=False, error=local_collision, usage_status="unavailable", eligible=True),
        dict(succeeded=False, error=None, usage_status="not_applicable", eligible=False),
    )
    for item in invalid_calls:
        with pytest.raises(ValueError, match="routing_attempt_quality_contract_invalid"):
            safe_complete_routing_attempt(
                config,
                attempt,
                succeeded=item["succeeded"],
                latency_ms=1,
                error=item["error"],
                usage_status=item["usage_status"],
                quality_observation_eligible=item["eligible"],
            )

    row = connect_database(database_url).execute(
        "SELECT status, completed_at FROM routing_attempts WHERE routing_attempt_id=?",
        (attempt.routing_attempt_id,),
    ).fetchone()
    assert tuple(row) == ("started", None)
