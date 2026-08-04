"""PRD-058 审计日志不可变 CSV 导出服务。

本模块在一个 ``BEGIN IMMEDIATE`` 事务内冻结稳定 sequence 水位、逐行生成 CSV，并原子
写入 AuditExport、自身 AuditLog 与持久 source event。CSV 只存在于当前响应内存中，数据库
和日志只保留不可逆指纹、哈希与规模，避免形成新的敏感审计副本。
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.auth import AuthContext

from .filters import AuditFilters, audit_where_clause, normalize_audit_filters
from .models import AuditExportResult
from .service import (
    ACTION_OBJECT_TYPES,
    SUMMARY_FIELDS,
    AuditRecord,
    AuditRequestMeta,
    decode_audit_log_row,
    dispatch_audit_recorded,
    new_audit_log_id,
    record_audit_log,
)

# 固定列顺序是对外 schema version 1；数据库将来新增字段不得自动漂移进导出文件。
CSV_COLUMNS = (
    "audit_log_id",
    "tenant_id",
    "actor_user_id",
    "actor_email",
    "actor_role",
    "action",
    "object_type",
    "object_id",
    "object_name",
    "before_summary_json",
    "after_summary_json",
    "changed_fields_json",
    "client_ip",
    "user_agent",
    "request_id",
    "trace_id",
    "created_at",
    "schema_version",
)
CSV_SCHEMA_VERSION = 1
MAX_EXPORT_ROWS = 10_000
MAX_EXPORT_BYTES = 16 * 1024 * 1024
_EXPORT_ID = re.compile(r"^aex_[0-9a-f]{32}$")
_AUDIT_ID = re.compile(r"^aud_[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^aexevt_[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FILE_NAME = re.compile(r"^metahub-audit-[0-9]{8}-[0-9a-f]{8}\.csv$")


def export_audit_logs(
    config: AppConfig,
    context: AuthContext,
    *,
    actor_user_id: str | None,
    action: str | None,
    object_type: str | None,
    object_id: str | None,
    request_id_filter: str | None,
    created_from: str | None,
    created_to: str | None,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> AuditExportResult:
    """导出当前认证管理员在当前租户可见的审计日志。

    参数：七个白名单筛选、可信认证上下文和统一链路 ID。
    返回：已经与不可变数据库事实原子提交且复核通过的 CSV。
    约束：同一 tenant/actor/request_id 只能绑定一组筛选；重放必须返回完全相同字节。
    """

    filters = normalize_audit_filters(
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id=request_id_filter,
        created_from=created_from,
        created_to=created_to,
        allowed_actions=ACTION_OBJECT_TYPES,
        allowed_object_types=SUMMARY_FIELDS,
    )
    filters_json = filters.canonical_json()
    fingerprint = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_existing(
            connection,
            context.user.tenant_id,
            context.user.user_id,
            request_id,
        )
        if existing is not None:
            result = _replay_existing(
                connection,
                existing,
                filters=filters,
                filters_json=filters_json,
                fingerprint=fingerprint,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result

        _verify_sequence_integrity(connection, context.user.tenant_id)
        snapshot_sequence, snapshot_id, snapshot_created_at = _snapshot_boundary(
            connection,
            context.user.tenant_id,
        )
        content, row_count, content_sha256 = _generate_csv(
            connection,
            context.user.tenant_id,
            filters,
            snapshot_sequence,
        )
        created_at = _utcnow()
        audit_export_id = f"aex_{uuid4().hex}"
        audit_log_id = new_audit_log_id()
        event_id = f"aexevt_{uuid4().hex}"
        file_name = _file_name(audit_export_id, created_at)
        payload = _event_payload(
            audit_export_id=audit_export_id,
            row_count=row_count,
            content_size_bytes=len(content),
            content_sha256=content_sha256,
            filter_fingerprint=fingerprint,
        )
        # 在任何事实入库前执行共享精确信封校验，防止坏 source 被永久保存后反复失败。
        build_webhook_event(
            tenant_id=context.user.tenant_id,
            event_type="audit.exported",
            event_id=event_id,
            timestamp=created_at,
            actor_id=context.user.user_id,
            object_id=audit_export_id,
            trace_id=trace_id,
            source_request_id=request_id,
            payload=payload,
        )
        _insert_export(
            connection,
            audit_export_id=audit_export_id,
            tenant_id=context.user.tenant_id,
            requested_by=context.user.user_id,
            audit_log_id=audit_log_id,
            filters_json=filters_json,
            fingerprint=fingerprint,
            snapshot_sequence=snapshot_sequence,
            snapshot_audit_log_id=snapshot_id,
            snapshot_created_at=snapshot_created_at,
            row_count=row_count,
            content_size_bytes=len(content),
            content_sha256=content_sha256,
            file_name=file_name,
            request_id=request_id,
            trace_id=trace_id,
            event_id=event_id,
            event_payload_json=_json_text(payload),
            created_at=created_at,
        )
        _insert_source_event(
            connection,
            event_id=event_id,
            tenant_id=context.user.tenant_id,
            audit_export_id=audit_export_id,
            created_at=created_at,
        )
        audit_record = record_audit_log(
            connection,
            context,
            action="audit.exported",
            object_type="audit_export",
            object_id=audit_export_id,
            object_name="audit-csv",
            before_summary={},
            after_summary={
                "status": "completed",
                "format": "csv",
                "schema_version": CSV_SCHEMA_VERSION,
                "row_count": row_count,
                "content_size_bytes": len(content),
                "content_sha256": content_sha256,
                "filter_fingerprint": fingerprint,
                "snapshot_audit_log_id": snapshot_id,
                "snapshot_created_at": snapshot_created_at,
            },
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            audit_log_id=audit_log_id,
            created_at=created_at,
        )
        if audit_record is None:
            # 该动作必须至少包含 completed/hash 等变化；None 表示内部摘要合同被错误修改。
            raise ApiError("audit_export.unavailable")
        connection.commit()
        result = AuditExportResult(
            audit_export_id=audit_export_id,
            file_name=file_name,
            content_sha256=content_sha256,
            content_size_bytes=len(content),
            row_count=row_count,
            request_id=request_id,
            trace_id=trace_id,
            content=content,
        )
    # 已归一化业务错误保留原 code，但仍要回滚整个强审计事务。
    except ApiError:
        _rollback(connection)
        raise
    # 其余底层失败可能携带 SQL、正文或数据库位置，统一折叠且不回显异常文本。
    except (sqlite3.Error, TypeError, ValueError, UnicodeError) as exc:
        _rollback(connection)
        # SQLite、CSV 和 Webhook 构造细节可能包含内部数据，只暴露稳定可重试错误码。
        raise ApiError("audit_export.unavailable") from exc
    finally:
        connection.close()
    # 普通 audit.recorded 是提交后尽力副作用；持久 audit.exported source 由独立 lease worker 恢复。
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    return result


def _load_existing(
    connection: sqlite3.Connection,
    tenant_id: str,
    requested_by: str,
    request_id: str,
) -> sqlite3.Row | None:
    """按复合 owner 幂等键读取导出，另一管理员同形 request_id 不会命中。"""

    return connection.execute(
        """
        SELECT * FROM audit_exports
        WHERE tenant_id=? AND requested_by=? AND request_id=?
        """,
        (tenant_id, requested_by, request_id),
    ).fetchone()


def _replay_existing(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    filters: AuditFilters,
    filters_json: str,
    fingerprint: str,
    request_id: str,
    trace_id: str,
) -> AuditExportResult:
    """按持久快照重新生成 CSV，并要求规模和哈希与首次事实完全一致。"""

    if row["filter_fingerprint"] != fingerprint or row["filters_json"] != filters_json:
        # 同一 actor 的 operation ID 已绑定原筛选，禁止静默改意图后读取另一份文件。
        raise ApiError("audit_export.request_conflict")
    try:
        export_id = _matched(row["audit_export_id"], _EXPORT_ID)
        _matched(row["audit_log_id"], _AUDIT_ID)
        _matched(row["event_id"], _EVENT_ID)
        file_name = _matched(row["file_name"], _FILE_NAME)
        stored_hash = _matched(row["content_sha256"], _HASH)
        snapshot_sequence = _strict_int(row["snapshot_sequence"], 0, 2**63 - 1)
        if row["status"] != "completed" or row["format"] != "csv" or row["schema_version"] != 1:
            # 幂等重放只能读取冻结的 completed/csv/v1 事实，任何漂移都按存量损坏处理。
            raise ValueError("export_contract")
        _verify_sequence_integrity(connection, row["tenant_id"])
        _verify_snapshot_boundary(connection, row)
        content, row_count, generated_hash = _generate_csv(
            connection,
            row["tenant_id"],
            filters,
            snapshot_sequence,
        )
        if (
            row_count != _strict_int(row["row_count"], 0, MAX_EXPORT_ROWS)
            or len(content) != _strict_int(row["content_size_bytes"], 1, MAX_EXPORT_BYTES)
            or generated_hash != stored_hash
        ):
            # 重生成结果必须逐字节等价，禁止把不一致文件冒充原不可变导出。
            raise ValueError("content_drift")
    # 已明确的导出冲突或完整性错误不得被二次改写为通用异常。
    except ApiError:
        raise
    # 持久字段、SQLite 读取或类型恢复失败都只暴露固定完整性错误。
    except (KeyError, sqlite3.Error, TypeError, ValueError) as exc:
        # 异常正文可能包含损坏值或 SQL，不能进入 API 响应。
        raise ApiError("audit_export.integrity_failed") from exc
    return AuditExportResult(
        audit_export_id=export_id,
        file_name=file_name,
        content_sha256=stored_hash,
        content_size_bytes=len(content),
        row_count=row_count,
        request_id=request_id,
        trace_id=trace_id,
        content=content,
    )


def _verify_sequence_integrity(connection: sqlite3.Connection, tenant_id: str) -> None:
    """确认当前租户每条 AuditLog 恰有一条同租户稳定 sequence 映射。"""

    row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM audit_logs WHERE tenant_id=?) AS log_count,
          (SELECT COUNT(*) FROM audit_log_export_sequences WHERE tenant_id=?) AS sequence_count,
          (SELECT COUNT(*) FROM audit_logs log
             LEFT JOIN audit_log_export_sequences seq
               ON seq.tenant_id=log.tenant_id AND seq.audit_log_id=log.audit_log_id
            WHERE log.tenant_id=? AND seq.audit_log_sequence IS NULL) AS missing_count,
          (SELECT COUNT(*) FROM audit_log_export_sequences seq
             LEFT JOIN audit_logs log
               ON log.tenant_id=seq.tenant_id AND log.audit_log_id=seq.audit_log_id
            WHERE seq.tenant_id=? AND log.audit_log_id IS NULL) AS orphan_count
        """,
        (tenant_id, tenant_id, tenant_id, tenant_id),
    ).fetchone()
    if row is None or any(
        type(row[name]) is not int
        for name in ("log_count", "sequence_count", "missing_count", "orphan_count")
    ):
        # 计数类型异常说明映射表已损坏，不能继续生成不完整快照。
        raise ApiError("audit_export.integrity_failed")
    if (
        row["log_count"] != row["sequence_count"]
        or row["missing_count"] != 0
        or row["orphan_count"] != 0
    ):
        # 缺失或孤儿映射会让 sequence 水位漏行，必须整体失败关闭。
        raise ApiError("audit_export.integrity_failed")


