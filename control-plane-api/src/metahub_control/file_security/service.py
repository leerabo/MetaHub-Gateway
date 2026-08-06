"""PRD-073 文件安全扫描、报告完整性与模型上下文准入服务。

服务只在后端授权调用栈内解密原文件或抽取正文。所有 SQL 都以 AuthContext tenant 为首个
owner，写事务再次复核 ``file.read`` 与 ``file.security.scan``；日志、响应和 Webhook 不包含
文件名、正文、OCR 文本、对象键、fingerprint、evidence HMAC 或报告 hash。
"""

from __future__ import annotations

import csv
import hmac
import io
import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from queue import LifoQueue
from typing import Literal, cast
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.file_security import (
    BOUNDARY_VERSION,
    DETECTOR_VERSION,
    OCR_ENGINE_VERSION,
    BoundedFileContext,
    CanonicalSecurityFinding,
    CanonicalSecurityReport,
    FileSecurityError,
    FileSecurityScanResult,
    RapidOcrBackend,
    SecurityFindingDraft,
    build_bounded_file_context,
    canonical_report_sha256,
    finding_evidence_fingerprint,
    scan_file_security,
)
from metahub_shared.files import (
    EncryptedLocalBlobStore,
    FileBlobOwner,
    FileContractError,
    decrypt_file_blob,
    file_content_fingerprint,
    validate_file_secret,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event
from pydantic import ValidationError

from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    FileSecurityFindingInfo,
    FileSecurityReport,
    FileSecurityReportResponse,
)

logger = logging.getLogger("metahub.control.file_security")

# ID、指纹、链路、格式集合与 SQL 投影共同冻结租户 owner 和报告完整性读取边界。
_FILE_ID = re.compile(r"^file_[0-9a-f]{32}$")
_SCAN_ID = re.compile(r"^fscn_[0-9a-f]{32}$")
_FINGERPRINT = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_CHAIN_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_FILE_KINDS = frozenset({"txt", "docx", "pdf"})
_FINDING_ORDER_SQL = """
ORDER BY CASE severity
  WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2
  WHEN 'low' THEN 3 ELSE 4 END,
  detector_code,source_kind,location_kind,COALESCE(location_index,-1),finding_id
LIMIT 257
"""

_OWNER_SELECT = """
SELECT
  file.tenant_id,file.file_id,file.status AS file_status,file.file_extension,
  file.content_fingerprint,file.original_object_key,file.dlp_scan_status,
  extract.file_extract_id,extract.status AS extract_status,extract.extractor_version,
  extract.text_fingerprint,extract.extracted_object_key
FROM uploaded_files file
LEFT JOIN file_extracts extract
  ON extract.tenant_id=file.tenant_id AND extract.file_id=file.file_id
WHERE file.tenant_id=? AND file.file_id=?
"""

_SCAN_SELECT = """
SELECT
  scan_id,tenant_id,file_id,file_extract_id,requested_by,source_request_id,
  source_trace_id,attempt_no,status,risk_level,risk_score,coverage_status,
  model_access_status,detector_version,ocr_status,ocr_engine_version,
  scanned_page_count,scanned_image_count,finding_count,high_finding_count,
  boundary_version,content_fingerprint,extract_fingerprint,report_sha256,
  failure_error_code,created_at,updated_at,completed_at
FROM file_security_scans
"""


@dataclass(frozen=True, slots=True)
class _FileSecurityOwner:
    """从同 tenant ready File/FileExtract 行恢复的最小可信扫描 owner。"""

    tenant_id: str
    file_id: str
    file_extract_id: str
    kind: Literal["txt", "docx", "pdf"]
    extractor_version: str
    content_fingerprint: str = field(repr=False)
    extract_fingerprint: str = field(repr=False)
    original_object_key: str = field(repr=False)
    extracted_object_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PersistedFinding:
    """写事务中的 finding 身份、公共字段和私有 evidence HMAC。"""

    finding_id: str
    draft: SecurityFindingDraft = field(repr=False)
    evidence_fingerprint: str = field(repr=False)
    created_at: str


class OcrBackendPool:
    """按部署容量复用多个懒加载 RapidOCR 实例，避免跨请求重复加载本地模型。"""

    engine_version = OCR_ENGINE_VERSION

    def __init__(self, capacity: int) -> None:
        """创建固定大小的后端池；构造阶段不加载模型，也不访问网络。"""

        # OCR 池容量只接受部署硬上限，错误配置不能退化为无界线程或共享单例。
        if type(capacity) is not int or not 1 <= capacity <= 2:
            raise FileSecurityError("file.security_scan_unavailable")
        self._pool: LifoQueue[RapidOcrBackend] = LifoQueue(maxsize=capacity)
        for _index in range(capacity):
            self._pool.put(RapidOcrBackend())

    def recognize(self, image: object) -> Iterable[str]:
        """从池中独占一个 OCR 实例识别图片，并在异常路径中保证归还容量。"""

        backend = self._pool.get()
        try:
            # RapidOcrBackend 返回前已经执行 box/文本硬上限；这里不再复制第二份结果集合。
            return backend.recognize(image)
        finally:
            # OCR 文本不会留在池对象；容量归还只保留已加载本地模型实例。
            self._pool.put(backend)


