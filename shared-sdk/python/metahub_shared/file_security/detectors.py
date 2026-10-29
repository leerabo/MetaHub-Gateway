"""PRD-073 TXT、DOCX 与 PDF 文件型 Prompt Injection 检测器。

检测器只接收 PRD-072 已认证的内存 bytes，不读取路径、不联网、不执行文档动作。第三方解析、
OCR 和结构异常统一转为固定 coverage finding；原文、XML path、对象 ID 和 OCR 文本不进入结果。
"""

from __future__ import annotations

import io
import math
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final
from xml.etree import ElementTree

from .contracts import (
    FileSecurityError,
    FileSecurityKind,
    FileSecurityScanResult,
    FindingCollector,
    FindingLocationKind,
    FindingSourceKind,
    OcrStatus,
)
from .ocr import OcrBackend, OcrBudget, OcrFailure, scan_image_bytes, scan_pdf_page
from .rules import (
    MAX_RULE_TEXT_CODE_POINTS,
    add_prompt_findings,
    find_prompt_injection,
    normalize_prompt_text,
)

# DOCX/PDF 再执行独立防御性边界；即使未来调用方绕过 PRD-072，也不能无界解压或遍历对象图。
MAX_DOCX_ENTRIES: Final[int] = 2_000
MAX_DOCX_ENTRY_BYTES: Final[int] = 16 * 1024 * 1024
MAX_DOCX_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO: Final[int] = 200
HARD_MAX_DOCX_IMAGES: Final[int] = 200
MAX_STYLE_DEPTH: Final[int] = 16
MAX_PDF_PAGES: Final[int] = 1_000
HARD_MAX_PDF_OCR_PAGES: Final[int] = 100
MAX_PDF_OPERATIONS: Final[int] = 200_000
MAX_PDF_GRAPHICS_DEPTH: Final[int] = 64
MAX_PDF_RESOURCE_DEPTH: Final[int] = 16
MAX_PDF_PAGE_CONTENT_BYTES: Final[int] = MAX_PDF_OPERATIONS * 2
MAX_PDF_TOTAL_CONTENT_BYTES: Final[int] = 16 * 1024 * 1024
MAX_PDF_TOTAL_OPERATIONS: Final[int] = 1_000_000
MAX_PDF_TOTAL_TEXT_CODE_POINTS: Final[int] = 4_000_000
MAX_PDF_CONTENT_STREAMS: Final[int] = 4_096
PROMPT_WINDOW_OVERLAP: Final[int] = 256

_W_NS: Final[str] = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W: Final[dict[str, str]] = {"w": _W_NS}
_W_VAL: Final[str] = f"{{{_W_NS}}}val"
_W_STYLE_ID: Final[str] = f"{{{_W_NS}}}styleId"
_DOCX_TEXT_PART_PREFIXES: Final[tuple[str, ...]] = (
    "word/document.xml",
    "word/header",
    "word/footer",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
)
_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
)
_UNSUPPORTED_VECTOR_SUFFIXES: Final[frozenset[str]] = frozenset({".emf", ".wmf", ".svg"})
_ARCHIVE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".docx", ".xlsx", ".pptx"}
)
_ARCHIVE_MAGIC: Final[tuple[bytes, ...]] = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"Rar!\x1a\x07",
    b"7z\xbc\xaf\x27\x1c",
    b"\x1f\x8b",
)


@dataclass(frozen=True, slots=True)
class _TextStyle:
    """DOCX 继承解析后的可见性属性；None 表示当前层没有覆盖父级。"""

    hidden: bool | None = None
    white: bool | None = None
    tiny: bool | None = None
    uncertain_color: bool = False

    def merge(self, child: _TextStyle) -> _TextStyle:
        """让更具体的样式覆盖父级，同时保留无法解释颜色的 coverage 标志。"""

        return _TextStyle(
            hidden=self.hidden if child.hidden is None else child.hidden,
            white=self.white if child.white is None else child.white,
            tiny=self.tiny if child.tiny is None else child.tiny,
            uncertain_color=self.uncertain_color or child.uncertain_color,
        )


@dataclass(frozen=True, slots=True)
class _DocxStyleDefinition:
    """单个 DOCX style 的父 ID 与直接可见性属性。"""

    based_on: str | None
    properties: _TextStyle


@dataclass(frozen=True, slots=True)
class _DocxStyleCatalog:
    """DOCX 文档默认 run 属性和具名样式表。

    ``docDefaults/rPrDefault`` 是所有段落、字符样式和直接 run 属性的最底层。将它与具名样式
    分开保存，可以避免解析无显式样式的 run 时漏掉全局 ``vanish``、白字或超小字号。
    """

    default_properties: _TextStyle
    definitions: Mapping[str, _DocxStyleDefinition]


@dataclass(frozen=True, slots=True)
class _PdfTextState:
    """PDF 图形栈中影响文本可见性的最小状态。"""

    render_mode: int = 0
    font_size: float = 12.0
    white_fill: bool = False
    white_stroke: bool = False
    transparent_fill: bool = False
    transparent_stroke: bool = False


@dataclass(slots=True)
class _PdfParseBudget:
    """一个 PDF 扫描共享的解压、操作符、正文和 Content Stream 预算。"""

    decoded_bytes: int = 0
    operation_count: int = 0
    text_code_points: int = 0
    stream_count: int = 0

    def consume_page_bytes(self, value: int, *, streams: int) -> None:
        """在物化操作列表前预扣单页/全文件解压 bytes 与流数量。"""

        if (
            type(value) is not int
            or not 0 <= value <= MAX_PDF_PAGE_CONTENT_BYTES
            or type(streams) is not int
            or streams < 0
            or self.decoded_bytes + value > MAX_PDF_TOTAL_CONTENT_BYTES
            or self.stream_count + streams > MAX_PDF_CONTENT_STREAMS
        ):
            # 任一页面或全局解码预算越界都意味着扫描覆盖不完整，必须失败关闭。
            raise FileSecurityError("file.security_scan_unavailable")
        self.decoded_bytes += value
        self.stream_count += streams

    def consume_operations(self, value: int) -> None:
        """在每页解析后累计操作符，防止大量小页面放大全文件 CPU。"""

        if (
            type(value) is not int
            or not 0 <= value <= MAX_PDF_OPERATIONS
            or self.operation_count + value > MAX_PDF_TOTAL_OPERATIONS
        ):
            # 操作符数量异常会放大解析耗时并留下未扫描尾部，因此统一阻断模型访问。
            raise FileSecurityError("file.security_scan_unavailable")
        self.operation_count += value

    def consume_text(self, value: str) -> None:
        """逐操作累计抽取正文，禁止先拼接超过 PRD-072 正文上限的字符串。"""

        if (
            type(value) is not str
            or self.text_code_points + len(value) > MAX_PDF_TOTAL_TEXT_CODE_POINTS
        ):
            # 正文类型异常或累计超限时无法证明完整检测，禁止截断后伪装为通过。
            raise FileSecurityError("file.security_scan_unavailable")
        self.text_code_points += len(value)


