"""PRD-044 受限策略 DSL 的编译、安全边界与纯求值专项测试。

测试不依赖数据库、网络或 sleep；超时通过可控单调时钟确定触发，保证 CI 能稳定证明
整组策略共享 deadline 且超时固定拒绝。非法输入断言只检查固定诊断，不传播攻击正文。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from metahub_shared.runtime.policy_dsl import (
    ALLOWED_VARIABLES,
    DSL_VERSION,
    MAX_COMPARISON_EXPLANATIONS,
    MAX_LOG_ONLY_EXPLANATIONS,
    MAX_MASK_EXPLANATIONS,
    MAX_POLICIES_PER_SET,
    CompiledPolicyExpression,
    PolicyActionError,
    PolicyContextError,
    PolicyDslError,
    PolicyEvaluationInput,
    canonical_policy_action,
    compile_policy_expression,
    evaluate_compiled_expression,
    evaluate_policy_set,
    policy_draft_fingerprint,
)


class FakeMonotonic:
    """按给定序列返回单调时间，用于无 sleep 地确定触发 deadline。"""

    def __init__(self, values: list[float]) -> None:
        """保存测试时间点；序列耗尽后持续返回最后一个值。"""

        self._values = iter(values)
        self._last = values[-1]

    def __call__(self) -> float:
        """返回下一个单调时间点，模拟策略或 AST 节点边界推进。"""

        self._last = next(self._values, self._last)
        return self._last


def _policy(
    policy_id: str,
    expression: str,
    action_type: str,
    *,
    priority: int = 100,
    target_model_id: str | None = None,
    status: str = "active",
) -> PolicyEvaluationInput:
    """构造带稳定 Rule/Action ID 的策略输入，减少求值测试样板代码。"""

    return PolicyEvaluationInput(
        policy_id=policy_id,
        policy_rule_id=f"prule_{policy_id}",
        policy_action_id=f"pact_{policy_id}",
        priority=priority,
        compiled_expression=compile_policy_expression(expression),
        action_type=action_type,  # type: ignore[arg-type]
        target_model_id=target_model_id,
        status=status,  # type: ignore[arg-type]
    )


def test_compile_builds_canonical_ast_json_hash_and_expression() -> None:
    """主流程：字符串与 Decimal 比较生成设计文档规定的稳定 AST/hash。"""

    compiled = compile_policy_expression(
        'env == "prod" and budget_remaining < 100.00'
    )

    assert compiled.dsl_version == DSL_VERSION
    assert compiled.ast == {
        "kind": "logical",
        "operator": "and",
        "children": [
            {
                "kind": "comparison",
                "variable": "env",
                "operator": "eq",
                "value": {"kind": "string", "value": "prod"},
            },
            {
                "kind": "comparison",
                "variable": "budget_remaining",
                "operator": "lt",
                "value": {"kind": "decimal", "value": "100"},
            },
        ],
    }
    expected_json = json.dumps(
        compiled.ast,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = hashlib.sha256(
        b"policy-dsl-v1\x00" + expected_json.encode("utf-8")
    ).hexdigest()
    assert compiled.canonical_json == expected_json
    assert compiled.canonical_ast_json == expected_json
    assert compiled.canonical_ast is compiled.ast
    assert compiled.condition_hash == expected_hash
    assert compiled.variables == ("budget_remaining", "env")
    assert compiled.canonical_expression == 'env == "prod" and budget_remaining < 100'


def test_compile_normalizes_unicode_decimal_and_negative_zero() -> None:
    """兼容性：NFC 等价字符串与 Decimal 等价写法必须产生相同 AST/hash。"""

    decomposed = compile_policy_expression('project == "e\u0301" and budget_remaining == -0.00')
    normalized = compile_policy_expression('project == "\u00e9" and budget_remaining == 0')

    assert decomposed.ast == normalized.ast
    assert decomposed.condition_hash == normalized.condition_hash
    assert '"é"' in decomposed.canonical_expression
    assert decomposed.canonical_expression.endswith("budget_remaining == 0")


@pytest.mark.parametrize(
    ("expression", "operator"),
    [
        ('tenant == "ten_default"', "eq"),
        ('env != "dev"', "ne"),
        ("budget_remaining < 1", "lt"),
        ("budget_remaining <= 1", "lte"),
        ("budget_remaining > 1", "gt"),
        ("budget_remaining >= 1", "gte"),
        ('model in ["a", "b"]', "in"),
        ('data_class not in ["public"]', "not_in"),
    ],
)
def test_compile_accepts_all_supported_operators(expression: str, operator: str) -> None:
    """主流程：八种 canonical 比较操作符均按白名单映射。"""

    assert compile_policy_expression(expression).ast["operator"] == operator


@pytest.mark.parametrize("variable", ALLOWED_VARIABLES)
def test_compile_accepts_all_six_variables(variable: str) -> None:
    """主流程：Excel 指定的六个变量可编译，且变量清单去重排序。"""

    scalar = "0" if variable == "budget_remaining" else '"value"'
    compiled = compile_policy_expression(f"{variable} == {scalar}")

    assert compiled.variables == (variable,)


def test_boolean_precedence_parentheses_flattening_and_not_are_stable() -> None:
    """语义：and 高于 or，同操作符扁平，括号与 not 只改变必要分组。"""

    default_precedence = compile_policy_expression(
        'env == "prod" or model == "a" and project == "p"'
    )
    grouped = compile_policy_expression(
        '(env == "prod" or model == "a") and project == "p"'
    )
    flattened = compile_policy_expression(
        'env == "prod" and (model == "a" and project == "p")'
    )
    negated = compile_policy_expression('not (env == "prod" or model == "a")')

    assert default_precedence.ast["operator"] == "or"
    assert default_precedence.ast["children"][1]["operator"] == "and"
    assert grouped.ast["operator"] == "and"
    assert len(flattened.ast["children"]) == 3
    assert flattened.canonical_expression == (
        'env == "prod" and model == "a" and project == "p"'
    )
    assert negated.canonical_expression == 'not (env == "prod" or model == "a")'


def test_membership_keeps_user_order_and_duplicate_values() -> None:
    """兼容性：列表保留输入顺序和重复项，解释与 hash 不做隐式重排去重。"""

    compiled = compile_policy_expression('env in ["prod", "dev", "prod"]')
    values = [item["value"] for item in compiled.ast["value"]["items"]]

    assert values == ["prod", "dev", "prod"]
    assert compiled.canonical_expression == 'env in ["prod", "dev", "prod"]'


@pytest.mark.parametrize(
    "expression",
    [
        '__import__("os").system("whoami")',
        'tenant.__class__ == "x"',
        'env == "prod"; deny()',
        'project matches ".*"',
        "budget_remaining < external_limit()",
        'env = "prod"',
        'env == \'prod\'',
        'env == `prod`',
        'env == "prod" # comment',
        'env == {"value": "prod"}',
        'env == "${SECRET}"',
        'ｅnv == "prod"',
        'env == "prod"\nexec("x")',
    ],
)
def test_compile_blocks_script_injection_and_unknown_syntax(expression: str) -> None:
    """安全：脚本、属性、函数、赋值和 Unicode 混淆标识符全部固定拒绝。"""

    with pytest.raises(PolicyDslError) as exc:
        compile_policy_expression(expression)

    assert exc.value.code == "policy.expression_invalid"
    assert len(exc.value.diagnostics) == 1
    diagnostic = exc.value.diagnostics[0]
    assert diagnostic.field == "condition_expr"
    assert expression not in str(exc.value)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   \t\r\n",
        'unknown == "x"',
        'env < "prod"',
        "env == 1",
        'budget_remaining == "1"',
        "budget_remaining in [1]",
        "env in []",
        'env in ["prod", 1]',
        'env in [["prod"]]',
        'env == "prod", model == "a"',
        'env == "prod" and',
        'not not env == "prod"',
        'not (not env == "prod")',
        "budget_remaining == 01",
        "budget_remaining == +1",
        "budget_remaining == 1e3",
        "budget_remaining == NaN",
        "budget_remaining == Infinity",
    ],
)
def test_compile_rejects_invalid_grammar_types_and_literals(expression: str) -> None:
    """异常流程：文法、类型和 Decimal 非普通写法不能形成可保存 AST。"""

    with pytest.raises(PolicyDslError):
        compile_policy_expression(expression)


@pytest.mark.parametrize(
    ("expression", "diagnostic_code"),
    [
        ("x" * 1_025, "policy.dsl.source_too_long"),
        ('env == "' + "界" * 700 + '"', "policy.dsl.source_too_long"),
        ('env == "' + "x" * 129 + '"', "policy.dsl.string_too_long"),
        ('env in [' + ",".join('"x"' for _ in range(21)) + "]", "policy.dsl.list_limit_exceeded"),
        ("((((env == \"prod\"))))", "policy.dsl.parenthesis_depth_exceeded"),
        (
            " and ".join('env == "prod"' for _ in range(17)),
            "policy.dsl.comparison_limit_exceeded",
        ),
        (
            " and ".join('env == "prod"' for _ in range(33)),
            "policy.dsl.token_limit_exceeded",
        ),
        (
            " and ".join('env in ["a", "b"]' for _ in range(16)),
            "policy.dsl.node_limit_exceeded",
        ),
        ("budget_remaining == 1234567890123", "policy.dsl.decimal_invalid"),
        ("budget_remaining == 1.1234567890123", "policy.dsl.decimal_invalid"),
    ],
)
def test_compile_enforces_resource_limits(expression: str, diagnostic_code: str) -> None:
    """安全：源码、token、节点、比较、括号、列表、字符串和精度门限逐项生效。"""

    with pytest.raises(PolicyDslError) as exc:
        compile_policy_expression(expression)

    assert exc.value.diagnostic.code == diagnostic_code


def test_json_string_control_characters_and_nul_are_rejected() -> None:
    """安全：源码 NUL 和 JSON 解码后的控制字符不能进入 canonical AST。"""

    with pytest.raises(PolicyDslError) as nul_error:
        compile_policy_expression('env == "prod"\x00')
    with pytest.raises(PolicyDslError) as escaped_control_error:
        compile_policy_expression('env == "prod\\u000a"')

    assert nul_error.value.diagnostic.code == "policy.dsl.nul_forbidden"
    assert escaped_control_error.value.diagnostic.code == (
        "policy.dsl.string_control_forbidden"
    )


def test_canonical_action_and_draft_fingerprint_follow_byte_contract() -> None:
    """主流程：动作 JSON 固定形状，草稿指纹精确覆盖 canonical AST、NUL 和动作。"""

    compiled = compile_policy_expression('env == "prod"')
    action_json = canonical_policy_action("route_to", "mdl_deepseek")
    expected = hashlib.sha256(
        b"policy-dsl-v1\x00"
        + compiled.canonical_json.encode("utf-8")
        + b"\x00"
        + action_json.encode("utf-8")
    ).hexdigest()

    assert action_json == '{"target_model_id":"mdl_deepseek","type":"route_to"}'
    assert canonical_policy_action("deny") == '{"target_model_id":null,"type":"deny"}'
    assert policy_draft_fingerprint(compiled, "route_to", "mdl_deepseek") == expected


@pytest.mark.parametrize(
    ("action_type", "target"),
    [
        ("script", None),
        ("route_to", None),
        ("route_to", ""),
        ("allow", "mdl_x"),
        ("deny", "mdl_x"),
        ("route_to", "mdl_\x00x"),
    ],
)
def test_canonical_action_rejects_invalid_target_combinations(
    action_type: str,
    target: str | None,
) -> None:
    """异常流程：未知动作、缺失 route target 与残留 target 均不能生成指纹。"""

    with pytest.raises(PolicyActionError):
        canonical_policy_action(action_type, target)


def test_expression_evaluation_uses_short_circuit_and_exact_decimal() -> None:
    """语义：短路分支不读取缺失字段，Decimal 比较不受二进制浮点误差影响。"""

    short_circuit = compile_policy_expression(
        'env == "prod" or budget_remaining < 0.3'
    )
    decimal_check = compile_policy_expression("budget_remaining == 0.3")

    short_result = evaluate_compiled_expression(
        short_circuit,
        {"env": "prod"},
        timeout_ms=100,
    )
    decimal_result = evaluate_compiled_expression(
        decimal_check,
        {"budget_remaining": Decimal("0.1") + Decimal("0.2")},
        timeout_ms=100,
    )

    assert short_result.matched is True
    assert [item.variable for item in short_result.comparisons] == ["env"]
    assert decimal_result.matched is True
    assert decimal_result.comparisons[0].actual == "0.3"
    assert decimal_result.comparisons[0].expected == "0.3"


def test_expression_evaluation_reports_lazy_missing_context_without_partial_values() -> None:
    """异常流程：实际访问缺失变量时返回稳定 reason，且清空此前比较值。"""

    compiled = compile_policy_expression(
        'env == "dev" or budget_remaining < 10'
    )
    result = evaluate_compiled_expression(compiled, {"env": "prod"}, timeout_ms=100)

    assert result.matched is False
    assert result.reason_code == "policy.context_missing"
    assert result.missing_variable == "budget_remaining"
    assert result.comparisons == ()
    assert result.timed_out is False


@pytest.mark.parametrize(
    "context",
    [
        {"budget_remaining": 0.3},
        {"budget_remaining": True},
        {"budget_remaining": "NaN"},
        {"budget_remaining": "-1"},
        {"budget_remaining": "1e3"},
    ],
)
def test_expression_evaluation_rejects_invalid_budget_context(
    context: dict[str, object],
) -> None:
    """安全：预算上下文拒绝 float、bool、非有限值、负数和指数写法。"""

    compiled = compile_policy_expression("budget_remaining >= 0")

    with pytest.raises(PolicyContextError) as exc:
        evaluate_compiled_expression(compiled, context, timeout_ms=100)

    assert exc.value.code == "policy.context_invalid"
    assert exc.value.variable == "budget_remaining"


def test_expression_rejects_huge_decimal_exponent_before_formatting() -> None:
    """资源边界：超大 Decimal 指数先按 tuple 拒绝，不能格式化为巨型临时字符串。"""

    compiled = compile_policy_expression("budget_remaining >= 0")

    with pytest.raises(PolicyContextError):
        evaluate_compiled_expression(
            compiled,
            {"budget_remaining": Decimal("1E+1000000")},
            timeout_ms=100,
        )


def test_policy_set_sorts_priority_then_id_and_continues_log_only() -> None:
    """主流程：低数值优先，同优先级按 ID 决胜，log_only 命中后继续。"""

    policies = [
        _policy("pol_z", 'env == "prod"', "deny", priority=100),
        _policy("pol_log", 'env == "prod"', "log_only", priority=50),
        _policy("pol_a", 'env == "prod"', "allow", priority=100),
    ]
    result = evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)

    assert result.matched is True
    assert result.decision == "allow"
    assert result.reason_code == "policy.rule_matched"
    assert result.policy_reason is not None
    assert result.policy_reason.policy_id == "pol_a"
    assert [item.policy_id for item in result.policy_checks] == ["pol_log", "pol_a"]
    assert [item.policy_id for item in result.log_only_matches] == ["pol_log"]
    assert result.checked_policy_count == 2
    assert [item.match_order for item in result.policy_checks] == [1, 2]


def test_policy_set_default_allow_skips_disabled_policies() -> None:
    """主流程：disabled 不参与求值，无决定性命中固定默认放行。"""

    policies = [
        _policy("pol_disabled", 'env == "prod"', "deny", status="disabled"),
        _policy("pol_no_match", 'env == "test"', "deny"),
    ]
    result = evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)

    assert result.matched is False
    assert result.decision == "allow"
    assert result.reason_code == "policy.no_match_default_allow"
    assert result.checked_policy_count == 1
    assert [item.policy_id for item in result.policy_checks] == ["pol_no_match"]


def test_policy_set_rejects_more_than_tenant_policy_quota() -> None:
    """资源边界：纯求值器同样限制 200 条策略，离线调用不能绕过控制面租户配额。"""

    compiled = compile_policy_expression('env == "prod"')
    policies = [
        PolicyEvaluationInput(
            policy_id=f"pol_{index:03d}",
            policy_rule_id=f"prule_{index:03d}",
            policy_action_id=f"pact_{index:03d}",
            priority=100,
            compiled_expression=compiled,
            action_type="log_only",
        )
        for index in range(MAX_POLICIES_PER_SET + 1)
    ]

    with pytest.raises(ValueError, match="200 policy limit"):
        evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)


@pytest.mark.parametrize(
    ("action_type", "target"),
    [("deny", None), ("route_to", "mdl_target")],
)
def test_policy_set_returns_decisive_action_without_side_effects(
    action_type: str,
    target: str | None,
) -> None:
    """主流程：deny/route_to 只形成决定与解释，不执行上游路由。"""

    result = evaluate_policy_set(
        [_policy("pol_decide", 'env == "prod"', action_type, target_model_id=target)],
        {"env": "prod"},
        timeout_ms=100,
    )

    assert result.decision == action_type
    assert result.target_model_id == target
    assert result.alert_required is False


def test_policy_set_keeps_mask_as_modifier_before_decisive_action() -> None:
    """PRD-068 主流程：mask 记录安全摘要后继续扫描，后续 deny 仍是最终决定。"""

    result = evaluate_policy_set(
        [
            _policy("pol_mask", 'env == "prod"', "mask", priority=10),
            _policy("pol_deny", 'model == "blocked"', "deny", priority=20),
        ],
        {"env": "prod", "model": "blocked"},
        timeout_ms=100,
    )

    assert result.decision == "deny"
    assert result.mask_required is True
    assert [item.policy_id for item in result.mask_matches] == ["pol_mask"]
    assert result.policy_reason is not None
    assert result.policy_reason.policy_id == "pol_deny"


def test_policy_set_truncates_comparisons_without_changing_decision() -> None:
    """资源边界：超过 64 条 comparison 只截断展示，后续最终 deny 仍完整执行。"""

    expression = " and ".join('env == "prod"' for _ in range(16))
    policies = [
        _policy(f"pol_log_{index:02d}", expression, "log_only", priority=index + 1)
        for index in range(5)
    ]
    policies.append(_policy("pol_final", 'env == "prod"', "deny", priority=100))
    result = evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)

    retained = sum(len(item.comparisons) for item in result.policy_checks)
    assert retained == MAX_COMPARISON_EXPLANATIONS
    assert result.explanation_truncated is True
    assert result.decision == "deny"
    assert result.checked_policy_count == 6


def test_policy_set_truncates_log_only_matches_without_stopping_evaluation() -> None:
    """资源边界：只保留前 20 条 log_only 命中，但仍执行后续决定性策略。"""

    policies = [
        _policy(f"pol_log_{index:02d}", 'env == "prod"', "log_only", priority=index + 1)
        for index in range(MAX_LOG_ONLY_EXPLANATIONS + 1)
    ]
    policies.append(_policy("pol_mask", 'env == "prod"', "mask", priority=100))
    policies.append(_policy("pol_final", 'env == "prod"', "deny", priority=101))
    result = evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)

    assert len(result.log_only_matches) == MAX_LOG_ONLY_EXPLANATIONS
    assert result.explanation_truncated is True
    assert result.decision == "deny"
    assert result.mask_required is True
    assert result.checked_policy_count == MAX_LOG_ONLY_EXPLANATIONS + 3


def test_policy_set_bounds_mask_matches_and_defaults_to_allow() -> None:
    """PRD-068 资源边界：只保留前 20 个 mask 摘要，但没有决定动作时仍默认 allow。"""

    policies = [
        _policy(f"pol_mask_{index:02d}", 'env == "prod"', "mask", priority=index + 1)
        for index in range(MAX_MASK_EXPLANATIONS + 1)
    ]
    result = evaluate_policy_set(policies, {"env": "prod"}, timeout_ms=100)

    assert result.decision == "allow"
    assert result.reason_code == "policy.no_match_default_allow"
    assert result.mask_required is True
    assert len(result.mask_matches) == MAX_MASK_EXPLANATIONS
    assert result.explanation_truncated is True


def test_policy_set_timeout_is_fail_closed_and_clears_partial_explanations() -> None:
    """安全：fake monotonic 跨过整组 deadline 后固定 deny、告警且无部分路由/解释。"""

    clock = FakeMonotonic([0.0, 0.001, 0.002, 0.003, 0.006])
    policies = [
        _policy("pol_route", 'env == "prod"', "route_to", target_model_id="mdl_target")
    ]
    result = evaluate_policy_set(
        policies,
        {"env": "prod"},
        timeout_ms=5,
        monotonic=clock,
    )

    assert result.decision == "deny"
    assert result.reason_code == "policy.evaluation_timeout"
    assert result.target_model_id is None
    assert result.timed_out is True
    assert result.alert_required is True
    assert result.policy_reason is None
    assert result.policy_checks == ()
    assert result.log_only_matches == ()


def test_expression_timeout_is_deterministic_without_sleep() -> None:
    """安全：单表达式也使用注入时钟，在节点边界超时且不抛未捕获异常。"""

    compiled = compile_policy_expression('env == "prod"')
    clock = FakeMonotonic([0.0, 0.001, 0.002, 0.006])
    result = evaluate_compiled_expression(
        compiled,
        {"env": "prod"},
        timeout_ms=5,
        monotonic=clock,
    )

    assert result.reason_code == "policy.evaluation_timeout"
    assert result.matched is False
    assert result.comparisons == ()
    assert result.timed_out is True
    assert result.alert_required is True


def test_policy_set_missing_context_is_fail_closed() -> None:
    """异常流程：策略集实际访问缺失变量时拒绝，不能落到 no-match 默认放行。"""

    result = evaluate_policy_set(
        [_policy("pol_budget", "budget_remaining < 10", "allow")],
        {"env": "prod"},
        timeout_ms=100,
    )

    assert result.decision == "deny"
    assert result.reason_code == "policy.context_missing"
    assert result.missing_variable == "budget_remaining"
    assert result.policy_checks == ()


def test_policy_result_explanation_omits_expression_ast_and_sensitive_fields() -> None:
    """安全：可序列化解释只含 ID/hash/比较，不扩散表达式、AST 或密钥字段。"""

    result = evaluate_policy_set(
        [_policy("pol_safe", 'env == "prod"', "deny")],
        {"env": "prod"},
        timeout_ms=100,
    )
    rendered = json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)

    assert "condition_expr" not in rendered
    assert "canonical_json" not in rendered
    assert '"kind"' not in rendered
    assert "provider_key" not in rendered.lower()
    assert "prompt" not in rendered.lower()


@pytest.mark.parametrize("timeout_ms", [0, 101, True, 1.5])
def test_evaluator_rejects_timeout_outside_hard_limit(timeout_ms: object) -> None:
    """安全：调用方不能以请求参数把 deadline 放宽到 100 ms 之外或传 bool/float。"""

    compiled = compile_policy_expression('env == "prod"')

    with pytest.raises(ValueError):
        evaluate_compiled_expression(
            compiled,
            {"env": "prod"},
            timeout_ms=timeout_ms,  # type: ignore[arg-type]
        )


def test_compiled_expression_integrity_is_checked_before_evaluation() -> None:
    """安全：AST 被内存或持久化层篡改后 hash 不匹配，求值器必须拒绝执行。"""

    compiled = compile_policy_expression('env == "prod"')
    tampered = CompiledPolicyExpression(
        dsl_version=compiled.dsl_version,
        ast={**compiled.ast, "operator": "ne"},
        canonical_json=compiled.canonical_json,
        canonical_expression=compiled.canonical_expression,
        variables=compiled.variables,
        condition_hash=compiled.condition_hash,
    )

    with pytest.raises(ValueError, match="integrity"):
        evaluate_compiled_expression(tampered, {"env": "prod"}, timeout_ms=100)