def scan_security_report(
    config: AppConfig,
    context: AuthContext,
    file_id: object,
    *,
    request_id: object,
    trace_id: object,
    ocr_backend: OcrBackendPool,
) -> tuple[FileSecurityReportResponse, WebhookEvent | None]:
    """扫描当前 tenant 的 ready 文件，或幂等复用当前检测器权威报告。

    参数：部署配置、可信认证上下文、文件 ID、中间件链路 ID 和进程级 OCR 池。返回：严格
    报告及可选 Prompt Injection Webhook。关键约束：对象读取前复核权限，写入时再复核两项
    权限；任何 failed 事实只有成功提交数据库后才可追踪。
    """

    normalized_file_id = _normalize_file_id(file_id)
    normalized_request_id = _normalize_chain_id(request_id)
    normalized_trace_id = _normalize_chain_id(trace_id)
    secret = _file_secret(config)
    owner, existing = _load_owner_for_scan(
        config,
        context,
        normalized_file_id,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    if existing is not None:
        return existing, None

    scan_id = f"fscn_{uuid4().hex}"
    try:
        content, extracted_text = _read_scan_inputs_after_permission_recheck(
            config,
            context,
            owner,
            secret=secret,
        )
        result = scan_file_security(
            content,
            owner.kind,
            trusted_extracted_text=extracted_text,
            ocr_backend=ocr_backend,
            max_ocr_pages=config.file_security_max_ocr_pages,
            max_images=config.file_security_max_images,
            max_image_pixels=config.file_security_max_image_pixels,
            max_total_pixels=config.file_security_max_total_pixels,
        )
    # Blob 读取前的实时撤权是业务拒绝，不能伪造 failed 扫描事实或吞掉固定权限错误码。
    except ApiError:
        raise
    # Blob owner、密钥或存储合同失败只转换为固定码，并在数据库可写时记录 failed attempt。
    except FileContractError as exc:
        error_code = (
            "file.integrity_failed"
            if exc.code == "file.integrity_failed"
            else "file.security_scan_unavailable"
        )
        winner = _persist_failed_scan(
            config,
            context,
            owner,
            scan_id=scan_id,
            error_code=error_code,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        if winner is not None:
            return winner, None
        # 只有 failed 事实已成功提交后才返回原固定错误，不能把异常对象或路径带入响应。
        raise ApiError(error_code) from None
    # 检测器和 OCR 的已分类故障同样折叠为可追踪 attempt，禁止泄露解析器异常文本。
    except FileSecurityError as exc:
        error_code = (
            "file.integrity_failed"
            if exc.code == "file.integrity_failed"
            else "file.security_scan_unavailable"
        )
        winner = _persist_failed_scan(
            config,
            context,
            owner,
            scan_id=scan_id,
            error_code=error_code,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        if winner is not None:
            return winner, None
        # 没有并发权威赢家时保留固定错误语义，调用方可凭 request_id 追踪本次 attempt。
        raise ApiError(error_code) from None
    except Exception:
        # 未知解析/OCR异常可能携带正文或宿主路径，先折叠为固定码再尝试持久化 failed 事实。
        winner = _persist_failed_scan(
            config,
            context,
            owner,
            scan_id=scan_id,
            error_code="file.security_scan_unavailable",
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        if winner is not None:
            return winner, None
        # 未知异常不得离开正文处理边界，统一转换为暂不可用且不保留异常链。
        raise ApiError("file.security_scan_unavailable") from None

    return _persist_successful_scan(
        config,
        context,
        owner,
        result,
        secret=secret,
        scan_id=scan_id,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )


def get_security_report(
    config: AppConfig,
    context: AuthContext,
    file_id: object,
    *,
    request_id: object,
    trace_id: object,
) -> FileSecurityReportResponse:
    """读取当前 detector 的权威报告，或没有权威结果时读取最新 failed attempt。"""

    normalized_file_id = _normalize_file_id(file_id)
    normalized_request_id = _normalize_chain_id(request_id)
    normalized_trace_id = _normalize_chain_id(trace_id)
    connection = _connect(config)
    try:
        # 先证明文件属于当前 tenant；跨 tenant 与随机 ID 保持同形 404。
        owner_row = connection.execute(
            "SELECT status FROM uploaded_files WHERE tenant_id=? AND file_id=?",
            (context.user.tenant_id, normalized_file_id),
        ).fetchone()
        # tenant-qualified 查询未命中时与随机、跨租户对象同形，避免存在性枚举。
        if owner_row is None:
            raise ApiError("file.not_found")
        scan_row = _select_current_scan(connection, context.user.tenant_id, normalized_file_id)
        # 文件真实存在但从未产生当前版本报告时，返回独立恢复语义而非伪造 clean。
        if scan_row is None:
            raise ApiError("file.security_report_not_found")
        return _report_response(
            connection,
            scan_row,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            reused=True,
        )
    # 已归类的 404 等业务错误必须保持原码，不能被完整性兜底覆盖。
    except ApiError:
        raise
    # canonical 合同或 DTO 校验失败说明报告事实不可信，读取路径统一失败关闭。
    except (FileSecurityError, ValidationError):
        raise ApiError("file.integrity_failed") from None
    # 数据库驱动故障不能伪装成报告缺失，统一返回可重试的扫描依赖错误。
    except sqlite3.Error:
        raise ApiError("file.security_scan_unavailable") from None
    finally:
        connection.close()


def export_security_report_csv(
    config: AppConfig,
    context: AuthContext,
    file_id: object,
    *,
    request_id: object,
    trace_id: object,
) -> tuple[str, bytes]:
    """复用同一严格报告读取路径并生成不含文件名或私有摘要的 UTF-8 CSV。"""

    response = get_security_report(
        config,
        context,
        file_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    report = response.report
    header = (
        "scan_id",
        "file_id",
        "status",
        "risk_level",
        "risk_score",
        "coverage_status",
        "model_access_status",
        "detector_version",
        "boundary_version",
        "ocr_status",
        "ocr_engine_version",
        "scanned_page_count",
        "scanned_image_count",
        "finding_count",
        "high_finding_count",
        "finding_id",
        "detector_code",
        "category",
        "severity",
        "confidence_milli",
        "source_kind",
        "location_kind",
        "location_index",
        "finding_status",
        "finding_created_at",
        "created_at",
        "completed_at",
    )
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(header)
    findings: Sequence[FileSecurityFindingInfo | None] = report.findings or (None,)
    for finding in findings:
        row = (
            report.scan_id,
            report.file_id,
            report.status,
            report.risk_level,
            "" if report.risk_score is None else report.risk_score,
            report.coverage_status,
            report.model_access_status,
            report.detector_version,
            report.boundary_version,
            report.ocr_status,
            report.ocr_engine_version or "",
            report.scanned_page_count,
            report.scanned_image_count,
            report.finding_count,
            report.high_finding_count,
            "" if finding is None else finding.finding_id,
            "" if finding is None else finding.detector_code,
            "" if finding is None else finding.category,
            "" if finding is None else finding.severity,
            "" if finding is None else finding.confidence_milli,
            "" if finding is None else finding.source_kind,
            "" if finding is None else finding.location_kind,
            "" if finding is None or finding.location_index is None else finding.location_index,
            "" if finding is None else finding.status,
            "" if finding is None else _to_db_time(finding.created_at),
            _to_db_time(report.created_at),
            _to_db_time(report.completed_at),
        )
        writer.writerow([_csv_cell(value) for value in row])
    filename = f"{report.file_id}-security-report.csv"
    return filename, stream.getvalue().encode("utf-8")


def build_file_model_context(
    config: AppConfig,
    context: AuthContext,
    file_id: object,
) -> BoundedFileContext:
    """验证当前可信报告后解密抽取正文，并返回唯一受支持的模型文件上下文。

    调用方仍须在 Chat/Batch 入口校验自己的领域权限。本函数只执行 ``file.read``、tenant owner、
    报告 canonical hash、Blob AAD/fingerprint 和动态边界；未扫描或非 passed 报告在解密正文前阻断。
    """

    normalized_file_id = _normalize_file_id(file_id)
    secret = _file_secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live_context = require_transaction_permission(
            connection,
            context,
            "file.read",
            unavailable_error_code="file.security_scan_unavailable",
        )
        row = connection.execute(
            _OWNER_SELECT,
            (live_context.user.tenant_id, normalized_file_id),
        ).fetchone()
        # 模型入口对跨租户和随机 ID 仍保持同形 404，不泄露其他租户文件事实。
        if row is None:
            raise ApiError("file.not_found")
        try:
            owner = _owner_from_row(row)
        # 扫描管理态不得从内部模型入口泄露，non-ready 一律折叠为模型准入阻断。
        except ApiError as exc:
            if exc.code == "file.security_not_ready":
                # 模型消费者只需要知道准入被拒绝；不能借内部入口区分 DLP blocked、处理中或
                # 抽取失败等文件状态。扫描管理 API 仍保留独立 409，便于授权用户排障。
                raise ApiError("file.model_access_blocked") from None
            raise
        scan_row = _select_authoritative_scan(
            connection,
            owner.tenant_id,
            owner.file_id,
            owner=owner,
        )
        # 未扫描或只有 failed attempt 时不得解密抽取正文，模型准入在 Blob 读取前结束。
        if scan_row is None:
            raise ApiError("file.model_access_blocked")
        report = _report_response(
            connection,
            scan_row,
            request_id="model-context",
            trace_id="model-context",
            reused=True,
        ).report
        # 仅 passed、complete、allowed 三项同时成立才允许继续读取抽取密文。
        if not (
            report.status == "passed"
            and report.coverage_status == "complete"
            and report.model_access_status == "allowed"
        ):
            # 任一可信终态条件缺失都必须在抽取 Blob 解密之前统一阻断模型准入。
            raise ApiError("file.model_access_blocked")
        scan_id = report.scan_id
        connection.commit()
    # 业务拒绝路径回滚只读事务并保留固定错误，避免误转成数据库依赖故障。
    except ApiError:
        connection.rollback()
        raise
    # 报告 hash、枚举或 DTO 失配表示持久事实损坏，必须按完整性错误关闭。
    except (FileSecurityError, ValidationError):
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    # 数据库不可读时无法证明准入结论，不能退化为默认放行或普通未扫描状态。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("file.security_scan_unavailable") from None
    finally:
        connection.close()

    try:
        text = _read_extracted_text_after_permission_recheck(
            config,
            context,
            owner,
            secret=secret,
        )
        return build_bounded_file_context(
            text,
            secret=secret,
            tenant_id=owner.tenant_id,
            file_id=owner.file_id,
            scan_id=scan_id,
            extract_fingerprint=owner.extract_fingerprint,
        )
    # 抽取正文必须保持 PRD-072 的唯一 UTF-8 表示，非法编码一律按完整性失败。
    except UnicodeError:
        raise ApiError("file.integrity_failed") from None
    # Blob/AAD/存储合同只暴露固定业务码，不把对象键、路径或密码学异常带出边界。
    except FileContractError as exc:
        code = (
            "file.integrity_failed"
            if exc.code == "file.integrity_failed"
            else "file.security_scan_unavailable"
        )
        # 只返回固定业务码并清除异常链，避免对象键、路径或密码学细节进入响应。
        raise ApiError(code) from None
    # 动态边界派生失败不能返回未包装正文，必须按 Shared 固定错误关闭。
    except FileSecurityError as exc:
        raise ApiError(exc.code) from None


def _load_owner_for_scan(
    config: AppConfig,
    context: AuthContext,
    file_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> tuple[_FileSecurityOwner, FileSecurityReportResponse | None]:
    """在触碰 Blob 前复核两项权限、ready owner、attempt 上限和可复用报告。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live_context = _require_scan_permissions(connection, context)
        row = connection.execute(
            _OWNER_SELECT,
            (live_context.user.tenant_id, file_id),
        ).fetchone()
        # tenant-qualified owner 未命中时保持随机和跨租户同形，禁止继续读取任何 Blob。
        if row is None:
            raise ApiError("file.not_found")
        owner = _owner_from_row(row)
        authoritative = _select_authoritative_scan(
            connection,
            owner.tenant_id,
            owner.file_id,
            owner=owner,
        )
        # 已有同 owner 和版本的权威结果时直接重验 hash 并复用，避免重复 OCR 和安全事实。
        if authoritative is not None:
            response = _report_response(
                connection,
                authoritative,
                request_id=request_id,
                trace_id=trace_id,
                reused=True,
            )
            connection.commit()
            return owner, response
        attempts = connection.execute(
            """
            SELECT COUNT(*) AS count FROM file_security_scans
            WHERE tenant_id=? AND file_id=? AND detector_version=?
            """,
            (owner.tenant_id, owner.file_id, DETECTOR_VERSION),
        ).fetchone()
        # attempt 计数类型异常表示数据库事实损坏，不能把它解释成首次扫描。
        if attempts is None or type(attempts["count"]) is not int:
            raise ApiError("file.integrity_failed")
        # 十次失败上限防止持续依赖故障放大数据库，恢复前固定返回可重试错误。
        if attempts["count"] >= 10:
            raise ApiError("file.security_scan_unavailable")
        connection.commit()
        return owner, None
    # 业务鉴权、owner 与状态错误保持原码，事务只负责回滚当前读取快照。
    except ApiError:
        connection.rollback()
        raise
    # 权威报告或 DTO 不自洽属于完整性故障，不能降级成未扫描继续处理。
    except (FileSecurityError, ValidationError):
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    # 数据库不可读时无法证明 owner 与 attempt 上限，统一返回扫描依赖不可用。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("file.security_scan_unavailable") from None
    finally:
        connection.close()


def _read_original_blob(
    config: AppConfig,
    owner: _FileSecurityOwner,
    *,
    secret: str,
) -> bytes:
    """按数据库 owner 读取、认证解密并重新计算原文件 keyed fingerprint。"""

    store = EncryptedLocalBlobStore(config.file_storage_root)
    ciphertext = store.read(owner.original_object_key)
    content = decrypt_file_blob(
        ciphertext,
        secret=secret,
        owner=FileBlobOwner(
            tenant_id=owner.tenant_id,
            file_id=owner.file_id,
            blob_kind="original",
            content_fingerprint=owner.content_fingerprint,
        ),
    )
    actual = file_content_fingerprint(
        content,
        secret=secret,
        tenant_id=owner.tenant_id,
        file_id=owner.file_id,
        blob_kind="original",
    )
    # 原文件明文必须与数据库分域指纹一致，否则检测器不能接收被交换或损坏的内容。
    if not hmac.compare_digest(actual, owner.content_fingerprint):
        raise FileContractError("file.integrity_failed")
    return content


def _read_extracted_blob_text(
    config: AppConfig,
    owner: _FileSecurityOwner,
    *,
    secret: str,
) -> str:
    """按数据库 owner 读取、认证抽取 Blob，并恢复唯一 UTF-8 模型正文。"""

    store = EncryptedLocalBlobStore(config.file_storage_root)
    ciphertext = store.read(owner.extracted_object_key)
    plaintext = decrypt_file_blob(
        ciphertext,
        secret=secret,
        owner=FileBlobOwner(
            tenant_id=owner.tenant_id,
            file_id=owner.file_id,
            blob_kind="extract",
            # PRD-072 抽取 Blob 的 AAD 绑定正文 keyed fingerprint，不能误用原文件指纹。
            content_fingerprint=owner.extract_fingerprint,
            file_extract_id=owner.file_extract_id,
            extractor_version=owner.extractor_version,
        ),
    )
    actual = file_content_fingerprint(
        plaintext,
        secret=secret,
        tenant_id=owner.tenant_id,
        file_id=owner.file_id,
        blob_kind="extract",
    )
    # 解密后重算分域 fingerprint，防止合法密文被错误 owner 或损坏元数据交换。
    if not hmac.compare_digest(actual, owner.extract_fingerprint):
        raise FileContractError("file.integrity_failed")
    try:
        return plaintext.decode("utf-8", errors="strict")
    # ready 抽取 Blob 必须是 PRD-072 的唯一 UTF-8 表示，损坏编码属于完整性失败。
    except UnicodeDecodeError:
        raise FileContractError("file.integrity_failed") from None


def _read_scan_inputs_after_permission_recheck(
    config: AppConfig,
    context: AuthContext,
    owner: _FileSecurityOwner,
    *,
    secret: str,
) -> tuple[bytes, str]:
    """在可串行化撤权的短事务内复核扫描权限，并读取两份认证扫描输入。

    ``BEGIN IMMEDIATE`` 把权限快照的线性化点放在 Blob 解密之前：先提交的撤权会被当前
    复核看到，后到的撤权必须等待本次短读取结束，避免普通只读事务在 WAL/并发写入下留下
    “复核已通过但撤权已提交”的明文读取窗口。原件负责结构/OCR，抽取正文负责模型语义。
    """

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = _require_scan_permissions(connection, context)
        _verify_owner_unchanged(connection, live_context, owner)
        content = _read_original_blob(config, owner, secret=secret)
        extracted_text = _read_extracted_blob_text(config, owner, secret=secret)
        connection.commit()
        return content, extracted_text
    # 权限、tenant owner 或文件状态变化保持原固定业务码；读取事务必须先释放写保留锁。
    except ApiError:
        connection.rollback()
        raise
    # 数据库无法建立实时权限快照时禁止读取 Blob，也不能把依赖故障伪装成扫描事实。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("file.security_scan_unavailable") from None
    # Blob/密码学固定异常由调用方转换并追踪；这里只负责在退出前释放事务边界。
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_extracted_text_after_permission_recheck(
    config: AppConfig,
    context: AuthContext,
    owner: _FileSecurityOwner,
    *,
    secret: str,
) -> str:
    """在短 ``BEGIN IMMEDIATE`` 权限快照内读取、认证并解码抽取正文。

    模型准入的报告验证和正文读取分属两个阶段；本函数在第二阶段重新验证 ``file.read`` 和
    完整 owner，并在解密结束前保持撤权写入不可提交，防止第一次报告检查后的 TOCTOU。
    """

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "file.read",
            unavailable_error_code="file.security_scan_unavailable",
        )
        _verify_owner_unchanged(connection, live_context, owner)
        text = _read_extracted_blob_text(config, owner, secret=secret)
        connection.commit()
        return text
    # 撤权、跨租户或 owner 漂移直接保留固定业务码，且不得触碰或返回任何正文。
    except ApiError:
        connection.rollback()
        raise
    # 数据库不可读时无法证明当前权限，模型上下文固定失败关闭。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("file.security_scan_unavailable") from None
    # 密文、存储和 UTF-8 合同由上层统一转换；这里确保异常路径也释放事务锁。
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _persist_successful_scan(
    config: AppConfig,
    context: AuthContext,
    owner: _FileSecurityOwner,
    result: FileSecurityScanResult,
    *,
    secret: str,
    scan_id: str,
    request_id: str,
    trace_id: str,
) -> tuple[FileSecurityReportResponse, WebhookEvent | None]:
    """原子插入发现、终态扫描与可选注入事件，任一步失败都回滚全部租户安全事实。"""

    completed_at = _to_db_time(_utcnow())
    persisted = tuple(
        _persisted_finding(
            draft,
            secret=secret,
            owner=owner,
            scan_id=scan_id,
            completed_at=completed_at,
        )
        for draft in result.findings
    )
    report_contract = _canonical_from_result(
        owner,
        result,
        scan_id=scan_id,
        completed_at=completed_at,
        findings=persisted,
    )
    finding_contracts = tuple(_canonical_from_persisted(item) for item in persisted)
    report_hash = canonical_report_sha256(report_contract, finding_contracts)
    event = _build_prompt_event(
        owner,
        result,
        scan_id=scan_id,
        actor_user_id=context.user.user_id,
        request_id=request_id,
        trace_id=trace_id,
        completed_at=completed_at,
    )

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = _require_scan_permissions(connection, context)
        _verify_owner_unchanged(connection, live_context, owner)
        winner = _select_authoritative_scan(
            connection,
            owner.tenant_id,
            owner.file_id,
            owner=owner,
        )
        # 写锁内再次查 winner，关闭多 worker 同时完成检测后重复提交权威报告的竞态。
        if winner is not None:
            response = _report_response(
                connection,
                winner,
                request_id=request_id,
                trace_id=trace_id,
                reused=True,
            )
            connection.commit()
            return response, None
        attempt_no = _next_attempt_no(connection, owner)
        for item in persisted:
            _insert_finding(connection, owner, scan_id=scan_id, item=item)
        _insert_scan(
            connection,
            owner,
            scan_id=scan_id,
            actor_user_id=live_context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            attempt_no=attempt_no,
            report=report_contract,
            report_hash=report_hash,
            failure_error_code=None,
        )
        if event is not None:
            _insert_security_event(connection, event, scan_id=scan_id, completed_at=completed_at)
        stored = connection.execute(
            f"{_SCAN_SELECT} WHERE tenant_id=? AND scan_id=?",
            (owner.tenant_id, scan_id),
        ).fetchone()
        # 刚插入的 tenant/scan owner 必须可在同事务回读，否则不能声称事实已持久化。
        if stored is None:
            raise ApiError("file.integrity_failed")
        response = _report_response(
            connection,
            stored,
            request_id=request_id,
            trace_id=trace_id,
            reused=False,
        )
        connection.commit()
        _log_scan(response.report, tenant_id=owner.tenant_id, actor_id=context.user.user_id)
        return response, event
    # 权限撤销或 owner 漂移等业务拒绝必须回滚 findings，并保持原固定错误码。
    except ApiError:
        connection.rollback()
        raise
    # Shared 合同、DTO 或 canonical 输入异常说明报告不可相信，整笔事务按完整性失败。
    except (FileSecurityError, ValidationError, ValueError):
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    # 唯一键或数据库 trigger 拒绝表示事实冲突，不能把半成品或底层 SQL 文本返回客户端。
    except sqlite3.IntegrityError:
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    # 其他数据库故障没有形成可信终态，统一按可重试扫描依赖不可用返回。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("file.security_scan_unavailable") from None
    finally:
        connection.close()


def _persist_failed_scan(
    config: AppConfig,
    context: AuthContext,
    owner: _FileSecurityOwner,
    *,
    scan_id: str,
    error_code: str,
    request_id: str,
    trace_id: str,
) -> FileSecurityReportResponse | None:
    """数据库可写时保存零 finding failed attempt；已有 winner 时改为返回权威报告。"""

    safe_error = (
        error_code
        if error_code in {"file.security_scan_unavailable", "file.integrity_failed"}
        else "file.security_scan_unavailable"
    )
    completed_at = _to_db_time(_utcnow())
    failed_report = CanonicalSecurityReport(
        scan_id=scan_id,
        file_id=owner.file_id,
        status="failed",
        risk_level="unknown",
        risk_score=None,
        coverage_status="unavailable",
        model_access_status="blocked",
        detector_version=DETECTOR_VERSION,
        boundary_version=BOUNDARY_VERSION,
        ocr_status="failed" if safe_error == "file.integrity_failed" else "unavailable",
        ocr_engine_version=None,
        scanned_page_count=0,
        scanned_image_count=0,
        finding_count=0,
        high_finding_count=0,
        created_at=completed_at,
        completed_at=completed_at,
    )
    report_hash = canonical_report_sha256(failed_report, ())
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = _require_scan_permissions(connection, context)
        _verify_owner_unchanged(connection, live_context, owner)
        winner = _select_authoritative_scan(
            connection,
            owner.tenant_id,
            owner.file_id,
            owner=owner,
        )
        if winner is not None:
            response = _report_response(
                connection,
                winner,
                request_id=request_id,
                trace_id=trace_id,
                reused=True,
            )
            connection.commit()
            return response
        attempt_no = _next_attempt_no(connection, owner)
        _insert_scan(
            connection,
            owner,
            scan_id=scan_id,
            actor_user_id=live_context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            attempt_no=attempt_no,
            report=failed_report,
            report_hash=report_hash,
            failure_error_code=safe_error,
        )
        connection.commit()
        return None
    # 已分类权限、上限或 owner 错误保持原码，不能伪造 failed attempt 已经提交。
    except ApiError:
        connection.rollback()
        raise
    # 数据库自身不可写时无法形成可信 failed 事实，只能返回统一依赖不可用。
    except sqlite3.Error:
        connection.rollback()
        # 数据库不可写时绝不能伪造 failed 已持久化；公共结果统一为可重试依赖不可用。
        raise ApiError("file.security_scan_unavailable") from None
    # failed canonical 合同异常表示服务或数据库事实损坏，按完整性错误失败关闭。
    except (FileSecurityError, ValueError):
        connection.rollback()
        raise ApiError("file.integrity_failed") from None
    finally:
        connection.close()


def _owner_from_row(row: sqlite3.Row) -> _FileSecurityOwner:
    """把 LEFT JOIN 文件行严格转换为 ready owner，非 ready 状态统一返回 409。"""

    # 扫描命令只接受 file/extract 双 ready；其他终态由管理 API 返回统一 409。
    if row["file_status"] != "ready" or row["extract_status"] != "ready":
        raise ApiError("file.security_not_ready")
    tenant_id = row["tenant_id"]
    file_id = row["file_id"]
    file_extract_id = row["file_extract_id"]
    kind = row["file_extension"]
    extractor_version = row["extractor_version"]
    content_fingerprint = row["content_fingerprint"]
    extract_fingerprint = row["text_fingerprint"]
    original_object_key = row["original_object_key"]
    extracted_object_key = row["extracted_object_key"]
    # owner 的 ID、格式、指纹、对象键和 DLP 状态必须全部来自同一可信 joined 行。
    if (
        type(tenant_id) is not str
        or type(file_id) is not str
        or _FILE_ID.fullmatch(file_id) is None
        or type(file_extract_id) is not str
        or not re.fullmatch(r"fext_[0-9a-f]{32}", file_extract_id)
        or kind not in _FILE_KINDS
        or type(extractor_version) is not str
        or type(content_fingerprint) is not str
        or _FINGERPRINT.fullmatch(content_fingerprint) is None
        or type(extract_fingerprint) is not str
        or _FINGERPRINT.fullmatch(extract_fingerprint) is None
        or type(original_object_key) is not str
        or type(extracted_object_key) is not str
        or row["dlp_scan_status"] not in {"clean", "matched"}
    ):
        # joined owner 任一字段失配都说明文件事实不可信，禁止局部构造后继续扫描。
        raise ApiError("file.integrity_failed")
    return _FileSecurityOwner(
        tenant_id=cast(str, tenant_id),
        file_id=cast(str, file_id),
        file_extract_id=cast(str, file_extract_id),
        kind=cast(Literal["txt", "docx", "pdf"], kind),
        extractor_version=cast(str, extractor_version),
        content_fingerprint=cast(str, content_fingerprint),
        extract_fingerprint=cast(str, extract_fingerprint),
        original_object_key=cast(str, original_object_key),
        extracted_object_key=cast(str, extracted_object_key),
    )


def _require_scan_permissions(
    connection: sqlite3.Connection,
    context: AuthContext,
) -> AuthContext:
    """在同一事务快照内依次复核 file.read 与 file.security.scan。"""

    live = require_transaction_permission(
        connection,
        context,
        "file.read",
        unavailable_error_code="file.security_scan_unavailable",
    )
    # 两项权限必须在同一事务快照逐项成立，read 不能替代有成本的扫描命令权限。
    return require_transaction_permission(
        connection,
        live,
        "file.security.scan",
        unavailable_error_code="file.security_scan_unavailable",
    )


def _verify_owner_unchanged(
    connection: sqlite3.Connection,
    context: AuthContext,
    owner: _FileSecurityOwner,
) -> None:
    """事务锁内重读同 tenant ready owner 和两类 fingerprint，丢弃扫描期发生的替换。"""

    row = connection.execute(
        _OWNER_SELECT,
        (context.user.tenant_id, owner.file_id),
    ).fetchone()
    # 写锁内 owner 消失时保持 tenant-qualified 404，不接受扫描前的旧内存 owner。
    if row is None:
        raise ApiError("file.not_found")
    current = _owner_from_row(row)
    # 任一 fingerprint、对象键或 extract owner 漂移都丢弃已计算结果，禁止提交过期报告。
    if current != owner:
        raise ApiError("file.integrity_failed")


def _select_authoritative_scan(
    connection: sqlite3.Connection,
    tenant_id: str,
    file_id: str,
    *,
    owner: _FileSecurityOwner,
) -> sqlite3.Row | None:
    """只选择与当前内容和抽取 fingerprint 匹配的 passed/blocked 权威结果。"""

    rows = connection.execute(
        f"""
        {_SCAN_SELECT}
        WHERE tenant_id=? AND file_id=? AND content_fingerprint=?
          AND extract_fingerprint=? AND detector_version=?
          AND status IN ('passed','blocked')
        LIMIT 2
        """,
        (
            tenant_id,
            file_id,
            owner.content_fingerprint,
            owner.extract_fingerprint,
            DETECTOR_VERSION,
        ),
    ).fetchall()
    if len(rows) > 1:
        # 正常数据库由部分唯一索引保证单行；读取仍反证，避免索引损坏后随意选中一份报告。
        raise FileSecurityError("file.integrity_failed")
    return rows[0] if rows else None


def _select_current_scan(
    connection: sqlite3.Connection,
    tenant_id: str,
    file_id: str,
) -> sqlite3.Row | None:
    """优先当前 detector 权威结果，否则返回最新 failed attempt。"""

    rows = connection.execute(
        f"""
        {_SCAN_SELECT}
        WHERE tenant_id=? AND file_id=? AND detector_version=?
        ORDER BY CASE WHEN status IN ('passed','blocked') THEN 0 ELSE 1 END,
                 attempt_no DESC,scan_id DESC
        LIMIT 2
        """,
        (tenant_id, file_id, DETECTOR_VERSION),
    ).fetchall()
    if len(rows) > 1 and all(row["status"] in {"passed", "blocked"} for row in rows):
        # 索引损坏后可能并存多个权威终态；读取和导出也不能按排序任取一份安全结论。
        raise FileSecurityError("file.integrity_failed")
    return rows[0] if rows else None


def _next_attempt_no(
    connection: sqlite3.Connection,
    owner: _FileSecurityOwner,
) -> int:
    """在独占写锁内分配一到十的单调尝试号，并对损坏计数或超过上限失败关闭。"""

    row = connection.execute(
        """
        SELECT COALESCE(MAX(attempt_no),0) AS maximum
        FROM file_security_scans
        WHERE tenant_id=? AND file_id=? AND detector_version=?
        """,
        (owner.tenant_id, owner.file_id, DETECTOR_VERSION),
    ).fetchone()
    # MAX 聚合必须返回整数；类型漂移说明数据库事实损坏，不能猜测下一个编号。
    if row is None or type(row["maximum"]) is not int:
        raise ApiError("file.integrity_failed")
    attempt = row["maximum"] + 1
    # 第十次之后禁止继续放大失败事实，调用方必须先恢复依赖或升级检测器版本。
    if not 1 <= attempt <= 10:
        raise ApiError("file.security_scan_unavailable")
    return attempt


def _persisted_finding(
    draft: SecurityFindingDraft,
    *,
    secret: str,
    owner: _FileSecurityOwner,
    scan_id: str,
    completed_at: str,
) -> _PersistedFinding:
    """为 finding 分配服务端 ID，并把关闭 repr 的 evidence 转为 owner-bound HMAC。"""

    return _PersistedFinding(
        finding_id=f"fsfd_{uuid4().hex}",
        draft=draft,
        evidence_fingerprint=finding_evidence_fingerprint(
            draft,
            secret=secret,
            tenant_id=owner.tenant_id,
            file_id=owner.file_id,
            scan_id=scan_id,
        ),
        created_at=completed_at,
    )


def _canonical_from_result(
    owner: _FileSecurityOwner,
    result: FileSecurityScanResult,
    *,
    scan_id: str,
    completed_at: str,
    findings: Sequence[_PersistedFinding],
) -> CanonicalSecurityReport:
    """把 Shared 检测结果映射为 canonical 报告头，并复核版本与计数。"""

    high_count = sum(
        1 for item in findings if item.draft.severity in {"high", "critical"}
    )
    # Shared 返回的检测器或边界版本漂移会破坏幂等键与模型合同，必须拒绝持久化。
    if result.detector_version != DETECTOR_VERSION or result.boundary_version != BOUNDARY_VERSION:
        raise FileSecurityError("file.integrity_failed")
    return CanonicalSecurityReport(
        scan_id=scan_id,
        file_id=owner.file_id,
        status=result.status,
        risk_level=result.risk_level,
        risk_score=result.risk_score,
        coverage_status=result.coverage_status,
        model_access_status=result.model_access_status,
        detector_version=result.detector_version,
        boundary_version=result.boundary_version,
        ocr_status=result.ocr_status,
        ocr_engine_version=result.ocr_engine_version,
        scanned_page_count=result.scanned_page_count,
        scanned_image_count=result.scanned_image_count,
        finding_count=len(findings),
        high_finding_count=high_count,
        created_at=completed_at,
        completed_at=completed_at,
    )


def _canonical_from_persisted(item: _PersistedFinding) -> CanonicalSecurityFinding:
    """投影报告 hash 覆盖的公开 finding 字段，不复制 evidence HMAC。"""

    return CanonicalSecurityFinding(
        finding_id=item.finding_id,
        detector_code=item.draft.detector_code,
        category=item.draft.category,
        severity=item.draft.severity,
        confidence_milli=item.draft.confidence_milli,
        source_kind=item.draft.source_kind,
        location_kind=item.draft.location_kind,
        location_index=item.draft.location_index,
        status="detected",
        created_at=item.created_at,
    )


def _insert_finding(
    connection: sqlite3.Connection,
    owner: _FileSecurityOwner,
    *,
    scan_id: str,
    item: _PersistedFinding,
) -> None:
    """插入单条 append-only finding；正文和 evidence bytes 不作为 SQL 参数。"""

    connection.execute(
        """
        INSERT INTO file_security_findings(
          finding_id,tenant_id,scan_id,file_id,detector_code,category,severity,
          confidence_milli,source_kind,location_kind,location_index,
          evidence_fingerprint,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item.finding_id,
            owner.tenant_id,
            scan_id,
            owner.file_id,
            item.draft.detector_code,
            item.draft.category,
            item.draft.severity,
            item.draft.confidence_milli,
            item.draft.source_kind,
            item.draft.location_kind,
            item.draft.location_index,
            item.evidence_fingerprint,
            "detected",
            item.created_at,
            item.created_at,
        ),
    )


def _insert_scan(
    connection: sqlite3.Connection,
    owner: _FileSecurityOwner,
    *,
    scan_id: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    attempt_no: int,
    report: CanonicalSecurityReport,
    report_hash: str,
    failure_error_code: str | None,
) -> None:
    """在 findings 之后插入终态 Scan，让数据库 trigger 反向证明完整集合。"""

    connection.execute(
        """
        INSERT INTO file_security_scans(
          scan_id,tenant_id,file_id,file_extract_id,requested_by,source_request_id,
          source_trace_id,attempt_no,status,risk_level,risk_score,coverage_status,
          model_access_status,detector_version,ocr_status,ocr_engine_version,
          scanned_page_count,scanned_image_count,finding_count,high_finding_count,
          boundary_version,content_fingerprint,extract_fingerprint,report_sha256,
          failure_error_code,created_at,updated_at,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scan_id,
            owner.tenant_id,
            owner.file_id,
            owner.file_extract_id,
            actor_user_id,
            request_id,
            trace_id,
            attempt_no,
            report.status,
            report.risk_level,
            report.risk_score,
            report.coverage_status,
            report.model_access_status,
            report.detector_version,
            report.ocr_status,
            report.ocr_engine_version,
            report.scanned_page_count,
            report.scanned_image_count,
            report.finding_count,
            report.high_finding_count,
            report.boundary_version,
            owner.content_fingerprint,
            owner.extract_fingerprint,
            report_hash,
            failure_error_code,
            report.created_at,
            report.created_at,
            report.completed_at,
        ),
    )


def _report_response(
    connection: sqlite3.Connection,
    scan_row: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    reused: bool,
) -> FileSecurityReportResponse:
    """以 LIMIT 257 读取 findings、重算 canonical hash 并构造严格公共 DTO。"""

    findings = connection.execute(
        f"""
        SELECT finding_id,detector_code,category,severity,confidence_milli,
               source_kind,location_kind,location_index,status,created_at
        FROM file_security_findings
        WHERE tenant_id=? AND scan_id=? AND file_id=?
        {_FINDING_ORDER_SQL}
        """,
        (scan_row["tenant_id"], scan_row["scan_id"], scan_row["file_id"]),
    ).fetchall()
    # LIMIT 257 反证服务上限和持久计数，任何越界或缺失 finding 都使报告失效。
    if len(findings) > 256 or len(findings) != scan_row["finding_count"]:
        raise FileSecurityError("file.integrity_failed")
    canonical_findings = tuple(_canonical_from_row(row) for row in findings)
    canonical_report = _canonical_report_from_row(scan_row)
    actual_hash = canonical_report_sha256(canonical_report, canonical_findings)
    stored_hash = scan_row["report_sha256"]
    # canonical hash 必须常量时间匹配，避免损坏头或 finding 集合被页面和模型消费。
    if type(stored_hash) is not str or not hmac.compare_digest(actual_hash, stored_hash):
        raise FileSecurityError("file.integrity_failed")
    public_findings = [
        FileSecurityFindingInfo(
            finding_id=row.finding_id,
            detector_code=row.detector_code,
            category=row.category,
            severity=row.severity,
            confidence_milli=row.confidence_milli,
            source_kind=row.source_kind,
            location_kind=row.location_kind,
            location_index=row.location_index,
            status=row.status,
            created_at=_parse_db_time(row.created_at),
        )
        for row in canonical_findings
    ]
    report = FileSecurityReport(
        scan_id=canonical_report.scan_id,
        file_id=canonical_report.file_id,
        status=canonical_report.status,
        risk_level=canonical_report.risk_level,
        risk_score=canonical_report.risk_score,
        coverage_status=canonical_report.coverage_status,
        model_access_status=canonical_report.model_access_status,
        detector_version=canonical_report.detector_version,
        boundary_version=canonical_report.boundary_version,
        ocr_status=canonical_report.ocr_status,
        ocr_engine_version=canonical_report.ocr_engine_version,
        scanned_page_count=canonical_report.scanned_page_count,
        scanned_image_count=canonical_report.scanned_image_count,
        finding_count=canonical_report.finding_count,
        high_finding_count=canonical_report.high_finding_count,
        findings=public_findings,
        created_at=_parse_db_time(canonical_report.created_at),
        completed_at=_parse_db_time(canonical_report.completed_at),
    )
    return FileSecurityReportResponse(
        report=report,
        reused=reused,
        request_id=request_id,
        trace_id=trace_id,
    )


def _canonical_from_row(row: sqlite3.Row) -> CanonicalSecurityFinding:
    """把数据库 finding 行转换为 canonical 结构；私有 HMAC 列未被查询。"""

    return CanonicalSecurityFinding(
        finding_id=row["finding_id"],
        detector_code=row["detector_code"],
        category=row["category"],
        severity=row["severity"],
        confidence_milli=row["confidence_milli"],
        source_kind=row["source_kind"],
        location_kind=row["location_kind"],
        location_index=row["location_index"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _canonical_report_from_row(row: sqlite3.Row) -> CanonicalSecurityReport:
    """把数据库 Scan 行投影为不含 owner/fingerprint/链路的 canonical 报告头。"""

    return CanonicalSecurityReport(
        scan_id=row["scan_id"],
        file_id=row["file_id"],
        status=row["status"],
        risk_level=row["risk_level"],
        risk_score=row["risk_score"],
        coverage_status=row["coverage_status"],
        model_access_status=row["model_access_status"],
        detector_version=row["detector_version"],
        boundary_version=row["boundary_version"],
        ocr_status=row["ocr_status"],
        ocr_engine_version=row["ocr_engine_version"],
        scanned_page_count=row["scanned_page_count"],
        scanned_image_count=row["scanned_image_count"],
        finding_count=row["finding_count"],
        high_finding_count=row["high_finding_count"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _insert_security_event(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    scan_id: str,
    completed_at: str,
) -> None:
    """把已验证的 Prompt Injection 信封写入同事务 source event。"""

    source = connection.execute(
        """
        SELECT source_request_id FROM file_security_scans
        WHERE tenant_id=? AND scan_id=? AND file_id=?
        """,
        (event.tenant_id, scan_id, event.object_id),
    ).fetchone()
    # source request 必须从刚提交的同 tenant Scan 回读，不能信任事件构造参数自行补齐。
    if source is None or type(source["source_request_id"]) is not str:
        raise ApiError("file.integrity_failed")
    connection.execute(
        """
        INSERT INTO file_security_events(
          file_security_event_id,tenant_id,scan_id,file_id,event_type,
          actor_user_id,source_request_id,source_trace_id,risk_level,
          finding_count,high_finding_count,model_access_status,
          detector_version,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.tenant_id,
            scan_id,
            event.object_id,
            event.event_type,
            event.actor_id,
            source["source_request_id"],
            event.trace_id,
            event.payload["risk_level"],
            event.payload["finding_count"],
            event.payload["high_finding_count"],
            event.payload["model_access_status"],
            event.payload["detector_version"],
            completed_at,
        ),
    )


def _build_prompt_event(
    owner: _FileSecurityOwner,
    result: FileSecurityScanResult,
    *,
    scan_id: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    completed_at: str,
) -> WebhookEvent | None:
    """构造仅含七个固定安全字段的 Prompt Injection Webhook 信封。"""

    if not any(
        item.detector_code in {"prompt.visible_instruction", "ocr.prompt_instruction"}
        for item in result.findings
    ):
        return None
    high_count = sum(
        1 for item in result.findings if item.severity in {"high", "critical"}
    )
    return build_webhook_event(
        tenant_id=owner.tenant_id,
        event_type="file.prompt_injection_detected",
        event_id=f"fsev_{uuid4().hex}",
        timestamp=_parse_db_time(completed_at),
        actor_id=actor_user_id,
        object_id=owner.file_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload={
            "scan_id": scan_id,
            "file_id": owner.file_id,
            "risk_level": result.risk_level,
            "finding_count": len(result.findings),
            "high_finding_count": high_count,
            "model_access_status": result.model_access_status,
            "detector_version": result.detector_version,
        },
    )


def _log_scan(report: FileSecurityReport, *, tenant_id: str, actor_id: str) -> None:
    """记录不含文件名、正文、证据、对象键和 hash 的安全扫描摘要。"""

    logger.info(
        "file_security.scan_completed",
        extra={
            "event": "file_security.scan_completed",
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "object_id": report.file_id,
            "scan_id": report.scan_id,
            "status": report.status,
            "risk_level": report.risk_level,
            "finding_count": report.finding_count,
            "high_finding_count": report.high_finding_count,
        },
    )


def _file_secret(config: AppConfig) -> str:
    """验证文件根密钥强度与跨域隔离，并只向当前调用栈返回字符串。"""

    secret = config.file_encryption_secret
    try:
        validate_file_secret(
            secret,
            production=config.environment == "prod",
            forbidden_secrets=(
                config.auth_token_secret,
                config.cache_affinity_hmac_secret,
                config.prompt_template_encryption_secret,
            ),
        )
    # 密钥弱度或跨域复用细节不得暴露，统一折叠为扫描依赖暂不可用。
    except FileContractError:
        raise ApiError("file.security_scan_unavailable") from None
    # 配置模型之外的内部误用也不能把任意对象当成密钥继续进入密码学调用。
    if type(secret) is not str:
        raise ApiError("file.security_scan_unavailable")
    # 合法密钥只返回当前调用栈，禁止写入日志、DTO、事件或持久报告。
    return secret


def _normalize_file_id(value: object) -> str:
    """校验公共 file ID；非法、随机和跨 tenant 对象保持同形 404。"""

    # 非规范 ID 与随机/跨 tenant ID 保持同形 404，避免通过输入差异枚举文件。
    if type(value) is not str or _FILE_ID.fullmatch(value) is None:
        raise ApiError("file.not_found")
    return value


def _normalize_chain_id(value: object) -> str:
    """校验中间件生成的 1..128 ASCII request/trace ID，禁止客户端覆盖。"""

    # 链路 ID 只接受中间件产生的有界 ASCII；异常值不能进入报告或 source event。
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or _CHAIN_ID.fullmatch(value) is None
    ):
        # 非规范链路值不能进入报告 hash 或 source event，统一按服务完整性错误拒绝。
        raise ApiError("file.integrity_failed")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建数据库连接，并把路径/驱动配置异常折叠为安全扫描不可用。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL、路径与驱动异常可能泄露宿主拓扑，统一转换为固定依赖错误。
    except DbError:
        raise ApiError("file.security_scan_unavailable") from None


def _utcnow() -> datetime:
    """返回无微秒 UTC，使 Scan、Finding 和 Event 使用同一 canonical 秒。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把有时区 datetime 编码为唯一 UTC 秒字符串。"""

    # 无时区时间会让 canonical hash 在宿主间漂移，必须在写入前完整拒绝。
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FileSecurityError("file.integrity_failed")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """严格解析数据库 UTC 秒；非 canonical 或本地时间按完整性失败关闭。"""

    # 数据库时间必须是 UTC Z 后缀的唯一表示，拒绝本地时间和隐式类型转换。
    if type(value) is not str or not value.endswith("Z"):
        raise FileSecurityError("file.integrity_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析异常只说明持久时间不可信，禁止把原字符串或第三方异常写入日志。
    except ValueError:
        raise FileSecurityError("file.integrity_failed") from None
    # 等价但非 canonical 的时区或精度表示同样会破坏报告 hash，必须失败关闭。
    if parsed.tzinfo is None or _to_db_time(parsed) != value:
        raise FileSecurityError("file.integrity_failed")
    return parsed.astimezone(UTC)


def _csv_cell(value: object) -> object:
    """对文本执行 CSV 公式前缀防护；整数保持原类型以便表格工具正确识别。"""

    if type(value) is not str:
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


__all__ = [
    "OcrBackendPool",
    "build_file_model_context",
    "export_security_report_csv",
    "get_security_report",
    "scan_security_report",
]
