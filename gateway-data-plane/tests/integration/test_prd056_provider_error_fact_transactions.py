"""PRD-056 RoutingAttempt/StreamSession 与 ProviderErrorFact 单事务专项测试。

测试使用完整 0046 SQLite、真实 Gateway service 和脱敏 matcher evidence。原始 Provider
正文仅在内存中短暂参与抽取，任何数据库断言、日志和失败信息都不输出该正文。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metahub_gateway.providers.base import NormalizedProviderError
from metahub_gateway.routing.attempts import (
    create_routing_attempt,
    safe_complete_routing_attempt,
)
from metahub_gateway.routing.models import GatewayRoutePlan, resolve_model_route
from metahub_gateway.streaming.sessions import (
    create_stream_session,
    safe_finish_stream_session,
)
import metahub_gateway.streaming.sessions as stream_sessions_module
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.provider_error_map import (
    ProviderErrorEvidence,
    builtin_provider_error_map_lease,
    extract_provider_error_facts,
    normalize_provider_error,
)

# 私密哨兵只用于证明原始 Provider 正文不会进入数据库、repr 或失败输出，测试结束后不持久化。
PRIVATE_SENTINEL = "prd056-private-provider-body-sentinel"
"""只在测试进程内存出现的原始上游正文哨兵，禁止进入数据库或 repr。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占文件转换为项目数据库模块支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _normalized_error(
    tenant_id: str,
    provider_id: str,
    *,
    evidence_override: ProviderErrorEvidence | None = None,
) -> NormalizedProviderError:
    """构造真实 builtin-429 归一错误，可选替换 evidence 注入触发器失败。"""

    raw_body = (
        '{"error":{"type":"rate_limit_error","message":"' + PRIVATE_SENTINEL + '"}}'
    ).encode("utf-8")
    extracted = extract_provider_error_facts(
        upstream_http_status=429,
        transport_kind="none",
        raw_body=raw_body,
        body_truncated=False,
        known_body_length=len(raw_body),
    )
    matched = normalize_provider_error(
        extracted,
        lease=builtin_provider_error_map_lease(tenant_id),
        provider_id=provider_id,
    )
    return NormalizedProviderError(
        code=matched.code,
        message=matched.message,
        http_status=429,
        retryable=matched.retryable,
        provider_error_type="rate_limit_error",
        error_category=matched.error_category,
        transport_kind=matched.transport_kind,
        evidence=evidence_override or matched.evidence,
    )


def _invalid_error(tenant_id: str, provider_id: str) -> NormalizedProviderError:
    """把 revision-0 empty overlay hash 改成错误值。

    该坏值用于验证 0046 Fact trigger 拒绝写入，并让 owner 与 Fact 在同一事务回滚。
    """

    valid = _normalized_error(tenant_id, provider_id)
    assert valid.evidence is not None
    invalid_evidence = replace(valid.evidence, overlay_content_hash="f" * 64)
    return replace(valid, evidence=invalid_evidence)


def _auth_error(tenant_id: str, provider_id: str) -> NormalizedProviderError:
    body = b'{"error":{"type":"authentication_error","message":"invalid key"}}'
    matched = normalize_provider_error(
        extract_provider_error_facts(
            upstream_http_status=401,
            transport_kind="none",
            raw_body=body,
            body_truncated=False,
            known_body_length=len(body),
        ),
        lease=builtin_provider_error_map_lease(tenant_id),
        provider_id=provider_id,
    )
    assert matched.code == "gateway.upstream_auth_failed"
    return NormalizedProviderError(
        code=matched.code,
        message=matched.message,
        http_status=401,
        retryable=False,
        provider_error_type="authentication_error",
        error_category=matched.error_category,
        transport_kind=matched.transport_kind,
        evidence=matched.evidence,
    )


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
):
    """应用完整迁移与 seed，返回真实 config、tenant、route 和最小 plan。"""

    database_url = _sqlite_url(tmp_path / name)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    config = load_app_config("gateway-data-plane")
    route = resolve_model_route(
        config,
        tenant_id=seed.tenant_id,
        model_alias="default-chat",
        request_id=f"req_prd056_{name}",
        trace_id=f"trc_prd056_{name}",
    )
    return (
        database_url,
        config,
        seed.tenant_id,
        route,
        GatewayRoutePlan(initial_route=route, initial_role="primary"),
    )


