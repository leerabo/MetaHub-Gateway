"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/config/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request

from metahub_shared.runtime.config import AppConfig, build_config_summary
from metahub_shared.runtime.errors import ApiError

router = APIRouter()


@router.get("/internal/config/summary")
async def get_config_summary(
    request: Request,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
) -> dict[str, object]:
    """返回安全脱敏后的运行配置摘要。

    PRD-002 只做只读摘要，不支持在线修改配置。生产环境必须带内部 token，
    这是 PRD-006 管理员登录/RBAC 完成前的最小后端保护，不能只靠控制台隐藏入口。
    """

    config: AppConfig = request.app.state.config
    _enforce_internal_config_access(config, internal_token)
    request_id = getattr(request.state, "request_id", "req_missing")
    return build_config_summary(config, request_id).model_dump(mode="json")


def _enforce_internal_config_access(config: AppConfig, supplied_token: str | None) -> None:
    """保护内部配置摘要接口。

    dev/test 允许本地直接访问，方便新员工启动和 CI；prod 必须显式配置并传入
    METAHUB_INTERNAL_CONFIG_TOKEN，避免配置摘要被未授权用户读取。
    """

    if config.environment != "prod":
        return
    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    if not config.internal_config_token:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(
            "infra.config_missing",
            message="METAHUB_INTERNAL_CONFIG_TOKEN is required in prod.",
            suggestion="Set METAHUB_INTERNAL_CONFIG_TOKEN and restart the control plane API.",
        )
    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    if not supplied_token:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("auth.internal_token_required")
    if supplied_token != config.internal_config_token:
        raise ApiError("auth.internal_token_invalid")