def scan_file_security(
    content: object,
    kind: FileSecurityKind,
    *,
    trusted_extracted_text: object,
    ocr_backend: OcrBackend | None = None,
    max_ocr_pages: int = 20,
    max_images: int = 50,
    max_image_pixels: int = 12_000_000,
    max_total_pixels: int = 50_000_000,
) -> FileSecurityScanResult:
    """按可信格式执行确定性安全扫描，并返回 passed/blocked 终态。

    参数：认证文件 bytes、PRD-072 格式、已认证抽取正文和可选离线 OCR 后端。返回：无正文的
    安全摘要。关键约束：原文件负责结构/OCR，抽取正文负责模型实际字符语义；两者缺一不可。
    """

    if type(content) is not bytes or not content or kind not in {"txt", "docx", "pdf"}:
        # 检测器只接受前序完整性校验后的非空内存字节，拒绝路径和未知格式进入解析器。
        raise FileSecurityError("file.integrity_failed")
    if (
        type(trusted_extracted_text) is not str
        or not trusted_extracted_text.strip()
        or len(trusted_extracted_text) > MAX_RULE_TEXT_CODE_POINTS
    ):
        # 权威扫描必须覆盖 PRD-072 持久正文；空值、子类或超限文本不能退回原始 PDF 字节猜测。
        raise FileSecurityError("file.integrity_failed")
    _validate_scan_budgets(
        max_ocr_pages=max_ocr_pages,
        max_images=max_images,
        max_image_pixels=max_image_pixels,
        max_total_pixels=max_total_pixels,
    )
    if kind == "txt":
        structural_result = _scan_txt(content)
    elif kind == "docx":
        structural_result = _scan_docx(
            content,
            ocr_backend=ocr_backend,
            max_images=max_images,
            max_image_pixels=max_image_pixels,
            max_total_pixels=max_total_pixels,
        )
    else:
        structural_result = _scan_pdf(
            content,
            ocr_backend=ocr_backend,
            max_ocr_pages=max_ocr_pages,
            max_images=max_images,
            max_image_pixels=max_image_pixels,
            max_total_pixels=max_total_pixels,
        )
    return _merge_trusted_extracted_text(structural_result, trusted_extracted_text)


def _merge_trusted_extracted_text(
    structural_result: FileSecurityScanResult,
    trusted_extracted_text: str,
) -> FileSecurityScanResult:
    """把已认证抽取正文检测结果并入原文件结构/OCR 结果。

    PDF 字体 CMap、DOCX 域或未来解析器可能让原始操作数与模型正文不同；因此必须扫描 PRD-072
    实际持久正文。证据仍只含静态规则 ID，正文不会进入返回值、日志或数据库。
    """

    collector = FindingCollector()
    # 可信抽取正文就是后续模型实际消费的语义，必须先于可能饱和的结构 finding 入队；否则真实
    # Prompt 或格式控制会只剩 coverage，而安全报告无法解释阻断原因，Prompt Webhook 也会漏发。
    add_prompt_findings(
        collector,
        trusted_extracted_text,
        source_kind="visible_text",
        location_kind="document",
        location_index=None,
        evidence_prefix=b"trusted_extract:",
    )
    if any(unicodedata.category(character) == "Cf" for character in trusted_extracted_text):
        # 模型正文中的格式控制即使不在原始可见操作数中，也必须形成固定高风险事实。
        collector.add(
            detector_code="visual.hidden_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=950,
            source_kind="visible_text",
            location_kind="document",
            location_index=None,
            evidence=b"trusted_extract_format_control",
        )
    for finding in structural_result.findings:
        collector.add(
            detector_code=finding.detector_code,
            category=finding.category,
            severity=finding.severity,
            confidence_milli=finding.confidence_milli,
            source_kind=finding.source_kind,
            location_kind=finding.location_kind,
            location_index=finding.location_index,
            evidence=finding.evidence,
        )
    return collector.freeze(
        kind=structural_result.kind,
        coverage_status=structural_result.coverage_status,
        ocr_status=structural_result.ocr_status,
        ocr_engine_version=structural_result.ocr_engine_version,
        scanned_page_count=structural_result.scanned_page_count,
        scanned_image_count=structural_result.scanned_image_count,
    )


def _validate_scan_budgets(
    *,
    max_ocr_pages: object,
    max_images: object,
    max_image_pixels: object,
    max_total_pixels: object,
) -> None:
    """校验只来自部署配置的 OCR 预算，并保留独立于配置的发布包硬上限。"""

    if (
        type(max_ocr_pages) is not int
        or not 1 <= max_ocr_pages <= HARD_MAX_PDF_OCR_PAGES
        or type(max_images) is not int
        or not 1 <= max_images <= HARD_MAX_DOCX_IMAGES
        or type(max_image_pixels) is not int
        or not 1 <= max_image_pixels <= 50_000_000
        or type(max_total_pixels) is not int
        or not max_image_pixels <= max_total_pixels <= 200_000_000
    ):
        # 部署预算必须同时满足类型、硬上限和单项不大于总量，防止错误配置绕过资源边界。
        raise FileSecurityError("file.security_scan_unavailable")


def _scan_txt(content: bytes) -> FileSecurityScanResult:
    """扫描 UTF-8 TXT 的可见强指令、零宽/方向控制和边界碰撞。"""

    collector = FindingCollector()
    try:
        text = content.decode("utf-8", errors="strict")
    # 非法 UTF-8 无法建立确定正文边界，不能替换坏字节后继续并声称扫描完整。
    except UnicodeDecodeError:
        raise FileSecurityError("file.integrity_failed") from None
    add_prompt_findings(collector, text)
    if any(unicodedata.category(character) == "Cf" for character in text):
        # 格式控制可能把指令藏在视觉文本中；证据只保留固定检测器标识，不保存字符。
        collector.add(
            detector_code="visual.hidden_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=950,
            source_kind="visible_text",
            location_kind="document",
            location_index=None,
            evidence=b"txt_format_control",
        )
    return collector.freeze(
        kind="txt",
        coverage_status="complete",
        ocr_status="not_required",
        ocr_engine_version=None,
        scanned_page_count=0,
        scanned_image_count=0,
    )


def _scan_docx(
    content: bytes,
    *,
    ocr_backend: OcrBackend | None,
    max_images: int,
    max_image_pixels: int,
    max_total_pixels: int,
) -> FileSecurityScanResult:
    """有界遍历 OOXML 文本、样式、容器和媒体，并只对候选位图执行 OCR。"""

    collector = FindingCollector()
    try:
        entries = _read_docx_entries(content)
    # ZIP 结构错误或任一有界读取失败都归一为完整性错误，避免泄露底层归档细节。
    except (FileSecurityError, zipfile.BadZipFile):
        raise FileSecurityError("file.integrity_failed") from None
    styles = _docx_styles(entries.get("word/styles.xml"), collector)
    visible_values: list[str] = []
    text_parts = sorted(
        name
        for name in entries
        if name == "word/document.xml"
        or any(name.startswith(prefix) for prefix in _DOCX_TEXT_PART_PREFIXES[1:])
    )
    for part_index, name in enumerate(text_parts, start=1):
        _scan_docx_text_part(
            entries[name],
            part_index=part_index,
            styles=styles,
            collector=collector,
            visible_values=visible_values,
        )

    media_entries: list[tuple[str, bytes]] = []
    for name, value in entries.items():
        lowered = name.casefold()
        suffix = _suffix(lowered)
        # 后缀和 OOXML 目录都可被攻击者伪装；对每个已受 ZIP 体积门禁的 entry 再检查固定
        # 归档魔数，确保 ``custom/item.bin`` 之类的多层容器也不能静默通过。
        has_archive_magic = any(value.startswith(magic) for magic in _ARCHIVE_MAGIC)
        if (
            lowered.startswith("word/embeddings/")
            or suffix in _ARCHIVE_SUFFIXES
            or has_archive_magic
        ):
            collector.add(
                detector_code="container.nested_archive",
                category="container",
                severity="critical",
                confidence_milli=1000,
                source_kind="document_container",
                location_kind="document",
                location_index=None,
                evidence=b"docx_nested_container",
            )
        elif lowered.startswith("word/media/"):
            if suffix in _UNSUPPORTED_VECTOR_SUFFIXES:
                collector.coverage_incomplete("docx_media_format_unsupported")
            elif suffix in _IMAGE_SUFFIXES:
                media_entries.append((name, value))
            else:
                collector.coverage_incomplete("docx_media_format_unknown")

    ocr_status: OcrStatus = "not_required"
    engine_version: str | None = None
    if media_entries:
        ocr_status, engine_version = _scan_docx_media(
            media_entries,
            visible_values=visible_values,
            collector=collector,
            backend=ocr_backend,
            max_images=max_images,
            max_image_pixels=max_image_pixels,
            max_total_pixels=max_total_pixels,
        )
    coverage = "partial" if _has_coverage_finding(collector) else "complete"
    return collector.freeze(
        kind="docx",
        coverage_status=coverage,
        ocr_status=ocr_status,
        ocr_engine_version=engine_version,
        scanned_page_count=0,
        scanned_image_count=len(media_entries),
    )


