"""模块职责：定义租户级 Environment 管理 API 的请求和响应 DTO。

维护边界：DTO 不接受 tenant_id，也不承载 Virtual Key、Provider Key、Authorization、Prompt 或响应正文。
来源文件：control-plane-api/src/metahub_control/environments/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EnvironmentInfo(BaseModel):
    """环境设置页和关联资源共用的安全环境信息。

    关联数量只用于控制台解释禁用影响，不改变 Environment 自身状态，也不暴露任何密钥内容。
    """

    environment_id: str
    tenant_id: str
    code: str
    name: str
    description: str | None
    status: str
    virtual_key_count: int = 0
    model_alias_count: int = 0
    budget_rule_count: int = 0
    created_at: datetime
    updated_at: datetime


class EnvironmentListResponse(BaseModel):
    """Environment 列表响应，统一携带链路标识用于控制台排障追踪。"""

    environments: list[EnvironmentInfo]
    request_id: str
    trace_id: str


class EnvironmentResponse(BaseModel):
    """Environment 创建或更新响应，供控制台保存后刷新。"""

    environment: EnvironmentInfo
    request_id: str
    trace_id: str


class CreateEnvironmentRequest(BaseModel):
    """创建 Environment 的请求体。

    code 只允许 dev/test/prod；tenant_id 必须由认证上下文恢复，不能由浏览器指定。
    """

    code: str | None = None
    name: str | None = None
    description: str | None = None


class UpdateEnvironmentRequest(BaseModel):
    """更新 Environment 的请求体。

    code 创建后不可修改，避免 Key、别名和历史日志中的环境代码快照发生漂移。
    """

    name: str | None = None
    description: str | None = None
    status: str | None = None