def _stream_context(tenant_id: str, route, suffix: str) -> RequestLogContext:
    """从同租户可信路由构造不含正文和凭据的 StreamSession owner 上下文。"""

    return RequestLogContext(
        tenant_id=tenant_id,
        request_id=f"req_prd056_stream_{suffix}",
        trace_id=f"trc_prd056_stream_{suffix}",
        provider_id=route.provider_id,
        provider_code=route.provider_code,
        model_id=route.model_id,
        request_model="default-chat",
        resolved_model=route.model_alias,
    )


def _mutated_owner_value(value: object) -> object:
    """为 owner 单列生成必然不同的测试值，触发 0046 反向冻结门禁。"""

    if value is None:
        return "prd056_mutated"
    if isinstance(value, bytes):
        return value + b"x"
    if isinstance(value, (int, float)):
        return value + 1
    return f"{value}_prd056_mutated"


def _assert_owner_fully_frozen(
    connection: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    tenant_id: str,
    owner_id: str,
    trigger_error: str,
) -> None:
    """逐列验证 Fact 存在后 owner 的 UPDATE、DELETE 与 REPLACE 全部失败关闭。

    表名只接受本测试冻结的两个 owner；列名来自正式 SQLite schema 并再次转义。该动态
    遍历能在迁移新增 owner 列却漏改触发器时立即失败，避免只抽测 status 掩盖证据漂移。
    """

    allowed = {
        "routing_attempts": "routing_attempt_id",
        "stream_sessions": "stream_session_id",
    }
    assert allowed.get(table) == id_column
    quoted_table = '"' + table.replace('"', '""') + '"'
    quoted_id = '"' + id_column.replace('"', '""') + '"'
    original = connection.execute(
        f"SELECT rowid,* FROM {quoted_table} WHERE tenant_id=? AND {quoted_id}=?",
        (tenant_id, owner_id),
    ).fetchone()
    assert original is not None
    columns = [
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    ]
    assert columns

    for column in columns:
        quoted_column = '"' + column.replace('"', '""') + '"'
        with pytest.raises(sqlite3.IntegrityError, match=trigger_error):
            connection.execute(
                f"UPDATE {quoted_table} SET {quoted_column}=? WHERE tenant_id=? AND {quoted_id}=?",
                (_mutated_owner_value(original[column]), tenant_id, owner_id),
            )
    with pytest.raises(sqlite3.IntegrityError, match=trigger_error):
        connection.execute(
            f"UPDATE {quoted_table} SET rowid=rowid+1000000 WHERE tenant_id=? AND {quoted_id}=?",
            (tenant_id, owner_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match=trigger_error):
        connection.execute(
            f"DELETE FROM {quoted_table} WHERE tenant_id=? AND {quoted_id}=?",
            (tenant_id, owner_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match=trigger_error):
        connection.execute(
            f"INSERT OR REPLACE INTO {quoted_table} "
            f"SELECT * FROM {quoted_table} WHERE tenant_id=? AND {quoted_id}=?",
            (tenant_id, owner_id),
        )
    current = connection.execute(
        f"SELECT rowid,* FROM {quoted_table} WHERE tenant_id=? AND {quoted_id}=?",
        (tenant_id, owner_id),
    ).fetchone()
    assert current is not None and tuple(current) == tuple(original)


def test_attempt_fact_commits_with_owner_and_trigger_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非流主/异常流程：合法 Fact 同提交，坏 evidence 使 attempt 保持 started。"""

    database_url, config, tenant_id, route, plan = _prepare(
        monkeypatch, tmp_path, "attempt-facts.db"
    )
    committed = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd056_attempt_committed",
        trace_id="trc_prd056_attempt_committed",
        attempt_sequence=1,
        attempt_role="primary",
    )
    safe_complete_routing_attempt(
        config,
        committed,
        succeeded=False,
        latency_ms=17,
        error=_normalized_error(tenant_id, route.provider_id),
        usage_status="unavailable",
        quality_observation_eligible=True,
    )
    rejected = create_routing_attempt(
        config,
        plan,
        route,
        request_id="req_prd056_attempt_rejected",
        trace_id="trc_prd056_attempt_rejected",
        attempt_sequence=1,
        attempt_role="primary",
    )
    safe_complete_routing_attempt(
        config,
        rejected,
        succeeded=False,
        latency_ms=19,
        error=_invalid_error(tenant_id, route.provider_id),
        usage_status="unavailable",
        quality_observation_eligible=True,
    )

    connection = connect_database(database_url)
    try:
        committed_row = connection.execute(
            "SELECT status,error_code,upstream_http_status,retryable FROM routing_attempts "
            "WHERE tenant_id=? AND routing_attempt_id=?",
            (tenant_id, committed.routing_attempt_id),
        ).fetchone()
        rejected_row = connection.execute(
            "SELECT status,error_code,completed_at FROM routing_attempts "
            "WHERE tenant_id=? AND routing_attempt_id=?",
            (tenant_id, rejected.routing_attempt_id),
        ).fetchone()
        fact = connection.execute(
            "SELECT * FROM provider_error_facts WHERE tenant_id=? AND routing_attempt_id=?",
            (tenant_id, committed.routing_attempt_id),
        ).fetchone()
        rejected_count = connection.execute(
            "SELECT COUNT(*) FROM provider_error_facts WHERE tenant_id=? AND routing_attempt_id=?",
            (tenant_id, rejected.routing_attempt_id),
        ).fetchone()[0]
        _assert_owner_fully_frozen(
            connection,
            table="routing_attempts",
            id_column="routing_attempt_id",
            tenant_id=tenant_id,
            owner_id=committed.routing_attempt_id,
            trigger_error="routing_attempt_provider_error_fact_immutable",
        )
    finally:
        connection.close()
    assert tuple(committed_row) == (
        "failed",
        "gateway.upstream_rate_limited",
        429,
        1,
    )
    assert tuple(rejected_row) == ("started", None, None)
    assert rejected_count == 0
    assert fact is not None
    assert fact["stream_session_id"] is None
    assert fact["normalized_output_error_code"] == "gateway.upstream_rate_limited"
    assert fact["owner_terminal_error_code"] == "gateway.upstream_rate_limited"
    assert PRIVATE_SENTINEL not in fact["redacted_projection_json"]


def test_stream_fact_commits_with_owner_and_trigger_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """流式主/异常流程：握手失败 Fact 同提交，坏 evidence 使 Session 保持 starting。"""

    database_url, config, tenant_id, route, _plan = _prepare(
        monkeypatch, tmp_path, "stream-facts.db"
    )
    committed = create_stream_session(
        config,
        _stream_context(tenant_id, route, "committed"),
        inter_token_timeout_ms=30_000,
    )
    assert (
        safe_finish_stream_session(
            config,
            committed,
            status="upstream_failed",
            error_code="gateway.upstream_stream_failed",
            quality_observation_eligible=True,
            error=_normalized_error(tenant_id, route.provider_id),
        )
        is True
    )
    # A fallback handshake creates a second owner with the same request id after
    # the first provider error fact is committed. The old PRD-056 trigger
    # rejected this legitimate retry before the fallback provider was called.
    rejected = create_stream_session(
        config,
        _stream_context(tenant_id, route, "committed"),
        inter_token_timeout_ms=30_000,
    )
    assert (
        safe_finish_stream_session(
            config,
            rejected,
            status="upstream_failed",
            error_code="gateway.upstream_stream_failed",
            quality_observation_eligible=True,
            error=_invalid_error(tenant_id, route.provider_id),
        )
        is False
    )

    connection = connect_database(database_url)
    try:
        committed_row = connection.execute(
            "SELECT status,error_code,upstream_http_status,retryable FROM stream_sessions "
            "WHERE tenant_id=? AND stream_session_id=?",
            (tenant_id, committed.stream_session_id),
        ).fetchone()
        rejected_row = connection.execute(
            "SELECT status,error_code,completed_at FROM stream_sessions "
            "WHERE tenant_id=? AND stream_session_id=?",
            (tenant_id, rejected.stream_session_id),
        ).fetchone()
        fact = connection.execute(
            "SELECT * FROM provider_error_facts WHERE tenant_id=? AND stream_session_id=?",
            (tenant_id, committed.stream_session_id),
        ).fetchone()
        rejected_count = connection.execute(
            "SELECT COUNT(*) FROM provider_error_facts WHERE tenant_id=? AND stream_session_id=?",
            (tenant_id, rejected.stream_session_id),
        ).fetchone()[0]
        routing_attempt_count = connection.execute(
            "SELECT COUNT(*) FROM routing_attempts WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        _assert_owner_fully_frozen(
            connection,
            table="stream_sessions",
            id_column="stream_session_id",
            tenant_id=tenant_id,
            owner_id=committed.stream_session_id,
            trigger_error="stream_session_provider_error_fact_immutable",
        )
    finally:
        connection.close()
    assert tuple(committed_row) == (
        "upstream_failed",
        "gateway.upstream_stream_failed",
        429,
        1,
    )
    assert tuple(rejected_row) == ("starting", None, None)
    assert rejected_count == 0
    # 握手阶段还未创建 RoutingAttempt，流式 Fact 只能绑定 StreamSession owner。
    assert routing_attempt_count == 0
    assert fact is not None
    assert fact["routing_attempt_id"] is None
    assert fact["normalized_output_error_code"] == "gateway.upstream_rate_limited"
    assert fact["owner_terminal_error_code"] == "gateway.upstream_stream_failed"
    assert PRIVATE_SENTINEL not in fact["redacted_projection_json"]


def test_stream_auth_failure_in_first_window_second_allows_fallback_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url, config, tenant_id, route, _plan = _prepare(
        monkeypatch, tmp_path, "stream-auth-first-second.db"
    )
    first = create_stream_session(
        config,
        _stream_context(tenant_id, route, "auth-first-second"),
        inter_token_timeout_ms=30_000,
    )
    current = datetime.now(UTC)
    first_window_second = current.replace(second=(current.second // 5) * 5, microsecond=123456)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return first_window_second

    monkeypatch.setattr(stream_sessions_module, "datetime", FixedDatetime)
    assert safe_finish_stream_session(
        config,
        first,
        status="upstream_failed",
        error_code="gateway.upstream_auth_failed",
        quality_observation_eligible=False,
        error=_auth_error(tenant_id, route.provider_id),
    )
    second = create_stream_session(
        config,
        _stream_context(tenant_id, route, "auth-first-second"),
        inter_token_timeout_ms=30_000,
    )
    connection = connect_database(database_url)
    try:
        fact = connection.execute(
            "SELECT created_at FROM provider_error_facts WHERE stream_session_id=?",
            (first.stream_session_id,),
        ).fetchone()
        source = connection.execute(
            "SELECT occurred_at FROM security_event_sources "
            "WHERE source_object_id IN (SELECT provider_error_fact_id FROM provider_error_facts "
            "WHERE stream_session_id=?)",
            (first.stream_session_id,),
        ).fetchone()
        assert fact is not None and source is not None
        assert fact["created_at"] == source["occurred_at"]
        assert fact["created_at"] == first_window_second.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        assert second.stream_session_id != first.stream_session_id
    finally:
        connection.close()
