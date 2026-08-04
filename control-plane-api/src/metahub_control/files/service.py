"""PRD-072 文件上传、解析、DLP 和加密对象存储的租户事务服务。

本服务只在授权调用栈中接触原文件、抽取正文和解密文件名。持久化层保存 owner-bound
AES-GCM 密文、分域 HMAC、固定安全计数和 source event；日志、Webhook 和公共响应均不
包含正文、对象键、fingerprint、规则 pattern 或命中片段。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.files import (
    EXTRACTOR_VERSION_BY_KIND,
    EncryptedLocalBlobStore,
    ExtractedText,
    FileBlobOwner,
    FileContractError,
    FileDlpDecision,
    FileDlpMatch,
    FileDlpRule,
    ValidatedUploadFormat,
    decrypt_file_blob,
    encrypt_file_blob,
    extract_file_text,
    file_content_fingerprint,
    file_object_key,
    normalize_filename,
    scan_file_text,
    validate_file_secret,
    validate_upload_format,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event
from pydantic import ValidationError

from metahub_control.auth import AuthContext, require_transaction_permission

from .models import FileExtractInfo, FileInfo, FileListResponse, FileResponse
from .multipart import ParsedFilePart

logger = logging.getLogger("metahub.control.files")

# 列表上限、资源标识、链路标识、游标签名域、终态集合和公共查询列共同冻结文件域的
# 输入边界与安全投影；任何新增状态或列都必须同步迁移、DTO、权限和完整性测试。
DEFAULT_FILE_LIST_LIMIT = 25
MAX_FILE_LIST_LIMIT = 100
MAX_FILE_DLP_RULES = 100
_FILE_ID_PATTERN = re.compile(r"^file_[0-9a-f]{32}$")
_CHAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
_CURSOR_DOMAIN = b"metahub/file/pagination/v1\x00"
_VALID_FILE_STATUSES = frozenset({"processing", "ready", "blocked", "failed"})
_TERMINAL_FAILURE_CODES = frozenset(
    {
        "file.structure_invalid",
        "file.no_extractable_text",
        "file.extract_limit_exceeded",
        "file.parse_failed",
        "file.dlp_unavailable",
        "file.storage_unavailable",
        "file.integrity_failed",
    }
)

_FILE_SELECT = """
SELECT
  file.file_id,file.tenant_id,file.uploaded_by,file.source_request_id,
  file.source_trace_id,file.original_name_ciphertext,file.file_extension,
  file.media_type,file.size_bytes,file.content_fingerprint,file.status,
  file.virus_scan_status,file.dlp_scan_status,file.dlp_action,
  file.effective_dlp_rule_id,file.match_count,file.ruleset_fingerprint,
  file.storage_backend,file.original_object_key,file.failure_error_code,
  file.created_at,file.updated_at,file.completed_at,
  extract.file_extract_id,extract.status AS extract_status,
  extract.extractor_version,extract.text_size_bytes,extract.text_code_points,
  extract.estimated_tokens,extract.estimator_version,extract.text_fingerprint,
  extract.storage_backend AS extract_storage_backend,
  extract.extracted_object_key,extract.failure_error_code AS extract_failure_error_code
FROM uploaded_files file
LEFT JOIN file_extracts extract
  ON extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
