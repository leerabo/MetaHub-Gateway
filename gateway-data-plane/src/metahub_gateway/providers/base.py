"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/providers/base.py
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_error_map import (
    ProviderErrorEvidence,
    ProviderErrorFacts,
    ProviderErrorMapLease,
)

if TYPE_CHECKING:
    from metahub_shared.runtime.config import AppConfig

    from metahub_gateway.cache_affinity.models import CacheAffinitySelectionHandle
    from metahub_gateway.providers.keypool import SelectedProviderKey
    from metahub_gateway.routing.models import GatewayRoute


@dataclass
class ProviderRequest:
    """标准化上游请求。

    Adapter 只接收当前租户已解析出的 route，不读取请求体里的租户字段；headers 中可能
    包含 Provider Key，日志和错误摘要必须避免输出 headers 原文。
    """

    provider_id: str
    provider_code: str
    provider_type: str
    adapter_name: str
    endpoint: str = field(repr=False)
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    payload: dict[str, Any] = field(repr=False)
    tenant_id: str
    model_id: str
    database_url: str | None = None
    provider_credential_id: str | None = field(default=None, repr=False)
    provider_key_source: str | None = None
    request_id: str = ""
    trace_id: str = ""
    cache_affinity_selection: CacheAffinitySelectionHandle | None = field(default=None, repr=False)
    # 流式握手超时随标准化请求传入 Adapter，测试和其他调用方可使用安全默认值。
    stream_connect_timeout_seconds: int = 30
    stream_write_timeout_seconds: int = 30
    stream_pool_timeout_seconds: int = 5
    # 冻结租约只含规则制品且 repr 隐藏，主调用、Fallback 和流握手必须复制同一对象引用。
    error_map_lease: ProviderErrorMapLease | None = field(default=None, repr=False)
    # 新字段放在旧默认字段末尾，既冻结执行版本，也保留外部 Adapter 的位置参数兼容顺序。
    adapter_version: str = ""
    # 目录 hash 与版本共同绑定本次网络请求，排障时可精确核对页面、迁移 seed 和执行制品。
    adapter_catalog_hash: str = ""


@dataclass(frozen=True)
class NormalizedUsage:
    """所有 Provider Adapter 必须输出的统一文本 Token 与成本结构。

    `cost_cny/provider_cost_amount` 表示 Provider 自报口径；MetaHub PricePlan 计算的内部
    成本由 Usage Ledger 后续写入，二者不能互相覆盖。`raw_usage` 只允许保存脱敏 usage。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    estimated: bool = False
    estimate_reason: str | None = None
    cost_cny: Decimal | None = None
    currency: str | None = None
    provider_cost_amount: Decimal | None = None
    normalization_warnings: tuple[str, ...] = ()
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def provider_raw_usage(self) -> dict[str, Any]:
        """以 PRD-032 标准名称返回脱敏 Provider usage，同时保留旧 `raw_usage` 构造兼容。"""

        return self.raw_usage

    def as_ledger_payload(self) -> dict[str, Any]:
        """把 Provider usage 转换为账本白名单字段。

        返回结构不携带 Prompt、Response、Header 或 Provider Key，并保留估算来源。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        return {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_tokens": self.cache_tokens,
            "estimated": self.estimated,
            "usage_source": "estimated" if self.estimated else "upstream",
            "estimate_reason": self.estimate_reason,
            "provider_cost_cny": str(self.cost_cny) if self.cost_cny is not None else None,
            "provider_cost_amount": str(self.provider_cost_amount)
            if self.provider_cost_amount is not None
            else None,
            "provider_cost_currency": self.currency,
            "normalization_warnings": list(self.normalization_warnings),
            "raw_usage": self.raw_usage,
        }


@dataclass(frozen=True)
class NormalizedResponse:
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    payload: dict[str, Any] = field(repr=False)
    usage: NormalizedUsage
    status_code: int


