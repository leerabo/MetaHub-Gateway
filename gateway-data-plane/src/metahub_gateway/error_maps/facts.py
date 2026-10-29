"""把 Adapter 的脱敏 Provider 错误 evidence 写入既有 owner 事务。

本模块不创建连接、不提交事务，也不捕获 SQLite 约束错误。调用方必须先更新
RoutingAttempt 或 StreamSession 终态，再在同一连接插入 Fact；任何 owner/evidence
校验失败都由外层整体回滚，不能留下无法解释的半终态。
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from metahub_shared.runtime.provider_error_map import ProviderErrorEvidence


def insert_provider_error_fact(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    provider_id: str,
    model_id: str,
    provider_code: str,
    evidence: ProviderErrorEvidence,
    owner_terminal_error_code: str,
    created_at: str,
    routing_attempt_id: str | None = None,
    stream_session_id: str | None = None,
) -> str:
    """在调用方事务内插入一条 attempt/session 二选一的冻结错误事实。

    参数全部来自服务端 owner 和 Adapter 的 repr-safe evidence；返回生成的 Fact ID。
    owner 必须精确二选一，原始 code/type/message/body、Endpoint、Header 和凭据没有入参，
    因而无法误写数据库。函数故意让数据库异常向外传播，以保护 owner 终态原子性。
    """

    # 同时为空会产生无归属事实，同时非空会让一个失败绑定两个状态机，二者都必须拒绝。
    if (routing_attempt_id is None) == (stream_session_id is None):
        raise ValueError("provider_error_fact_owner_invalid")
    if not isinstance(evidence, ProviderErrorEvidence):
        # 普通 ApiError 或调用方拼装字典没有 Adapter 信任边界，不能取得事实写入资格。
        raise ValueError("provider_error_fact_evidence_invalid")
    fact_id = f"pef_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO provider_error_facts(
          provider_error_fact_id, tenant_id, request_id, trace_id,
          routing_attempt_id, stream_session_id, provider_id, model_id, provider_code,
          upstream_http_status, transport_kind,
          provider_error_code_class, provider_error_type_class,
          provider_error_code_present, provider_error_type_present, message_present,
          redacted_projection_json, projection_hash, body_length_bucket, body_truncated, is_json,
          matched_error_map_id, matched_rule_version, matched_pattern_hash, matched_rule_source,
          error_map_revision, overlay_content_hash, builtin_catalog_version,
          error_map_snapshot_source, match_basis, normalized_output_error_code,
          owner_terminal_error_code, created_at
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?
        )
        """,
        (
            fact_id,
            tenant_id,
            request_id,
            trace_id,
            routing_attempt_id,
            stream_session_id,
            provider_id,
            model_id,
            provider_code,
            evidence.upstream_http_status,
            evidence.transport_kind,
            evidence.provider_error_code_class,
            evidence.provider_error_type_class,
            1 if evidence.provider_error_code_present else 0,
            1 if evidence.provider_error_type_present else 0,
            1 if evidence.message_present else 0,
            evidence.redacted_projection_json,
            evidence.projection_hash,
            evidence.body_length_bucket,
            1 if evidence.body_truncated else 0,
            1 if evidence.is_json else 0,
            evidence.matched_error_map_id,
            evidence.matched_rule_version,
            evidence.matched_pattern_hash,
            evidence.matched_rule_source,
            evidence.error_map_revision,
            evidence.overlay_content_hash,
            evidence.builtin_catalog_version,
            evidence.error_map_snapshot_source,
            evidence.match_basis,
            evidence.normalized_output_error_code,
            owner_terminal_error_code,
            created_at,
        ),
    )
    return fact_id


__all__ = ["insert_provider_error_fact"]
