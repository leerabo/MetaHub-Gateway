"""PRD-068 行业/场景策略模板库的严格 HTTP 数据模型。

本模块只描述管理面允许交换的安全字段。租户、actor、生成 Policy/Snapshot ID、排序结果和
适配结论都由服务端计算，客户端不能通过正文覆盖。涉及绑定、阈值和原因的字段关闭 ``repr``，
避免框架调试输出把企业治理配置复制到日志。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from metahub_control.policies.models import PolicyInfo
from metahub_control.policy_snapshots.models import PolicySnapshotInfo

PolicyTemplateCode = Literal[
    "low_cost_mode",
    "high_security_mode",
    "domestic_models_only",
    "budget_burst_guard",
    "customer_service_redaction",
]


class PolicyTemplateBindingsRequest(BaseModel):
    """五个内置模板共用的有界绑定联合体。

    字段是否允许由模板 code 再做 exact-set 校验。例如预算模板只接受 project_id 与
    threshold_cny；保留一个联合 DTO 只是为了让两个固定 HTTP 端点拥有稳定 wire，并不允许
    客户端给不相关模板夹带 Alias 或阈值。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: StrictStr = Field(min_length=1, max_length=128, repr=False)
    model_alias_id: StrictStr | None = Field(default=None, min_length=1, max_length=128, repr=False)
    threshold_cny: StrictStr | None = Field(default=None, min_length=1, max_length=32, repr=False)


class ApplyPolicyTemplateRequest(BaseModel):
    """应用一次精确预览的完整 CAS 请求。

    preview_nonce 只是规划盐，不是授权凭证；服务层仍会在写锁内重读数据库权限、绑定 owner、
    Snapshot head 和全部适配依赖。时间保留为严格字符串，以便服务层拒绝宽松时区和精度表示。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    template_version: StrictInt = Field(ge=1, le=2_147_483_647)
    preview_nonce: StrictStr = Field(min_length=64, max_length=64, repr=False)
    preview_as_of: StrictStr = Field(min_length=20, max_length=32, repr=False)
    impact_hash: StrictStr = Field(min_length=64, max_length=64, repr=False)
    bindings: PolicyTemplateBindingsRequest = Field(repr=False)
    reason: StrictStr = Field(min_length=1, max_length=256, repr=False)


class PolicyTemplateProjectOption(BaseModel):
    """当前租户可绑定的 active Project 最小投影。"""

    project_id: str
    name: str
    status: Literal["active"] = "active"


class PolicyTemplateAliasOption(BaseModel):
    """高安全模板可选择的同租户 active Alias 最小投影。"""

    model_alias_id: str
    alias: str
    environment_id: str
    environment_code: str
    status: Literal["active"] = "active"


class PolicyTemplateBindingOptions(BaseModel):
    """目录页面表单使用的安全绑定选项，不含 Provider URL、Key 或完整价格规则。"""

    projects: list[PolicyTemplateProjectOption] = Field(default_factory=list, max_length=200)
    model_aliases: list[PolicyTemplateAliasOption] = Field(default_factory=list, max_length=200)


class PolicyTemplateCompatibility(BaseModel):
    """服务端基于当前租户事实给出的可应用性和复合权限结论。"""

    status: Literal["unbound", "ready", "ready_with_warnings", "incompatible"]
    reason_codes: list[str] = Field(default_factory=list, max_length=32)
    warning_codes: list[str] = Field(default_factory=list, max_length=32)
    can_apply: bool
    requires_permissions: list[str] = Field(default_factory=list, max_length=4)


class PolicyTemplateImpactOrderItem(BaseModel):
    """精确预览中的一条有界求值顺序摘要。"""

    policy_id: str
    priority: int
    action_type: Literal["allow", "deny", "mask", "log_only", "route_to"]
    generated: bool = False
    shadowing: Literal["none", "warning", "blocking"] = "none"


class PolicyTemplateImpact(BaseModel):
    """确认弹窗需要展示的精确影响；不携带 DSL 正文或敏感依赖值。"""

    project_id: str
    generated_resource_types: list[str] = Field(max_length=4)
    evaluation_order: list[PolicyTemplateImpactOrderItem] = Field(max_length=201)
    before_policy_count: int = Field(ge=0, le=200)
    after_policy_count: int = Field(ge=1, le=201)
    derived_target_model_id: str | None = None
    allowed_model_alias_names: list[str] = Field(default_factory=list, max_length=20)
    frozen_environment_codes: list[str] = Field(default_factory=list, max_length=20)
    expected_head_version: int = Field(ge=1)
    rollback_target_snapshot_id: str
    shadowing_policy_ids: list[str] = Field(default_factory=list, max_length=200)


class PolicyTemplateInfo(BaseModel):
    """五模板目录和精确预览共用的安全响应对象。"""

    policy_template_id: str
    tenant_id: str
    template_code: PolicyTemplateCode
    name: str
    description: str
    category: str
    scenario: str
    status: Literal["active"] = "active"
    version: int
    priority: int
    condition_blueprint: str
    action_type: Literal["deny", "mask", "route_to"]
    required_bindings: list[str] = Field(max_length=3)
    binding_options: PolicyTemplateBindingOptions
    compatibility: PolicyTemplateCompatibility
    impact: PolicyTemplateImpact | None = None
    preview_nonce: str | None = None
    preview_as_of: datetime | None = None
    impact_hash: str | None = None
    before_candidate_hash: str | None = None
    after_candidate_hash: str | None = None
    expected_head_version: int | None = None
    generated_resource_ids: dict[str, str] | None = None


class PolicyTemplateListResponse(BaseModel):
    """固定五模板目录响应，统一携带 request_id 与 trace_id。"""

    policy_templates: list[PolicyTemplateInfo] = Field(min_length=5, max_length=5)
    total: Literal[5] = 5
    request_id: str
    trace_id: str


class PolicyTemplateApplicationInfo(BaseModel):
    """一次成功应用的不可变安全事实，不返回绑定正文、价格或预算余额。"""

    application_id: str
    policy_template_id: str
    template_code: PolicyTemplateCode
    template_version: int
    status: Literal["applied", "rolled_back"]
    generated_policy_id: str
    generated_policy_version: int
    rolled_back_policy_version: int | None = None
    previous_snapshot_id: str
    previous_snapshot_version: int
    result_snapshot_id: str
    result_snapshot_version: int
    rollback_snapshot_id: str | None = None
    rollback_snapshot_version: int | None = None
    impact_hash: str
    bindings_hash: str
    applied_at: datetime
    rolled_back_at: datetime | None = None


class PolicyTemplateApplyResponse(BaseModel):
    """模板应用成功或幂等回放的聚合结果。"""

    application: PolicyTemplateApplicationInfo
    generated_policy: PolicyInfo
    policy_snapshot: PolicySnapshotInfo
    rollback_target_snapshot_id: str
    idempotent_replay: bool
    request_id: str
    trace_id: str