def _read_docx_entries(content: bytes) -> dict[str, bytes]:
    """读取受限 DOCX ZIP entry，不向磁盘解压，也不允许路径或压缩比绕过。"""

    entries: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_DOCX_ENTRIES:
            # 空归档不是有效 DOCX，过多 entry 还可能造成遍历放大，二者均拒绝解析。
            raise FileSecurityError("file.integrity_failed")
        for info in infos:
            name = info.filename.replace("\\", "/")
            if (
                not name
                or name.startswith("/")
                or ".." in name.split("/")
                or info.file_size > MAX_DOCX_ENTRY_BYTES
            ):
                # 绝对路径、路径穿越和超大 entry 都突破内存解析边界，必须在读取前拒绝。
                raise FileSecurityError("file.integrity_failed")
            total += info.file_size
            if total > MAX_DOCX_TOTAL_BYTES:
                # 累计解压体积超过硬上限时不能继续物化剩余内容，避免归档炸弹耗尽内存。
                raise FileSecurityError("file.integrity_failed")
            compressed = max(info.compress_size, 1)
            if (
                info.file_size > 1_048_576
                and info.file_size / compressed > MAX_DOCX_COMPRESSION_RATIO
            ):
                # 大 entry 的异常压缩比是归档炸弹信号，必须在实际解压前失败关闭。
                raise FileSecurityError("file.integrity_failed")
            if info.is_dir():
                continue
            value = archive.read(info)
            if len(value) != info.file_size:
                # 实际读取长度与目录声明不一致说明容器损坏，不能扫描不可信的截断内容。
                raise FileSecurityError("file.integrity_failed")
            entries[name] = value
    if "word/document.xml" not in entries:
        # 主文档 part 是 DOCX 正文检测入口，缺失时无法证明文本和样式已完整覆盖。
        raise FileSecurityError("file.integrity_failed")
    return entries


def _docx_styles(
    value: bytes | None,
    collector: FindingCollector,
) -> _DocxStyleCatalog:
    """解析文档默认 run 属性、具名样式及 basedOn；无法解释时按 coverage 失败关闭。"""

    if value is None:
        return _DocxStyleCatalog(default_properties=_TextStyle(), definitions={})
    try:
        root = ElementTree.fromstring(value)
    # 样式 XML 损坏时正文仍可枚举，但可见性无法确定，因此记录 coverage 而非静默忽略。
    except ElementTree.ParseError:
        collector.coverage_incomplete("docx_styles_invalid")
        return _DocxStyleCatalog(default_properties=_TextStyle(), definitions={})
    default_properties = _docx_run_properties(
        root.find("w:docDefaults/w:rPrDefault/w:rPr", _W)
    )
    styles: dict[str, _DocxStyleDefinition] = {}
    for node in root.findall("w:style", _W):
        style_id = node.get(_W_STYLE_ID)
        if not style_id or len(style_id) > 128:
            collector.coverage_incomplete("docx_style_id_invalid")
            continue
        based = node.find("w:basedOn", _W)
        based_on = based.get(_W_VAL) if based is not None else None
        styles[style_id] = _DocxStyleDefinition(
            based_on=based_on,
            properties=_docx_run_properties(node.find("w:rPr", _W)),
        )
    return _DocxStyleCatalog(
        default_properties=default_properties,
        definitions=styles,
    )


def _resolve_docx_style(
    style_id: str | None,
    styles: _DocxStyleCatalog,
    collector: FindingCollector,
) -> _TextStyle:
    """解析具名样式链；文档默认属性由调用方只合并一次，避免覆盖段落显式属性。"""

    if not style_id:
        return _TextStyle()
    chain: list[_DocxStyleDefinition] = []
    visited: set[str] = set()
    current: str | None = style_id
    while current:
        if current in visited or len(chain) >= MAX_STYLE_DEPTH:
            collector.coverage_incomplete("docx_style_inheritance_invalid")
            return _TextStyle()
        visited.add(current)
        definition = styles.definitions.get(current)
        if definition is None:
            collector.coverage_incomplete("docx_style_reference_unknown")
            return _TextStyle()
        chain.append(definition)
        current = definition.based_on
    resolved = _TextStyle()
    for definition in reversed(chain):
        resolved = resolved.merge(definition.properties)
    return resolved


def _scan_docx_text_part(
    value: bytes,
    *,
    part_index: int,
    styles: _DocxStyleCatalog,
    collector: FindingCollector,
    visible_values: list[str],
) -> None:
    """扫描一个编号后的 OOXML 文本 part；part 名称永不进入 finding 或异常。"""

    try:
        root = ElementTree.fromstring(value)
    # 任一正文 part 无法解析都会留下未检测文本，必须把该 part 降级为覆盖不完整。
    except ElementTree.ParseError:
        collector.coverage_incomplete("docx_text_part_invalid")
        return
    part_texts: list[str] = []
    visible_part_texts: list[str] = []
    for paragraph in root.findall(".//w:p", _W):
        paragraph_style_node = paragraph.find("w:pPr/w:pStyle", _W)
        paragraph_style = styles.default_properties.merge(
            _resolve_docx_style(
                paragraph_style_node.get(_W_VAL) if paragraph_style_node is not None else None,
                styles,
                collector,
            )
        )
        paragraph_texts: list[str] = []
        visible_paragraph_texts: list[str] = []
        for run in paragraph.findall(".//w:r", _W):
            text = "".join(node.text or "" for node in run.findall(".//w:t", _W))
            if not text:
                continue
            paragraph_texts.append(text)
            run_style_node = run.find("w:rPr/w:rStyle", _W)
            style = paragraph_style.merge(
                _resolve_docx_style(
                    run_style_node.get(_W_VAL) if run_style_node is not None else None,
                    styles,
                    collector,
                )
            ).merge(_docx_run_properties(run.find("w:rPr", _W)))
            if style.uncertain_color:
                collector.coverage_incomplete("docx_color_unresolved")
            _add_docx_visual_findings(
                collector,
                text,
                style=style,
                part_index=part_index,
            )
            if not style.hidden and not style.white:
                visible_values.append(text)
                visible_paragraph_texts.append(text)
        paragraph_text = "".join(paragraph_texts)
        if paragraph_text:
            part_texts.append(paragraph_text)
            _add_prompt_windows(
                collector,
                paragraph_text,
                source_kind="docx_xml",
                location_kind="part",
                location_index=part_index,
                evidence_prefix=f"docx_part_{part_index}:".encode("ascii"),
            )
        visible_paragraph = "".join(visible_paragraph_texts)
        if visible_paragraph:
            # OCR 往往按行或 box 返回；保存可见段落聚合值可避免把同一正文误判为图片新增指令。
            visible_values.append(visible_paragraph)
            visible_part_texts.append(visible_paragraph)
    part_text = "\n".join(part_texts)
    if part_text:
        # Word 抽取会重新拼接跨段落/跨 run 文本，规则也必须在相同有界聚合语义上检测。
        _add_prompt_windows(
            collector,
            part_text,
            source_kind="docx_xml",
            location_kind="part",
            location_index=part_index,
            evidence_prefix=f"docx_part_{part_index}:".encode("ascii"),
        )
    if visible_part_texts:
        visible_values.append("\n".join(visible_part_texts))