"""


@dataclass(frozen=True, slots=True)
class _ProcessingFile:
    """processing owner 提交后继续解析所需的最小内存事实。

    原始正文和独立根密钥关闭 repr；文件名只存在于 Shared 验证对象中并由其关闭 repr。
    source event 已通过 exact-key Webhook 合同，可在终态后安全派发。
    """

    tenant_id: str
    actor_user_id: str
    file_id: str
    validated: ValidatedUploadFormat
    content: bytes = field(repr=False)
    content_fingerprint: str = field(repr=False)
    secret: str = field(repr=False)
    blob_store: EncryptedLocalBlobStore = field(repr=False)
    request_id: str
    trace_id: str
    created_at: datetime
    uploaded_event: WebhookEvent


@dataclass(frozen=True, slots=True)
class _TerminalSpec:
    """UploadedFile/FileExtract 终态事务和扫描事件的完整安全输入。"""

    status: Literal["ready", "blocked", "failed"]
    dlp_scan_status: Literal["pending", "clean", "matched", "blocked", "unavailable"]
    dlp_action: Literal["allow", "log_only", "block"] | None
    effective_dlp_rule_id: str | None
    match_count: int
    ruleset_fingerprint: str | None
    extract_status: Literal["ready", "blocked", "failed"]
    file_extract_id: str
    extractor_version: str
    text_size_bytes: int | None
    text_code_points: int | None
    estimated_tokens: int | None
    estimator_version: str | None
    text_fingerprint: str | None = field(repr=False)
    original_object_key: str | None = field(repr=False)
    extracted_object_key: str | None = field(repr=False)
    matches: tuple[FileDlpMatch, ...]
    failure_error_code: str | None
    completed_at: datetime


class FileProcessingError(Exception):
    """携带已提交安全事件的内部上传失败控制流。

    路由仍把 ``error`` 转为全局统一错误体；``events`` 只含 Shared WebhookEvent，不含文件名、
    正文或存储定位，可安全附加到错误响应的 BackgroundTasks。
    """

    def __init__(self, error: ApiError, events: tuple[WebhookEvent, ...]) -> None:
        """保存固定业务错误和已经提交的 source event 信封。"""

        super().__init__(error.code)
        self.error = error
        self.events = events


def create_file(
    config: AppConfig,
    context: AuthContext,
    part: ParsedFilePart,
    *,
    request_id: str,
    trace_id: str,
) -> tuple[FileResponse, tuple[WebhookEvent, ...]]:
    """创建 processing owner，同步解析并提交唯一终态。

    参数：部署配置、可信认证上下文、鉴权后读取的有界 part 和链路 ID。返回：ready 安全
    投影及 uploaded/scanned 事件。关键约束：格式门禁在业务 owner 前；processing 与终态
    分两笔事务，且每笔写事务都重新复核 ``file.write``，关闭撤权竞态。
    """

    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=128)
    if len(part.content) > config.file_max_upload_bytes:
        # multipart 已执行同一上限；服务层仍复核，防止内部调用绕过 HTTP 边界。
        raise ApiError("file.too_large")
    try:
        validated = validate_upload_format(
            filename=part.filename,
            media_type=part.media_type,
            content=part.content,
        )
        secret = _file_secret(config)
        blob_store = EncryptedLocalBlobStore(config.file_storage_root)
    # 格式、密钥或存储根合同失败发生在 owner 创建前，不能产生半可信数据库记录。
    except FileContractError as exc:
        # owner 尚未创建，合同错误只返回固定码，不能记录文件名或 Shared 异常链。
        raise ApiError(exc.code) from None

    processing = _create_processing_file(
        config,
        context,
        validated=validated,
        content=part.content,
        secret=secret,
        blob_store=blob_store,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    try:
        return _process_file(config, context, processing)
    # 已提交终态的控制流携带安全 source event，必须原样交给路由安排后台派发。
    except FileProcessingError:
        # 已完成固定错误与 source event 编排的控制流必须原样交给路由。
        raise
    except ApiError as exc:
        # 二次权限或终态数据库故障可能让 owner 保持 processing；至少派发已提交 uploaded。
        raise FileProcessingError(exc, (processing.uploaded_event,)) from None
    except FileContractError as exc:
        # 未被具体阶段收敛的 Shared 错误仍只暴露固定业务码，不链接敏感异常。
        raise FileProcessingError(
            ApiError(exc.code),
            (processing.uploaded_event,),
        ) from None
    except Exception:
        # 未知内部异常不得携带 parser、路径、正文或 SQL；processing 行保留崩溃可观测性。
        raise FileProcessingError(
            ApiError("file.storage_unavailable"),
            (processing.uploaded_event,),
        ) from None


def get_file(
    config: AppConfig,
    context: AuthContext,
    file_id: object,
    *,
    request_id: str,
    trace_id: str,
) -> FileResponse:
    """按 tenant + file_id 返回一个授权安全投影。

    参数中的资源 ID 只用于当前 tenant SQL；非法、不存在和跨租户同形统一返回
    ``file.not_found``，避免枚举其他租户对象。
    """

    normalized_id = _normalize_file_id(file_id)
    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=160)
    secret = _file_secret_api(config)
    connection = _connect(config)
    try:
        row = connection.execute(
            f"{_FILE_SELECT} WHERE file.tenant_id=? AND file.file_id=?",
            (context.user.tenant_id, normalized_id),
        ).fetchone()
        if row is None:
            # tenant-qualified 查询无结果时统一返回不存在，不能区分跨租户对象与随机 ID。
            raise ApiError("file.not_found")
        info = _file_info(row, secret=secret)
        return FileResponse(
            file=info,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
    # 业务不存在或完整性错误已经脱敏，必须保持原始统一错误码。
    except ApiError:
        raise
    # 文件名密文或 owner 合同失败不得回显密文、对象键或底层异常文本。
    except FileContractError as exc:
        # Shared 固定错误码可以安全映射，异常链仍从公共响应中移除。
        raise ApiError(exc.code) from None
    # 数据库读取故障包含路径或 SQL 风险，必须在进入统一响应前折叠。
    except sqlite3.Error:
        # SQL、路径和驱动文本都可能包含部署信息，读接口统一折叠为文件存储不可用。
        raise ApiError("file.storage_unavailable") from None
    finally:
        connection.close()


def list_files(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> FileListResponse:
    """按 tenant、可选状态和签名游标列出文件安全投影。

    游标签名绑定 tenant、筛选和当前 authorization fingerprint；权限矩阵变化后旧游标失败，
    不能被用于跨授权快照继续枚举文件名。
    """

    normalized_status = _normalize_optional_status(status)
    normalized_limit = _normalize_limit(limit)
    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=160)
    secret = _file_secret_api(config)
    after = _decode_cursor(
        cursor,
        secret=secret,
        tenant_id=context.user.tenant_id,
        status=normalized_status,
        authorization_fingerprint=context.authorization_fingerprint,
    )
    clauses = ["file.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status is not None:
        clauses.append("file.status=?")
        params.append(normalized_status)
    if after is not None:
        clauses.append(
            "(file.created_at<? OR (file.created_at=? AND file.file_id<?))"
        )
        params.extend([after[0], after[0], after[1]])
    params.append(normalized_limit + 1)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            {_FILE_SELECT}
            WHERE {' AND '.join(clauses)}
            ORDER BY file.created_at DESC,file.file_id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        has_more = len(rows) > normalized_limit
        visible_rows = rows[:normalized_limit]
        files = [_file_info(row, secret=secret) for row in visible_rows]
        next_cursor: str | None = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_cursor(
                secret=secret,
                tenant_id=context.user.tenant_id,
                status=normalized_status,
                authorization_fingerprint=context.authorization_fingerprint,
                created_at=str(last["created_at"]),
                file_id=str(last["file_id"]),
            )
        return FileListResponse(
            files=files,
            next_cursor=next_cursor,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
    # 游标签名、授权指纹或 DTO 完整性错误已经规范化，保持精确业务码。
    except ApiError:
        raise
    # 解密或格式合同异常只映射稳定文件错误，不泄漏安全材料。
    except FileContractError as exc:
        # 去掉异常链可防止根密钥、密文或 owner AAD 出现在日志中。
        raise ApiError(exc.code) from None
    # SQL 查询或物化失败统一关闭列表，不返回可能不完整的租户文件集合。
    except sqlite3.Error:
        # 固定不可用错误隐藏数据库路径、语句和驱动实现细节。
        raise ApiError("file.storage_unavailable") from None
    finally:
        connection.close()


def dispatch_file_events_safely(
    config: AppConfig,
    events: tuple[WebhookEvent, ...],
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后按 uploaded -> scanned 顺序机会性派发安全 Webhook。

    网络、Endpoint 或 delivery 数据库失败由现有 Webhook 服务持久追踪；这里不能反向撤销
    文件终态，也不能记录异常文本、URL、文件名或事件 payload。
    """

    from metahub_control.webhooks.service import dispatch_webhook_event

    for event in events:
        try:
            dispatch_webhook_event(
                config,
                event.tenant_id,
                event,
                request_id=request_id,
                trace_id=trace_id,
            )
        except Exception:
            # 后台投递失败只记录固定身份与错误码，payload 和底层异常均不得进入日志。
            logger.error(
                "file.webhook_dispatch_failed",
                extra={
                    "event": "file.webhook_dispatch_failed",
                    "tenant_id": event.tenant_id,
                    "object_id": event.object_id,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "error_code": "webhook.delivery_unavailable",
                },
            )


