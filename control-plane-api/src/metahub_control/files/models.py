"""PRD-072 文件 API 的安全响应模型。

本模块只描述浏览器可见的状态、计数和链路标识。抽取正文、内容 fingerprint、DLP 规则
身份、对象键和宿主路径没有模型字段，从类型边界阻止服务层误回显敏感数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FileExtractInfo(BaseModel):
    """一次文件文本抽取的安全计数投影。

    ready/blocked 终态会提供完整计数；解析前失败时相关计数为 null。正文和正文 fingerprint
    永远不属于公共 DTO。
    """

    file_extract_id: str
    status: Literal["ready", "blocked", "failed"]
    extractor_version: Literal["txt-v1", "docx-v1", "pdf-v1"]
    text_size_bytes: int | None
    text_code_points: int | None
    estimated_tokens: int | None
    estimator_version: Literal["file-token-estimator-v1"] | None
    failure_error_code: str | None


class FileInfo(BaseModel):
    """当前租户内一个 UploadedFile 的授权安全投影。

    文件名仅在 ``file.read`` 或本次 ``file.write`` 已通过后解密，并关闭 repr，避免框架
    调试对象意外复制企业文档名称。规则 ID、fingerprint 和存储定位信息不对外公开。
    """

    file_id: str
    original_name: str = Field(repr=False)
    file_extension: Literal["txt", "docx", "pdf"]
    media_type: Literal[
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/pdf",
    ]
    size_bytes: int
    status: Literal["processing", "ready", "blocked", "failed"]
    virus_scan_status: Literal["not_configured"]
    dlp_scan_status: Literal["pending", "clean", "matched", "blocked", "unavailable"]
    dlp_action: Literal["allow", "log_only", "block"] | None
    match_count: int
    failure_error_code: str | None
    upload_request_id: str
    extract: FileExtractInfo | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class FileResponse(BaseModel):
    """上传或详情接口返回的单文件信封。"""

    file: FileInfo
    request_id: str
    trace_id: str


class FileListResponse(BaseModel):
    """稳定游标分页的当前租户文件列表信封。"""

    files: list[FileInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str