def _add_prompt_windows(
    collector: FindingCollector,
    text: str,
    *,
    source_kind: FindingSourceKind,
    location_kind: FindingLocationKind,
    location_index: int,
    evidence_prefix: bytes,
) -> None:
    """用带重叠的固定窗口扫描聚合文本，避免跨 run/段落拆分绕过规则。

    单窗口不超过规则库 400 万字符上限；256 字符重叠大于任何固定规则的最大间距，因而
    不会在窗口边界漏掉已支持的注入短语。finding 仍只保存静态 rule id。
    """

    step = MAX_RULE_TEXT_CODE_POINTS - PROMPT_WINDOW_OVERLAP
    for start in range(0, max(len(text), 1), step):
        window = text[start : start + MAX_RULE_TEXT_CODE_POINTS]
        if not window:
            break
        add_prompt_findings(
            collector,
            window,
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence_prefix=evidence_prefix,
        )
        if start + MAX_RULE_TEXT_CODE_POINTS >= len(text):
            break


def _docx_run_properties(node: ElementTree.Element | None) -> _TextStyle:
    """读取 run 属性；color=auto 不直接判白，记录未知覆盖供上层失败关闭。"""

    if node is None:
        return _TextStyle()
    hidden: bool | None = None
    for name in ("w:vanish", "w:webHidden"):
        candidate = node.find(name, _W)
        if candidate is not None:
            hidden = _xml_bool(candidate.get(_W_VAL))
            if hidden:
                break
    color_node = node.find("w:color", _W)
    white: bool | None = None
    uncertain_color = False
    if color_node is not None:
        value = (color_node.get(_W_VAL) or "").strip().casefold()
        if value == "auto":
            uncertain_color = True
        elif len(value) == 6:
            try:
                channels = tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))
                white = all(channel >= 250 for channel in channels)
            # 非法十六进制颜色不能猜测为可见色，交由上层形成覆盖不完整 finding。
            except ValueError:
                uncertain_color = True
        else:
            uncertain_color = True
    size_node = node.find("w:sz", _W)
    if size_node is None:
        # ElementTree 空元素的布尔值为 false，必须显式判断，否则会漏掉合法的 <w:sz/>。
        size_node = node.find("w:szCs", _W)
    tiny: bool | None = None
    if size_node is not None:
        try:
            tiny = int(size_node.get(_W_VAL, "")) <= 6
        # 非法字号无法可靠判断文本是否被视觉隐藏，保留未知状态而不制造确定结论。
        except ValueError:
            tiny = None
    return _TextStyle(hidden=hidden, white=white, tiny=tiny, uncertain_color=uncertain_color)


def _add_docx_visual_findings(
    collector: FindingCollector,
    text: str,
    *,
    style: _TextStyle,
    part_index: int,
) -> None:
    """把 run 可见性属性转换为固定 finding；证据不含 run 文本或 XML path。"""

    prefix = f"docx_part_{part_index}".encode("ascii")
    if style.hidden:
        collector.add(
            detector_code="visual.hidden_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=1000,
            source_kind="docx_xml",
            location_kind="part",
            location_index=part_index,
            evidence=prefix + b":hidden",
        )
    if style.white:
        collector.add(
            detector_code="visual.white_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=980,
            source_kind="docx_xml",
            location_kind="part",
            location_index=part_index,
            evidence=prefix + b":white",
        )
    if style.tiny:
        severity = "high" if find_prompt_injection(text) else "medium"
        collector.add(
            detector_code="visual.tiny_text",
            category="visual_hiding",
            severity=severity,
            confidence_milli=950,
            source_kind="docx_xml",
            location_kind="part",
            location_index=part_index,
            evidence=prefix + b":tiny",
        )


def _scan_docx_media(
    entries: list[tuple[str, bytes]],
    *,
    visible_values: Iterable[str],
    collector: FindingCollector,
    backend: OcrBackend | None,
    max_images: int,
    max_image_pixels: int,
    max_total_pixels: int,
) -> tuple[OcrStatus, str | None]:
    """按文档级预算 OCR 位图；引擎缺失仅在确有媒体候选时形成 blocked coverage。"""

    if len(entries) > max_images:
        collector.coverage_incomplete("docx_image_count_limit")
        entries = entries[:max_images]
    if backend is None:
        collector.coverage_incomplete("ocr_engine_unavailable")
        return "unavailable", None
    budget = OcrBudget(
        max_images=max_images,
        max_image_pixels=max_image_pixels,
        max_total_pixels=max_total_pixels,
    )
    visible = {
        normalize_prompt_text(value)
        for value in visible_values
        if normalize_prompt_text(value)
    }
    engine_version: str | None = None
    failed = False
    for image_index, (_name, value) in enumerate(entries, start=1):
        try:
            result = scan_image_bytes(value, backend=backend, budget=budget)
            engine_version = result.engine_version
        except OcrFailure as exc:
            collector.coverage_incomplete(exc.reason)
            failed = True
            continue
        _add_ocr_findings(
            collector,
            result.texts,
            visible=visible,
            source_kind="docx_image",
            location_kind="image",
            location_index=image_index,
        )
    return ("partial" if failed else "completed"), engine_version


def _scan_pdf(
    content: bytes,
    *,
    ocr_backend: OcrBackend | None,
    max_ocr_pages: int,
    max_images: int,
    max_image_pixels: int,
    max_total_pixels: int,
) -> FileSecurityScanResult:
    """有界遍历 PDF 页面内容、图层、动作和图片资源，并 OCR 候选页。"""

    collector = FindingCollector()
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            # 加密 PDF 无法在当前无密码检测链路中完整读取正文，禁止带密文继续处理。
            raise FileSecurityError("file.integrity_failed")
    # 已归一化的安全错误必须原样保留，避免再次包装后改变对外错误语义。
    except FileSecurityError:
        raise
    except Exception:
        # 第三方解析异常不得携带对象内容外泄，统一转换为无细节的完整性错误。
        raise FileSecurityError("file.integrity_failed") from None
    page_count = len(reader.pages)
    if page_count < 1:
        # 没有页面的 PDF 不存在可验证正文入口，不能作为已完整扫描文件放行。
        raise FileSecurityError("file.integrity_failed")
    if page_count > MAX_PDF_PAGES:
        collector.coverage_incomplete("pdf_page_count_limit")
    _scan_pdf_catalog(reader, collector)
    candidate_pages: list[int] = []
    visible_by_page: dict[int, set[str]] = {}
    image_count = 0
    parse_budget = _PdfParseBudget()
    for page_index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        inline_images, visible_text = _scan_pdf_content(
            page,
            reader,
            page_index=page_index,
            collector=collector,
            budget=parse_budget,
        )
        visible_by_page[page_index] = {
            normalized
            for normalized in (normalize_prompt_text(visible_text),)
            if normalized
        }
        _add_prompt_windows(
            collector,
            visible_text,
            source_kind="pdf_content",
            location_kind="page",
            location_index=page_index,
            evidence_prefix=f"pdf_page_{page_index}:".encode("ascii"),
        )
        try:
            # Inline image 不出现在 /Resources/XObject 图中，必须与间接 Image/Form 资源一起计数；
            # 否则 BI/ID/EI 中的图片指令会绕过候选页 OCR 并被误判为 complete。
            page_images = inline_images + _pdf_image_count(page, depth=0, visited=set())
        except FileSecurityError:
            collector.coverage_incomplete("pdf_resource_graph_invalid", location_index=page_index)
            page_images = 0
        if page_images:
            image_count += page_images
            if image_count <= max_images:
                candidate_pages.append(page_index)
            else:
                # 图片对象超出部署预算后不能继续选择页面，否则“已扫描”计数会掩盖未覆盖尾部。
                collector.coverage_incomplete(
                    "pdf_image_count_limit",
                    location_index=page_index,
                )
        _scan_pdf_page_actions(page, collector, page_index=page_index)

    ocr_status: OcrStatus = "not_required"
    engine_version: str | None = None
    if candidate_pages:
        ocr_status, engine_version = _scan_pdf_candidates(
            content,
            candidate_pages,
            visible_by_page=visible_by_page,
            collector=collector,
            backend=ocr_backend,
            max_ocr_pages=max_ocr_pages,
            max_image_pixels=max_image_pixels,
            max_total_pixels=max_total_pixels,
        )
    coverage = "partial" if _has_coverage_finding(collector) else "complete"
    return collector.freeze(
        kind="pdf",
        coverage_status=coverage,
        ocr_status=ocr_status,
        ocr_engine_version=engine_version,
        scanned_page_count=min(page_count, MAX_PDF_PAGES),
        scanned_image_count=min(image_count, 10_000),
    )


