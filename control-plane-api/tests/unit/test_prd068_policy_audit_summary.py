"""PRD-068 策略模板依赖的 Policy 审计摘要安全回归测试。"""

from __future__ import annotations

import pytest
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit.service import (
    SENSITIVE_FIELD_NAMES,
    _validate_summary,
)


def test_policy_dsl_variable_names_are_the_only_variables_exception() -> None:
    """Policy 可记录固定 DSL 名称，但全局敏感字段集合仍必须保护 Prompt 变量。"""

    assert "variables" in SENSITIVE_FIELD_NAMES
    assert _validate_summary(
        "policy",
        {"variables": "budget_remaining,env"},
    ) == {"variables": "budget_remaining,env"}

    unsafe_values: tuple[object, ...] = (
        "customer_name",
        "env,env",
        "env,budget_remaining",
        "env,customer_name",
        "",
        ["env"],
    )
    for value in unsafe_values:
        with pytest.raises(ApiError) as error:
            _validate_summary("policy", {"variables": value})
        assert error.value.code == "audit_log.summary_invalid"

    # Prompt 模板对象即使只使用看似安全的 DSL 名称，也不能复用 Policy 的领域例外。
    with pytest.raises(ApiError) as error:
        _validate_summary("prompt_template", {"variables": "env"})
    assert error.value.code == "audit_log.summary_invalid"