def _create_processing_file(
    config: AppConfig,
    context: AuthContext,
    *,
    validated: ValidatedUploadFormat,
    content: bytes,
    secret: str,
    blob_store: EncryptedLocalBlobStore,
    request_id: str,
    trace_id: str,
) -> _ProcessingFile:
    """原子写入 processing owner 与上传 source event。

    参数包含可信认证上下文、格式验证结果、调用栈正文和独立文件密钥，返回后续解析所需
    最小事实。写事务内再次校验 ``file.write``，文件名只以 owner-bound 密文持久化。
    """

    file_id = f"file_{uuid4().hex}"
    created_at = _utcnow()
    created_at_db = _to_db_time(created_at)
    try:
        content_fingerprint = file_content_fingerprint(
            content,
            secret=secret,
            tenant_id=context.user.tenant_id,
            file_id=file_id,
            blob_kind="original",
        )
        filename_ciphertext = encrypt_file_blob(
            validated.original_name.encode("utf-8", errors="strict"),
            secret=secret,
            owner=FileBlobOwner(
                tenant_id=context.user.tenant_id,
                file_id=file_id,
                blob_kind="filename",
                content_fingerprint=content_fingerprint,
            ),
        )
        event = _uploaded_event(
            tenant_id=context.user.tenant_id,
            actor_user_id=context.user.user_id,
            file_id=file_id,
            media_type=validated.media_type,
            size_bytes=len(content),
            request_id=request_id,
            trace_id=trace_id,
            created_at=created_at,
        )
    # 文件名编码、加密或事件合同任一异常都发生在建 owner 前，禁止留下半成品记录。
    except (UnicodeEncodeError, ValueError, FileContractError):
        # 密钥、AAD、文件名编码或 exact-key 事件任一故障都发生在 owner 提交前，统一失败关闭。
        raise ApiError("file.storage_unavailable") from None

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "file.write",
            unavailable_error_code="file.storage_unavailable",
        )
        connection.execute(
            """
            INSERT INTO uploaded_files(
              file_id,tenant_id,uploaded_by,source_request_id,source_trace_id,
              original_name_ciphertext,file_extension,media_type,size_bytes,
              content_fingerprint,status,virus_scan_status,dlp_scan_status,
              dlp_action,effective_dlp_rule_id,match_count,ruleset_fingerprint,
              storage_backend,original_object_key,failure_error_code,
              created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'processing','not_configured','pending',
                     NULL,NULL,0,NULL,NULL,NULL,NULL,?,?,NULL)
            """,
            (
                file_id,
                live_context.user.tenant_id,
                live_context.user.user_id,
                request_id,
                trace_id,
                filename_ciphertext,
                validated.extension,
                validated.media_type,
                len(content),
                content_fingerprint,
                created_at_db,
                created_at_db,
            ),
        )
        _insert_file_event(
            connection,
            event,
            file_status="processing",
            media_type=validated.media_type,
            size_bytes=len(content),
            virus_scan_status="not_configured",
            dlp_scan_status="pending",
            dlp_action=None,
            match_count=0,
            estimated_tokens=None,
            extractor_version=None,
        )
        connection.commit()
    # 事务内权限撤销或预先规范化完整性错误必须先回滚，再保持原错误码。
    except ApiError:
        connection.rollback()
        raise
    # 唯一键、外键或 trigger 拒绝意味着 owner 状态图不一致，不能按普通不可用重试。
    except sqlite3.IntegrityError:
        connection.rollback()
        # 固定完整性错误隐藏 SQL、约束名和租户内对象标识。
        raise ApiError("file.integrity_failed") from None
    # 其他 SQLite 故障统一回滚并折叠为可恢复存储不可用。
    except sqlite3.Error:
        connection.rollback()
        # 公共错误不得暴露数据库文件路径、锁状态或驱动异常消息。
        raise ApiError("file.storage_unavailable") from None
    finally:
        connection.close()

    _log_file_state(
        event="file.uploaded",
        tenant_id=context.user.tenant_id,
        actor_user_id=context.user.user_id,
        file_id=file_id,
        request_id=request_id,
        trace_id=trace_id,
        status="processing",
        media_type=validated.media_type,
        size_bytes=len(content),
        estimated_tokens=None,
        error_code=None,
    )
    return _ProcessingFile(
        tenant_id=context.user.tenant_id,
        actor_user_id=context.user.user_id,
        file_id=file_id,
        validated=validated,
        content=content,
        content_fingerprint=content_fingerprint,
        secret=secret,
        blob_store=blob_store,
        request_id=request_id,
        trace_id=trace_id,
        created_at=created_at,
        uploaded_event=event,
    )


