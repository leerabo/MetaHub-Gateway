"""管理操作审计的 API 数据结构。

审计 DTO 只描述已脱敏的管理变更事实。Provider Key、Virtual Key 明文、完整哈希、
DLP 原始 pattern、Prompt、Response 和认证 Token 都不属于该模块的数据合同。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogInfo(BaseModel):
    """单条管理审计日志的安全展示结构。"""

    audit_log_id: str
    tenant_id: str
    actor_user_id: str
    actor_email: str
    actor_role: str
    action: str
    object_type: str
    object_id: str
    object_name: str | None
    before_summary: dict[str, Any]
    after_summary: dict[str, Any]
    changed_fields: list[str]
    client_ip: str | None
    user_agent: str | None
    request_id: str
    trace_id: str
    created_at: datetime


class AuditLogListResponse(BaseModel):
    """租户级审计列表响应，保留统一链路字段和分页元数据。"""

    audit_logs: list[AuditLogInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class AuditExportResult:
    """一次已提交审计 CSV 导出的安全返回结构。

    `content` 只在当前 HTTP 响应的内存中存在，数据库仅保存行数、字节数和 SHA-256
    复核事实。调用方不得把 CSV 或筛选原文写入日志、Webhook 或普通 JSON 响应。
    """

    audit_export_id: str
    file_name: str
    content_sha256: str
    content_size_bytes: int
    row_count: int
    request_id: str
    trace_id: str
    content: bytes
