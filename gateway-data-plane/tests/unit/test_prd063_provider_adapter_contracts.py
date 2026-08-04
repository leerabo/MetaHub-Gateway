"""PRD-063 Gateway Provider Adapter 最小合同测试。

本文件只验证九类冻结 spec 与数据面请求构造的一致性；PRD-064 的通用契约执行框架、
公网厂商探测和 CI 扩展不在这里提前实现。测试使用内存路由和假 Key，不访问真实 Endpoint，
也不把 Provider Key、Prompt 或 Response 写入日志和断言输出。
"""

from __future__ import annotations

import pytest
from metahub_gateway.providers import factory as factory_module
from metahub_gateway.providers import openai_compatible as openai_compatible_module
from metahub_gateway.providers.base import ProviderRequest
from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.providers.generic_http import GenericHttpConfigAdapter
from metahub_gateway.providers.keypool import SelectedProviderKey
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    DOMESTIC_PROVIDER_TYPES,
    provider_adapter_spec,
    provider_adapter_specs,
)


def _route(
    provider_type: str,
    *,
    auth_type: str = "bearer",
) -> GatewayRoute:
    """构造只含可信租户和脱敏 Provider 元数据的 Gateway 路由快照。"""

    return GatewayRoute(
        tenant_id="tenant_prd063",
        model_id="model_prd063",
        model_alias="internal-chat",
        upstream_model_name="provider-chat-model",
        capabilities={"chat": True, "stream": True},
        provider_id="provider_prd063",
        provider_code="provider_contract",
        provider_type=provider_type,
        provider_auth_type=auth_type,
        provider_name="Provider Contract Fixture",
        provider_base_url="http://provider.test/v1",
    )


def _selected_key() -> SelectedProviderKey:
    """返回仅在内存构造 Header 使用、repr 永不暴露明文的测试 Key。"""

    return SelectedProviderKey(
        "credential-prd063",
        "prd063-provider-secret",
        "test",
        "prd063-prefix",
    )


def test_provider_request_preserves_legacy_positional_field_order() -> None:
    """兼容流程：新增版本字段不能占用旧 database_url 等位置参数的槽位。"""

    request = ProviderRequest(
        "provider_prd063",
        "provider_contract",
        "openai_compatible",
        "openai_compatible",
        "chat/completions",
        "http://provider.test/v1/chat/completions",
        {},
        {"model": "provider-chat-model"},
        "tenant_prd063",
        "model_prd063",
        "sqlite:///contract.db",
    )

    assert request.database_url == "sqlite:///contract.db"
    assert request.adapter_version == ""
    assert request.adapter_catalog_hash == ""


@pytest.mark.parametrize(
    ("provider_type", "adapter_type"),
    [
        (spec.provider_type, OpenAICompatibleAdapter)
        if spec.adapter_name == "openai_compatible"
        else (spec.provider_type, GenericHttpConfigAdapter)
        for spec in provider_adapter_specs()
    ],
)
def test_factory_matches_every_frozen_spec(
    provider_type: str,
    adapter_type: type[OpenAICompatibleAdapter],
) -> None:
    """主流程：九类 Provider 都选择与共享 spec 名称和版本完全一致的 Adapter。"""

    adapter = adapter_for_route(_route(provider_type))
    spec = provider_adapter_spec(provider_type)

    assert isinstance(adapter, adapter_type)
    assert adapter.name == spec.adapter_name
    assert adapter.version == spec.adapter_version


@pytest.mark.parametrize(
    "provider_type",
    ["unknown", "openrouter", "siliconflow", "silicon_flow", ""],
)
def test_factory_rejects_unregistered_or_competitor_types(provider_type: str) -> None:
    """异常流程：未知类型和竞品平台名称不能回退到通用 Adapter。"""

    with pytest.raises(ApiError) as caught:
        adapter_for_route(_route(provider_type))

    assert caught.value.code == "gateway.provider_adapter_spec_missing"