def _process_file(
    config: AppConfig,
    context: AuthContext,
    processing: _ProcessingFile,
) -> tuple[FileResponse, tuple[WebhookEvent, ...]]:
    """执行深层解析、全文 DLP、加密发布和终态提交。"""

    extractor_version = EXTRACTOR_VERSION_BY_KIND[processing.validated.kind]
    file_extract_id = f"fext_{uuid4().hex}"
    try:
        extracted = extract_file_text(processing.content, processing.validated.kind)
    # 深层容器或抽取边界失败必须推进同一 owner 的 failed 终态，不能只抛出后遗留 processing。
    except FileContractError as exc:
        # 已建立可信 owner 后，解析失败必须写 FileExtract/file.scanned 固定失败终态。
        return _record_failure_and_raise(
            config,
            context,
            processing,
            file_extract_id=file_extract_id,
            extractor_version=extractor_version,
            error_code=exc.code,
            extracted=None,
            decision=None,
            text_fingerprint=None,
        )

    extracted_bytes = extracted.text.encode("utf-8")
    try:
        text_fingerprint = file_content_fingerprint(
            extracted_bytes,
            secret=processing.secret,
            tenant_id=processing.tenant_id,
            file_id=processing.file_id,
            blob_kind="extract",
        )
    # 抽取正文指纹失败时不得发布任一 Blob，仍需记录可追踪 failed 终态。
    except FileContractError as exc:
        return _record_failure_and_raise(
            config,
            context,
            processing,
            file_extract_id=file_extract_id,
            extractor_version=extractor_version,
            error_code=exc.code,
            extracted=None,
            decision=None,
            text_fingerprint=None,
        )

    try:
        rules = _load_file_dlp_rules(config, processing.tenant_id)
        decision = scan_file_text(extracted.text, rules)
    # 规则读取、编译或扫描任一失败都视为 DLP 不可用并失败关闭全文处理。
    except (FileContractError, ApiError):
        # 规则数据库、编译或扫描异常一律失败关闭，不得继续加密发布正文对象。
        return _record_failure_and_raise(
            config,
            context,
            processing,
            file_extract_id=file_extract_id,
            extractor_version=extractor_version,
            error_code="file.dlp_unavailable",
            extracted=extracted,
            decision=None,
            text_fingerprint=text_fingerprint,
        )

    if decision.status == "blocked":
        # 阻断终态只保存安全计数和每规则摘要，原文件与抽取正文均不创建 Blob。
        completed_at = _utcnow()
        spec = _TerminalSpec(
            status="blocked",
            dlp_scan_status="blocked",
            dlp_action="block",
            effective_dlp_rule_id=decision.effective_dlp_rule_id,
            match_count=decision.match_count,
            ruleset_fingerprint=decision.ruleset_fingerprint,
            extract_status="blocked",
            file_extract_id=file_extract_id,
            extractor_version=extracted.extractor_version,
            text_size_bytes=extracted.text_size_bytes,
            text_code_points=extracted.text_code_points,
            estimated_tokens=extracted.estimated_tokens,
            estimator_version=extracted.estimator_version,
            text_fingerprint=text_fingerprint,
            original_object_key=None,
            extracted_object_key=None,
            matches=decision.matches,
            failure_error_code="file.dlp_blocked",
            completed_at=completed_at,
        )
        info, scanned_event = _commit_terminal(config, context, processing, spec)
        _log_terminal(processing, spec)
        # 返回 403 的同时保留可通过列表按 upload_request_id 查到的 blocked 事实。
        raise FileProcessingError(
            ApiError("file.dlp_blocked"),
            (processing.uploaded_event, scanned_event),
        )

    try:
        original_object_key, extracted_object_key = _publish_ready_blobs(
            processing,
            file_extract_id=file_extract_id,
            extracted=extracted,
            extracted_bytes=extracted_bytes,
            text_fingerprint=text_fingerprint,
        )
    # 密文 stage/publish 失败由统一失败终态追踪，不能保留部分对象或宣告 ready。
    except FileContractError as exc:
        return _record_failure_and_raise(
            config,
            context,
            processing,
            file_extract_id=file_extract_id,
            extractor_version=extractor_version,
            error_code=exc.code,
            extracted=extracted,
            decision=decision,
            text_fingerprint=text_fingerprint,
        )

    spec = _TerminalSpec(
        status="ready",
        dlp_scan_status=decision.status,
        dlp_action=decision.action,
        effective_dlp_rule_id=decision.effective_dlp_rule_id,
        match_count=decision.match_count,
        ruleset_fingerprint=decision.ruleset_fingerprint,
        extract_status="ready",
        file_extract_id=file_extract_id,
        extractor_version=extracted.extractor_version,
        text_size_bytes=extracted.text_size_bytes,
        text_code_points=extracted.text_code_points,
        estimated_tokens=extracted.estimated_tokens,
        estimator_version=extracted.estimator_version,
        text_fingerprint=text_fingerprint,
        original_object_key=original_object_key,
        extracted_object_key=extracted_object_key,
        matches=decision.matches,
        failure_error_code=None,
        completed_at=_utcnow(),
    )
    try:
        info, scanned_event = _commit_terminal(config, context, processing, spec)
    except Exception:
        # Blob 已先于数据库发布；终态提交失败时尽力删除本次两个认证密文，避免孤儿积累。
        _delete_published_quietly(
            processing.blob_store,
            (original_object_key, extracted_object_key),
        )
        raise
    _log_terminal(processing, spec)
    return (
        FileResponse(
            file=info,
            request_id=processing.request_id,
            trace_id=processing.trace_id,
        ),
        (processing.uploaded_event, scanned_event),
    )


def _record_failure_and_raise(
    config: AppConfig,
    context: AuthContext,
    processing: _ProcessingFile,
    *,
    file_extract_id: str,
    extractor_version: str,
    error_code: str,
    extracted: ExtractedText | None,
    decision: FileDlpDecision | None,
    text_fingerprint: str | None,
) -> tuple[FileResponse, tuple[WebhookEvent, ...]]:
    """提交 failed FileExtract/UploadedFile 后抛出带安全事件的统一错误。"""

    normalized_error = error_code if error_code in _TERMINAL_FAILURE_CODES else "file.parse_failed"
    dlp_status: Literal["pending", "clean", "matched", "unavailable"]
    # DLP 基础设施故障必须显式标记 unavailable，不能伪装为 clean 或普通解析失败。
    if normalized_error == "file.dlp_unavailable":
        dlp_status = "unavailable"
    elif decision is None:
        dlp_status = "pending"
    elif decision.status == "blocked":
        # 非阻断错误不能带 blocked DLP 组合；否则 UploadedFile CHECK 会把失败伪装为阻断。
        dlp_status = "unavailable"
    else:
        # storage/完整性失败发生在 DLP 后，保留已形成的 clean/matched 安全事实。
        dlp_status = "clean" if decision.status == "clean" else "matched"
    persisted_decision = decision if dlp_status in {"clean", "matched"} else None
    spec = _TerminalSpec(
        status="failed",
        dlp_scan_status=dlp_status,
        dlp_action=persisted_decision.action if persisted_decision is not None else None,
        effective_dlp_rule_id=(
            persisted_decision.effective_dlp_rule_id
            if persisted_decision is not None
            else None
        ),
        match_count=persisted_decision.match_count if persisted_decision is not None else 0,
        ruleset_fingerprint=(
            persisted_decision.ruleset_fingerprint
            if persisted_decision is not None
            else None
        ),
        extract_status="failed",
        file_extract_id=file_extract_id,
        extractor_version=extractor_version,
        text_size_bytes=(
            extracted.text_size_bytes
            if extracted is not None and text_fingerprint is not None
            else None
        ),
        text_code_points=(
            extracted.text_code_points
            if extracted is not None and text_fingerprint is not None
            else None
        ),
        estimated_tokens=(
            extracted.estimated_tokens
            if extracted is not None and text_fingerprint is not None
            else None
        ),
        estimator_version=(
            extracted.estimator_version
            if extracted is not None and text_fingerprint is not None
            else None
        ),
        text_fingerprint=text_fingerprint,
        original_object_key=None,
        extracted_object_key=None,
        # 0064 只允许 ready/blocked owner 保存逐规则摘要；failed 仅保留 UploadedFile 决策计数。
        matches=(),
        failure_error_code=normalized_error,
        completed_at=_utcnow(),
    )
    _info, scanned_event = _commit_terminal(config, context, processing, spec)
    _log_terminal(processing, spec)
    # 终态已提交后用内部控制流同时带出固定错误和两条安全事件，不包含正文或文件名。
    raise FileProcessingError(
        ApiError(normalized_error),
        (processing.uploaded_event, scanned_event),
    )


