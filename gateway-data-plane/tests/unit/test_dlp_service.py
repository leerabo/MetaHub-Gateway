"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_dlp_service.py
"""

from __future__ import annotations

import sqlite3

import pytest

from metahub_gateway.dlp.models import DlpDecision
from metahub_gateway.dlp.service import _load_rules, apply_dlp_decision
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex


def test_safe_dlp_regex_rejects_redos_patterns_without_echoing_pattern() -> None:
    """异常流程：危险正则必须被拒绝，异常消息只包含安全原因码，不回显管理员提交的 pattern。"""

    with pytest.raises(UnsafeRegexError) as nested_error:
        compile_safe_dlp_regex("(a+)+")
    with pytest.raises(UnsafeRegexError) as lookaround_error:
        compile_safe_dlp_regex("(?=secret)")

    assert nested_error.value.reason_code == "regex.nested_quantifier_unsafe"
    assert str(nested_error.value) == "regex.nested_quantifier_unsafe"
    assert "(a+)+" not in str(nested_error.value)
    assert lookaround_error.value.reason_code == "regex.lookaround_unsafe"


def test_apply_dlp_decision_enriches_request_log_context_without_plaintext() -> None:
    """主流程：DLP 决策只把规则 ID、动作、命中数和原因码写入 request log context。"""

    context = RequestLogContext(
        request_id="req_dlp_unit",
        tenant_id="tenant_1",
        trace_id="trc_dlp_unit",
        virtual_key_id="vk_1",
        endpoint="/v1/chat/completions",
        request_model="company-default",
    )
    decision = DlpDecision(
        status="blocked",
        rule_id="dlp_1",
        action="block",
        match_count=2,
        reason_code="dlp.keyword_matched",
    )

    enriched = apply_dlp_decision(context, decision)

    assert enriched.dlp_status == "blocked"
    assert enriched.dlp_rule_id == "dlp_1"
    assert enriched.dlp_action == "block"
    assert enriched.dlp_match_count == 2
    assert enriched.dlp_reason_code == "dlp.keyword_matched"
    assert "敏感词" not in repr(enriched)


def test_load_rules_filters_input_scope_tenant_and_preserves_order() -> None:
    """作用域边界：输入守卫只加载本租户 input/both 规则，并保持既有优先级排序。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE dlp_rules(
          dlp_rule_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          name TEXT NOT NULL,
          rule_type TEXT NOT NULL,
          pattern TEXT NOT NULL,
          action TEXT NOT NULL,
          scan_scope TEXT NOT NULL,
          status TEXT NOT NULL,
          priority INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO dlp_rules(
          dlp_rule_id, tenant_id, name, rule_type, pattern, action,
          scan_scope, status, priority, updated_at
        )
        VALUES(?, ?, ?, 'keyword', ?, 'block', ?, 'active', ?, ?)
        """,
        [
            ("dlp_output", "tenant_1", "输出专用", "secret", "output", 1, "2026-08-04T10:00:00Z"),
            ("dlp_input_new", "tenant_1", "输入新规则", "secret", "input", 20, "2026-08-04T11:00:00Z"),
            ("dlp_both_old", "tenant_1", "双向旧规则", "secret", "both", 20, "2026-08-04T09:00:00Z"),
            ("dlp_other_tenant", "tenant_2", "其他租户", "secret", "input", 0, "2026-08-04T12:00:00Z"),
        ],
    )

    rules = _load_rules(connection, "tenant_1")

    assert [rule.dlp_rule_id for rule in rules] == ["dlp_input_new", "dlp_both_old"]
    assert all(rule.tenant_id == "tenant_1" for rule in rules)