def _scan_pdf_content(
    page: object,
    reader: object,
    *,
    page_index: int,
    collector: FindingCollector,
    budget: _PdfParseBudget,
) -> tuple[int, str]:
    """从页面入口递归解释实际调用的 Content/Form，并返回图片数与有界正文。"""

    getter = getattr(page, "get", None)
    if not callable(getter):
        collector.coverage_incomplete("pdf_content_stream_invalid", location_index=page_index)
        return 0, ""
    return _scan_pdf_content_value(
        getter("/Contents"),
        reader,
        resource_owner=page,
        page_index=page_index,
        collector=collector,
        budget=budget,
        initial_state=_PdfTextState(),
        depth=0,
        active_forms=frozenset(),
    )


def _scan_pdf_content_value(
    value: object,
    reader: object,
    *,
    resource_owner: object,
    page_index: int,
    collector: FindingCollector,
    budget: _PdfParseBudget,
    initial_state: _PdfTextState,
    depth: int,
    active_forms: frozenset[tuple[str, int, int]],
) -> tuple[int, str]:
    """有界解释一个页面或 Form 内容值，并沿 ``Do`` 递归实际调用的 Form。

    参数中的资源 owner 决定字体、ExtGState 与 XObject 名称解析；Form 未声明 ``/Resources``
    时继承调用者。递归只保存间接对象号或进程内对象身份，不把 PDF 对象 ID 写入 finding。
    """

    if depth > MAX_PDF_RESOURCE_DEPTH:
        collector.coverage_incomplete("pdf_form_depth_limit", location_index=page_index)
        return 0, ""

    try:
        from pypdf.generic import ContentStream, DecodedStreamObject

        content_bytes, stream_count = _bounded_pdf_content_value(value)
        budget.consume_page_bytes(len(content_bytes), streams=stream_count)
        if not content_bytes:
            return 0, ""
        decoded = DecodedStreamObject()
        decoded.set_data(content_bytes)
        operations = ContentStream(decoded, reader).operations
        budget.consume_operations(len(operations))
    except Exception:
        collector.coverage_incomplete("pdf_content_stream_invalid", location_index=page_index)
        return 0, ""
    state = initial_state
    stack: list[_PdfTextState] = []
    inline_image_count = 0
    extracted_texts: list[str] = []
    for operands, operator in operations:
        try:
            if operator == b"q":
                if len(stack) >= MAX_PDF_GRAPHICS_DEPTH:
                    # 图形状态嵌套超过硬上限会放大内存和 CPU，且尾部状态无法可靠解释。
                    raise FileSecurityError("file.security_scan_unavailable")
                stack.append(state)
            elif operator == b"Q":
                if not stack:
                    # 无匹配保存操作的状态恢复说明内容流损坏，后续文本可见性不再可信。
                    raise FileSecurityError("file.security_scan_unavailable")
                state = stack.pop()
            elif operator == b"Tr" and operands:
                render_mode = int(operands[0])
                if not 0 <= render_mode <= 7:
                    # 标准之外的渲染模式无法确定文字是否可见，禁止按默认可见状态继续。
                    raise FileSecurityError("file.security_scan_unavailable")
                state = _replace_pdf_state(state, render_mode=render_mode)
            elif operator == b"INLINE IMAGE":
                # pypdf 把 BI/ID/EI 归一为单个操作。图片 bytes 不进入 finding，只把页面列为
                # OCR 候选；计数仍受 MAX_PDF_OPERATIONS 和部署 max_images 双重限制。
                inline_image_count += 1
            elif operator == b"Tf" and len(operands) >= 2:
                state = _replace_pdf_state(state, font_size=abs(float(operands[1])))
            elif operator in {b"g", b"rg", b"k", b"G", b"RG", b"K"}:
                white = _pdf_color_is_white(operator, operands)
                state = (
                    _replace_pdf_state(state, white_stroke=white)
                    if operator.isupper()
                    else _replace_pdf_state(state, white_fill=white)
                )
            elif operator == b"gs" and operands:
                transparent_fill, transparent_stroke, complete = (
                    _pdf_ext_gstate_transparency(
                        resource_owner,
                        operands[0],
                        current_fill=state.transparent_fill,
                        current_stroke=state.transparent_stroke,
                    )
                )
                state = _replace_pdf_state(
                    state,
                    transparent_fill=transparent_fill,
                    transparent_stroke=transparent_stroke,
                )
                if not complete:
                    # Soft mask 会影响文字最终可见性，但本检测器不执行任意透明度图；必须诚实降级。
                    collector.coverage_incomplete(
                        "pdf_ext_gstate_unsupported",
                        location_index=page_index,
                    )
            elif operator in {b"cs", b"CS"} and operands and str(operands[0]) not in {
                "/DeviceGray",
                "/DeviceRGB",
                "/DeviceCMYK",
            }:
                collector.coverage_incomplete("pdf_color_space_unknown", location_index=page_index)
            elif operator in {b"Tj", b"TJ", b"'", b'"'}:
                text = _pdf_operand_text(operands)
                if text:
                    budget.consume_text(text)
                    extracted_texts.append(text)
                    _add_pdf_visual_findings(
                        collector,
                        text,
                        state=state,
                        page_index=page_index,
                    )
            elif operator == b"Do" and operands:
                subtype, reference, xobject = _pdf_xobject(resource_owner, operands[0])
                if subtype == "/Image":
                    # 图片候选由同页资源图统一计数并触发整页 OCR；这里不重复解码图片 stream。
                    continue
                if subtype != "/Form":
                    raise FileSecurityError("file.security_scan_unavailable")
                marker = _pdf_object_marker(reference, xobject)
                if marker in active_forms:
                    # 自引用或循环 Form 会让内容解释永不终止，必须按覆盖不完整失败关闭。
                    raise FileSecurityError("file.security_scan_unavailable")
                form_resources = xobject.get("/Resources")
                child_resource_owner = (
                    resource_owner
                    if form_resources is None
                    or form_resources.__class__.__name__ == "NullObject"
                    else xobject
                )
                child_images, child_text = _scan_pdf_content_value(
                    xobject,
                    reader,
                    resource_owner=child_resource_owner,
                    page_index=page_index,
                    collector=collector,
                    budget=budget,
                    initial_state=state,
                    depth=depth + 1,
                    active_forms=active_forms | {marker},
                )
                inline_image_count += child_images
                if child_text:
                    extracted_texts.append(child_text)
        # 单个操作数异常会破坏后续图形状态解释，按覆盖不完整提前结束当前内容流。
        except (TypeError, ValueError, OverflowError, FileSecurityError):
            collector.coverage_incomplete("pdf_operation_invalid", location_index=page_index)
            return inline_image_count, "\n".join(extracted_texts)
    if stack:
        collector.coverage_incomplete("pdf_graphics_stack_unbalanced", location_index=page_index)
    return inline_image_count, "\n".join(extracted_texts)