def _snapshot_boundary(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> tuple[int, str | None, str | None]:
    """读取当前租户最大稳定 sequence 及其可人工复核的 AuditLog 边界。"""

    row = connection.execute(
        """
        SELECT seq.audit_log_sequence, log.audit_log_id, log.created_at
        FROM audit_log_export_sequences seq
        JOIN audit_logs log
          ON log.tenant_id=seq.tenant_id AND log.audit_log_id=seq.audit_log_id
        WHERE seq.tenant_id=?
        ORDER BY seq.audit_log_sequence DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if row is None:
        return 0, None, None
    sequence = _strict_int(row["audit_log_sequence"], 1, 2**63 - 1)
    audit_log_id = _matched(row["audit_log_id"], _AUDIT_ID)
    # 快照边界必须逐字引用不可变 AuditLog；这里只验证时区，不把 +08:00 改写成 Z。
    created_at = _stored_db_time(row["created_at"])
    return sequence, audit_log_id, created_at


def _verify_snapshot_boundary(connection: sqlite3.Connection, export: sqlite3.Row) -> None:
    """复核幂等导出的水位仍指向首次记录的同租户不可变边界。"""

    sequence = _strict_int(export["snapshot_sequence"], 0, 2**63 - 1)
    if sequence == 0:
        if export["snapshot_audit_log_id"] is not None or export["snapshot_created_at"] is not None:
            # 零水位必须同时没有边界 ID/时间，防止伪造空租户快照。
            raise ApiError("audit_export.integrity_failed")
        return
    boundary = connection.execute(
        """
        SELECT log.audit_log_id, log.created_at
        FROM audit_log_export_sequences seq
        JOIN audit_logs log
          ON log.tenant_id=seq.tenant_id AND log.audit_log_id=seq.audit_log_id
        WHERE seq.tenant_id=? AND seq.audit_log_sequence=?
        """,
        (export["tenant_id"], sequence),
    ).fetchone()
    if (
        boundary is None
        or boundary["audit_log_id"] != export["snapshot_audit_log_id"]
        or _stored_db_time(boundary["created_at"]) != export["snapshot_created_at"]
    ):
        # 水位对应行的 ID/时间任一漂移都会破坏确定性重放。
        raise ApiError("audit_export.integrity_failed")


def _generate_csv(
    connection: sqlite3.Connection,
    tenant_id: str,
    filters: AuditFilters,
    snapshot_sequence: int,
) -> tuple[bytes, int, str]:
    """用 cursor 逐行生成 BOM/CRLF CSV，并在每行后执行行数和真实字节门禁。"""

    where_sql, params = audit_where_clause(tenant_id, filters, table_alias="log")
    cursor = connection.execute(
        f"""
        SELECT log.*
        FROM audit_log_export_sequences seq
        JOIN audit_logs log
          ON log.tenant_id=seq.tenant_id AND log.audit_log_id=seq.audit_log_id
        WHERE {where_sql} AND seq.audit_log_sequence<=?
        ORDER BY log.created_at ASC, log.audit_log_id ASC
        """,
        [*params, snapshot_sequence],
    )
    buffer = io.BytesIO()
    buffer.write(codecs.BOM_UTF8)
    text = io.TextIOWrapper(buffer, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(text, lineterminator="\r\n")
    row_count = 0
    try:
        writer.writerow(CSV_COLUMNS)
        if buffer.tell() > MAX_EXPORT_BYTES:
            # 上限包含 BOM 和表头，超限时不允许释放任何部分文件。
            raise ApiError("audit_export.too_large")
        for stored_row in cursor:
            row_count += 1
            if row_count > MAX_EXPORT_ROWS:
                # 第 10001 行在写入前即失败，避免返回被截断却看似完整的 CSV。
                raise ApiError("audit_export.too_large")
            try:
                item = decode_audit_log_row(stored_row)
            # 存量 AuditLog 违反严格合同意味着整份导出不可信，不能跳过坏行。
            except ApiError as exc:
                # 统一转换为导出完整性错误，且不泄露坏行字段。
                raise ApiError("audit_export.integrity_failed") from exc
            writer.writerow(_csv_row(item))
            # write_through 让 tell() 反映包含 BOM、引号和 CRLF 的真实 UTF-8 字节数。
            if buffer.tell() > MAX_EXPORT_BYTES:
                raise ApiError("audit_export.too_large")
        text.flush()
        content = buffer.getvalue()
    finally:
        # detach 防止 TextIOWrapper 析构时关闭仍需读取的 BytesIO；cursor 由连接生命周期释放。
        text.detach()
    return content, row_count, hashlib.sha256(content).hexdigest()


def _csv_row(item: object) -> list[str]:
    """把严格 AuditLog DTO 映射为固定 18 列，并对每个单元格做公式注入防护。"""

    values = (
        item.audit_log_id,
        item.tenant_id,
        item.actor_user_id,
        item.actor_email,
        item.actor_role,
        item.action,
        item.object_type,
        item.object_id,
        item.object_name or "",
        _json_text(item.before_summary),
        _json_text(item.after_summary),
        _json_text(item.changed_fields),
        item.client_ip or "",
        item.user_agent or "",
        item.request_id,
        item.trace_id,
        _to_db_time(item.created_at),
        str(CSV_SCHEMA_VERSION),
    )
    return [_safe_csv_cell(value) for value in values]


def _safe_csv_cell(value: object) -> str:
    """先归一全部 Unicode C* 控制字符，再阻断 Excel/LibreOffice 公式前缀。"""

    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in str(value)
    )
    if cleaned.lstrip().startswith(("=", "+", "-", "@")):
        cleaned = "'" + cleaned
    # 顺序错误会让控制字符绕过公式检测；最终不变量在返回前再次失败关闭。
    if any(unicodedata.category(character).startswith("C") for character in cleaned):
        # 归一后仍有控制字符说明实现合同失效，禁止把危险单元格写入 CSV。
        raise ApiError("audit_export.integrity_failed")
    if cleaned.lstrip().startswith(("=", "+", "-", "@")):
        # 最终值仍以公式前缀开头时必须失败，而不能依赖办公软件的运行设置。
        raise ApiError("audit_export.integrity_failed")
    return cleaned


def _insert_export(
    connection: sqlite3.Connection,
    *,
    audit_export_id: str,
    tenant_id: str,
    requested_by: str,
    audit_log_id: str,
    filters_json: str,
    fingerprint: str,
    snapshot_sequence: int,
    snapshot_audit_log_id: str | None,
    snapshot_created_at: str | None,
    row_count: int,
    content_size_bytes: int,
    content_sha256: str,
    file_name: str,
    request_id: str,
    trace_id: str,
    event_id: str,
    event_payload_json: str,
    created_at: datetime,
) -> None:
    """插入不可变 AuditExport 复核事实，不保存 CSV 正文或任意数据库路径。"""

    timestamp = _to_db_time(created_at)
    connection.execute(
        """
        INSERT INTO audit_exports(
          audit_export_id, tenant_id, requested_by, audit_log_id, status, format,
          schema_version, filters_json, filter_fingerprint, snapshot_sequence,
          snapshot_audit_log_id, snapshot_created_at, row_count, content_size_bytes,
          content_sha256, file_name, request_id, trace_id, event_id,
          event_payload_json, created_at, updated_at
        ) VALUES(?, ?, ?, ?, 'completed', 'csv', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_export_id,
            tenant_id,
            requested_by,
            audit_log_id,
            filters_json,
            fingerprint,
            snapshot_sequence,
            snapshot_audit_log_id,
            snapshot_created_at,
            row_count,
            content_size_bytes,
            content_sha256,
            file_name,
            request_id,
            trace_id,
            event_id,
            event_payload_json,
            timestamp,
            timestamp,
        ),
    )


def _insert_source_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    tenant_id: str,
    audit_export_id: str,
    created_at: datetime,
) -> None:
    """插入可恢复 source 状态；事件 payload 由不可变 AuditExport 持有。"""

    timestamp = _to_db_time(created_at)
    connection.execute(
        """
        INSERT INTO audit_export_events(
          audit_export_event_id, tenant_id, audit_export_id, status, attempt_count,
          next_attempt_at, claim_token, claim_owner, lease_until, last_error_code,
          dispatched_at, created_at, updated_at
        ) VALUES(?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
        """,
        (event_id, tenant_id, audit_export_id, timestamp, timestamp, timestamp),
    )


