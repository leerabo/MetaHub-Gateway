"""Response Cache 的结构资格与服务端可信标签构造。

本模块不连接数据库、不读取正文内容，也不做任何日志记录。调用方必须在 Policy、路由、DLP、
Intent、Session Risk 与 Rate Limit 均通过后调用；返回允许只表示可以继续读取 CachePolicy，
并不代表最终一定 lookup、命中或填充。
"""

from __future__ import annotations

from typing import Literal

from metahub_shared.response_cache import CacheTag, canonicalize_cache_tags

from metahub_gateway.routing.models import GatewayRoute

ResponseCacheStructuralBypass = Literal[
    "disabled",
    "ineligible",
    "bypassed_experiment",
]


def classify_response_cache_request(
    *,
    globally_enabled: bool,
    request_kind: str,
    payload: dict[str, object],
    public_virtual_key: bool,
    internal_invocation: bool,
    policy_decision: str,
    experiment_disposition: str,
    route: GatewayRoute,
) -> ResponseCacheStructuralBypass | None:
    """判断请求是否具备读取 CachePolicy 的结构资格。

    返回 ``None`` 仅表示可以继续 tenant-qualified policy/key-state 检查。活跃实验的 sampled、
    unsampled 与不可用臂统一旁路；``unresolved`` 是实验服务的失败关闭状态，不能被误降级为
    普通 miss，因此这里用固定内部异常暴露调用顺序错误。
    """

    if type(globally_enabled) is not bool or not globally_enabled:
        # 可选能力默认关闭；关闭时不能连接 cache 表或产生 lease、副作用和额外故障。
        return "disabled"
    if experiment_disposition == "unresolved":
        # 实验范围不确定时必须由 PRD-075 返回固定 503，绝不能借缓存路径继续 Provider。
        raise ValueError("response_cache_experiment_disposition_unresolved")
    if experiment_disposition == "active_scope_bypass":
        return "bypassed_experiment"
    if experiment_disposition != "not_in_scope":
        # 未知枚举不能默认允许缓存；固定内部错误便于专项发现漏赋分支。
        raise ValueError("response_cache_experiment_disposition_invalid")
    if (
        request_kind != "chat"
        or payload.get("stream") is True
        or public_virtual_key is not True
        or internal_invocation is not False
        or policy_decision == "route_to"
        or route.model_alias_id is None
        or route.public_model_alias is None
    ):
        return "ineligible"
    return None


def trusted_response_cache_tags(
    *,
    tenant_id: str,
    cache_policy_id: str,
    model_alias_id: str,
    model_id: str,
    virtual_key_id: str,
    data_class: str,
    policy_tags: tuple[str, ...],
) -> tuple[CacheTag, ...]:
    """从后端 owner 构造 fill/invalidation 使用的完整可信标签集合。

    参数只能来自认证 claim、tenant-qualified Alias/Model 与 CachePolicy；客户端 metadata、Header、
    Prompt 和任意请求体标签均不在函数签名中。``tenant_id`` 只用于拒绝空 owner，真正租户隔离
    还会在每个 tag HMAC 中再次绑定。
    """

    # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
    if not tenant_id:
        # 数据面缓存处理必须保持租户隔离、代际围栏和敏感正文不落盘的边界。
        raise ValueError("response_cache_tenant_owner_invalid")
    tags = [
        CacheTag(kind="policy", value=cache_policy_id),
        CacheTag(kind="model_alias", value=model_alias_id),
        CacheTag(kind="model", value=model_id),
        CacheTag(kind="virtual_key", value=virtual_key_id),
        CacheTag(kind="data_class", value=data_class),
    ]
    tags.extend(CacheTag(kind="tag", value=value) for value in policy_tags)
    # 5 个强制标签加最多 16 个策略标签，小于 Shared 32 个硬上限；重复或非法值固定拒绝。
    return canonicalize_cache_tags(tags, maximum=21)


__all__ = [
    "ResponseCacheStructuralBypass",
    "classify_response_cache_request",
    "trusted_response_cache_tags",
]
