"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/providers/factory.py
"""

from __future__ import annotations

from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    ProviderAdapterSpecError,
    validate_provider_auth,
)

from metahub_gateway.routing.models import GatewayRoute

from .base import ProviderAdapter
from .generic_http import GenericHttpConfigAdapter
from .openai_compatible import OpenAICompatibleAdapter


# 本地执行类映射只接受 Shared 目录声明的稳定名称，不能再次维护 Provider 类型白名单。
_ADAPTER_TYPES = {
    "openai_compatible": OpenAICompatibleAdapter,
    "generic_http": GenericHttpConfigAdapter,
}


def adapter_for_route(route: GatewayRoute) -> ProviderAdapter:
    """按共享 spec 为可信路由选择且校验 Adapter。

    参数：Control 已持久化并由 Gateway 按 tenant 解析的路由快照。返回：与 spec 名称和版本
    完全一致的 Adapter。未知类型、鉴权不兼容或制品漂移都必须在 Key 选择和网络 I/O 前失败，
    绝不能回退到通用 HTTP Adapter。
    """

    try:
        spec = validate_provider_auth(route.provider_type, route.provider_auth_type)
    # Shared 领域异常只携带固定 code/reason，这里仍转换为数据面专用错误避免职责泄漏。
    except ProviderAdapterSpecError as exc:
        # Gateway 不暴露持久化脏值；缺 spec 与不支持的鉴权都收敛为固定数据面错误。
        code = (
            "gateway.provider_adapter_spec_missing"
            if exc.code == "provider.adapter_spec_missing"
            else "gateway.provider_adapter_spec_invalid"
        )
        # 不保留异常链，避免未来 Shared 诊断字段被调试中间件意外序列化到公开响应。
        raise ApiError(code) from None
    adapter_type = _ADAPTER_TYPES.get(spec.adapter_name)
    if adapter_type is None:
        # 目录若声明当前二进制未知的 Adapter，继续执行会把请求发往错误协议。
        raise ApiError("gateway.provider_adapter_spec_invalid")
    adapter = adapter_type()
    if adapter.name != spec.adapter_name or adapter.version != spec.adapter_version:
        # 版本漂移意味着页面/激活事实与实际执行代码不同，必须等待一致制品部署。
        raise ApiError("gateway.provider_adapter_spec_invalid")
    return adapter
