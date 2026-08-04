"""模块职责：定义 Project/Application 管理 API 的请求和响应 DTO。

维护边界：DTO 只承载租户内项目、应用和脱敏 Key 归属元数据，不允许加入明文 Key、Provider Key、Prompt 或上游响应内容。
来源文件：control-plane-api/src/metahub_control/projects/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ProjectInfo(BaseModel):
    """项目列表和详情共用的安全响应对象。

    application_count 与 virtual_key_count 是控制台扫描用的聚合字段，不改变项目本身生命周期。
    """

    project_id: str
    tenant_id: str
    project_code: str
    name: str
    description: str | None
    owner_user_id: str | None
    owner_display_name: str | None = None
    status: str
    application_count: int = 0
    virtual_key_count: int = 0
    created_at: datetime
    updated_at: datetime


class ApplicationInfo(BaseModel):
    """应用列表和详情共用的安全响应对象。

    应用始终挂在项目下，因此响应同时返回项目名称，方便控制台列表直接展示归属。
    """

    application_id: str
    tenant_id: str
    project_id: str
    project_code: str | None = None
    project_name: str | None = None
    application_code: str
    name: str
    description: str | None
    status: str
    virtual_key_count: int = 0
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    """项目列表响应，统一携带 request_id/trace_id 便于审计和排障。"""

    projects: list[ProjectInfo]
    request_id: str
    trace_id: str


class ProjectResponse(BaseModel):
    """单项目写入或详情响应，保持与其他控制面 API 的响应信封一致。"""

    project: ProjectInfo
    request_id: str
    trace_id: str


class ApplicationListResponse(BaseModel):
    """应用列表响应，支持按项目过滤并统一携带链路标识。"""

    applications: list[ApplicationInfo]
    request_id: str
    trace_id: str


class ApplicationResponse(BaseModel):
    """单应用写入或详情响应，供控制台保存后局部刷新使用。"""

    application: ApplicationInfo
    request_id: str
    trace_id: str


class CreateProjectRequest(BaseModel):
    """创建项目请求体。

    请求体不接受 tenant_id，服务层必须从登录上下文恢复租户，避免跨租户伪造归属边界。
    """

    project_code: str | None = None
    name: str | None = None
    description: str | None = None
    owner_user_id: str | None = None


class UpdateProjectRequest(BaseModel):
    """更新项目请求体。

    project_code 不允许修改，避免外部对账编码在历史日志和预算解释中漂移。
    """

    name: str | None = None
    description: str | None = None
    owner_user_id: str | None = None
    status: str | None = None


class CreateApplicationRequest(BaseModel):
    """创建应用请求体。

    应用必须归属一个项目，M0.5 不提供跨项目共享应用模型。
    """

    project_id: str | None = None
    application_code: str | None = None
    name: str | None = None
    description: str | None = None


class UpdateApplicationRequest(BaseModel):
    """更新应用请求体。

    允许迁移项目，但服务层必须同步校正已绑定该应用的 Virtual Key 项目归属。
    """

    project_id: str | None = None
    name: str | None = None
    description: str | None = None
    status: str | None = None


class ProjectEventInfo(BaseModel):
    """项目域事件的安全快照对象，用于测试和后续 Webhook 复用。"""

    project_event_id: str
    tenant_id: str
    project_id: str | None
    application_id: str | None
    event_type: str
    actor_user_id: str | None
    request_id: str
    trace_id: str | None
    payload_json: dict[str, Any]
    created_at: datetime