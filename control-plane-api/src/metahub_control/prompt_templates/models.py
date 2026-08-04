"""企业 Prompt 模板管理 API 的严格请求与安全响应模型。

请求模型全部 ``extra=forbid`` 且启用严格类型，防止浏览器注入 tenant、actor、Provider、Key、
密文或任意模板执行参数。正文、变量 schema 和 change summary 关闭 ``repr``，即使框架调试
对象被打印也不应复制敏感定义。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class PromptTemplateVariableRequest(BaseModel):
    """客户端提交的一条受限变量声明，不支持类型转换、默认表达式或嵌套 schema。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr = Field(min_length=1, max_length=64, repr=False)
    label: StrictStr = Field(min_length=1, max_length=80, repr=False)
    required: StrictBool = Field(repr=False)
    max_chars: StrictInt = Field(ge=1, le=8_000, repr=False)


class PromptTemplateDefinitionRequest(BaseModel):
    """创建和发布共享的完整定义与不可变绑定请求。

    ``project_ids`` 与推荐模型顺序属于发布意图；服务层还会执行 exact-set、同租户 owner、
    active 状态和占位符校验，不能只依赖字段长度。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    content: StrictStr = Field(min_length=1, max_length=32_768, repr=False)
    variables: list[PromptTemplateVariableRequest] = Field(
        default_factory=list,
        max_length=32,
        repr=False,
    )
    project_scope: Literal["all", "selected"]
    project_ids: list[StrictStr] = Field(default_factory=list, max_length=100, repr=False)
    recommended_model_alias_ids: list[StrictStr] = Field(
        default_factory=list,
        max_length=8,
        repr=False,
    )
    change_summary: StrictStr = Field(min_length=1, max_length=300, repr=False)


class CreatePromptTemplateRequest(PromptTemplateDefinitionRequest):
    """创建模板并在同一事务封印 v1 的完整请求。"""

    name: StrictStr = Field(min_length=1, max_length=120)
    description: StrictStr = Field(default="", max_length=1_000, repr=False)


class PublishPromptTemplateVersionRequest(PromptTemplateDefinitionRequest):
    """基于 metadata revision CAS 发布下一不可变版本的完整请求。"""

    expected_revision: StrictInt = Field(ge=1)


class PatchPromptTemplateRequest(BaseModel):
    """修改模板元数据或启停状态的 CAS 请求。

    至少出现 name/description/status 之一；显式空 description 是合法更新，不能用 truthy 判断
    把它误当成字段缺失。服务层会把实际 no-op 与真实变化分开处理。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    description: StrictStr | None = Field(default=None, max_length=1_000, repr=False)
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def require_mutation_field(self) -> PatchPromptTemplateRequest:
        """拒绝只含 expected_revision 的空 PATCH，避免创建无意义 Operation。"""

        provided = self.model_fields_set.intersection({"name", "description", "status"})
        if not provided or any(getattr(self, field) is None for field in provided):
            # 空 PATCH 或显式 null 都不能生成 revision、Operation、Audit/Event 等伪变更事实。
            raise ValueError("prompt_template.request_invalid")
        return self


class PromptTemplateVariableInfo(BaseModel):
    """授权页面或 Chat 最小投影中的变量表单字段。"""

    name: str
    label: str
    required: bool
    max_chars: int


class PromptTemplateVersionInfo(BaseModel):
    """不可变版本的安全元数据，不包含定义密文、正文或 change summary。"""

    prompt_template_version_id: str
    prompt_template_id: str
    version_number: int
    state: Literal["published"] = "published"
    project_scope: Literal["all", "selected"]
    project_ids: list[str]
    recommended_model_alias_ids: list[str]
    variable_count: int
    project_count: int
    recommended_model_count: int
    definition_size_bytes: int
    created_at: datetime
    published_at: datetime


class PromptTemplateInfo(BaseModel):
    """模板列表和详情共用的安全 metadata 投影。"""

    prompt_template_id: str
    name: str
    description: str
    status: Literal["active", "disabled"]
    metadata_revision: int
    current_version_id: str
    current_version_number: int
    project_count: int
    recommended_model_count: int
    created_at: datetime
    updated_at: datetime


class PromptTemplateListResponse(BaseModel):
    """稳定排序的模板列表响应，列表绝不包含正文或变量 schema。"""

    prompt_templates: list[PromptTemplateInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class PromptTemplateDetailResponse(BaseModel):
    """一个模板及其不可变版本历史响应。"""

    template: PromptTemplateInfo
    versions: list[PromptTemplateVersionInfo]
    request_id: str
    trace_id: str


class PromptTemplateDefinitionInfo(BaseModel):
    """授权管理用户可读取的一份解密定义。

    所有敏感字段关闭 repr；该对象只用于当前 no-store HTTP 响应，不能被审计、事件或日志复用。
    """

    content: str = Field(repr=False)
    variables: list[PromptTemplateVariableInfo] = Field(repr=False)
    project_scope: Literal["all", "selected"]
    project_ids: list[str] = Field(repr=False)
    recommended_model_alias_ids: list[str] = Field(repr=False)
    change_summary: str = Field(repr=False)


class PromptTemplateDefinitionResponse(BaseModel):
    """精确版本的授权定义响应，统一携带 request/trace 标识。"""

    prompt_template_id: str
    prompt_template_version_id: str
    version_number: int
    definition: PromptTemplateDefinitionInfo = Field(repr=False)
    request_id: str
    trace_id: str


class PromptTemplateMutationResponse(BaseModel):
    """创建、PATCH 和发布共用的幂等结果。

    PATCH 没有版本或 source event 时对应字段为 null；create/publish 必须由服务层同时返回精确
    version/event。``replayed`` 只表示命中 Operation，不声称重建历史时点模板快照。
    """

    template: PromptTemplateInfo
    version: PromptTemplateVersionInfo | None = None
    event_id: str | None = None
    changed: bool
    replayed: bool
    request_id: str
    trace_id: str


class PromptTemplateProjectOption(BaseModel):
    """模板表单可绑定的 active Project 最小投影。"""

    project_id: str
    name: str
    status: Literal["active"] = "active"


class PromptTemplateModelAliasOption(BaseModel):
    """模板表单可排序推荐的 active ModelAlias 最小投影。"""

    model_alias_id: str
    alias: str
    display_name: str
    environment_id: str
    status: Literal["active"] = "active"


class PromptTemplateOptionsResponse(BaseModel):
    """不授予 Project/路由管理能力的模板表单最小目录。"""

    projects: list[PromptTemplateProjectOption]
    model_aliases: list[PromptTemplateModelAliasOption]
    request_id: str
    trace_id: str


class ChatPromptTemplateOption(BaseModel):
    """仅凭 prompt_template.use 可获得的 PAGE-093 最小调用投影。"""

    prompt_template_id: str
    prompt_template_version_id: str
    version_number: int
    name: str
    variables: list[PromptTemplateVariableInfo]
    project_scope: Literal["all", "selected"]
    applicable_project_ids: list[str]
    recommended_model_alias_ids: list[str]
