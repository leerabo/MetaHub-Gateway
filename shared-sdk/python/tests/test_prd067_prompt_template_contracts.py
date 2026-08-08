"""PRD-067 Prompt 模板共享解析、渲染与独立密文专项测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from metahub_shared.prompt_templates import (
    PromptTemplateContractError,
    PromptTemplateDefinitionUnavailable,
    PromptTemplateOwner,
    PromptTemplateSecretUnavailable,
    canonical_definition_bytes,
    change_summary_hmac,
    decrypt_definition,
    encrypt_definition,
    parse_definition_bytes,
    prompt_template_intent_hmac,
    render_prompt,
    validate_definition,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label

# 测试根密钥只在进程内使用，不能复用到开发库、运行证据或生产配置。
SECRET = "prd067-test-only-prompt-template-secret-0123456789abcdef"


def _definition():
    """构造包含必填与可选变量的最小合法 definition。"""

    return validate_definition(
        content="请为 ${customer_name} 总结：${topic}",
        variables=[
            {
                "name": "customer_name",
                "label": "客户名称",
                "required": True,
                "max_chars": 80,
            },
            {
                "name": "topic",
                "label": "主题",
                "required": False,
                "max_chars": 200,
            },
        ],
        change_summary="初始版本",
    )


def test_definition_canonical_round_trip_and_repr_hide_plaintext() -> None:
    """规范字节往返必须稳定，dataclass repr 不得复制正文或 schema。"""

    definition = _definition()
    canonical = canonical_definition_bytes(definition)
    restored = parse_definition_bytes(canonical)

    assert canonical_definition_bytes(restored) == canonical
    assert "customer_name" not in repr(definition)
    assert "初始版本" not in repr(definition)
    assert json.loads(canonical)["change_summary"] == "初始版本"


@pytest.mark.parametrize(
    ("content", "variables"),
    (
        ("$name", [{"name": "name", "label": "名称", "required": True, "max_chars": 8}]),
        ("$$", []),
        ("${name.foo}", [{"name": "name", "label": "名称", "required": True, "max_chars": 8}]),
        ("{{name}}", [{"name": "name", "label": "名称", "required": True, "max_chars": 8}]),
        (
            "${__proto__}",
            [{"name": "__proto__", "label": "名称", "required": True, "max_chars": 8}],
        ),
        ("无变量", [{"name": "unused", "label": "名称", "required": True, "max_chars": 8}]),
    ),
)
def test_definition_rejects_expressions_reserved_keys_and_non_exact_schema(
    content: str,
    variables: list[dict[str, object]],
) -> None:
    """验证 v1 拒绝表达式、保留键和非 exact schema，只接受受限 braced 变量语法。"""

    with pytest.raises(PromptTemplateContractError):
        validate_definition(
            content=content,
            variables=variables,
            change_summary="测试",
        )


def test_render_requires_exact_string_values_and_enforces_required() -> None:
    """变量缺失、额外、非字符串与必填空白统一返回变量错误。"""

    definition = _definition()
    assert render_prompt(
        definition,
        {"customer_name": "华东团队", "topic": "本月用量"},
    ) == "请为 华东团队 总结：本月用量"
    for values in (
        {"customer_name": "华东团队"},
        {"customer_name": "华东团队", "topic": "本月", "extra": "x"},
        {"customer_name": 1, "topic": "本月"},
        {"customer_name": "  ", "topic": "本月"},
    ):
        with pytest.raises(
            PromptTemplateContractError,
            match="prompt_template.variables_invalid",
        ):
            render_prompt(definition, values)


def test_cipher_round_trip_binds_full_owner_aad() -> None:
    """验证密文只能由完全相同 tenant/template/version/number owner AAD 解密。"""

    canonical = canonical_definition_bytes(_definition())
    owner = PromptTemplateOwner(
        tenant_id="tenant_a",
        prompt_template_id="ptpl_a",
        prompt_template_version_id="ptv_a",
        version_number=1,
    )
    encrypted = encrypt_definition(_definition(), secret=SECRET, owner=owner)
    assert "华东" not in encrypted
    assert canonical_definition_bytes(
        decrypt_definition(encrypted, secret=SECRET, owner=owner)
    ) == canonical

    for changed in (
        PromptTemplateOwner("tenant_b", "ptpl_a", "ptv_a", 1),
        PromptTemplateOwner("tenant_a", "ptpl_b", "ptv_a", 1),
        PromptTemplateOwner("tenant_a", "ptpl_a", "ptv_b", 1),
        PromptTemplateOwner("tenant_a", "ptpl_a", "ptv_a", 2),
    ):
        with pytest.raises(PromptTemplateDefinitionUnavailable):
            decrypt_definition(encrypted, secret=SECRET, owner=changed)


def test_cipher_missing_secret_and_tampering_use_fixed_safe_errors() -> None:
    """配置缺失与认证失败语义分离，异常文本不包含密文。"""

    owner = PromptTemplateOwner("tenant_a", "ptpl_a", "ptv_a", 1)
    with pytest.raises(PromptTemplateSecretUnavailable, match="prompt_template.unavailable"):
        encrypt_definition(_definition(), secret=None, owner=owner)
    encrypted = encrypt_definition(_definition(), secret=SECRET, owner=owner)
    tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")
    with pytest.raises(PromptTemplateDefinitionUnavailable) as caught:
        decrypt_definition(tampered, secret=SECRET, owner=owner)
    assert tampered not in str(caught.value)


def test_domain_hmacs_are_stable_and_not_interchangeable() -> None:
    """幂等与 change summary 使用不同派生域，输入相同也不能得到同一摘要。"""

    intent = {"operation": "create", "change_summary": "版本说明"}
    first = prompt_template_intent_hmac(SECRET, intent)
    second = prompt_template_intent_hmac(SECRET, intent)
    summary = change_summary_hmac(SECRET, "版本说明")

    assert first == second
    assert len(first) == len(summary) == 64
    assert first != summary


def test_runtime_config_keeps_prompt_secret_optional_private_and_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模板密钥缺失不拖垮旧能力，显式值不进入 repr/摘要且不能复用 Auth 密钥。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.delenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", raising=False)
    missing = load_app_config("control-plane-api")
    assert missing.prompt_template_encryption_secret is None

    monkeypatch.setenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", SECRET)
    configured = load_app_config("gateway-data-plane")
    assert configured.prompt_template_encryption_secret == SECRET
    assert "prompt_template_encryption_secret" not in configured.model_dump()
    assert SECRET not in repr(configured)
    assert SECRET not in "\n".join(item.model_dump_json() for item in configured.config_items)

    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", SECRET)
    with pytest.raises(ConfigError, match="must be different"):
        load_app_config("gateway-data-plane")


def test_prompt_template_error_catalog_has_stable_statuses_and_labels() -> None:
    """管理、调用和依赖错误必须共享固定 HTTP 状态与双语安全标签。"""

    expected = {
        "prompt_template.not_found": 404,
        "prompt_template.request_invalid": 422,
        "prompt_template.definition_invalid": 422,
        "prompt_template.binding_not_found": 404,
        "prompt_template.variables_invalid": 422,
        "prompt_template.name_conflict": 409,
        "prompt_template.revision_conflict": 409,
        "prompt_template.idempotency_conflict": 409,
        "prompt_template.disabled": 409,
        "prompt_template.version_conflict": 409,
        "prompt_template.not_applicable": 409,
        "prompt_template.definition_unavailable": 500,
        "prompt_template.integrity_failed": 500,
        "prompt_template.unavailable": 503,
    }
    assert {code: ERROR_SPECS[code].http_status for code in expected} == expected
    for code in expected:
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