def test_factory_rejects_unsupported_auth_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全流程：历史 custom_header 在实例化 Adapter 和选择 Key 前失败关闭。"""

    def reject_construction() -> GenericHttpConfigAdapter:
        """若门禁顺序回归到先构造 Adapter，就让测试立即失败。"""

        # 该哨兵只证明执行顺序，不携带 Provider、Endpoint 或测试 Key 等敏感正文。
        raise AssertionError("adapter must not be constructed before auth validation")

    monkeypatch.setitem(factory_module._ADAPTER_TYPES, "generic_http", reject_construction)

    with pytest.raises(ApiError) as caught:
        adapter_for_route(_route("custom", auth_type="custom_header"))

    assert caught.value.code == "gateway.provider_adapter_spec_invalid"


@pytest.mark.parametrize("provider_type", sorted(DOMESTIC_PROVIDER_TYPES))
def test_domestic_vendor_request_binds_model_bearer_version_and_stream_contract(
    provider_type: str,
) -> None:
    """主流程：六类国内上游逐一绑定模型、Bearer、版本和 final usage 选项。"""

    payload = {
        "model": "internal-chat",
        "messages": [{"role": "user", "content": "contract-fixture"}],
        "stream": True,
        "stream_options": {"client_option": True},
    }
    adapter = adapter_for_route(_route(provider_type))
    request = adapter.build_request(
        _route(provider_type),
        "chat/completions",
        payload,
        selected_key=_selected_key(),
    )

    assert request.payload["model"] == "provider-chat-model"
    assert request.payload["stream_options"] == {
        "client_option": True,
        "include_usage": True,
    }
    assert request.headers["Authorization"] == "Bearer prd063-provider-secret"
    assert request.adapter_name == provider_adapter_spec(provider_type).adapter_name
    assert request.adapter_version == provider_adapter_spec(provider_type).adapter_version
    assert request.adapter_catalog_hash == CATALOG_HASH
    # Adapter 必须复制请求和嵌套 stream options，不能把网关私有选项写回客户端对象。
    assert payload["model"] == "internal-chat"
    assert payload["stream_options"] == {"client_option": True}


def test_local_compatible_none_auth_discards_accidentally_supplied_key() -> None:
    """安全流程：none 鉴权即使误传 Key，也不能生成 Header 或保留凭据关联。"""

    adapter = adapter_for_route(_route("openai_compatible", auth_type="none"))
    request = adapter.build_request(
        _route("openai_compatible", auth_type="none"),
        "chat/completions",
        {"model": "internal-chat"},
        selected_key=_selected_key(),
    )

    assert request.headers == {"Content-Type": "application/json"}
    assert request.provider_credential_id is None
    assert request.provider_key_source is None
    rendered = repr(request)
    assert "prd063-provider-secret" not in rendered
    assert "provider.test" not in rendered


@pytest.mark.parametrize("auth_type", ["none", "bearer"])
def test_custom_generic_stream_does_not_inject_openai_private_option(auth_type: str) -> None:
    """兼容流程：通用 JSON/SSE 只保留客户端字段，不猜测 OpenAI include_usage。"""

    payload = {
        "model": "internal-chat",
        "stream": True,
        "stream_options": {"client_option": True},
    }
    adapter = adapter_for_route(_route("custom", auth_type=auth_type))
    request = adapter.build_request(
        _route("custom", auth_type=auth_type),
        "chat/completions",
        payload,
        selected_key=_selected_key() if auth_type == "bearer" else None,
    )

    assert isinstance(adapter, GenericHttpConfigAdapter)
    assert request.payload["stream_options"] == {"client_option": True}
    assert "include_usage" not in request.payload["stream_options"]
    assert payload["stream_options"] == {"client_option": True}
    # bearer 与 none 必须产生精确相反的 Header 结果，不能按 Key 是否存在进行模糊推断。
    if auth_type == "bearer":
        assert request.headers["Authorization"] == "Bearer prd063-provider-secret"
    else:
        assert "Authorization" not in request.headers


def test_direct_adapter_revalidates_auth_before_legacy_keypool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防绕过：兼容调用点直接实例化 Adapter 时也必须先拒绝 custom_header。"""

    def reject_key_selection(*_args: object, **_kwargs: object) -> SelectedProviderKey:
        """若不支持的鉴权先进入 KeyPool，就用断言证明执行顺序错误。"""

        # 自定义 Header 没有可信 profile，触发本哨兵即代表网关在失败关闭前错误访问 KeyPool。
        raise AssertionError("unsupported auth must not select a provider key")

    monkeypatch.setattr(
        openai_compatible_module,
        "_selected_provider_key",
        reject_key_selection,
    )

    with pytest.raises(ApiError) as caught:
        GenericHttpConfigAdapter().build_request(
            _route("custom", auth_type="custom_header"),
            "chat/completions",
            {"model": "internal-chat"},
        )

    assert caught.value.code == "gateway.provider_adapter_spec_invalid"


def test_adapter_version_drift_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常流程：执行类版本与目录版本漂移时不得返回可用 Adapter。"""

    monkeypatch.setattr(OpenAICompatibleAdapter, "version", "9.9.9")

    with pytest.raises(ApiError) as caught:
        adapter_for_route(_route("deepseek"))

    assert caught.value.code == "gateway.provider_adapter_spec_invalid"


def test_direct_wrong_adapter_name_fails_before_request_creation() -> None:
    """异常流程：直接用 OpenAI Adapter 执行 generic spec 时必须拒绝名称漂移。"""

    with pytest.raises(ApiError) as caught:
        OpenAICompatibleAdapter().build_request(
            _route("custom", auth_type="none"),
            "chat/completions",
            {"model": "internal-chat"},
        )

    assert caught.value.code == "gateway.provider_adapter_spec_invalid"


@pytest.mark.parametrize(
    "code",
    [
        "gateway.provider_adapter_spec_missing",
        "gateway.provider_adapter_spec_invalid",
    ],
)
def test_gateway_adapter_spec_errors_are_registered_as_traceable_503(code: str) -> None:
    """错误目录：两类数据面 spec 失败必须保留专用 code，并统一返回可重试 503。"""

    error = ApiError(code)

    assert error.code == code
    assert error.http_status == 503
    assert "adapter" in error.message.lower()