def _bounded_pdf_content_value(value: object) -> tuple[bytes, int]:
    """有界解码 Page ``/Contents`` 或 Form stream，不调用无上限 ``get_data``。

    只支持无过滤器和无 Predictor 的 FlateDecode；其他过滤链无法在当前进程证明解压上限，
    因而由调用方记录 coverage 并阻断，而不是先物化无界解压结果。
    """

    if value is None or value.__class__.__name__ == "NullObject":
        return b"", 0
    resolved = value.get_object() if hasattr(value, "get_object") else value
    references = list(resolved) if isinstance(resolved, (list, tuple)) else [resolved]
    if len(references) > MAX_PDF_CONTENT_STREAMS:
        # 内容流数量超出固定预算时仍有尾部未解析，不能截断后返回完整覆盖结论。
        raise FileSecurityError("file.security_scan_unavailable")
    combined = bytearray()
    for reference in references:
        stream = reference.get_object() if hasattr(reference, "get_object") else reference
        separator_bytes = 1 if combined else 0
        remaining = MAX_PDF_PAGE_CONTENT_BYTES - len(combined) - separator_bytes
        if remaining < 0:
            # 多流拼接连分隔符都无法容纳时已超出页面预算，必须在继续解码前阻断。
            raise FileSecurityError("file.security_scan_unavailable")
        chunk = _decode_pdf_content_stream(stream, maximum=remaining)
        if separator_bytes:
            combined.extend(b"\n")
        combined.extend(chunk)
    return bytes(combined), len(references)


def _pdf_xobject(resource_owner: object, name: object) -> tuple[str, object, object]:
    """从当前资源 owner 解析一个 XObject，并保留引用供递归环检测。

    返回：固定 subtype、原引用和解析对象。任何缺失或未知结构只抛固定内部错误，调用方会
    转为 coverage finding，禁止把资源名或对象 repr 带出检测器。
    """

    getter = getattr(resource_owner, "get", None)
    if not callable(getter):
        # 资源 owner 缺少字典接口时无法解析 Do 的真实目标，不能忽略潜在隐藏内容。
        raise FileSecurityError("file.security_scan_unavailable")
    resources = getter("/Resources")
    if resources is None:
        # 当前资源作用域缺失时 XObject 名称没有可信绑定，必须失败关闭而非跨域猜测。
        raise FileSecurityError("file.security_scan_unavailable")
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    xobjects = resources.get("/XObject")
    if xobjects is None:
        # Do 操作存在但资源表没有 XObject 映射，说明内容流与资源图不一致。
        raise FileSecurityError("file.security_scan_unavailable")
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    reference = xobjects.get(name)
    if reference is None:
        # 未绑定的资源名可能掩盖 Form 或图片内容，不能把缺失引用当作空操作跳过。
        raise FileSecurityError("file.security_scan_unavailable")
    resolved = reference.get_object() if hasattr(reference, "get_object") else reference
    subtype = str(resolved.get("/Subtype", ""))
    if subtype not in {"/Image", "/Form"}:
        # 当前检测器仅能证明 Image 与 Form 的覆盖，未知子类型必须按未覆盖资源阻断。
        raise FileSecurityError("file.security_scan_unavailable")
    return subtype, reference, resolved


def _decode_pdf_content_stream(stream: object, *, maximum: int) -> bytes:
    """按调用方剩余预算解码单个 PDF 内容流，绝不修改 pypdf 进程级全局上限。"""

    getter = getattr(stream, "get", None)
    raw = getattr(stream, "_data", None)
    if not callable(getter) or type(raw) is not bytes or type(maximum) is not int or maximum < 0:
        # 缺少原始字节或合法剩余预算时无法执行有界解码，禁止调用第三方无界接口兜底。
        raise FileSecurityError("file.security_scan_unavailable")
    filter_value = getter("/Filter")
    if filter_value is None or filter_value.__class__.__name__ == "NullObject":
        if len(raw) > maximum:
            # 未压缩内容同样受页面剩余预算约束，不能只对压缩流执行体积限制。
            raise FileSecurityError("file.security_scan_unavailable")
        return raw
    if isinstance(filter_value, (list, tuple)) or str(filter_value) not in {
        "/FlateDecode",
        "/Fl",
    }:
        # 多级或未知过滤器无法在当前实现中证明解压上限，拒绝后续无界物化。
        raise FileSecurityError("file.security_scan_unavailable")
    parameters = getter("/DecodeParms")
    if parameters is not None and parameters.__class__.__name__ != "NullObject":
        parameter_getter = getattr(parameters, "get", None)
        if not callable(parameter_getter) or parameter_getter("/Predictor", 1) != 1:
            # PNG/TIFF Predictor 需要额外缓冲和行宽解释；当前发布版选择诚实阻断。
            raise FileSecurityError("file.security_scan_unavailable")
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(raw, maximum + 1)
        if (
            len(decoded) > maximum
            or decompressor.unconsumed_tail
            or not decompressor.eof
            or decompressor.unused_data
        ):
            # 超限、截断或尾随压缩数据都使流边界不可信，必须在解析操作符前拒绝。
            raise FileSecurityError("file.security_scan_unavailable")
        flushed = decompressor.flush(maximum + 1 - len(decoded))
    # 主动预算拒绝需要保留固定安全错误，不能被当作普通解压异常吞掉。
    except FileSecurityError:
        raise
    # 解压库错误不携带原始字节出检测器，统一收敛到覆盖不可用错误。
    except (ValueError, zlib.error):
        raise FileSecurityError("file.security_scan_unavailable") from None
    if len(decoded) + len(flushed) > maximum:
        # flush 仍可能产生尾部字节，最终结果必须再次执行同一硬预算检查。
        raise FileSecurityError("file.security_scan_unavailable")
    return decoded + flushed


def _replace_pdf_state(
    state: _PdfTextState,
    *,
    render_mode: int | None = None,
    font_size: float | None = None,
    white_fill: bool | None = None,
    white_stroke: bool | None = None,
    transparent_fill: bool | None = None,
    transparent_stroke: bool | None = None,
) -> _PdfTextState:
    """不可变更新 PDF 文本状态，避免 q/Q 栈共享可变对象。"""

    return _PdfTextState(
        render_mode=state.render_mode if render_mode is None else render_mode,
        font_size=state.font_size if font_size is None else font_size,
        white_fill=state.white_fill if white_fill is None else white_fill,
        white_stroke=state.white_stroke if white_stroke is None else white_stroke,
        transparent_fill=(
            state.transparent_fill if transparent_fill is None else transparent_fill
        ),
        transparent_stroke=(
            state.transparent_stroke if transparent_stroke is None else transparent_stroke
        ),
    )


def _pdf_color_is_white(operator: bytes, operands: object) -> bool:
    """解释 DeviceGray/RGB/CMYK 的近白填充或描边；非法数值转 coverage。"""

    values = [float(item) for item in operands]
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in values):
        # 非有限数或标准色域外分量无法可靠判断白色，禁止污染后续文本可见性状态。
        raise ValueError("non-finite")
    normalized = operator.lower()
    if normalized == b"g" and len(values) == 1:
        return values[0] >= 0.98
    if normalized == b"rg" and len(values) == 3:
        return all(value >= 0.98 for value in values)
    if normalized == b"k" and len(values) == 4:
        return all(value <= 0.02 for value in values)
    # 操作符与分量数量不匹配说明颜色状态损坏，必须交由调用方转为覆盖不完整。
    raise ValueError("operand-count")


