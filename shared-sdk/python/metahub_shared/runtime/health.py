"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/health.py
"""

from __future__ import annotations

import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, Field

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError

from .config import AppConfig, build_service_info

HealthStatus = Literal["ok", "degraded", "fail"]
DependencyStatus = Literal["ok", "skipped", "fail"]


class HealthDependencyCheck(BaseModel):
    """承载 健康检查 的 HealthDependencyCheck 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    name: str
    status: DependencyStatus
    required: bool
    latency_ms: int | None = None
    message: str


class HealthCheckResult(BaseModel):
    """承载 健康检查 的 HealthCheckResult 数据结构，明确跨模块传递时允许出现的字段。
    
    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """


    service_name: str
    status: HealthStatus
    live: bool
    ready: bool
    environment: str
    checks: list[HealthDependencyCheck]
    version: str
    build_commit: str
    build_time: str
    started_at: str
    uptime_seconds: float
    request_id: str
    trace_id: str
    # capability 只含固定非敏感协议名；不能暴露主机名、内部地址、配置值或副本拓扑。
    capabilities: list[str] = Field(default_factory=list)


def build_liveness_result(config: AppConfig, request_id: str, trace_id: str) -> HealthCheckResult:
    """构造 liveness 结果。

    live 只回答“进程还能不能响应 HTTP”，绝不能访问数据库、Redis 或 Provider。
    否则外部依赖抖动会导致容器被错误重启，和 readiness 的语义混在一起。
    """

    service_info = build_service_info(config, request_id, trace_id)
    return HealthCheckResult(
        service_name=config.service_name,
        status="ok",
        live=True,
        ready=False,
        environment=config.environment,
        checks=[],
        version=service_info.version,
        build_commit=service_info.build_commit,
        build_time=service_info.build_time,
        started_at=service_info.started_at.isoformat(),
        uptime_seconds=service_info.uptime_seconds,
        request_id=request_id,
        trace_id=trace_id,
    )


def build_readiness_result(
    config: AppConfig,
    request_id: str,
    trace_id: str,
    *,
    capabilities: tuple[str, ...] = (),
) -> HealthCheckResult:
    """构造 readiness 结果。

    readiness 可以检查必需依赖。M0 阶段数据库是唯一会阻塞 ready 的依赖；
    Redis 和 Provider 先作为可选检查，避免未接入驱动或上游网络波动拖慢 M0。
    ``capabilities`` 只能由服务启动自检写入 app.state，不能从请求或租户数据注入。
    """

    checks = [
        _check_database(config),
        _check_redis(config),
        *_check_providers(config),
    ]
    required_failures = [item for item in checks if item.required and item.status == "fail"]
    ready = len(required_failures) == 0
    if ready and any(item.status == "fail" for item in checks):
        status: HealthStatus = "degraded"
    elif ready:
        status = "ok"
    else:
        status = "fail"

    service_info = build_service_info(config, request_id, trace_id)
    return HealthCheckResult(
        service_name=config.service_name,
        status=status,
        live=True,
        ready=ready,
        environment=config.environment,
        checks=checks,
        version=service_info.version,
        build_commit=service_info.build_commit,
        build_time=service_info.build_time,
        started_at=service_info.started_at.isoformat(),
        uptime_seconds=service_info.uptime_seconds,
        request_id=request_id,
        trace_id=trace_id,
        capabilities=sorted(set(capabilities)),
    )


def _check_database(config: AppConfig) -> HealthDependencyCheck:
    """在 健康检查 内部复用 check database 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    if not config.database_url:
        return HealthDependencyCheck(
            name="database",
            status="skipped",
            required=False,
            message="database URL is not configured",
        )
    started = time.perf_counter()
    try:
        connection = connect_database(config.database_url)
        connection.execute("SELECT 1").fetchone()
        connection.close()
        return HealthDependencyCheck(
            name="database",
            status="ok",
            required=True,
            latency_ms=_elapsed_ms(started),
            message="database ping ok",
        )
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except (DbError, sqlite3.Error) as exc:
        # 只返回安全摘要，不把数据库 URL、用户名、密码写进响应或日志。
        return HealthDependencyCheck(
            name="database",
            status="fail",
            required=True,
            latency_ms=_elapsed_ms(started),
            message=f"database ping failed: {type(exc).__name__}",
        )


def _check_redis(config: AppConfig) -> HealthDependencyCheck:
    """在 健康检查 内部复用 check redis 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    if not config.redis_url:
        return HealthDependencyCheck(
            name="redis",
            status="skipped",
            required=False,
            message="redis URL is not configured",
        )
    return HealthDependencyCheck(
        name="redis",
        status="skipped",
        required=False,
        message="redis driver is not enabled in M0",
    )


def _check_providers(config: AppConfig) -> list[HealthDependencyCheck]:
    """在 健康检查 内部复用 check providers 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    if not config.provider_base_urls:
        return [
            HealthDependencyCheck(
                name="provider",
                status="skipped",
                required=False,
                message="provider base URL is not configured",
            )
        ]
    return [
        HealthDependencyCheck(
            name=f"provider:{provider_code}",
            status="skipped",
            required=False,
            message="provider deep connectivity is optional in M0",
        )
        for provider_code in sorted(config.provider_base_urls)
    ]


def _elapsed_ms(started: float) -> int:
    """在 健康检查 内部复用 elapsed ms 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return int((time.perf_counter() - started) * 1000)
