"""PRD-073 文件安全检测的纯数据合同与有界 finding 收集器。

本模块不读取数据库、对象存储或 HTTP 请求。命中证据只保存在关闭 ``repr`` 的内存字段中，
持久化前必须由 Control 使用文件密钥计算分域 HMAC；任何异常仅携带固定错误码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal, cast

FileSecurityKind = Literal["txt", "docx", "pdf"]
FindingCategory = Literal[
    "prompt_injection",
    "visual_hiding",
    "hidden_layer",
    "ocr",
    "container",
    "coverage",
]
FindingSeverity = Literal["info", "low", "medium", "high", "critical"]
FindingSourceKind = Literal[
    "visible_text",
    "docx_xml",
    "docx_image",
    "pdf_content",
    "pdf_image",
    "document_container",
    "scan_coverage",
]
FindingLocationKind = Literal["document", "page", "part", "image"]
CoverageStatus = Literal["complete", "partial", "unavailable"]
OcrStatus = Literal["not_required", "completed", "partial", "unavailable", "failed"]
RiskLevel = Literal["none", "low", "medium", "high", "critical"]
ScanStatus = Literal["passed", "blocked"]
ModelAccessStatus = Literal["allowed", "blocked"]

# 版本、容量和 ID 规则同时由 Shared、Control、迁移和测试冻结，调用方不能按请求放宽。
DETECTOR_VERSION: Final[str] = "file-security-detector-v1"
PROMPT_RULESET_VERSION: Final[str] = "file-prompt-injection-rules-v1"
DOCX_DETECTOR_VERSION: Final[str] = "file-docx-visual-v1"
PDF_DETECTOR_VERSION: Final[str] = "file-pdf-visual-v1"
BOUNDARY_VERSION: Final[str] = "file-context-boundary-v1"
MAX_SECURITY_FINDINGS: Final[int] = 256
MAX_CONCRETE_FINDINGS: Final[int] = MAX_SECURITY_FINDINGS - 1
MAX_EVIDENCE_BYTES: Final[int] = 4096

_DETECTOR_CODES: Final[frozenset[str]] = frozenset(
    {
        "prompt.visible_instruction",
        "visual.hidden_text",
        "visual.white_text",
        "visual.tiny_text",
        "visual.hidden_layer",
        "ocr.prompt_instruction",
        "ocr.text_observed",
        "container.nested_archive",
        "coverage.incomplete",
    }
)
# Prompt 命中会驱动独立 Webhook 和安全运营处置；finding 饱和后仍必须优先保留至少一条真实命中。
_PROMPT_DETECTOR_CODES: Final[frozenset[str]] = frozenset(
    {"prompt.visible_instruction", "ocr.prompt_instruction"}
)
_CATEGORY_VALUES: Final[frozenset[str]] = frozenset(
    {"prompt_injection", "visual_hiding", "hidden_layer", "ocr", "container", "coverage"}
)
_SEVERITY_VALUES: Final[frozenset[str]] = frozenset(
    {"info", "low", "medium", "high", "critical"}
)
_SOURCE_VALUES: Final[frozenset[str]] = frozenset(
    {
        "visible_text",
        "docx_xml",
        "docx_image",
        "pdf_content",
        "pdf_image",
        "document_container",
        "scan_coverage",
    }
)
_LOCATION_VALUES: Final[frozenset[str]] = frozenset({"document", "page", "part", "image"})
_SAFE_DETAIL = re.compile(r"^[a-z0-9_.:-]{1,128}$")


class FileSecurityError(ValueError):
    """只携带固定文件安全错误码的异常，禁止拼接正文或第三方异常。"""

    def __init__(self, code: str) -> None:
        """保存可由 Control 映射为统一错误的固定 code。"""

        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SecurityFindingDraft:
    """尚未持久化的安全 finding；原始证据仅在当前调用栈中使用。

    ``evidence`` 用于计算 tenant/file/scan 分域 HMAC，关闭 repr 且不能进入公共 DTO、日志、
    Webhook 或异常。location 只保存有界序号，不保存 XML path、PDF object ID 或坐标框。
    """

    detector_code: str
    category: FindingCategory
    severity: FindingSeverity
    confidence_milli: int
    source_kind: FindingSourceKind
    location_kind: FindingLocationKind
    location_index: int | None
    evidence: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class FileSecurityScanResult:
    """格式检测器返回的终态安全摘要和有界 finding 集合。

    结果不包含文件名、正文、对象键或公开 hash。``findings`` 中的 evidence 仍是敏感内存数据，
    因此整个字段关闭 repr；Control 持久化后必须立即释放本对象。
    """

    kind: FileSecurityKind
    status: ScanStatus
    risk_level: RiskLevel
    risk_score: int
    coverage_status: CoverageStatus
    model_access_status: ModelAccessStatus
    ocr_status: OcrStatus
    ocr_engine_version: str | None
    scanned_page_count: int
    scanned_image_count: int
    findings: tuple[SecurityFindingDraft, ...] = field(repr=False)
    detector_version: str = DETECTOR_VERSION
    boundary_version: str = BOUNDARY_VERSION


class FindingCollector:
    """在扫描过程中去重并强制 256 条硬上限的内存收集器。

    具体发现最多保留 255 条；继续溢出时补一条 high coverage finding，保证数据库和响应规模
    可预测。溢出后普通事实停止收集，但 Prompt 命中可替换一条非 Prompt 结构事实，避免数量攻击
    同时压掉安全事件。去重键不包含证据正文，只使用调用栈内 bytes 比较。
    """

    def __init__(self) -> None:
        """创建空收集器；私有集合不会出现在扫描结果或日志。"""

        self._items: list[SecurityFindingDraft] = []
        self._keys: set[tuple[object, ...]] = set()
        self._overflowed = False

    @property
    def overflowed(self) -> bool:
        """返回是否因 finding 数量超限而产生 coverage 失败。"""

        return self._overflowed

    @property
    def has_incomplete_coverage(self) -> bool:
        """返回是否已记录固定 coverage finding，不暴露其 evidence。"""

        return any(item.detector_code == "coverage.incomplete" for item in self._items)

    def add(
        self,
        *,
        detector_code: str,
        category: FindingCategory,
        severity: FindingSeverity,
        confidence_milli: int,
        source_kind: FindingSourceKind,
        location_kind: FindingLocationKind,
        location_index: int | None,
        evidence: bytes,
    ) -> None:
        """校验并加入一条 finding；重复证据忽略，超限时收敛为 coverage finding。"""

        item = build_finding(
            detector_code=detector_code,
            category=category,
            severity=severity,
            confidence_milli=confidence_milli,
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence=evidence,
        )
        key = (
            item.detector_code,
            item.source_kind,
            item.location_kind,
            item.location_index,
            item.evidence,
        )
        if key in self._keys:
            # 同一结构证据只形成一个事实，避免 OCR/可见文本重叠造成计数放大。
            return
        if self._overflowed:
            # 即使已经饱和，也要先执行上面的完整合同校验；内部解析器伪造字段必须失败关闭。
            self._retain_prompt_after_overflow(item, key)
            return
        if len(self._items) >= MAX_CONCRETE_FINDINGS:
            self._overflowed = True
            overflow = build_finding(
                detector_code="coverage.incomplete",
                category="coverage",
                severity="high",
                confidence_milli=1000,
                source_kind="scan_coverage",
                location_kind="document",
                location_index=None,
                evidence=b"finding_limit_exceeded",
            )
            self._items.append(overflow)
            self._keys.add(
                (
                    overflow.detector_code,
                    overflow.source_kind,
                    overflow.location_kind,
                    overflow.location_index,
                    overflow.evidence,
                )
            )
            # 第 256 个候选本身可能是可信抽取正文的 Prompt；先保留 coverage，再替换一条结构事实。
            self._retain_prompt_after_overflow(item, key)
            return
        self._items.append(item)
        self._keys.add(key)

    def _retain_prompt_after_overflow(
        self,
        item: SecurityFindingDraft,
        key: tuple[object, ...],
    ) -> None:
        """在固定上限内以 Prompt 替换一条结构事实，同时保留 coverage 和内部去重一致性。"""

        if item.detector_code not in _PROMPT_DETECTOR_CODES:
            return
        for index in range(len(self._items) - 1, -1, -1):
            existing = self._items[index]
            if existing.detector_code in _PROMPT_DETECTOR_CODES | {"coverage.incomplete"}:
                continue
            existing_key = (
                existing.detector_code,
                existing.source_kind,
                existing.location_kind,
                existing.location_index,
                existing.evidence,
            )
            # coverage 已证明有事实被舍弃；替换而非追加才能同时维持数据库 256 条硬上限。
            self._items[index] = item
            self._keys.remove(existing_key)
            self._keys.add(key)
            return

    def coverage_incomplete(self, reason: str, *, location_index: int | None = None) -> None:
        """用固定安全 reason 记录覆盖不完整，禁止传入第三方异常或用户文本。"""

        if _SAFE_DETAIL.fullmatch(reason) is None:
            # reason 会参与 HMAC 证据，仍必须是静态内部枚举，不能成为异常文本旁路。
            raise FileSecurityError("file.security_scan_unavailable")
        self.add(
            detector_code="coverage.incomplete",
            category="coverage",
            severity="high",
            confidence_milli=1000,
            source_kind="scan_coverage",
            location_kind="document" if location_index is None else "page",
            location_index=location_index,
            evidence=reason.encode("ascii"),
        )

    def freeze(
        self,
        *,
        kind: FileSecurityKind,
        coverage_status: CoverageStatus,
        ocr_status: OcrStatus,
        ocr_engine_version: str | None,
        scanned_page_count: int,
        scanned_image_count: int,
    ) -> FileSecurityScanResult:
        """汇总风险和准入终态，任何 coverage 不完整都失败关闭。"""

        if len(self._items) > MAX_SECURITY_FINDINGS:
            # 内部状态超过数据库硬上限说明实现合同已损坏，不能继续返回可信报告。
            raise FileSecurityError("file.security_scan_unavailable")
        effective_coverage: CoverageStatus = (
            "partial" if self._overflowed and coverage_status == "complete" else coverage_status
        )
        risk_level = _highest_risk(self._items)
        if effective_coverage != "complete" and risk_level not in {"high", "critical"}:
            # 正常调用会由 coverage finding 把风险提升为 high；此分支防止调用方遗漏该事实。
            risk_level = "high"
        blocked = effective_coverage != "complete" or risk_level in {"high", "critical"}
        status: ScanStatus = "blocked" if blocked else "passed"
        model_access: ModelAccessStatus = "blocked" if blocked else "allowed"
        return FileSecurityScanResult(
            kind=kind,
            status=status,
            risk_level=risk_level,
            risk_score=_risk_score(risk_level),
            coverage_status=effective_coverage,
            model_access_status=model_access,
            ocr_status=ocr_status,
            ocr_engine_version=ocr_engine_version,
            scanned_page_count=_bounded_count(scanned_page_count),
            scanned_image_count=_bounded_count(scanned_image_count),
            findings=tuple(self._items),
        )


def build_finding(
    *,
    detector_code: str,
    category: FindingCategory,
    severity: FindingSeverity,
    confidence_milli: int,
    source_kind: FindingSourceKind,
    location_kind: FindingLocationKind,
    location_index: int | None,
    evidence: bytes,
) -> SecurityFindingDraft:
    """构造经过 exact-enum、位置、置信度和证据体积校验的内存 finding。"""

    if (
        detector_code not in _DETECTOR_CODES
        or category not in _CATEGORY_VALUES
        or severity not in _SEVERITY_VALUES
        or source_kind not in _SOURCE_VALUES
        or location_kind not in _LOCATION_VALUES
    ):
        # 未登记枚举会破坏数据库、Webhook 和前端严格 DTO，必须在最早边界拒绝。
        raise FileSecurityError("file.security_scan_unavailable")
    # bool 也是 int 的子类，因此必须用 exact type 拒绝伪造的布尔置信度和越界分数。
    if type(confidence_milli) is not int or not 0 <= confidence_milli <= 1000:
        raise FileSecurityError("file.security_scan_unavailable")
    # evidence 只能是有界 bytes，避免下游 HMAC 隐式编码不同类型或复制超大敏感正文。
    if type(evidence) is not bytes or not evidence or len(evidence) > MAX_EVIDENCE_BYTES:
        # 证据只用于 HMAC，仍限制体积以防 OCR 或 XML 文本放大敏感内存。
        raise FileSecurityError("file.security_scan_unavailable")
    if location_kind == "document":
        if location_index is not None:
            # 文档级 finding 没有页/part 坐标，伪造索引会造成公共报告定位语义漂移。
            raise FileSecurityError("file.security_scan_unavailable")
    elif type(location_index) is not int or not 1 <= location_index <= 100_000:
        # 页、part 和图片 finding 必须有受限正整数坐标，拒绝 bool、空值与资源放大索引。
        raise FileSecurityError("file.security_scan_unavailable")
    if detector_code == "coverage.incomplete" and (
        category != "coverage" or severity != "high" or source_kind != "scan_coverage"
    ):
        # coverage finding 的严重度固定 high；unknown 只属于无法形成 findings 的 failed scan。
        raise FileSecurityError("file.security_scan_unavailable")
    return SecurityFindingDraft(
        detector_code=detector_code,
        category=cast(FindingCategory, category),
        severity=cast(FindingSeverity, severity),
        confidence_milli=confidence_milli,
        source_kind=cast(FindingSourceKind, source_kind),
        location_kind=cast(FindingLocationKind, location_kind),
        location_index=location_index,
        evidence=evidence,
    )


def _highest_risk(items: list[SecurityFindingDraft]) -> RiskLevel:
    """按冻结严重度顺序返回最高风险；info 与 low 统一映射为 low。"""

    severity_rank = {"info": 1, "low": 1, "medium": 2, "high": 3, "critical": 4}
    highest = max((severity_rank[item.severity] for item in items), default=0)
    return cast(RiskLevel, {0: "none", 1: "low", 2: "medium", 3: "high", 4: "critical"}[highest])


def _risk_score(level: RiskLevel) -> int:
    """把离散风险映射为稳定整数分数；failed scan 不调用本函数而由数据库保存 null。"""

    return {"none": 0, "low": 20, "medium": 50, "high": 80, "critical": 100}[level]


def _bounded_count(value: object) -> int:
    """校验公开页面/图片计数，拒绝 bool、负数和异常放大值。"""

    # 页面和图片计数来自解析器边界，拒绝 bool/负数/异常大值，不能在报告层静默夹断。
    if type(value) is not int or not 0 <= value <= 10_000:
        raise FileSecurityError("file.security_scan_unavailable")
    return value
