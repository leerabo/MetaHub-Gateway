"""PRD-052 BudgetScope 稳定身份与优先级共享合同测试。"""

from __future__ import annotations

import pytest

from metahub_shared.runtime.budget_scopes import (
    BUDGET_SCOPE_PRIORITIES,
    budget_scope_priority,
    build_budget_scope_id,
)


def test_budget_scope_id_freezes_utf8_length_and_original_case() -> None:
    """中文、冒号和原始大小写必须按冻结的 UTF-8 length-prefix 算法生成稳定 ID。"""

    scope_id = build_budget_scope_id(
        tenant_id="租户甲",
        environment_id="env:prod",
        scope_type="project",
        scope_id="prj:账务",
    )

    assert scope_id == "bsc_20a59cc91e4c11e0cd5828aedb290fb4"
    assert scope_id != build_budget_scope_id(
        tenant_id="租户甲",
        environment_id="ENV:PROD",
        scope_type="project",
        scope_id="prj:账务",
    )


def test_budget_scope_id_length_prefix_prevents_field_boundary_collision() -> None:
    """字段中含冒号时也不能通过重新划分字段边界得到相同摘要。"""

    first = build_budget_scope_id(
        tenant_id="tenant:a",
        environment_id="env",
        scope_type="tenant",
        scope_id="tenant:a",
    )
    second = build_budget_scope_id(
        tenant_id="tenant",
        environment_id="a:env",
        scope_type="tenant",
        scope_id="tenant:a",
    )

    assert first != second


def test_budget_scope_priority_keeps_application_compatibility_order() -> None:
    """三级主维度仍需把既有 Application 固定在 Project 与 Key 之间。"""

    assert BUDGET_SCOPE_PRIORITIES == {
        "tenant": 0,
        "project": 1,
        "application": 2,
        "virtual_key": 3,
    }
    assert [budget_scope_priority(item) for item in BUDGET_SCOPE_PRIORITIES] == [0, 1, 2, 3]


@pytest.mark.parametrize(
    "arguments",
    [
        {"tenant_id": "", "environment_id": "env", "scope_type": "tenant", "scope_id": "t"},
        {"tenant_id": "t", "environment_id": "", "scope_type": "tenant", "scope_id": "t"},
        {"tenant_id": "t", "environment_id": "env", "scope_type": "department", "scope_id": "d"},
        {"tenant_id": "t", "environment_id": "env", "scope_type": "project", "scope_id": ""},
    ],
)
def test_budget_scope_id_rejects_incomplete_or_unknown_identity(arguments: dict[str, str]) -> None:
    """缺失可信身份或未支持层级时必须失败关闭，不能生成可误用的稳定对象 ID。"""

    with pytest.raises(ValueError, match="budget.scope_invalid"):
        build_budget_scope_id(**arguments)


def test_budget_scope_priority_rejects_unknown_type() -> None:
    """排序入口不得把未知 scope 静默降级成 Virtual Key。"""

    with pytest.raises(ValueError, match="budget.scope_invalid"):
        budget_scope_priority("department")