def _load_file_dlp_rules(config: AppConfig, tenant_id: str) -> tuple[FileDlpRule, ...]:
    """按 tenant-first 权威顺序冻结文件 DLP 规则快照。

    参数为部署配置和可信认证 tenant，返回最多一百条 active input/both 规则。规则只在
    当前扫描调用栈使用；查询、类型或编译输入异常都失败关闭，绝不降级为空规则集。
    """

    connection = _connect(config, error_code="file.dlp_unavailable")
    try:
        rows = connection.execute(
            """
            SELECT dlp_rule_id,rule_type,pattern,pattern_hash,action,priority,updated_at
            FROM dlp_rules
            WHERE tenant_id=? AND status='active' AND scan_scope IN ('input','both')
            ORDER BY priority ASC,updated_at DESC,dlp_rule_id ASC
            LIMIT ?
            """,
            (tenant_id, MAX_FILE_DLP_RULES + 1),
        ).fetchall()
        # 第 101 条用于探测规则集是否超出同步扫描边界；静默截断会让尾部 block 规则被绕过。
        if len(rows) > MAX_FILE_DLP_RULES:
            raise ApiError("file.dlp_unavailable")
        return tuple(
            FileDlpRule(
                dlp_rule_id=str(row["dlp_rule_id"]),
                rule_type=row["rule_type"],
                pattern=row["pattern"],
                pattern_hash=row["pattern_hash"],
                action=row["action"],
                priority=row["priority"],
                updated_at=row["updated_at"],
                order=index,
            )
            for index, row in enumerate(rows)
        )
    # 规则 SQL、字段类型或枚举漂移都不能静默跳过，否则敏感文件可能绕过预扫描。
    except (sqlite3.Error, TypeError, ValueError):
        # SQL 或持久规则形状异常都不得跳过，文件全文预扫描统一失败关闭。
        raise ApiError("file.dlp_unavailable") from None
    finally:
        connection.close()


def _publish_ready_blobs(
    processing: _ProcessingFile,
    *,
    file_extract_id: str,
    extracted: ExtractedText,
    extracted_bytes: bytes,
    text_fingerprint: str,
) -> tuple[str, str]:
    """加密、stage 并原子发布原文件和抽取正文两个对象。"""

    original_stage = None
    extract_stage = None
    published: list[str] = []
    try:
        original_ciphertext = encrypt_file_blob(
            processing.content,
            secret=processing.secret,
            owner=FileBlobOwner(
                tenant_id=processing.tenant_id,
                file_id=processing.file_id,
                blob_kind="original",
                content_fingerprint=processing.content_fingerprint,
            ),
        )
        extract_ciphertext = encrypt_file_blob(
            extracted_bytes,
            secret=processing.secret,
            owner=FileBlobOwner(
                tenant_id=processing.tenant_id,
                file_id=processing.file_id,
                file_extract_id=file_extract_id,
                blob_kind="extract",
                content_fingerprint=text_fingerprint,
                extractor_version=extracted.extractor_version,
            ),
        )
        original_stage = processing.blob_store.stage(
            original_ciphertext,
            object_key=file_object_key(processing.file_id, "original"),
        )
        extract_stage = processing.blob_store.stage(
            extract_ciphertext,
            object_key=file_object_key(processing.file_id, "extract"),
        )
        original_key = processing.blob_store.publish(original_stage)
        published.append(original_key)
        extracted_key = processing.blob_store.publish(extract_stage)
        published.append(extracted_key)
        return original_key, extracted_key
    # Shared 存储合同错误保留固定错误码，同时必须清理 staging 与已发布密文。
    except FileContractError:
        # 任一 stage/publish 失败都清理未发布 staging 和本次已发布密文，再保留固定错误码。
        if original_stage is not None:
            processing.blob_store.discard(original_stage)
        if extract_stage is not None:
            processing.blob_store.discard(extract_stage)
        _delete_published_quietly(processing.blob_store, tuple(published))
        raise
    except Exception:
        if original_stage is not None:
            processing.blob_store.discard(original_stage)
        if extract_stage is not None:
            processing.blob_store.discard(extract_stage)
        _delete_published_quietly(processing.blob_store, tuple(published))
        # 未知 I/O 或加密异常统一折叠，禁止把路径、密钥材料或正文写进异常链。
        raise FileContractError("file.storage_unavailable") from None


def _delete_published_quietly(
    blob_store: EncryptedLocalBlobStore,
    object_keys: tuple[str, ...],
) -> None:
    """尽力删除本次已发布密文且不覆盖原始事务错误。"""

    for object_key in object_keys:
        try:
            blob_store.delete(object_key)
        # 清理是原失败的补偿动作，不能用二次删除异常覆盖更准确的事务错误。
        except FileContractError:
            # 清理失败由对象巡检处理；路径、对象键和底层 I/O 异常都不得进入日志。
            continue


