"""PRD-073 文件安全报告的浏览器可见严格响应模型。

模型只包含规范 ID、固定枚举、计数和时间。tenant、actor、文件名、正文、OCR 文本、证据 HMAC、
报告 hash、对象键和规则表达式没有字段，从类型边界阻止误回显。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileSecurityFindingInfo(BaseModel):
    """一个不含原始证据的公开安全发现。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    finding_id: str
    detector_code: str
    category: Literal[
        "prompt_injection", "visual_hiding", "hidden_layer", "ocr", "container", "coverage"
    ]
    severity: Literal["info", "low", "medium", "high", "critical"]
    confidence_milli: int = Field(ge=0, le=1000)
    source_kind: Literal[
        "visible_text",
        "docx_xml",
        "docx_image",
        "pdf_content",
        "pdf_image",
        "document_container",
        "scan_coverage",
    ]
    location_kind: Literal["document", "page", "part", "image"]
    location_index: int | None
    status: Literal["detected"]
    created_at: datetime


class FileSecurityReport(BaseModel):
    """当前 detector version 的可信终态报告。

    failed 报告没有可信风险分，``risk_score`` 必须为 null；页面不能把它渲染成 0 或 clean。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    scan_id: str
    file_id: str
    status: Literal["passed", "blocked", "failed"]
    risk_level: Literal["none", "low", "medium", "high", "critical", "unknown"]
    risk_score: int | None
    coverage_status: Literal["complete", "partial", "unavailable"]
    model_access_status: Literal["allowed", "blocked"]
    detector_version: Literal["file-security-detector-v1"]
    boundary_version: Literal["file-context-boundary-v1"]
    ocr_status: Literal["not_required", "completed", "partial", "unavailable", "failed"]
    ocr_engine_version: str | None
    scanned_page_count: int = Field(ge=0, le=10_000)
    scanned_image_count: int = Field(ge=0, le=10_000)
    finding_count: int = Field(ge=0, le=256)
    high_finding_count: int = Field(ge=0, le=256)
    findings: list[FileSecurityFindingInfo] = Field(max_length=256)
    created_at: datetime
    completed_at: datetime


class FileSecurityReportResponse(BaseModel):
    """扫描与读取接口共用的统一链路信封。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    report: FileSecurityReport
    reused: bool
    request_id: str
    trace_id: str


__all__ = [
    "FileSecurityFindingInfo",
    "FileSecurityReport",
    "FileSecurityReportResponse",
]
