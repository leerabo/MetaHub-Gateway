"""PRD-063 Shared Provider Adapter 冻结目录与失败关闭专项测试。

测试只构造公开合同和固定假值，不使用真实 Provider Key、Token、Prompt、Response 或客户
Endpoint；篡改用例直接调用纯校验器，不执行数据库、网络或跨租户操作。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import cast

import metahub_shared.runtime.provider_adapter_spec as registry
import pytest
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_CANONICAL_BYTE_LENGTH,
    CATALOG_CANONICAL_BYTES,
    CATALOG_HASH,
    CATALOG_VERSION,
    CUSTOMER_MANAGED,
    DOMESTIC_PROVIDER_TYPES,
    EXPECTED_PROVIDER_TYPES,
    GENERIC_ADAPTER,
    LOCAL_COMPATIBLE,
    MIGRATION_PLATFORM,
    MODEL_VENDOR_CLOUD,
    OPENAI_ADAPTER,
    OPTIONAL,
    PRICE_FIELDS,
    ProviderAdapterCatalogIdentity,
    ProviderAdapterSpec,
    ProviderAdapterSpecError,
    ProviderErrorProfile,
    ProviderStreamProfile,
    ProviderUsageProfile,
    catalog_identity,
    provider_adapter_spec,
    provider_adapter_specs,
    provider_types,
    validate_provider_auth,
)

# 固定摘要必须以字面量进入测试，避免实现和断言同时动态计算后掩盖目录漂移。
EXPECTED_CANONICAL_BYTE_LENGTH = 15_151
EXPECTED_CATALOG_HASH = "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf"


def _assert_catalog_error(
    specs: object,
    *,
    reason: str,
) -> ProviderAdapterSpecError:
    """执行篡改目录校验并断言固定错误码、原因和安全异常正文。"""

    with pytest.raises(ProviderAdapterSpecError) as captured:
        registry._validate_catalog(specs)
    assert captured.value.code == "adapter.spec_catalog_invalid"
    assert captured.value.reason == reason
    assert str(captured.value) == "Provider adapter spec catalog is invalid."
    return captured.value


def _replace_spec(
    provider_type: str,
    **changes: object,
) -> tuple[ProviderAdapterSpec, ...]:
    """复制十类目录并仅替换目标 spec，供失败关闭测试构造单一漂移。"""

    return tuple(
        dataclasses.replace(spec, **changes) if spec.provider_type == provider_type else spec
        for spec in provider_adapter_specs()
    )


def test_catalog_identity_freezes_version_length_hash_and_order() -> None:
    """主流程：版本、十类数量、UTF-8 长度、SHA-256 与稳定排序必须精确一致。"""

    specs = provider_adapter_specs()
    identity = catalog_identity()

    assert isinstance(identity, ProviderAdapterCatalogIdentity)
    assert identity.version == identity.catalog_version == CATALOG_VERSION == "adapter-spec-v2"
    assert identity.spec_count == len(specs) == 10
    assert identity.canonical_byte_length == CATALOG_CANONICAL_BYTE_LENGTH
    assert CATALOG_CANONICAL_BYTE_LENGTH == EXPECTED_CANONICAL_BYTE_LENGTH
    assert CATALOG_HASH == EXPECTED_CATALOG_HASH
    assert hashlib.sha256(CATALOG_CANONICAL_BYTES).hexdigest() == EXPECTED_CATALOG_HASH
    assert len(CATALOG_CANONICAL_BYTES) == EXPECTED_CANONICAL_BYTE_LENGTH
    assert tuple(spec.provider_type for spec in specs) == tuple(sorted(EXPECTED_PROVIDER_TYPES))


def test_catalog_bytes_are_canonical_utf8_json_with_nested_profiles() -> None:
    """序列化合同：canonical JSON 无环境空白，中文保留 UTF-8，子 profile 为完整对象。"""

    decoded = json.loads(CATALOG_CANONICAL_BYTES)
    reencoded = json.dumps(
        decoded,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert reencoded == CATALOG_CANONICAL_BYTES
    assert decoded["catalog_version"] == CATALOG_VERSION
    assert [item["provider_type"] for item in decoded["specs"]] == sorted(
        EXPECTED_PROVIDER_TYPES
    )
    assert all(isinstance(item["usage_profile"], dict) for item in decoded["specs"])
    assert all(isinstance(item["error_profile"], dict) for item in decoded["specs"])
    assert all(isinstance(item["stream_profile"], dict) for item in decoded["specs"])


def test_catalog_and_all_nested_profiles_are_deeply_frozen() -> None:
    """不可变合同：spec、usage、error、stream 及返回容器均不能被调用方原地修改。"""

    spec = provider_adapter_spec("deepseek")
    assert isinstance(spec.usage_profile, ProviderUsageProfile)
    assert isinstance(spec.error_profile, ProviderErrorProfile)
    assert isinstance(spec.stream_profile, ProviderStreamProfile)

    for value, field_name, replacement in (
        (spec, "adapter_version", "2.0.0"),
        (spec.usage_profile, "profile_id", "changed"),
        (spec.error_profile, "tenant_overlay_enabled", False),
        (spec.stream_profile, "allow_include_usage", False),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field_name, replacement)

    wire = spec.to_wire()
    cast(list[str], wire["allowed_auth_types"]).append("custom_header")
    usage_wire = cast(dict[str, object], wire["usage_profile"])
    cast(list[str], usage_wire["root_paths"]).append("private")
    assert spec.allowed_auth_types == ("bearer",)
    assert "private" not in spec.usage_profile.root_paths


def test_domestic_and_migration_classifications_are_not_conflated() -> None:
    """产品边界：六类国内上游完整，竞品名称只属于 optional migration examples。"""

    domestic = provider_types(classification=MODEL_VENDOR_CLOUD)
    migration = provider_types(classification=MIGRATION_PLATFORM)

    assert domestic == DOMESTIC_PROVIDER_TYPES
    assert len(domestic) == 6
    assert migration == frozenset({"migration_compatible"})
    migration_spec = provider_adapter_spec("migration_compatible")
    assert migration_spec.status == OPTIONAL
    assert migration_spec.platform_examples == ("OpenRouter", "硅基流动")
    assert "openrouter" not in EXPECTED_PROVIDER_TYPES
    assert "siliconflow" not in EXPECTED_PROVIDER_TYPES
    assert "silicon_flow" not in EXPECTED_PROVIDER_TYPES
    vendor_wire = json.dumps(
        [provider_adapter_spec(item).to_wire() for item in sorted(domestic)],
        ensure_ascii=False,
    )
    assert "OpenRouter" not in vendor_wire
    assert "硅基流动" not in vendor_wire


def test_customer_endpoint_classifications_remain_separate() -> None:
    """分类边界：本地兼容与客户自管 Endpoint 不得混入国内厂家或迁移平台。"""

    assert provider_types(classification=LOCAL_COMPATIBLE) == frozenset(
        {"openai_compatible", "local_endpoint"}
    )
    assert provider_types(classification=CUSTOMER_MANAGED) == frozenset({"custom"})
    assert provider_adapter_spec("openai_compatible").adapter_name == OPENAI_ADAPTER
    assert provider_adapter_spec("custom").adapter_name == GENERIC_ADAPTER


@pytest.mark.parametrize("provider_type", sorted(EXPECTED_PROVIDER_TYPES))
def test_every_allowed_provider_has_a_complete_minimum_contract(provider_type: str) -> None:
    """最小契约：每个允许类型都具备 auth/model/usage/error/stream/price/case 字段。"""

    spec = provider_adapter_spec(provider_type)

    assert isinstance(spec, ProviderAdapterSpec)
    assert spec.provider_type == provider_type
    assert spec.spec_id == f"adapter-spec-{provider_type}-v1"
    assert spec.adapter_version == "1.0.0"
    assert spec.allowed_auth_types
    assert spec.model_id_strategy
    assert spec.usage_profile.profile_id == spec.usage_profile_id
    assert spec.usage_profile.root_paths == (
        "usage",
        "usage_metadata",
        "usageMetadata",
        "output.usage",
    )
    assert spec.usage_profile.input_token_fields
    assert spec.usage_profile.output_token_fields
    assert spec.usage_profile.cache_token_fields
    assert spec.usage_profile.total_token_fields
    assert spec.error_profile.profile_id == spec.error_profile_id == "provider_error_map_v1"
    assert spec.error_profile.tenant_overlay_enabled is True
    assert spec.error_profile.request_lease_required is True
    assert spec.error_profile.safe_classification_required is True
    assert spec.stream_profile.profile_id == spec.stream_profile_id
    assert spec.stream_include_usage == spec.stream_profile.allow_include_usage
    assert spec.price_fields == PRICE_FIELDS
    expected_case = (
        "prd084_local_endpoint_minimum_contract"
        if provider_type == "local_endpoint"
        else f"prd063_{provider_type}_minimum_contract"
    )
    assert spec.contract_case_id == expected_case


def test_execution_profiles_match_openai_and_generic_boundaries() -> None:
    """执行合同：OpenAI 路径才注入 final usage，通用路径不猜测私有 SSE 行为。"""

    deepseek = provider_adapter_spec("deepseek")
    custom = provider_adapter_spec("custom")

    assert deepseek.adapter_name == OPENAI_ADAPTER
    assert deepseek.stream_profile.sse_dialect == "openai_sse"
    assert deepseek.stream_profile.termination_marker == "[DONE]"
    assert deepseek.stream_profile.allow_include_usage is True
    assert custom.adapter_name == GENERIC_ADAPTER
    assert custom.stream_profile.sse_dialect == "generic_json_sse"
    assert custom.stream_profile.termination_marker is None
    assert custom.stream_profile.allow_include_usage is False


def test_auth_contract_accepts_only_declared_combinations() -> None:
    """鉴权主流程：国内厂家只用 Bearer，本地兼容和 custom 可显式选择 none。"""

    assert validate_provider_auth("DeepSeek", "BEARER").provider_type == "deepseek"
    assert validate_provider_auth(" openai_compatible ", "none").provider_type == (
        "openai_compatible"
    )
    assert validate_provider_auth("custom", "bearer").provider_type == "custom"
    assert validate_provider_auth("local_endpoint", "none").provider_type == "local_endpoint"


@pytest.mark.parametrize(
    ("provider_type", "auth_type", "code", "reason"),
    [
        ("custom", "custom_header", "provider.auth_not_supported", "auth_not_supported"),
        ("deepseek", "none", "provider.auth_not_supported", "auth_not_supported"),
        ("openrouter", "bearer", "provider.adapter_spec_missing", "provider_type_missing"),
        ("siliconflow", "bearer", "provider.adapter_spec_missing", "provider_type_missing"),
        ("silicon_flow", "bearer", "provider.adapter_spec_missing", "provider_type_missing"),
    ],
)
def test_auth_contract_fails_closed_without_echo(
    provider_type: object,
    auth_type: object,
    code: str,
    reason: str,
) -> None:
    """鉴权异常：历史 custom_header 和竞品别名均在 I/O 前拒绝且不回显输入。"""

    with pytest.raises(ProviderAdapterSpecError) as captured:
        validate_provider_auth(provider_type, auth_type)

    assert captured.value.code == code
    assert captured.value.reason == reason
    assert str(provider_type) not in str(captured.value)
    assert str(auth_type) not in str(captured.value)


@pytest.mark.parametrize("value", [None, "", "../private", "OPENROUTER\nsecret", "x" * 65])
def test_invalid_lookup_returns_fixed_domain_error_without_echo(value: object) -> None:
    """查询异常：非文本、越界、控制字符和未知类型均使用固定 spec missing 语义。"""

    with pytest.raises(ProviderAdapterSpecError) as captured:
        provider_adapter_spec(value)

    assert captured.value.code == "provider.adapter_spec_missing"
    assert captured.value.reason == "provider_type_missing"
    # 空字符串天然属于任何正文，只有非空原值才有可验证的“不回显”语义。
    if str(value):
        assert str(value) not in str(captured.value)


def test_provider_type_filter_is_immutable_and_rejects_unknown_values() -> None:
    """过滤合同：公共集合不可变，未知 classification 使用固定 422 可映射领域码。"""

    assert isinstance(provider_types(), frozenset)
    with pytest.raises(AttributeError):
        provider_types().add("openrouter")  # type: ignore[attr-defined]

    with pytest.raises(ProviderAdapterSpecError) as captured:
        provider_types(classification="vendor\nprivate")
    assert captured.value.code == "adapter.spec_filter_invalid"
    assert captured.value.reason == "classification_invalid"
    assert "vendor" not in str(captured.value)


def test_duplicate_type_spec_id_and_contract_id_fail_closed() -> None:
    """唯一性异常：type、spec ID 与 contract case ID 任一重复均阻断目录导入。"""

    specs = provider_adapter_specs()
    duplicate_type = list(specs)
    duplicate_type[1] = dataclasses.replace(
        duplicate_type[1],
        provider_type=duplicate_type[0].provider_type,
    )
    _assert_catalog_error(tuple(duplicate_type), reason="provider_type_duplicate")

    _assert_catalog_error(
        _replace_spec("deepseek", spec_id=provider_adapter_spec("custom").spec_id),
        reason="spec_id_duplicate",
    )
    _assert_catalog_error(
        _replace_spec(
            "deepseek",
            contract_case_id=provider_adapter_spec("custom").contract_case_id,
        ),
        reason="contract_case_duplicate",
    )


@pytest.mark.parametrize(
    ("field_name", "value", "reason"),
    [
        ("classification", "unknown_vendor", "classification_unknown"),
        ("adapter_name", "native_magic", "adapter_name_unknown"),
        ("protocol_family", "responses_v2", "protocol_family_unknown"),
        ("model_id_strategy", "tenant_expression", "model_id_strategy_unknown"),
        ("probe_profile", "private_probe", "probe_profile_unknown"),
        ("status", "recommended", "status_unknown"),
    ],
)
def test_unknown_catalog_enums_fail_closed(
    field_name: str,
    value: object,
    reason: str,
) -> None:
    """枚举异常：任何未知执行字段都不能借合法 Provider 类型进入目录。"""

    _assert_catalog_error(
        _replace_spec("deepseek", **{field_name: value}),
        reason=reason,
    )


@pytest.mark.parametrize("display_name", ["厂家\n注入", "DeepSeek\x7f隐藏", "e\u0301"])
def test_control_or_noncanonical_text_fails_closed(display_name: str) -> None:
    """文本异常：控制字符、DEL 和非 NFC 文本不得进入 JSON、日志或 Console。"""

    _assert_catalog_error(
        _replace_spec("deepseek", display_name=display_name),
        reason="display_name_invalid",
    )


def test_auth_header_and_order_are_strictly_versioned() -> None:
    """Header 异常：custom_header、乱序 auth 与非法 Header 均不能被解释为 Bearer。"""

    _assert_catalog_error(
        _replace_spec("custom", allowed_auth_types=("none", "custom_header")),
        reason="auth_types_invalid",
    )
    _assert_catalog_error(
        _replace_spec("custom", allowed_auth_types=("bearer", "none")),
        reason="auth_types_invalid",
    )
    _assert_catalog_error(
        _replace_spec("deepseek", credential_header="X API Key"),
        reason="credential_profile_invalid",
    )


def test_nested_profile_and_price_drift_fail_closed() -> None:
    """子结构异常：usage/error/stream/price 任一漂移均不能发布为同一 catalog 版本。"""

    deepseek = provider_adapter_spec("deepseek")
    custom = provider_adapter_spec("custom")
    _assert_catalog_error(
        _replace_spec(
            "deepseek",
            usage_profile=dataclasses.replace(
                deepseek.usage_profile,
                root_paths=("usage", "private_usage"),
            ),
        ),
        reason="usage_profile_invalid",
    )
    _assert_catalog_error(
        _replace_spec(
            "deepseek",
            error_profile=dataclasses.replace(
                deepseek.error_profile,
                request_lease_required=False,
            ),
        ),
        reason="error_profile_invalid",
    )
    _assert_catalog_error(
        _replace_spec(
            "custom",
            stream_profile=dataclasses.replace(
                custom.stream_profile,
                allow_include_usage=True,
            ),
        ),
        reason="stream_profile_invalid",
    )
    _assert_catalog_error(
        _replace_spec("deepseek", price_fields=("currency",)),
        reason="price_fields_invalid",
    )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        {"api_key": "fixed"},
        {"display_name": "https://private.invalid/v1"},
        {"display_name": "Bearer sk-sensitivevalue"},
    ],
)
def test_secret_like_field_or_value_is_rejected(unsafe_value: dict[str, object]) -> None:
    """隐私异常：秘密类字段、真实 URL 形态和疑似凭据值在目录发布前失败关闭。"""

    with pytest.raises(ProviderAdapterSpecError) as captured:
        registry._validate_public_wire(unsafe_value)
    assert captured.value.code == "adapter.spec_catalog_invalid"
    assert captured.value.reason in {
        "sensitive_field_forbidden",
        "sensitive_value_forbidden",
    }
    assert all(str(value) not in str(captured.value) for value in unsafe_value.values())


def test_public_repr_and_wire_do_not_contain_endpoint_or_credential_values() -> None:
    """公开 DTO：repr/canonical/wire 只能含固定 Header 元数据，不能含 URL 或秘密 canary。"""

    public_text = repr(provider_adapter_specs()) + CATALOG_CANONICAL_BYTES.decode("utf-8")
    lowered = public_text.lower()

    assert "https://" not in lowered
    assert "sk-sensitivevalue" not in lowered
    assert "bearer sk-" not in lowered
    assert "endpoint_url" not in lowered
    assert "base_url" not in lowered
    assert "credential_value" not in lowered


def test_catalog_length_and_hash_drift_have_distinct_fixed_reasons() -> None:
    """身份异常：长度漂移与同长内容漂移均在导入期阻断并给出固定安全原因。"""

    with pytest.raises(ProviderAdapterSpecError) as length_error:
        registry._assert_catalog_identity(
            CATALOG_CANONICAL_BYTES,
            expected_length=EXPECTED_CANONICAL_BYTE_LENGTH + 1,
            expected_hash=EXPECTED_CATALOG_HASH,
        )
    assert length_error.value.reason == "catalog_length_drift"

    with pytest.raises(ProviderAdapterSpecError) as hash_error:
        registry._assert_catalog_identity(
            CATALOG_CANONICAL_BYTES,
            expected_length=EXPECTED_CANONICAL_BYTE_LENGTH,
            expected_hash="0" * 64,
        )
    assert hash_error.value.reason == "catalog_hash_drift"
    assert EXPECTED_CATALOG_HASH not in str(hash_error.value)