def _commit_terminal(
    config: AppConfig,
    context: AuthContext,
    processing: _ProcessingFile,
    spec: _TerminalSpec,
) -> tuple[FileInfo, WebhookEvent]:
    """原子推进 UploadedFile、插入 FileExtract/命中摘要和 scanned source event。

    参数：两阶段 owner 与完整终态规格。返回：提交前已验证的公共投影和安全事件。关键约束：
    先 CAS 更新 file，再插入要求终态 owner 的 extract；整笔事务再次复核 ``file.write``。
    """

    try:
        scanned_event = _scanned_event(processing, spec)
    # exact-key Webhook 合同拒绝意味着终态字段组合损坏，事务开始前即失败关闭。
    except ValueError:
        # exact-key 事件构造失败意味着服务字段组合不完整，业务终态不得先行提交。
        raise ApiError("file.integrity_failed") from None

    completed_at_db = _to_db_time(spec.completed_at)
    storage_backend = "local_encrypted_v1" if spec.status == "ready" else None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "file.write",
            unavailable_error_code="file.storage_unavailable",
        )
        # tenant/file/status 三重 CAS 防止迟到解析覆盖已经形成的 blocked/failed/ready 终态。
        cursor = connection.execute(
            """
            UPDATE uploaded_files
            SET status=?,dlp_scan_status=?,dlp_action=?,effective_dlp_rule_id=?,
                match_count=?,ruleset_fingerprint=?,storage_backend=?,
                original_object_key=?,failure_error_code=?,updated_at=?,completed_at=?
            WHERE tenant_id=? AND file_id=? AND status='processing'
            """,
            (
                spec.status,
                spec.dlp_scan_status,
                spec.dlp_action,
                spec.effective_dlp_rule_id,
                spec.match_count,
                spec.ruleset_fingerprint,
                storage_backend,
                spec.original_object_key,
                spec.failure_error_code,
                completed_at_db,
                completed_at_db,
                live_context.user.tenant_id,
                processing.file_id,
            ),
        )
        if cursor.rowcount != 1:
            # 同 owner 已被终结或跨 tenant 时不能猜测现存结果，统一按完整性失败关闭。
            raise ApiError("file.integrity_failed")

        connection.execute(
            """
            INSERT INTO file_extracts(
              file_extract_id,tenant_id,file_id,status,extractor_version,
              text_size_bytes,text_code_points,estimated_tokens,estimator_version,
              text_fingerprint,storage_backend,extracted_object_key,failure_error_code,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                spec.file_extract_id,
                live_context.user.tenant_id,
                processing.file_id,
                spec.extract_status,
                spec.extractor_version,
                spec.text_size_bytes,
                spec.text_code_points,
                spec.estimated_tokens,
                spec.estimator_version,
                spec.text_fingerprint,
                storage_backend,
                spec.extracted_object_key,
                spec.failure_error_code,
                completed_at_db,
                completed_at_db,
            ),
        )
        if spec.status in {"ready", "blocked"}:
            # 逐规则安全摘要只允许绑定 ready/blocked extract；failed 由固定错误和总计数追踪。
            for match in spec.matches:
                connection.execute(
                    """
                    INSERT INTO file_dlp_matches(
                      file_dlp_match_id,tenant_id,file_id,file_extract_id,dlp_rule_id,
                      rule_type,action,ruleset_fingerprint,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"fdlm_{uuid4().hex}",
                        live_context.user.tenant_id,
                        processing.file_id,
                        spec.file_extract_id,
                        match.dlp_rule_id,
                        match.rule_type,
                        match.action,
                        spec.ruleset_fingerprint,
                        completed_at_db,
                    ),
                )
        _insert_file_event(
            connection,
            scanned_event,
            file_status=spec.status,
            media_type=processing.validated.media_type,
            size_bytes=len(processing.content),
            virus_scan_status="not_configured",
            dlp_scan_status=spec.dlp_scan_status,
            dlp_action=spec.dlp_action,
            match_count=spec.match_count,
            estimated_tokens=spec.estimated_tokens,
            extractor_version=spec.extractor_version,
        )
        row = connection.execute(
            f"{_FILE_SELECT} WHERE file.tenant_id=? AND file.file_id=?",
            (live_context.user.tenant_id, processing.file_id),
        ).fetchone()
        if row is None:
            # CAS 后回读同 tenant owner 仍无结果表示事务图损坏，禁止提交不可验收终态。
            raise ApiError("file.integrity_failed")
        info = _file_info(row, secret=processing.secret)
        connection.commit()
        return info, scanned_event
    # 事务内撤权、CAS 或 DTO 完整性错误必须回滚，并保持原统一错误语义。
    except ApiError:
        connection.rollback()
        raise
    # Shared 解密或 owner 合同错误需要回滚，不能提交无法安全投影的终态。
    except FileContractError as exc:
        connection.rollback()
        # 只传播固定文件错误码，去掉可能含密钥域或对象键的异常链。
        raise ApiError(exc.code) from None
    # 约束、trigger 或唯一键失败代表数据库状态图损坏，不能当作普通连接故障。
    except sqlite3.IntegrityError:
        # 约束或 owner trigger 失败表示状态图不自洽，不能根据 SQLite 文本猜测业务原因。
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    # 其余数据库异常同样回滚，但映射为可重试存储不可用并隐藏部署细节。
    except sqlite3.Error:
        connection.rollback()
        # 错误响应不得包含 SQL、数据库路径、锁信息或底层异常正文。
        raise ApiError("file.storage_unavailable") from None
    finally:
        connection.close()


def _uploaded_event(
    *,
    tenant_id: str,
    actor_user_id: str,
    file_id: str,
    media_type: str,
    size_bytes: int,
    request_id: str,
    trace_id: str,
    created_at: datetime,
) -> WebhookEvent:
    """构造上传受理 source event 的安全信封。

    参数来自已认证 owner 与规范链路，返回通过 Shared exact-key 校验的 WebhookEvent。
    payload 只含文件 ID、类型、字节数和占位扫描状态，绝不包含文件名、正文或对象键。
    """

    created_at_db = _to_db_time(created_at)
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type="file.uploaded",
        event_id=f"fevt_{uuid4().hex}",
        timestamp=created_at,
        actor_id=actor_user_id,
        object_id=file_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload={
            "file_id": file_id,
            "status": "processing",
            "media_type": media_type,
            "size_bytes": size_bytes,
            "virus_scan_status": "not_configured",
            "created_at": created_at_db,
        },
    )


def _scanned_event(processing: _ProcessingFile, spec: _TerminalSpec) -> WebhookEvent:
    """构造文件终态扫描 source event 安全信封。

    参数为同一租户 processing owner 与已验证终态规格，返回 exact-key WebhookEvent；
    事件只投影状态、计数和固定错误码，不携带规则 ID、命中片段、正文或存储定位。
    """

    return build_webhook_event(
        tenant_id=processing.tenant_id,
        event_type="file.scanned",
        event_id=f"fevt_{uuid4().hex}",
        timestamp=spec.completed_at,
        actor_id=processing.actor_user_id,
        object_id=processing.file_id,
        trace_id=processing.trace_id,
        source_request_id=processing.request_id,
        payload={
            "file_id": processing.file_id,
            "status": spec.status,
            "media_type": processing.validated.media_type,
            "size_bytes": len(processing.content),
            "virus_scan_status": "not_configured",
            "dlp_scan_status": spec.dlp_scan_status,
            "dlp_action": spec.dlp_action,
            "match_count": spec.match_count,
            "estimated_tokens": spec.estimated_tokens,
            "extractor_version": spec.extractor_version,
            "completed_at": _to_db_time(spec.completed_at),
            "error_code": spec.failure_error_code,
        },
    )


