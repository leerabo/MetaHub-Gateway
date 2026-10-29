"""PRD-056 RequestLog ProviderErrorFact 精确 DTO、租户 owner 隔离与异常投影单元测试。"""

from __future__ import annotations

import json
import sqlite3

import pytest
from metahub_control.request_logs.service import _load_provider_error_fact
from metahub_shared.runtime.errors import ApiError


def _connection() -> sqlite3.Connection:
    """建立只含 DTO 查询列的内存表，测试不访问开发数据库或本机服务。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE provider_error_facts(
          provider_error_fact_id TEXT, tenant_id TEXT, routing_attempt_id TEXT,
          stream_session_id TEXT, matched_error_map_id TEXT, matched_rule_source TEXT,
          matched_rule_version INTEGER, matched_pattern_hash TEXT,
          error_map_revision INTEGER, overlay_content_hash TEXT,
          builtin_catalog_version TEXT, error_map_snapshot_source TEXT,
          normalized_output_error_code TEXT, owner_terminal_error_code TEXT,
          upstream_http_status INTEGER, transport_kind TEXT, match_basis TEXT,
          provider_error_code_class TEXT,
          provider_error_type_class TEXT, provider_error_code_present INTEGER,
          provider_error_type_present INTEGER, message_present INTEGER,
          redacted_projection_json TEXT, projection_hash TEXT,
          body_length_bucket TEXT, body_truncated INTEGER, is_json INTEGER
        )
        """
    )
    return connection


def _insert_fact(connection: sqlite3.Connection, **overrides: object) -> None:
    """插入一条仅含脱敏投影的 owner 事实，允许用 overrides 构造异常合同。"""

    projection = {
        "body_length_bucket": "1_255",
        "body_truncated": False,
        "is_json": True,
        "message": "[REDACTED]",
        "message_present": True,
        "provider_error_code_class": "model_not_found_error",
        "provider_error_code_present": True,
        "provider_error_type_class": None,
        "provider_error_type_present": False,
    }
    values: dict[str, object] = {
        "provider_error_fact_id": "pef_" + "1" * 32,
        "tenant_id": "tenant_a",
        "routing_attempt_id": "attempt_a",
        "stream_session_id": None,
        "matched_error_map_id": "builtin_model_not_found_signal",
        "matched_rule_source": "builtin",
        "matched_rule_version": None,
        "matched_pattern_hash": "2" * 64,
        "error_map_revision": 0,
        "overlay_content_hash": "3" * 64,
        "builtin_catalog_version": "provider-error-builtin-v1",
        "error_map_snapshot_source": "builtin_fallback",
        "normalized_output_error_code": "gateway.upstream_model_not_found",
        "owner_terminal_error_code": "gateway.upstream_model_not_found",
        "upstream_http_status": 404,
        "transport_kind": "none",
        "match_basis": "provider_error_code",
        "provider_error_code_class": "model_not_found_error",
        "provider_error_type_class": None,
        "provider_error_code_present": 1,
        "provider_error_type_present": 0,
        "message_present": 1,
        "redacted_projection_json": json.dumps(
            projection, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ),
        "projection_hash": "4" * 64,
        "body_length_bucket": "1_255",
        "body_truncated": 0,
        "is_json": 1,
    }
    values.update(overrides)
    columns = tuple(values)
    connection.execute(
        f"INSERT INTO provider_error_facts({','.join(columns)}) "
        f"VALUES({','.join(':' + column for column in columns)})",
        values,
    )


def test_attempt_fact_uses_tenant_owner_and_owner_retryable() -> None:
    """主流程：同租户 attempt 返回 exact DTO，跨租户同 ID 不可见。"""

    connection = _connection()
    try:
        _insert_fact(connection)
        fact = _load_provider_error_fact(
            connection,
            "tenant_a",
            routing_attempt_id="attempt_a",
            stream_session_id=None,
            owner_retryable=True,
        )
        assert fact is not None
        assert fact.routing_attempt_id == "attempt_a"
        assert fact.stream_session_id is None
        assert fact.retryable is True
        assert set(fact.redacted_projection_json) == {
            "body_length_bucket",
            "body_truncated",
            "is_json",
            "message",
            "message_present",
            "provider_error_code_class",
            "provider_error_code_present",
            "provider_error_type_class",
            "provider_error_type_present",
        }
        assert _load_provider_error_fact(
            connection,
            "tenant_b",
            routing_attempt_id="attempt_a",
            stream_session_id=None,
            owner_retryable=False,
        ) is None
    finally:
        connection.close()


@pytest.mark.parametrize("failure_kind", ["projection", "owner"])
def test_fact_contract_drift_fails_closed(failure_kind: str) -> None:
    """异常流程：投影扩列或 owner 非二选一均返回统一查询故障。"""

    connection = _connection()
    try:
        if failure_kind == "projection":
            unsafe = {
                "body_length_bucket": "1_255",
                "body_truncated": False,
                "is_json": True,
                "message": "[REDACTED]",
                "message_present": True,
                "provider_error_code_class": "model_not_found_error",
                "provider_error_code_present": True,
                "provider_error_type_class": None,
                "provider_error_type_present": False,
                "raw_message": "must-not-pass",
            }
            _insert_fact(connection, redacted_projection_json=json.dumps(unsafe))
        else:
            _insert_fact(connection)
        with pytest.raises(ApiError) as error:
            _load_provider_error_fact(
                connection,
                "tenant_a",
                routing_attempt_id=(None if failure_kind == "owner" else "attempt_a"),
                stream_session_id=None,
                owner_retryable=False,
            )
        assert error.value.code == "request_log.query_unavailable"
    finally:
        connection.close()


def test_stream_fact_keeps_stream_owner_separate() -> None:
    """权限/归属流程：流式 Fact 只挂 StreamSession，不伪造 RoutingAttempt。"""

    connection = _connection()
    try:
        _insert_fact(
            connection,
            routing_attempt_id=None,
            stream_session_id="stream_a",
        )
        fact = _load_provider_error_fact(
            connection,
            "tenant_a",
            routing_attempt_id=None,
            stream_session_id="stream_a",
            owner_retryable=False,
        )
        assert fact is not None
        assert fact.routing_attempt_id is None
        assert fact.stream_session_id == "stream_a"
    finally:
        connection.close()
