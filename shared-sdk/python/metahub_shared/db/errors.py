"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/errors.py
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DbError(Exception):
    """数据库 CLI 可预期错误。

    CLI 没有 HTTP response，但仍然沿用 code/message/run_id/suggestion 结构，
    方便研发把终端输出、日志和后续审计事件串起来。
    """

    code: str
    message: str
    suggestion: str

    def __str__(self) -> str:
        """在 数据库基础设施 内部复用 str 辅助逻辑，集中处理边界值和错误映射。
        
        边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
        """


        return self.message