def _insert_file_event(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    file_status: str,
    media_type: str,
    size_bytes: int,
    virus_scan_status: str,
    dlp_scan_status: str,
    dlp_action: str | None,
    match_count: int,
    estimated_tokens: int | None,
    extractor_version: str | None,
) -> None:
    """把已验证信封写为 append-only 文件 source event。

    参数包含当前写事务、Shared WebhookEvent 和规范化安全投影；函数无返回值。tenant、file、
    actor 与 source request 均从同 owner 事实绑定，禁止调用方另行提供跨租户归属。
    """

    connection.execute(
        """
        INSERT INTO file_events(
          file_event_id,tenant_id,file_id,event_type,actor_user_id,
          source_request_id,source_trace_id,file_status,media_type,size_bytes,
          virus_scan_status,dlp_scan_status,dlp_action,match_count,
          estimated_tokens,extractor_version,error_code,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.tenant_id,
            event.object_id,
            event.event_type,
            event.actor_id,
            # source_request_id 不属于 WebhookEvent 公开属性，只能取回同 owner UploadedFile 事实。
            _event_source_request_id(connection, event),
            event.trace_id,
            file_status,
            media_type,
            size_bytes,
            virus_scan_status,
            dlp_scan_status,
            dlp_action,
            match_count,
            estimated_tokens,
            extractor_version,
            event.payload.get("error_code"),
            event.timestamp,
        ),
    )


def _event_source_request_id(
    connection: sqlite3.Connection,
    event: WebhookEvent,
) -> str:
    """从同 tenant/file owner 回读 source_request_id，避免信封对象缺字段时猜测。"""

    row = connection.execute(
        """
        SELECT source_request_id FROM uploaded_files
        WHERE tenant_id=? AND file_id=?
        """,
        (event.tenant_id, event.object_id),
    ).fetchone()
    # source request 必须从同 tenant/file owner 精确回读，缺行或类型漂移都代表事件归属损坏。
    if row is None or type(row["source_request_id"]) is not str:
        # source event 找不到同租户 owner 时不能猜测 request ID，否则会破坏链路归属证据。
        raise ApiError("file.integrity_failed")
    return row["source_request_id"]


def _file_info(row: sqlite3.Row, *, secret: str) -> FileInfo:
    """解密授权文件名并把 joined 行转换为不含正文/对象键的公共 DTO。"""

    try:
        ciphertext = row["original_name_ciphertext"]
        fingerprint = row["content_fingerprint"]
        if type(ciphertext) is not bytes or type(fingerprint) is not str:
            # 文件名密文与内容指纹必须同时存在，缺失时禁止尝试解密或返回部分 DTO。
            raise FileContractError("file.integrity_failed")
        original_name = decrypt_file_blob(
            ciphertext,
            secret=secret,
            owner=FileBlobOwner(
                tenant_id=row["tenant_id"],
                file_id=row["file_id"],
                blob_kind="filename",
                content_fingerprint=fingerprint,
            ),
        ).decode("utf-8", errors="strict")
        normalized_name, kind = normalize_filename(original_name)
        if kind != row["file_extension"]:
            # 密文名称与持久格式漂移意味着 owner 图损坏，不能向页面返回矛盾内容。
            raise FileContractError("file.integrity_failed")

        extract: FileExtractInfo | None = None
        if row["file_extract_id"] is not None:
            extract = FileExtractInfo(
                file_extract_id=row["file_extract_id"],
                status=row["extract_status"],
                extractor_version=row["extractor_version"],
                text_size_bytes=row["text_size_bytes"],
                text_code_points=row["text_code_points"],
                estimated_tokens=row["estimated_tokens"],
                estimator_version=row["estimator_version"],
                failure_error_code=row["extract_failure_error_code"],
            )
        return FileInfo(
            file_id=row["file_id"],
            original_name=normalized_name,
            file_extension=row["file_extension"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            status=row["status"],
            virus_scan_status=row["virus_scan_status"],
            dlp_scan_status=row["dlp_scan_status"],
            dlp_action=row["dlp_action"],
            match_count=row["match_count"],
            failure_error_code=row["failure_error_code"],
            upload_request_id=row["source_request_id"],
            extract=extract,
            created_at=_parse_db_time(row["created_at"]),
            updated_at=_parse_db_time(row["updated_at"]),
            completed_at=(
                _parse_db_time(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
        )
    # 已判定的密文、owner 或字段完整性错误保持固定码，不能被宽泛转换覆盖。
    except FileContractError:
        raise
    # 解码、DTO 校验或持久字段类型漂移均可能包含敏感值，只统一映射完整性失败。
    except (UnicodeDecodeError, ValidationError, TypeError, ValueError, KeyError):
        # 持久值、密文 UTF-8 或 DTO 枚举异常都按完整性失败关闭，禁止回显数据库内容。
        raise FileContractError("file.integrity_failed") from None


def _encode_cursor(
    *,
    secret: str,
    tenant_id: str,
    status: str | None,
    authorization_fingerprint: str,
    created_at: str,
    file_id: str,
) -> str:
    """签发绑定 tenant、筛选和授权指纹的分页游标。"""

    payload = json.dumps(
        {
            "authorization_fingerprint": authorization_fingerprint,
            "created_at": created_at,
            "file_id": file_id,
            "status": status,
            "tenant_id": tenant_id,
            "version": 1,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        secret.encode("utf-8"),
        _CURSOR_DOMAIN + payload,
        hashlib.sha256,
    ).digest()
    return f"{_b64url(payload)}.{_b64url(signature)}"


def _decode_cursor(
    value: str | None,
    *,
    secret: str,
    tenant_id: str,
    status: str | None,
    authorization_fingerprint: str,
) -> tuple[str, str] | None:
    """认证并解析当前 tenant/筛选/授权快照的分页游标。"""

    if value is None:
        return None
    try:
        if type(value) is not str or len(value) > 2_048 or value.count(".") != 1:
            # 游标必须是有界的两段字符串，拒绝结构歧义和资源放大输入。
            raise ValueError
        encoded_payload, encoded_signature = value.split(".", 1)
        payload = _b64url_decode(encoded_payload)
        provided_signature = _b64url_decode(encoded_signature)
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            _CURSOR_DOMAIN + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            # 签名不匹配可能来自跨租户、撤权后旧游标或篡改，统一按无效输入处理。
            raise ValueError
        decoded = json.loads(payload.decode("ascii", errors="strict"))
        expected_fields = {
            "authorization_fingerprint",
            "created_at",
            "file_id",
            "status",
            "tenant_id",
            "version",
        }
        if type(decoded) is not dict or set(decoded) != expected_fields:
            # exact-key JSON 防止额外 tenant、排序或版本字段改变分页语义。
            raise ValueError
        if (
            decoded["version"] != 1
            or decoded["tenant_id"] != tenant_id
            or decoded["status"] != status
            or decoded["authorization_fingerprint"] != authorization_fingerprint
            or _FILE_ID_PATTERN.fullmatch(decoded["file_id"]) is None
        ):
            # tenant、筛选、授权指纹或资源 ID 任一漂移都使旧游标立即失效。
            raise ValueError
        created_at = decoded["created_at"]
        _parse_db_time(created_at)
        # 重新编码必须与输入 payload 完全相同，拒绝重复键、空白和非 canonical JSON 变体。
        canonical = json.dumps(
            decoded,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if canonical != payload:
            # 非 canonical JSON 可能隐藏重复键或替代编码，必须拒绝别名表示。
            raise ValueError
        return created_at, decoded["file_id"]
    # 游标签名、JSON、编码或字段校验任一失败都不能区分原因，以免形成授权探针。
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        # 固定校验错误不回显游标、签名、tenant 或授权指纹。
        raise ApiError("infra.validation_failed") from None


def _b64url(value: bytes) -> str:
    """把签名材料编码为规范 URL-safe Base64。

    参数为内存中的游标 payload 或 HMAC，返回无 padding ASCII 字符串；该函数不记录输入，
    以免 tenant、授权指纹或签名材料进入日志。
    """

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """严格解码规范 URL-safe Base64 游标分段。

    参数必须是非空白名单 ASCII，返回原始 bytes；解码后会重新编码比对，任何 padding、
    字符别名或非规范表示都抛出内部 ValueError 并由游标入口统一脱敏。
    """

    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        # 空值或非 URL-safe 字符会产生多种解码语义，必须在库调用前拒绝。
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _b64url(decoded) != value:
        # 重编码不一致代表 padding 或别名形式，不能接受为同一个签名输入。
        raise ValueError
    return decoded


def _file_secret(config: AppConfig) -> str:
    """验证文件独立根密钥强度和跨域隔离后返回当前调用栈 secret。"""

    secret = config.file_encryption_secret
    validate_file_secret(
        secret,
        production=config.environment == "prod",
        forbidden_secrets=(
            config.auth_token_secret,
            config.cache_affinity_hmac_secret,
            config.prompt_template_encryption_secret,
        ),
    )
    # 配置值只有在通过独立密钥强度与跨域复用检查后才允许离开该验证函数。
    if type(secret) is not str:
        # 配置模型允许缺省值用于禁用能力，但实际文件调用必须证明独立字符串密钥存在。
        raise FileContractError("file.storage_unavailable")
    # 只向当前调用栈返回已验证 secret，调用方不得记录或持久化该值。
    return secret


def _file_secret_api(config: AppConfig) -> str:
    """把根密钥合同异常折叠为文件 API 固定不可用错误。"""

    try:
        # 读取接口也必须执行与写入相同的独立密钥验证，不能使用未验证配置解密文件名。
        return _file_secret(config)
    # 密钥缺失、复用或强度不足都属于部署不可用，不向客户端区分具体原因。
    except FileContractError:
        # 去掉异常链，避免 secret 长度、环境或被比较的其他密钥域进入日志。
        raise ApiError("file.storage_unavailable") from None


def _normalize_file_id(value: object) -> str:
    """校验公共 file ID；非法格式与不存在统一返回 404。"""

    if type(value) is not str or _FILE_ID_PATTERN.fullmatch(value) is None:
        # 非法 ID 与跨租户同形对象都统一成 not_found，阻断资源枚举侧信道。
        raise ApiError("file.not_found")
    return value


def _normalize_chain_id(value: object, *, maximum: int) -> str:
    """校验写入 owner 与事件的链路标识。

    参数为中间件生成值和字段长度上限，返回规范 ASCII 标识；客户端正文、query 或 multipart
    不能覆盖 request_id/trace_id，装配漂移按服务完整性错误失败关闭。
    """

    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or _CHAIN_ID_PATTERN.fullmatch(value) is None
    ):
        # 中间件理论上保证格式；漂移属于服务装配完整性问题，不能接受客户端覆盖。
        raise ApiError("file.integrity_failed")
    return value


def _normalize_optional_status(value: object) -> str | None:
    """校验可选文件状态筛选并返回规范值。

    参数只能是 None 或冻结状态字符串，返回值直接进入 tenant-qualified SQL 参数；未知类型
    和状态统一拒绝，不能拼接 SQL 或静默降级为空筛选。
    """

    if value is None:
        return None
    if type(value) is not str or value not in _VALID_FILE_STATUSES:
        # 未知筛选值不能被当作全量查询，以免客户端错误扩大文件名可见范围。
        raise ApiError("infra.validation_failed")
    return value


def _normalize_limit(value: object) -> int:
    """校验内部列表 limit，bool 不得冒充整数。"""

    if type(value) is not int or not 1 <= value <= MAX_FILE_LIST_LIMIT:
        # 精确 int 与硬上限共同约束数据库读取和文件名解密成本，禁止自动截断。
        raise ApiError("infra.validation_failed")
    return value


def _connect(
    config: AppConfig,
    *,
    error_code: str = "file.storage_unavailable",
) -> sqlite3.Connection:
    """创建数据库连接并隐藏路径、驱动和配置错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL、驱动或文件路径异常均是部署细节，不能进入文件 API 响应和日志异常链。
    except DbError:
        # 调用方选择的稳定错误码区分 DLP 与存储阶段，但不会泄漏底层连接信息。
        raise ApiError(error_code) from None


def _utcnow() -> datetime:
    """返回去微秒 UTC，使 owner、事件和 trigger 使用同一秒。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把有时区时间编码为数据库规范 UTC 秒。

    参数为内部生成或已解析的 datetime，返回以 Z 结尾且无微秒的字符串，确保 owner、
    source event、游标和 trigger 对同一业务时刻使用唯一表示。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """严格解析持久 UTC 时间；非法值按文件完整性失败关闭。"""

    if type(value) is not str:
        # 非字符串时间代表迁移或持久化合同损坏，不能用隐式转换生成游标。
        raise FileContractError("file.integrity_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非 ISO 时间无法参与稳定排序和事件校验，统一视为文件事实完整性损坏。
    except ValueError:
        # 固定错误码不回显数据库原值，避免异常文本泄漏持久化内容。
        raise FileContractError("file.integrity_failed") from None
    if parsed.tzinfo is None:
        # 无时区时间会在不同部署主机产生不同 UTC 结果，不能接受为持久事实。
        raise FileContractError("file.integrity_failed")
    return parsed.astimezone(UTC)


def _log_terminal(processing: _ProcessingFile, spec: _TerminalSpec) -> None:
    """记录不含文件名、正文、hash、对象键或规则 ID 的终态摘要。"""

    _log_file_state(
        event="file.scanned",
        tenant_id=processing.tenant_id,
        actor_user_id=processing.actor_user_id,
        file_id=processing.file_id,
        request_id=processing.request_id,
        trace_id=processing.trace_id,
        status=spec.status,
        media_type=processing.validated.media_type,
        size_bytes=len(processing.content),
        estimated_tokens=spec.estimated_tokens,
        error_code=spec.failure_error_code,
    )


def _log_file_state(
    *,
    event: str,
    tenant_id: str,
    actor_user_id: str,
    file_id: str,
    request_id: str,
    trace_id: str,
    status: str,
    media_type: str,
    size_bytes: int,
    estimated_tokens: int | None,
    error_code: str | None,
) -> None:
    """写入文件处理安全结构化日志。"""

    logger.info(
        event,
        extra={
            "event": event,
            "tenant_id": tenant_id,
            "actor_id": actor_user_id,
            "object_id": file_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "status": status,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "estimated_tokens": estimated_tokens,
            "error_code": error_code,
        },
    )