def _pdf_ext_gstate_transparency(
    page: object,
    name: object,
    *,
    current_fill: bool,
    current_stroke: bool,
) -> tuple[bool, bool, bool]:
    """按增量语义解析 ``gs`` 的填充/描边 alpha，并报告 Soft Mask 覆盖状态。"""

    resources = page.get("/Resources")
    if resources is None:
        # 缺少资源作用域时无法解析透明度状态，不能沿用默认不透明值掩盖隐藏文字。
        raise ValueError("resources-missing")
    resources = resources.get_object()
    states = resources.get("/ExtGState")
    if states is None:
        # 内容流引用 gs 但资源表无 ExtGState，后续可见性判断没有可信依据。
        raise ValueError("ext-gstate-missing")
    states = states.get_object()
    state = states.get(name)
    if state is None:
        # 未绑定的图形状态名称不能被当作默认状态，否则可能漏掉透明文本。
        raise ValueError("ext-gstate-name-missing")
    state = state.get_object()
    fill_alpha = float(state.get("/ca", 0.0 if current_fill else 1.0))
    stroke_alpha = float(state.get("/CA", 0.0 if current_stroke else 1.0))
    if (
        not math.isfinite(fill_alpha)
        or not 0.0 <= fill_alpha <= 1.0
        or not math.isfinite(stroke_alpha)
        or not 0.0 <= stroke_alpha <= 1.0
    ):
        # Alpha 必须是有限的标准区间值，异常数值无法安全推导填充和描边可见性。
        raise ValueError("ext-gstate-alpha-invalid")
    soft_mask = state.get("/SMask")
    complete = soft_mask is None or str(soft_mask) == "/None"
    return fill_alpha <= 0.02, stroke_alpha <= 0.02, complete


def _pdf_operand_text(operands: object) -> str:
    """从文本显示操作数提取 Unicode；非空 ByteString 必须按 coverage 失败关闭。

    pypdf 无法按 PDFDocEncoding/Unicode 解码时会保留 ``ByteStringObject``。Latin-1 虽可一一
    映射 bytes，却不等于文档字体实际映射；把它当正文会制造虚假的完整覆盖。``TJ`` 中的数字
    仅表示字距，可以安全忽略。
    """

    if not isinstance(operands, (list, tuple)):
        # 文本操作数结构异常时无法确定实际字符序列，不能把未知内容视为空文本。
        raise FileSecurityError("file.security_scan_unavailable")
    texts: list[str] = []
    for operand in operands:
        candidates = operand if isinstance(operand, (list, tuple)) else (operand,)
        for item in candidates:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, bytes):
                if item:
                    # 非空字节串缺少可靠字体映射，使用 Latin-1 猜测会制造虚假完整覆盖。
                    raise FileSecurityError("file.security_scan_unavailable")
            elif not isinstance(item, (int, float)):
                # TJ 仅允许文本与字距数字，其他对象可能隐藏未解释字符，必须失败关闭。
                raise FileSecurityError("file.security_scan_unavailable")
    # 不能在单个 Tj/TJ 操作数内静默截断，否则攻击者可把指令放在截断点之后绕过检测。
    # 完整字符串交给调用方的全 PDF 正文预算统一计数；超过 4,000,000 code point 时失败关闭。
    return "".join(texts)


def _add_pdf_visual_findings(
    collector: FindingCollector,
    text: str,
    *,
    state: _PdfTextState,
    page_index: int,
) -> None:
    """把 PDF 文本渲染状态映射为隐藏、白色和超小 finding。"""

    prefix = f"pdf_page_{page_index}".encode("ascii")
    # Tr=3 不描边不填充，Tr=7 只更新 clipping path；两者都不会把字形绘制到页面。
    uses_fill = state.render_mode in {0, 2, 4, 6}
    uses_stroke = state.render_mode in {1, 2, 5, 6}
    if (
        state.render_mode in {3, 7}
        or (uses_fill and state.transparent_fill)
        or (uses_stroke and state.transparent_stroke)
    ):
        collector.add(
            detector_code="visual.hidden_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=1000,
            source_kind="pdf_content",
            location_kind="page",
            location_index=page_index,
            evidence=prefix + b":hidden",
        )
    if (uses_fill and state.white_fill) or (uses_stroke and state.white_stroke):
        collector.add(
            detector_code="visual.white_text",
            category="visual_hiding",
            severity="high",
            confidence_milli=980,
            source_kind="pdf_content",
            location_kind="page",
            location_index=page_index,
            evidence=prefix + b":white",
        )
    if state.font_size <= 3.0:
        collector.add(
            detector_code="visual.tiny_text",
            category="visual_hiding",
            severity="high" if find_prompt_injection(text) else "medium",
            confidence_milli=950,
            source_kind="pdf_content",
            location_kind="page",
            location_index=page_index,
            evidence=prefix + b":tiny",
        )


def _scan_pdf_catalog(reader: object, collector: FindingCollector) -> None:
    """检查默认关闭图层、嵌入附件和启动/脚本动作，不遍历无界任意对象。"""

    try:
        root = reader.trailer["/Root"].get_object()
        oc_properties = root.get("/OCProperties")
        if oc_properties:
            properties = oc_properties.get_object()
            defaults = properties.get("/D")
            off = defaults.get_object().get("/OFF") if defaults else None
            base_state = defaults.get_object().get("/BaseState") if defaults else None
            groups = properties.get("/OCGs")
            if (off and len(off) > 0) or (
                str(base_state) == "/OFF" and groups and len(groups) > 0
            ):
                collector.add(
                    detector_code="visual.hidden_layer",
                    category="hidden_layer",
                    severity="high",
                    confidence_milli=1000,
                    source_kind="pdf_content",
                    location_kind="document",
                    location_index=None,
                    evidence=b"pdf_ocg_default_off",
                )
        names = root.get("/Names")
        embedded = names.get_object().get("/EmbeddedFiles") if names else None
        dangerous_action = _pdf_action_is_dangerous(root.get("/OpenAction"))
        dangerous_additional = _pdf_additional_actions_are_dangerous(root.get("/AA"))
        associated_files = root.get("/AF")
        if embedded or associated_files or dangerous_action or dangerous_additional:
            _add_pdf_container_finding(collector, b"pdf_catalog_action_or_attachment")
    except Exception:
        collector.coverage_incomplete("pdf_catalog_invalid")


def _scan_pdf_page_actions(page: object, collector: FindingCollector, *, page_index: int) -> None:
    """检查页面附件和危险动作；只保存页序号，不保存注释或对象 ID。"""

    try:
        annotations = list(page.get("/Annots") or [])
        if len(annotations) > 1_000:
            # 即使危险附件位于第 1001 项之后，coverage finding 也会阻止模型准入。
            collector.coverage_incomplete("pdf_annotation_limit", location_index=page_index)
        if page.get("/AF"):
            _add_pdf_container_finding(
                collector,
                f"pdf_page_{page_index}_associated_file".encode("ascii"),
                page_index=page_index,
            )
        for reference in annotations[:1_000]:
            annotation = reference.get_object()
            dangerous_action = _pdf_action_is_dangerous(annotation.get("/A"))
            dangerous_additional = _pdf_additional_actions_are_dangerous(
                annotation.get("/AA")
            )
            if (
                annotation.get("/Subtype") == "/FileAttachment"
                or dangerous_action
                or dangerous_additional
            ):
                _add_pdf_container_finding(
                    collector,
                    f"pdf_page_{page_index}_attachment_or_action".encode("ascii"),
                    page_index=page_index,
                )
    except Exception:
        collector.coverage_incomplete("pdf_annotation_invalid", location_index=page_index)


