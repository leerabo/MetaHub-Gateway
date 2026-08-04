"""Prompt Cache 亲和路由在 Gateway 内部传递的脱敏状态类型。

这些对象只在单次请求内连接路由冻结、KeyPool 选择和 Provider 终态。摘要字段虽然不可逆，
仍可能用于跨请求关联，因此全部从 ``repr`` 排除，且不得进入 API、Webhook 或结构化日志。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from metahub_shared.runtime.routing_policy import RoutingSelectionResult


@dataclass(frozen=True, slots=True)
class CacheAffinityRouteContext:
    """保存已提交 route reservation 的最小可信快照。

    参数均来自同一租户的 RoutingPolicy 事务。``scope_digest`` 和 endpoint revision 只供后续
    KeyPool 重验，调用方不得自行构造；配置在两段事务之间变化时必须失败关闭。
    """

    tenant_id: str
    virtual_key_id: str = field(repr=False)
    cache_affinity_id: str
    cache_affinity_version: int
    model_alias_id: str
    ttl_seconds: int
    digest_schema_version: int
    cache_affinity_scope_id: str = field(repr=False)
    cache_affinity_binding_id: str = field(repr=False)
    model_endpoint_id: str
    pending_lease_generation: int
    endpoint_preference_applied: bool
    endpoint_preference_state: str
    scope_digest: str = field(repr=False)
    endpoint_revision_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CacheAffinitySelectionHandle:
    """引用 Provider 前已提交的 Selection，供终态 CAS 使用。

    该句柄不携带 Prompt、data_class、Key 明文或可展示的 Credential 标签。目标快照只用于
    防止 K1 -> K2 -> K1 的 ABA 暖写，因而同样禁止出现在 ``repr`` 和日志中。
    """

    cache_affinity_selection_id: str
    tenant_id: str
    request_id: str
    trace_id: str
    attempt_sequence: int
    routing_attempt_id: str | None
    stream_session_id: str | None
    cache_affinity_binding_id: str = field(repr=False)
    target_kind: str
    target_generation: int
    pending_lease_generation: int
    model_endpoint_id: str
    provider_id: str
    endpoint_revision_digest: str = field(repr=False)
    provider_credential_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CacheAffinityRouteFacts:
    """返回亲和重排后的安全解释和后续 route context。

    ``candidate_summary`` 已经过固定白名单裁剪，可以并入 RoutingDecision；上下文为 ``None``
    表示配置禁用、前缀不合格或请求不属于持久 Alias，旧路由与 KeyPool 行为应完全不变。
    """

    selection: RoutingSelectionResult
    context: CacheAffinityRouteContext | None
    candidate_summary: dict[str, object] | None
