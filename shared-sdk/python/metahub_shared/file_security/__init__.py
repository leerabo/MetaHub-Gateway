"""PRD-073 文件型 Prompt Injection 检测公共入口。"""

# 本入口只聚合检测、离线 OCR 与完整性合同；授权、tenant 查询和 Blob 解密必须留在 Control 服务。

from .contracts import (
    BOUNDARY_VERSION,
    DETECTOR_VERSION,
    DOCX_DETECTOR_VERSION,
    MAX_SECURITY_FINDINGS,
    PDF_DETECTOR_VERSION,
    PROMPT_RULESET_VERSION,
    FileSecurityError,
    FileSecurityScanResult,
    FindingCollector,
    SecurityFindingDraft,
)
from .detectors import scan_file_security
from .integrity import (
    BoundedFileContext,
    CanonicalSecurityFinding,
    CanonicalSecurityReport,
    build_bounded_file_context,
    canonical_report_sha256,
    finding_evidence_fingerprint,
)
from .ocr import (
    OCR_ENGINE_VERSION,
    OcrBackend,
    OcrBudget,
    OcrFailure,
    OcrImageResult,
    RapidOcrBackend,
    scan_image_bytes,
    scan_pdf_page,
)
from .rules import add_prompt_findings, find_prompt_injection, normalize_prompt_text

__all__ = [
    "BOUNDARY_VERSION",
    "DETECTOR_VERSION",
    "DOCX_DETECTOR_VERSION",
    "MAX_SECURITY_FINDINGS",
    "PDF_DETECTOR_VERSION",
    "PROMPT_RULESET_VERSION",
    "FileSecurityError",
    "FileSecurityScanResult",
    "FindingCollector",
    "SecurityFindingDraft",
    "BoundedFileContext",
    "CanonicalSecurityFinding",
    "CanonicalSecurityReport",
    "OCR_ENGINE_VERSION",
    "OcrBackend",
    "OcrBudget",
    "OcrFailure",
    "OcrImageResult",
    "RapidOcrBackend",
    "add_prompt_findings",
    "find_prompt_injection",
    "normalize_prompt_text",
    "scan_image_bytes",
    "scan_file_security",
    "scan_pdf_page",
    "build_bounded_file_context",
    "canonical_report_sha256",
    "finding_evidence_fingerprint",
]
