"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/rate_limits/models.py
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitRule:
    """数据面使用的 Virtual Key 限流规则。

    规则来自数据库 `virtual_key_rate_limit_rules`，所有字段都已经按 tenant_id 和
    virtual_key_id 过滤；这里不保存明文 Virtual Key 或 Redis URL。
    """

    tenant_id: str
    virtual_key_id: str
    rpm_limit: int | None
    tpm_limit: int | None
    concurrency_limit: int | None

    def has_limits(self) -> bool:
        """判断该规则是否至少启用一种限制。"""

        return any(
            value is not None
            for value in (self.rpm_limit, self.tpm_limit, self.concurrency_limit)
        )


@dataclass(frozen=True)
class RateLimitHit:
    """记录一次命中的限流类型和安全计数摘要。"""

    limit_type: str
    current: int
    limit: int
    window_seconds: int | None
