"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/console/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NavigationItem(BaseModel):
    """控制台导航项 DTO。

    该对象由后端按当前认证上下文生成，前端只能消费结果，不能用请求体里的角色或租户字段伪造可见入口。
    """

    id: str
    label: str
    group: str
    target_section: str
    required_roles: list[str]
    # required_permissions 是前端裁剪的权威字段；required_roles 仅用于旧客户端展示兼容。
    required_permissions: list[str]
    visible: bool
    disabled: bool
    description: str
    empty_state_hint: str
    order: int


class UserPreferenceInfo(BaseModel):
    """控制台用户偏好 DTO。

    偏好只保存 UI 状态，不保存 Token、密钥、Prompt、Response 或业务筛选条件等敏感上下文。
    """

    preference_id: str
    tenant_id: str
    user_id: str
    active_navigation_item_id: str
    sidebar_collapsed: bool
    created_at: datetime
    updated_at: datetime


class ConsoleNavigationResponse(BaseModel):
    """控制台导航响应，统一携带 request_id/trace_id 便于定位导航与权限问题。"""

    navigation_items: list[NavigationItem]
    active_navigation_item_id: str
    gateway_base_url: str | None
    gateway_base_url_status: Literal["ready", "configuration_required"]
    # 三个字段必须与同请求认证上下文完全一致，前端据此淘汰迟到导航响应。
    permission_codes: list[str]
    permission_revision: int
    authorization_fingerprint: str
    request_id: str
    trace_id: str


class UserPreferenceResponse(BaseModel):
    """控制台用户偏好响应，供前端恢复上一次导航位置。"""

    preference: UserPreferenceInfo
    request_id: str
    trace_id: str


class UpdateUserPreferenceRequest(BaseModel):
    """更新控制台用户偏好的请求体。

    安全：禁止额外字段，避免前端把 tenant_id、user_id 或敏感业务上下文夹带进偏好表。
    """

    active_navigation_item_id: str | None = Field(default=None, max_length=80)
    sidebar_collapsed: bool | None = None

    model_config = ConfigDict(extra="forbid")


class ConsoleEventRequest(BaseModel):
    """控制台事件写入请求体。

    payload 只允许服务层白名单过滤后的安全字段进入数据库；任何密钥、Token 或 Prompt 类字段都会被拒绝。
    """

    event_type: Literal[
        "console.page_viewed",
        "dashboard.viewed",
        "dashboard.role_viewed",
        "migration.guide_viewed",
        "help.competitor_viewed",
        "status.viewed",
    ] = "console.page_viewed"
    navigation_item_id: str = Field(min_length=1, max_length=80)
    target_section: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=240)
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ConsoleEventResponse(BaseModel):
    """控制台事件写入响应，返回 event_id 供排障和后续审计链路串联。"""

    accepted: bool
    event_id: str
    request_id: str
    trace_id: str