def _pdf_action_is_dangerous(value: object) -> bool:
    """只识别可执行或外部副作用动作；普通 GoTo 书签不能被误报为容器攻击。"""

    if value is None:
        return False
    action = value.get_object() if hasattr(value, "get_object") else value
    getter = getattr(action, "get", None)
    if not callable(getter):
        # OpenAction 允许 destination array/name；它们只导航文档，不产生可执行副作用。
        return False
    action_kind = str(getter("/S", ""))
    return action_kind in {
        "/Launch",
        "/JavaScript",
        "/SubmitForm",
        "/ImportData",
        "/GoToE",
        "/Rendition",
        "/RichMediaExecute",
    }


def _pdf_additional_actions_are_dangerous(value: object) -> bool:
    """有界检查 Additional Actions 字典，拒绝用大量键制造对象遍历放大。"""

    if value is None:
        return False
    actions = value.get_object() if hasattr(value, "get_object") else value
    values = getattr(actions, "values", None)
    if not callable(values):
        # 附加动作必须是可枚举字典，未知结构可能隐藏脚本或外部提交动作。
        raise ValueError("additional-actions-invalid")
    candidates = list(values())
    if len(candidates) > 64:
        # 动作数量超过固定上限时不能只检查前缀，否则危险动作可能藏在未扫描尾部。
        raise ValueError("additional-actions-limit")
    return any(_pdf_action_is_dangerous(candidate) for candidate in candidates)


def _add_pdf_container_finding(
    collector: FindingCollector,
    evidence: bytes,
    *,
    page_index: int | None = None,
) -> None:
    """记录 PDF 嵌入附件或可执行动作的 critical 容器事实。"""

    collector.add(
        detector_code="container.nested_archive",
        category="container",
        severity="critical",
        confidence_milli=1000,
        source_kind="document_container",
        location_kind="document" if page_index is None else "page",
        location_index=page_index,
        evidence=evidence,
    )


def _pdf_image_count(
    page: object,
    *,
    depth: int,
    visited: set[tuple[str, int, int]],
) -> int:
    """有界遍历页面/Form XObject 资源，统计潜在 raster image 而不解码内容。"""

    if depth > MAX_PDF_RESOURCE_DEPTH:
        # 资源图递归过深时无法证明剩余 Form 中没有图片，必须阻断而非静默截断。
        raise FileSecurityError("file.security_scan_unavailable")
    resources = page.get("/Resources")
    if resources is None:
        return 0
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return 0
    count = 0
    references = list(xobjects.get_object().values())
    if len(references) > 10_000:
        # 单层资源引用过多会放大遍历成本且超出可验证边界，不能返回截断计数。
        raise FileSecurityError("file.security_scan_unavailable")
    for reference in references:
        obj = reference.get_object()
        marker = _pdf_object_marker(reference, obj)
        if marker in visited:
            continue
        visited.add(marker)
        subtype = obj.get("/Subtype")
        if subtype == "/Image":
            count += 1
        elif subtype == "/Form":
            count += _pdf_image_count(obj, depth=depth + 1, visited=visited)
    return count


def _pdf_object_marker(reference: object, resolved: object) -> tuple[str, int, int]:
    """优先使用稳定间接对象号去重；直接对象才退回当前解析器实例身份。"""

    for candidate in (reference, getattr(resolved, "indirect_reference", None)):
        if candidate is None:
            continue
        object_number = getattr(candidate, "idnum", None)
        generation = getattr(candidate, "generation", 0)
        if type(object_number) is int and type(generation) is int:
            return ("indirect", object_number, generation)
    return ("direct", id(resolved), 0)


def _scan_pdf_candidates(
    content: bytes,
    candidates: list[int],
    *,
    visible_by_page: Mapping[int, set[str]],
    collector: FindingCollector,
    backend: OcrBackend | None,
    max_ocr_pages: int,
    max_image_pixels: int,
    max_total_pixels: int,
) -> tuple[OcrStatus, str | None]:
    """OCR 含图片候选页；纯文本页不进入本函数，也不依赖 OCR 部署。"""

    if len(candidates) > max_ocr_pages:
        collector.coverage_incomplete("pdf_ocr_page_limit")
        candidates = candidates[:max_ocr_pages]
    if backend is None:
        collector.coverage_incomplete("ocr_engine_unavailable")
        return "unavailable", None
    budget = OcrBudget(
        max_images=max_ocr_pages,
        max_image_pixels=max_image_pixels,
        max_total_pixels=max_total_pixels,
    )
    engine_version: str | None = None
    failed = False
    for page_index in candidates:
        try:
            result = scan_pdf_page(content, page_index - 1, backend=backend, budget=budget)
            engine_version = result.engine_version
        except OcrFailure as exc:
            collector.coverage_incomplete(exc.reason, location_index=page_index)
            failed = True
            continue
        _add_ocr_findings(
            collector,
            result.texts,
            visible=visible_by_page.get(page_index, set()),
            source_kind="pdf_image",
            location_kind="page",
            location_index=page_index,
        )
    return ("partial" if failed else "completed"), engine_version


def _add_ocr_findings(
    collector: FindingCollector,
    texts: Iterable[str],
    *,
    visible: set[str] | frozenset[str],
    source_kind: FindingSourceKind,
    location_kind: FindingLocationKind,
    location_index: int,
) -> None:
    """去除与可见抽取重复的 OCR 文本，只为新增文字生成低风险观察或 critical 指令。"""

    novel_texts: list[str] = []
    for box_index, text in enumerate(texts, start=1):
        normalized = normalize_prompt_text(text)
        if not normalized or normalized in visible or any(
            normalized in visible_text for visible_text in visible
        ):
            continue
        novel_texts.append(text)
        add_prompt_findings(
            collector,
            text,
            detector_code="ocr.prompt_instruction",
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence_prefix=f"ocr_{location_index}_{box_index}:".encode("ascii"),
        )
    aggregate = " ".join(novel_texts)
    normalized_aggregate = normalize_prompt_text(aggregate)
    if normalized_aggregate and not any(
        normalized_aggregate in visible_text for visible_text in visible
    ):
        # OCR 引擎常把一句话拆成多行/box；模型消费时会重新拼接，因此安全规则也必须扫描聚合值。
        add_prompt_findings(
            collector,
            aggregate,
            detector_code="ocr.prompt_instruction",
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence_prefix=f"ocr_{location_index}_aggregate:".encode("ascii"),
        )
    if novel_texts:
        collector.add(
            detector_code="ocr.text_observed",
            category="ocr",
            severity="low",
            confidence_milli=850,
            source_kind=source_kind,
            location_kind=location_kind,
            location_index=location_index,
            evidence=f"ocr_text_observed_{location_index}".encode("ascii"),
        )


def _has_coverage_finding(collector: FindingCollector) -> bool:
    """只检查固定 detector code，避免通过私有 evidence 推导覆盖状态。"""

    return collector.has_incomplete_coverage


def _xml_bool(value: str | None) -> bool:
    """解释 OOXML on/off 值；缺省表示 true，显式 false/0/off/no 表示关闭。"""

    return (value or "true").strip().casefold() not in {"0", "false", "off", "no"}


def _suffix(name: str) -> str:
    """返回不含路径解释的末段小写后缀，缺失时为空。"""

    leaf = name.rsplit("/", 1)[-1]
    return f".{leaf.rsplit('.', 1)[-1]}" if "." in leaf else ""