@dataclass(frozen=True)
class NormalizedProviderError:
    """封装 Provider 与 KeyPool 管理 的异常信息，向上层暴露统一错误码而不是底层异常细节。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    code: str
    message: str
    http_status: int | None
    retryable: bool
    # 兼容旧构造字段，但任意上游 type/code 不得进入 repr 或持久日志。
    provider_error_type: str = field(repr=False)
    error_category: str = "other"
    transport_kind: str = "none"
    # evidence 只保存脱敏事实；原始正文和匹配候选不会进入该对象。
    evidence: ProviderErrorEvidence | None = field(default=None, repr=False)


# 现有持久枚举中只有这三类能证明请求到达了可归因的上游失败边界。
QUALITY_OBSERVATION_ERROR_CATEGORIES = frozenset({"timeout", "rate_limited", "upstream_5xx"})
# URL/协议/本地 HTTP 编码错误由租户配置或 Adapter 产生，不能冒充 Provider 网络故障。
LOCAL_TRANSPORT_ERROR_TYPES = frozenset({"InvalidURL", "UnsupportedProtocol", "LocalProtocolError"})


def quality_observation_error(error: NormalizedProviderError) -> NormalizedProviderError:
    """返回供 attempt/session 持久化的质量归因错误，不修改客户端公开异常。

    旧 ``gateway.upstream_unavailable`` 同时覆盖真实网络传输失败和 InvalidURL 等本地配置
    故障，历史迁移只能保守排除。新请求把明确的非本地传输异常改写为专用内部错误码，
    使 down/up 能从持久事实精确重建；HTTP 细分码和本地类型均保持原诊断信息。
    """

    if (
        error.code == "gateway.upstream_unavailable"
        and error.provider_error_type not in LOCAL_TRANSPORT_ERROR_TYPES
    ):
        return replace(error, code="gateway.upstream_transport_failed")
    return error


def is_quality_observation_eligible_error(error: NormalizedProviderError | None) -> bool:
    """仅允许超时、429、5xx 和明确网络传输失败形成模型质量错误样本。

    上游鉴权、配额、模型映射、请求参数以及本地 URL/Adapter 错误虽然仍可追踪，
    但属于 Key、账号或配置治理问题，不能降低模型本身的质量评分。
    """

    # 未归一化异常没有可信 Provider 分类，必须按本地故障排除，不能仅凭公开错误码处罚模型。
    if not isinstance(error, NormalizedProviderError):
        return False
    return (
        error.error_category in QUALITY_OBSERVATION_ERROR_CATEGORIES
        or error.code == "gateway.upstream_transport_failed"
    )


def is_upstream_cost_exposure_possible(error: NormalizedProviderError | None) -> bool:
    """判断失败分项是否可能已经到达上游并产生费用。

    参数：Adapter 归一化错误。返回值：只有可证明发生在本地 URL/协议构造阶段的错误为
    false；HTTP 错误、超时和不确定网络失败均保守返回 true。该判断独立于模型质量资格，
    防止“不计质量”被错误解释为“必定不计费”。
    """

    # 未归一化错误无法证明请求未发出，预算账按可能产生上游成本的保守口径处理。
    if not isinstance(error, NormalizedProviderError):
        return True
    return error.provider_error_type not in LOCAL_TRANSPORT_ERROR_TYPES


class UpstreamApiError(ApiError):
    """携带受控上游元数据的统一 API 错误。

    HTTP 响应仍由 ApiError 父类按全局错误码生成；额外字段只供网关编排器判断
    PRD-036 的超时、429、5xx 白名单，绝不携带上游正文、Header 或异常字符串。
    """

    def __init__(self, error: NormalizedProviderError) -> None:
        """保存 Adapter 已归一化的安全错误，并沿用统一错误响应 message。"""

        super().__init__(error.code, message=error.message)
        self.normalized_error = error


@dataclass
class ProviderStreamResponse:
    """持有一次上游流及其可幂等关闭的网络资源。

    看门狗、异步迭代器和外层 finally 都可能触发关闭；异步锁确保底层 stream context
    与 client 只关闭一次，避免并发清理覆盖真正的流终态。
    """

    client: httpx.AsyncClient = field(repr=False)
    response: httpx.Response = field(repr=False)
    stream_context: Any = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """逐块返回非空响应字节，并在迭代结束时释放上游资源。"""

        try:
            async for chunk in self.response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """显式且幂等地关闭上游响应上下文和 HTTP 客户端。

        `_closed` 只在持锁后判断，防止 timeout 与客户端断连同时进入时执行两次
        `__aexit__`。即使关闭响应上下文失败，也必须继续关闭 client。
        """

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self.stream_context.__aexit__(None, None, None)
            finally:
                await self.client.aclose()


class ProviderAdapter(Protocol):
    """承载 Provider 与 KeyPool 管理 的 ProviderAdapter 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    name: str
    version: str

    def build_request(
        self,
        route: GatewayRoute,
        endpoint: str,
        payload: dict[str, Any],
        config: AppConfig | None = None,
        *,
        selected_key: SelectedProviderKey | None = None,
        cache_affinity_selection: CacheAffinitySelectionHandle | None = None,
        error_map_lease: ProviderErrorMapLease | None = None,
    ) -> ProviderRequest:
        """实现 Provider 与 KeyPool 管理 的 build request 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        ...

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """发送非流式 Provider 请求并返回标准响应。

        调用方只传已完成租户路由和凭据选择的请求，异常必须使用固定安全分类。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        ...

    async def send_stream(self, request: ProviderRequest) -> ProviderStreamResponse:
        """实现 Provider 与 KeyPool 管理 的 send stream 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        ...

    def parse_usage(self, payload: Mapping[str, Any]) -> NormalizedUsage:
        """实现 Provider 与 KeyPool 管理 的 parse usage 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        ...

    def normalize_error(
        self,
        response: httpx.Response | None = None,
        exc: Exception | None = None,
        *,
        request: ProviderRequest | None = None,
        extracted_facts: ProviderErrorFacts | None = None,
    ) -> NormalizedProviderError:
        """把 Provider HTTP 或传输异常归一成安全错误。

        返回值不得携带上游正文、Header、Endpoint 或底层异常字符串。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        ...


@lru_cache(maxsize=256)
def _normalize_error_capability(unbound: object) -> tuple[bool, bool]:
    """缓存未绑定 ``normalize_error`` 对两个新关键字的真实绑定能力。

    参数：Adapter 类上的未绑定方法对象。返回值：依次表示是否接受 ``request`` 和
    ``extracted_facts``。使用 ``Signature.bind`` 同时校验 self/response/exc 三个位置参数，
    避免只按参数名猜测一个实际不可调用的签名；缓存有界，防止动态 Adapter 类无限占用内存。
    """

    try:
        signature = inspect.signature(unbound)
    # 不可反射的第三方 callable 保守按旧合同调用；不能用运行时 TypeError 做探测。
    except (TypeError, ValueError):
        return False, False

    def accepts(keyword: str) -> bool:
        """验证一个新关键字能否与固定旧位置参数共同绑定，不执行 Adapter 业务代码。"""

        try:
            signature.bind(object(), None, None, **{keyword: None})
        # 绑定失败只表示旧 Adapter 不支持该关键字，不能执行函数并用业务 TypeError 探测能力。
        except TypeError:
            return False
        return True

    return accepts("request"), accepts("extracted_facts")


def invoke_normalize_error(
    adapter: ProviderAdapter,
    response: httpx.Response | None = None,
    exc: Exception | None = None,
    *,
    request: ProviderRequest | None = None,
    extracted_facts: ProviderErrorFacts | None = None,
) -> NormalizedProviderError:
    """兼容调用新旧 Adapter 的 ``normalize_error``。

    参数：Adapter、可空响应/异常，以及新合同的冻结请求和已提取事实。返回统一 Provider 错误。
    关键约束：能力只通过未绑定方法签名预判；禁止先传新关键字再捕获 TypeError 重试，因为
    Adapter 内部真实 TypeError 必须原样暴露，不能造成二次调用或吞掉副作用。
    """

    unbound = type(adapter).normalize_error
    capability = _normalize_error_capability(unbound)
    kwargs: dict[str, object] = {}
    if capability[0]:
        kwargs["request"] = request
    if capability[1]:
        kwargs["extracted_facts"] = extracted_facts
    return adapter.normalize_error(response, exc, **kwargs)


def clear_normalize_error_capability_cache_for_tests() -> None:
    """仅供合同测试清空签名缓存，生产调用不应在请求间重置该进程级状态。"""

    _normalize_error_capability.cache_clear()
