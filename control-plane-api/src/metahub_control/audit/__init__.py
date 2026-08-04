"""管理操作审计模块入口，统一导出同事务写入、请求元数据和只读查询能力。"""

from .export_scheduler import install_audit_export_scheduler
from .models import AuditExportResult, AuditLogInfo, AuditLogListResponse
from .routes import router

# 公共审计合同必须先绑定；Webhook 路由在初始化期间会反向导入这些符号。
from .service import (
    AuditActor,
    AuditRecord,
    AuditRequestMeta,
    build_audit_request_meta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    dispatch_audit_recorded_fact,
    list_audit_logs,
    new_audit_log_id,
    record_audit_log,
    record_audit_log_for_actor,
)

__all__ = [
    "AuditLogInfo",
    "AuditLogListResponse",
    "AuditExportResult",
    "install_audit_export_scheduler",
    "AuditActor",
    "AuditRecord",
    "AuditRequestMeta",
    "build_audit_request_meta",
    "commit_audited_transaction",
    "dispatch_audit_recorded",
    "dispatch_audit_recorded_fact",
    "list_audit_logs",
    "new_audit_log_id",
    "record_audit_log",
    "record_audit_log_for_actor",
    "router",
]