def _event_payload(
    *,
    audit_export_id: str,
    row_count: int,
    content_size_bytes: int,
    content_sha256: str,
    filter_fingerprint: str,
) -> dict[str, object]:
    """构造共享 Webhook 合同允许的固定八字段导出摘要。"""

    return {
        "audit_export_id": audit_export_id,
        "status": "completed",
        "format": "csv",
        "schema_version": CSV_SCHEMA_VERSION,
        "row_count": row_count,
        "content_size_bytes": content_size_bytes,
        "content_sha256": content_sha256,
        "filter_fingerprint": filter_fingerprint,
    }


def _file_name(audit_export_id: str, created_at: datetime) -> str:
    """生成不含租户、邮箱、筛选和路径字符的 ASCII 下载文件名。"""

    return f"metahub-audit-{created_at.astimezone(UTC):%Y%m%d}-{audit_export_id[4:12]}.csv"


# canonical JSON 是 CSV 重放、筛选指纹和事件摘要保持同字节的共同基础。
def _json_text(value: object) -> str:
    """生成拒绝 NaN、固定键顺序的规范 UTF-8 JSON，保证重放字节稳定。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    """校验持久安全 ID/hash/文件名，不把损坏原值放进错误。"""

    if type(value) is not str or pattern.fullmatch(value) is None:
        # 损坏持久标识不得回显原值，只交给上层转换成固定完整性错误。
        raise ValueError("stored_text")
    return value


def _strict_int(value: object, minimum: int, maximum: int) -> int:
    """恢复 SQLite 严格整数；bool、字符串和越界值均视为完整性损坏。"""

    if type(value) is not int or not minimum <= value <= maximum:
        # bool、字符串或越界数字会破坏规模和水位语义，必须拒绝隐式转换。
        raise ValueError("stored_integer")
    return value


def _stored_db_time(value: object) -> str:
    """验证存量时间带时区并保留原始文本，供不可变快照 owner 逐字核对。"""

    if type(value) is not str:
        # 非文本数据库时间无法形成可复核边界，统一按完整性损坏处理。
        raise ApiError("audit_export.integrity_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析失败时不得把原始数据库值放进响应或日志。
    except ValueError as exc:
        # 只暴露固定完整性错误，保留异常链供受控测试定位。
        raise ApiError("audit_export.integrity_failed") from exc
    if parsed.tzinfo is None:
        # 无时区时间在不同部署环境会产生不同 CSV，强审计必须失败关闭。
        raise ApiError("audit_export.integrity_failed")
    # AuditExport trigger 要求边界时间与 AuditLog 完全相同，不能把合法 +08:00 改写为 Z。
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建启用外键的短连接，并隐藏数据库地址与驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 连接异常可能含绝对路径或驱动细节，统一转换后再离开服务边界。
    except DbError as exc:
        # 导出不可用允许调用方用同一 operation ID 安全重试。
        raise ApiError("audit_export.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前导出事务，回滚异常不得覆盖首个领域错误。"""

    try:
        connection.rollback()
    # 回滚失败不能覆盖首个导出错误，也不能输出数据库异常正文。
    except sqlite3.Error:
        pass


def _utcnow() -> datetime:
    """返回带时区 UTC 当前时间，集中时间来源便于专项测试注入。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把带时区时间稳定写成 UTC 秒级文本。"""

    if value.tzinfo is None:
        # 内部时间也必须带时区，避免写入依赖主机本地时区的不可重放事实。
        raise ValueError("timezone_required")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["CSV_COLUMNS", "export_audit_logs"]
