"""PRD-062 配置迁移 AuditLog 动作级摘要合同测试。

本文件验证导入与导出字段互斥、完整计数、纯整数范围和 hash 格式；同一验证函数同时供写入与
存量解码调用，因此这些纯函数测试直接冻结两条路径的安全语义。
"""

from __future__ import annotations

import copy

import pytest

from metahub_control.audit.service import _expected_changed_fields


def _import_summary() -> dict[str, object]:
    """构造字段完整且计数关系合法的 config.imported 摘要。"""

    return {
        "schema_version": "metahub.config/v1",
        "manifest_sha256": "a" * 64,
        "providers_created_count": 1,
        "providers_unchanged_count": 0,
        "models_created_count": 2,
        "models_unchanged_count": 0,
        "aliases_created_count": 1,
        "aliases_unchanged_count": 0,
        "virtual_keys_created_count": 1,
        "virtual_keys_unchanged_count": 0,
        "generated_virtual_key_count": 1,
        "disabled_virtual_key_count": 0,
    }


def _export_summary() -> dict[str, object]:
    """构造字段完整且在集合与总量上限内的 config.exported 摘要。"""

    return {
        "schema_version": "metahub.config/v1",
        "manifest_sha256": "b" * 64,
        "providers_count": 1,
        "models_count": 2,
        "aliases_count": 1,
        "virtual_keys_count": 1,
    }


def test_config_transfer_audit_accepts_only_complete_action_specific_summaries() -> None:
    """合法导入和导出摘要返回各自动作的完整稳定 changed_fields。"""

    imported = _import_summary()
    exported = _export_summary()

    assert _expected_changed_fields("config.imported", "config_transfer", {}, imported) == tuple(
        sorted(imported)
    )
    assert _expected_changed_fields("config.exported", "config_transfer", {}, exported) == tuple(
        sorted(exported)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "mixed_action_field",
        "missing_field",
        "boolean_count",
        "negative_count",
        "bad_hash",
        "generated_mismatch",
        "total_overflow",
        "nonempty_before",
    ],
)
def test_config_imported_audit_rejects_malformed_or_mixed_facts(mutation: str) -> None:
    """导入事实拒绝字段混用、缺字段、伪整数、越界计数和伪造 before。"""

    summary = copy.deepcopy(_import_summary())
    before: dict[str, object] = {}
    if mutation == "mixed_action_field":
        summary["providers_count"] = 1
    elif mutation == "missing_field":
        summary.pop("models_created_count")
    elif mutation == "boolean_count":
        summary["providers_created_count"] = True
    elif mutation == "negative_count":
        summary["aliases_created_count"] = -1
    elif mutation == "bad_hash":
        summary["manifest_sha256"] = "G" * 64
    elif mutation == "generated_mismatch":
        summary["generated_virtual_key_count"] = 0
    elif mutation == "total_overflow":
        summary.update(
            {
                "providers_created_count": 500,
                "models_created_count": 2_000,
                "aliases_created_count": 2_000,
                "virtual_keys_created_count": 501,
                "generated_virtual_key_count": 501,
            }
        )
    else:
        before = {"schema_version": "metahub.config/v1"}

    with pytest.raises(ValueError):
        _expected_changed_fields("config.imported", "config_transfer", before, summary)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("providers_count", True),
        ("models_count", -1),
        ("aliases_count", 2_001),
        ("manifest_sha256", "short"),
    ],
)
def test_config_exported_audit_rejects_invalid_scalar_contract(field: str, value: object) -> None:
    """导出事实拒绝 bool、负数、集合越界和非规范 manifest hash。"""

    summary = _export_summary()
    summary[field] = value

    with pytest.raises(ValueError):
        _expected_changed_fields("config.exported", "config_transfer", {}, summary)
