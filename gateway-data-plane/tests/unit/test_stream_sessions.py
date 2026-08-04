"""PRD-037 StreamSession 审计闸门、终态幂等和安全事件测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metahub_gateway.routing.models import resolve_model_route
from metahub_gateway.streaming import sessions as stream_sessions
from metahub_gateway.streaming.sessions import (
    build_stream_timeout_event,
    create_stream_session,
    mark_stream_session_active,
    safe_finish_stream_session,
)
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError


def _sqlite_url(path: Path) -> str:
    """把临时数据库文件转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _session_context(config, tenant_id: str, request_id: str) -> RequestLogContext:
    """从真实种子路由构造完整、安全且同租户的会话上下文。"""

    route = resolve_model_route(
        config,
        tenant_id=tenant_id,
        model_alias="default-chat",
        request_id=request_id,
        trace_id=f"trc_{request_id}",
    )
    return RequestLogContext(
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=f"trc_{request_id}",
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        model_id=route.model_id,
        request_model="default-chat",
        resolved_model=route.model_alias,
    )


def test_stream_session_lifecycle_persists_counts_and_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：starting、active、completed 状态和内存分片计数按一次终态写入持久化。"""

    database_url = _sqlite_url(tmp_path / "stream-session.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    context = _session_context(config, seed.tenant_id, "req_stream_lifecycle")

    session = create_stream_session(config, context, inter_token_timeout_ms=30_000)
    mark_stream_session_active(config, session)
    session.note_chunk(b"data: first\n\n")
    session.note_chunk(b"data: [DONE]\n\n")
    assert safe_finish_stream_session(
        config,
        session,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
    ) is True
    # 完全相同的 finally 重入只能确认同一事实，不能重复改写或报错。
    assert safe_finish_stream_session(
        config,
        session,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
    ) is True

    row = connect_database(database_url).execute(
        "SELECT * FROM stream_sessions WHERE stream_session_id=?",
        (session.stream_session_id,),
    ).fetchone()
    assert row["tenant_id"] == seed.tenant_id
    assert row["status"] == "completed"
    assert row["chunk_count"] == 2
    assert row["bytes_sent"] == len(b"data: first\n\n") + len(b"data: [DONE]\n\n")
    assert row["first_chunk_at"] is not None
    assert row["last_chunk_at"] is not None
    assert row["completed_at"] is not None


def test_duplicate_request_is_fail_closed_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：同租户重复 request_id 无法创建第二个会话，统一返回审计不可用错误。"""

    database_url = _sqlite_url(tmp_path / "stream-duplicate.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    context = _session_context(config, seed.tenant_id, "req_stream_duplicate")
    create_stream_session(config, context, inter_token_timeout_ms=30_000)

    with pytest.raises(ApiError) as duplicate:
        create_stream_session(config, context, inter_token_timeout_ms=30_000)

    assert duplicate.value.code == "gateway.stream_session_unavailable"
    count = connect_database(database_url).execute(
        "SELECT COUNT(*) AS count FROM stream_sessions WHERE tenant_id=? AND request_id=?",
        (seed.tenant_id, context.request_id),
    ).fetchone()
    assert count["count"] == 1


def test_timeout_event_contains_only_safe_trace_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全流程：timeout 事件引用已提交会话，且 payload 不包含 URL、Header、Key 或正文。"""

    database_url = _sqlite_url(tmp_path / "stream-event.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    context = _session_context(config, seed.tenant_id, "req_stream_timeout_event")
    session = create_stream_session(config, context, inter_token_timeout_ms=12_000)
    mark_stream_session_active(config, session)
    session.note_chunk(b"private-response-not-retained")
    assert safe_finish_stream_session(
        config,
        session,
        status="timed_out",
        error_code="gateway.upstream_stream_timeout",
        quality_observation_eligible=True,
    ) is True

    event = build_stream_timeout_event(session)
    serialized = json.dumps(event.to_payload(), ensure_ascii=False)

    assert event.event_type == "stream.timeout"
    assert event.object_id == session.stream_session_id
    assert event.payload["request_id"] == context.request_id
    assert event.payload["chunk_count"] == 1
    assert set(event.payload) == {
        "stream_session_id",
        "request_id",
        "model_id",
        "provider_id",
        "provider_code",
        "inter_token_timeout_ms",
        "chunk_count",
        "bytes_sent",
    }
    assert "private-response-not-retained" not in serialized
    assert "Authorization" not in serialized
    assert "provider.invalid" not in serialized


def test_stream_quality_observation_counts_upstream_terminals_and_rolls_back_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """流成功/超时各计一次，客户端断开排除，Score 失败时终态同步回滚。

    参数：真实迁移库与四个同租户会话。返回值：无。关键约束：幂等 finally 不得重复
    计分；``client_disconnected`` 不是模型质量；Score 一致性失败时 session 必须仍为 active，
    防止出现终态已提交但统计样本丢失的不可复现事实。
    """

    database_url = _sqlite_url(tmp_path / "stream-quality.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")

    completed_context = _session_context(config, seed.tenant_id, "req_prd048_stream_completed")
    completed = create_stream_session(config, completed_context, inter_token_timeout_ms=30_000)
    mark_stream_session_active(config, completed)
    assert safe_finish_stream_session(
        config,
        completed,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
    ) is True
    assert safe_finish_stream_session(
        config,
        completed,
        status="completed",
        error_code=None,
        quality_observation_eligible=True,
    ) is True

    timeout_context = _session_context(config, seed.tenant_id, "req_prd048_stream_timeout")
    timed_out = create_stream_session(config, timeout_context, inter_token_timeout_ms=30_000)
    mark_stream_session_active(config, timed_out)
    assert safe_finish_stream_session(
        config,
        timed_out,
        status="timed_out",
        error_code="gateway.upstream_stream_timeout",
        quality_observation_eligible=True,
    ) is True

    disconnected_context = _session_context(
        config,
        seed.tenant_id,
        "req_prd048_stream_disconnected",
    )
    disconnected = create_stream_session(
        config,
        disconnected_context,
        inter_token_timeout_ms=30_000,
    )
    mark_stream_session_active(config, disconnected)
    assert safe_finish_stream_session(
        config,
        disconnected,
        status="client_disconnected",
        error_code="gateway.client_disconnected",
        quality_observation_eligible=False,
    ) is True

    atomic_context = _session_context(config, seed.tenant_id, "req_prd048_stream_atomic")
    atomic = create_stream_session(config, atomic_context, inter_token_timeout_ms=30_000)
    mark_stream_session_active(config, atomic)

    def fail_quality_observation(*_args, **_kwargs) -> None:
        """模拟质量增量失败，要求 safe finish 返回 False 且回滚 session 终态。"""

        # 私有数据库异常用于证明 session 终态和质量增量同事务失败且日志保持脱敏。
        raise stream_sessions.sqlite3.IntegrityError("private score failure")

    monkeypatch.setattr(
        stream_sessions,
        "record_model_quality_observation",
        fail_quality_observation,
    )
    assert safe_finish_stream_session(
        config,
        atomic,
        status="upstream_failed",
        error_code="gateway.upstream_stream_failed",
        quality_observation_eligible=True,
    ) is False

    connection = connect_database(database_url)
    score = connection.execute(
        """
        SELECT sample_count, success_count, error_count, latency_sample_count,
               statistics_version
        FROM model_quality_scores WHERE tenant_id=? AND model_id=?
        """,
        (seed.tenant_id, completed_context.model_id),
    ).fetchone()
    assert tuple(score) == (2, 1, 1, 2, 2)
    states = connection.execute(
        """
        SELECT request_id, status FROM stream_sessions
        WHERE request_id LIKE 'req_prd048_stream_%' ORDER BY request_id
        """
    ).fetchall()
    assert [tuple(row) for row in states] == [
        ("req_prd048_stream_atomic", "active"),
        ("req_prd048_stream_completed", "completed"),
        ("req_prd048_stream_disconnected", "client_disconnected"),
        ("req_prd048_stream_timeout", "timed_out"),
    ]


def test_stream_quality_latency_uses_exact_floor_milliseconds() -> None:
    """实时质量延迟使用整数微秒向下截断，并与 0037 历史重建保持一致。

    参数：同秒 1.6ms、跨秒 1.6ms 和负时钟差。返回值：无。关键约束：不能经 float
    四舍五入为 2ms，负差值仍按既有安全边界收敛为零。
    """

    same_second_start = datetime(2026, 7, 19, 10, 0, 0, 100, tzinfo=UTC)
    same_second_end = datetime(2026, 7, 19, 10, 0, 0, 1_700, tzinfo=UTC)
    cross_second_start = datetime(2026, 7, 19, 10, 0, 0, 999_100, tzinfo=UTC)
    cross_second_end = datetime(2026, 7, 19, 10, 0, 1, 700, tzinfo=UTC)

    assert stream_sessions._elapsed_milliseconds(same_second_start, same_second_end) == 1
    assert stream_sessions._elapsed_milliseconds(cross_second_start, cross_second_end) == 1
    assert stream_sessions._elapsed_milliseconds(same_second_end, same_second_start) == 0


def test_stream_quality_contract_rejects_inconsistent_status_error_and_eligibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """终态、内部错误码和显式质量资格不一致时在写库前失败关闭。

    参数：一个尚为 active 的真实会话。返回值：无。关键约束：completed=false、客户端
    断开=true、上游归因码=false 都不得留下终态或质量样本。
    """

    database_url = _sqlite_url(tmp_path / "stream-quality-contract.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    context = _session_context(config, seed.tenant_id, "req_prd048_stream_contract")
    session = create_stream_session(config, context, inter_token_timeout_ms=30_000)
    mark_stream_session_active(config, session)

    invalid_contracts = (
        ("completed", None, False),
        ("client_disconnected", "gateway.client_disconnected", True),
        ("upstream_failed", "gateway.upstream_stream_failed", False),
        ("upstream_failed", "gateway.upstream_timeout", False),
        ("timed_out", "gateway.upstream_unavailable", True),
    )
    for status, error_code, eligible in invalid_contracts:
        with pytest.raises(ValueError, match="stream_session_quality_contract_invalid"):
            safe_finish_stream_session(
                config,
                session,
                status=status,
                error_code=error_code,
                quality_observation_eligible=eligible,
            )

    row = connect_database(database_url).execute(
        "SELECT status, completed_at FROM stream_sessions WHERE stream_session_id=?",
        (session.stream_session_id,),
    ).fetchone()
    assert tuple(row) == ("active", None)


def test_output_safety_stream_terminals_require_active_and_do_not_score_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """输出安全终态缺少原子 scan 证据时必须在任何状态下拒绝。

    参数：两个真实租户会话。返回值：无。关键约束：前端或旧调用方只传会话终态时，
    starting/active 都不能留下没有 OutputSafetyScan/source event 的孤立安全状态。
    """

    database_url = _sqlite_url(tmp_path / "stream-output-safety.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")

    blocked = create_stream_session(
        config,
        _session_context(config, seed.tenant_id, "req_prd071_stream_blocked"),
        inter_token_timeout_ms=30_000,
    )
    # 即使 Provider 尚未握手，缺少 scan 的安全终态也属于调用合同错误而不是普通状态竞争。
    with pytest.raises(ValueError, match="stream_session_output_safety_contract_invalid"):
        safe_finish_stream_session(
            config,
            blocked,
            status="output_blocked",
            error_code="content_policy_violation",
            quality_observation_eligible=False,
        )
    mark_stream_session_active(config, blocked)
    blocked.note_chunk(b"data: safe-prefix\n\n")
    with pytest.raises(ValueError, match="stream_session_output_safety_contract_invalid"):
        safe_finish_stream_session(
            config,
            blocked,
            status="output_blocked",
            error_code="content_policy_violation",
            quality_observation_eligible=False,
        )

    unavailable = create_stream_session(
        config,
        _session_context(config, seed.tenant_id, "req_prd071_stream_unavailable"),
        inter_token_timeout_ms=30_000,
    )
    mark_stream_session_active(config, unavailable)
    with pytest.raises(ValueError, match="stream_session_output_safety_contract_invalid"):
        safe_finish_stream_session(
            config,
            unavailable,
            status="output_safety_unavailable",
            error_code="gateway.output_safety_unavailable",
            quality_observation_eligible=False,
        )

    rows = connect_database(database_url).execute(
        """
        SELECT request_id, status, error_code FROM stream_sessions
        WHERE tenant_id=? AND request_id LIKE 'req_prd071_stream_%'
        ORDER BY request_id
        """,
        (seed.tenant_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "req_prd071_stream_blocked",
            "active",
            None,
        ),
        (
            "req_prd071_stream_unavailable",
            "active",
            None,
        ),
    ]
