"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/user_preferences/models.py
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from metahub_shared.runtime.i18n import LocaleInfo


class UserPreferenceSettings(BaseModel):
    """当前用户语言偏好响应对象。

    tenant_id 和 user_id 只用于审计与测试断言；API 请求不能传入这些字段来改变租户或用户边界。
    """

    preference_id: str
    tenant_id: str
    user_id: str
    locale: str
    available_locales: list[LocaleInfo]
    created_at: datetime
    updated_at: datetime


class UserPreferenceResponse(BaseModel):
    """用户偏好 API 响应，统一携带 request_id/trace_id 便于排查语言设置问题。"""

    preference: UserPreferenceSettings
    request_id: str
    trace_id: str


class UpdateUserPreferenceRequest(BaseModel):
    """更新用户语言偏好的请求体。

    这里允许暂存额外字段并交给服务层统一返回 user_preference.payload_invalid，避免 FastAPI 默认 422
    把错误码降级为 infra.validation_failed。
    """

    locale: str | None = Field(default=None, max_length=16)

    model_config = ConfigDict(extra="allow")
