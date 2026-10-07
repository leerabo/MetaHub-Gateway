"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/model_aliases/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RoutingPolicyCandidateInfo(BaseModel):
    """RoutingPolicy 当前主备配置的安全摘要。

    该结构只说明 Alias 指向的 Model/Provider 元数据，并可返回模型目录已保存的
    输入、输出和缓存命中价格；Endpoint、KeyPool 数量和任何凭据永远不进入响应。
    """

    candidate_role: str
    configured_priority: int
    model_id: str
    internal_model_id: str
    display_name: str | None
    model_status: str
    provider_code: str
    provider_name: str
    provider_status: str
    # The catalog model name is safe user-facing metadata.  Keep it in the
    # candidate snapshot so an older model list cannot fall back to a UUID.
    provider_model_id: str | None = None
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_price_per_1k: float | None = None
    currency: str | None = None


class QualityCostConfigInfo(BaseModel):
    """RoutingPolicy 的质量/成本/延迟手动配置。

    权重和评分均使用基点整数，避免 JSON float 在浏览器、Python 和 SQLite 间产生
    不同舍入；该对象不包含当前模型评分，运行事实只存在 Candidate 历史中。
    """

    quality_weight_bps: int
    cost_weight_bps: int
    latency_weight_bps: int
    quality_floor_bps: int
    minimum_sample_count: int


class RoutingPolicyInfo(BaseModel):
    """ModelAlias 一对一 RoutingPolicy 的版本化安全展示对象。

    该对象返回当前租户可见的模式、版本和候选摘要；候选可带已保存的三项价格，
    但不携带凭据或 Endpoint。
    """

    routing_policy_id: str
    tenant_id: str
    model_alias_id: str
    routing_mode: str
    quality_cost_config: QualityCostConfigInfo
    status: str
    version: int
    candidates: list[RoutingPolicyCandidateInfo]
    warnings: list[str]
    created_at: datetime
    updated_at: datetime


class FallbackPolicyInfo(BaseModel):
    """模型别名的非流式 Fallback 策略摘要。

    对外只返回经过服务端解析的条件枚举和固定最大尝试数，不暴露数据库中的
    JSON 表达式，避免客户端把存储格式误当成可执行规则语言。
    """

    fallback_policy_id: str
    model_alias_id: str
    status: str
    version: int
    priority: int
    trigger_conditions: list[str]
    max_attempts: int
    created_at: datetime
    updated_at: datetime


class CacheAffinityInfo(BaseModel):
    """ModelAlias 的 Prompt Cache 亲和配置安全摘要。

    该 DTO 只公开管理员和开发者配置页面需要的字段。Prompt 指纹、Binding、
    Provider Credential 以及任何 Key 标识都属于运行时内部事实，不能进入控制面响应。
    """

    cache_affinity_id: str
    tenant_id: str
    model_alias_id: str
    status: str
    data_class: str | None
    minimum_prefix_chars: int
    ttl_seconds: int
    version: int
    created_at: datetime
    updated_at: datetime


class ModelAliasInfo(BaseModel):
    """模型别名的对外展示信息。

    该对象把默认/备用模型的内部 ID 和 Provider 摘要展开，便于控制台解释 Gateway
    为什么会把稳定别名解析到某个上游模型。
    """

    model_alias_id: str
    tenant_id: str
    alias: str
    environment: str
    environment_id: str
    environment_name: str
    environment_status: str
    default_model_id: str
    default_internal_model_id: str
    default_display_name: str | None
    default_model_status: str
    default_provider_code: str
    default_provider_name: str
    default_provider_status: str
    fallback_model_id: str | None
    fallback_internal_model_id: str | None
    fallback_display_name: str | None
    fallback_provider_code: str | None
    fallback_provider_name: str | None
    # 新接口返回完整有序备用候选；旧的单个 fallback_* 字段继续保留兼容历史客户端。
    fallback_model_ids: list[str] = Field(default_factory=list)
    fallback_policy: FallbackPolicyInfo | None = None
    # CacheAffinity 仅返回 Alias 级安全配置，不暴露 Prompt 摘要、Binding 或凭据快照。
    cache_affinity: CacheAffinityInfo | None = None
    # 迁移后正常 Alias 必须有持久策略；nullable 只用于旧夹具或损坏库的只读兼容。
    routing_policy: RoutingPolicyInfo | None = None
    effective_routing_mode: str = "fixed"
    routing_policy_implicit: bool = False
    status: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class ModelAliasListResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    model_aliases: list[ModelAliasInfo]
    request_id: str
    trace_id: str


class ModelAliasResponse(BaseModel):
    """定义 模型目录 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    model_alias: ModelAliasInfo
    request_id: str
    trace_id: str


class CreateModelAliasRequest(BaseModel):
    """创建模型别名请求。

    字段先保持宽松可空，业务校验在 service 层返回 model_alias.* 统一错误码。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    alias: str | None = None
    environment_id: str | None = None
    # 未提供 ID 和兼容代码时由服务层解析默认 dev，避免 ID 被 DTO 默认值错误覆盖。
    environment: str | None = None
    default_model_id: str | None = None
    fallback_model_id: str | None = None
    fallback_model_ids: list[str] | None = Field(default=None, max_length=31)
    description: str | None = None
    status: str | None = "active"


class UpdateModelAliasRequest(BaseModel):
    """更新模型别名请求。

    PATCH 语义下未传字段表示不修改，不能用默认值覆盖已有配置。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    alias: str | None = None
    environment_id: str | None = None
    environment: str | None = None
    default_model_id: str | None = None
    fallback_model_id: str | None = None
    fallback_model_ids: list[str] | None = Field(default=None, max_length=31)
    description: str | None = None
    status: str | None = None
    # 只要修改 Alias 路由图就必须提交当前策略版本；description-only 更新允许省略。
    expected_routing_policy_version: int | None = Field(default=None, ge=1)


class UpdateFallbackPolicyRequest(BaseModel):
    """修改模型别名非流式 Fallback 策略的严格请求体。

    `fallback_model_id` 是否出现在请求中具有业务含义：省略表示保留现有备用模型，
    显式 null 表示清除备用模型并禁用策略，因此服务层会读取 model_fields_set。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    fallback_model_id: str | None = None
    fallback_model_ids: list[str] | None = Field(default=None, max_length=31)
    trigger_conditions: list[str] | None = Field(default=None, max_length=5)
    expected_routing_policy_version: int = Field(ge=1)


class FallbackPolicyResponse(BaseModel):
    """Fallback 策略写接口响应，统一携带模型别名快照和链路标识。"""

    model_alias: ModelAliasInfo
    fallback_policy: FallbackPolicyInfo
    request_id: str
    trace_id: str


class RoutingPolicyMutationResponse(BaseModel):
    """Routing mode 写接口响应，显式说明本次是否形成了新配置版本。"""

    routing_policy: RoutingPolicyInfo
    changed: bool
    request_id: str
    trace_id: str


class CacheAffinityMutationResponse(BaseModel):
    """CacheAffinity 完整快照写接口的响应。

    `changed` 明确区分真实配置变更与已消费 request_id 的 no-op；两种成功都携带
    统一链路标识，便于控制台在网络结果未知时按相同意图安全重放。
    """

    cache_affinity: CacheAffinityInfo
    changed: bool
    request_id: str
    trace_id: str
