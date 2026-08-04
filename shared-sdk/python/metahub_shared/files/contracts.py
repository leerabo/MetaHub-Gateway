"""PRD-072 文件上传的纯数据合同、格式识别和确定性 Token 估算。

本模块不读取数据库、HTTP 请求或对象存储。原文件和抽取正文只在调用栈内传递，敏感字段
关闭 ``repr``；所有校验异常只携带固定错误码，禁止把文件名、正文或解析器细节带入日志。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field
from typing import Final, Literal, cast

FileKind = Literal["txt", "docx", "pdf"]

# 文件大小、名称和文本边界是 Control、解析器与测试共享的发布合同，调用方不得自行放宽。
DEFAULT_MAX_FILE_BYTES: Final[int] = 20 * 1024 * 1024
MIN_MAX_FILE_BYTES: Final[int] = 1 * 1024 * 1024
MAX_MAX_FILE_BYTES: Final[int] = 50 * 1024 * 1024
MAX_FILENAME_BYTES: Final[int] = 255
MAX_EXTRACTED_TEXT_BYTES: Final[int] = 8 * 1024 * 1024
MAX_EXTRACTED_CODE_POINTS: Final[int] = 4_000_000
TOKEN_ESTIMATOR_VERSION: Final[str] = "file-token-estimator-v1"

# MIME 与扩展名必须一一对应；octet-stream 不作为浏览器兼容后门，避免内容嗅探产生双重解释。
MEDIA_TYPE_BY_KIND: Final[dict[FileKind, str]] = {
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}
EXTRACTOR_VERSION_BY_KIND: Final[dict[FileKind, str]] = {
    "txt": "txt-v1",
    "docx": "docx-v1",
    "pdf": "pdf-v1",
}

# 双向控制字符和可执行后缀可能让界面显示与真实文件类型不同，必须在持久化文件名之前拒绝。
_BIDI_CONTROL_CHARACTERS: Final[frozenset[str]] = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_DANGEROUS_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "bat",
        "cmd",
        "com",
        "exe",
        "html",
        "hta",
        "jar",
        "js",
        "lnk",
        "msi",
        "ps1",
        "scr",
        "sh",
        "svg",
        "vbs",
    }
)


class FileContractError(ValueError):
    """只携带稳定文件错误码的合同异常。

    参数：公开固定错误码。返回值：无。关键约束：异常文本不得拼接文件名、正文、MIME、
    密文或第三方解析器消息，避免框架诊断扩大敏感数据暴露面。
    """

    def __init__(self, code: str) -> None:
        """保存固定错误码，供 Control API 统一映射。"""

        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedUploadFormat:
    """扩展名、MIME 和魔数完成一致性校验后的文件格式。

    原始名称是授权页面所需字段但仍属敏感业务数据，因此关闭 repr；服务端对象路径绝不能
    从该名称派生，只能使用随机 file ID。
    """

    original_name: str = field(repr=False)
    kind: FileKind
    extension: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """解析器返回的有界抽取正文和不可逆计数。

    ``text`` 只允许交给 DLP、Token 估算和加密存储，关闭 repr 且不得进入普通数据库列；
    其余字段可以作为 FileExtract 安全投影。
    """

    text: str = field(repr=False)
    extractor_version: str
    text_size_bytes: int
    text_code_points: int
    estimated_tokens: int
    estimator_version: str = TOKEN_ESTIMATOR_VERSION


def normalize_filename(value: object) -> tuple[str, FileKind]:
    """规范化并校验客户端文件名。

    参数：multipart Content-Disposition 解出的 filename。返回：NFC 名称和受支持格式。
    关键约束：拒绝路径、控制字符、双向欺骗和危险复合后缀；错误不回显原始名称。
    """

    if type(value) is not str:
        # 非字符串名称不能由 ``str`` 静默转换，否则对象 repr 可能进入后续异常或页面。
        raise FileContractError("file.name_invalid")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or len(normalized.encode("utf-8")) > MAX_FILENAME_BYTES
    ):
        # 文件名不能参与路径解释，超长名称也会放大密文和响应体。
        raise FileContractError("file.name_invalid")
    if any(
        unicodedata.category(character) in {"Cc", "Cf"}
        or character in _BIDI_CONTROL_CHARACTERS
        for character in normalized
    ):
        # Cc/Cf 与双向控制会让日志、浏览器和文件系统看到不同名称，统一拒绝。
        raise FileContractError("file.name_invalid")

    parts = normalized.rsplit(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        # 显式扩展名是 MIME/魔数三方一致性的一部分，不能靠内容嗅探猜测。
        raise FileContractError("file.unsupported_format")
    extension = parts[1].lower()
    if extension not in MEDIA_TYPE_BY_KIND:
        # 白名单外扩展名没有可信解析器，必须在创建 tenant 文件 owner 前直接拒绝。
        raise FileContractError("file.unsupported_format")

    earlier_suffixes = [part.lower() for part in parts[0].split(".")[1:]]
    if any(suffix in _DANGEROUS_SUFFIXES for suffix in earlier_suffixes):
        # 例如 invoice.exe.pdf 会在部分终端中隐藏最后扩展名，不能作为普通 PDF 接收。
        raise FileContractError("file.name_invalid")
    return normalized, cast(FileKind, extension)


def validate_upload_format(
    *,
    filename: object,
    media_type: object,
    content: object,
) -> ValidatedUploadFormat:
    """执行文件名、MIME 和魔数的低成本三方一致性校验。

    参数：multipart 文件名、part MIME 和已受大小限制的 bytes。返回：可信格式投影。
    关键约束：该函数在创建 UploadedFile owner 前运行，未知格式不得污染业务表。
    """

    original_name, kind = normalize_filename(filename)
    if type(media_type) is not str or type(content) is not bytes:
        # 禁止隐式字符串化或 bytes 转换，以免对象 repr 或含糊编码进入格式判断。
        raise FileContractError("file.unsupported_format")
    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    if normalized_media_type != MEDIA_TYPE_BY_KIND[kind]:
        # MIME 不一致必须拒绝，不能让扩展名成为唯一信任来源。
        raise FileContractError("file.unsupported_format")
    if not content:
        # 空文件不能形成可扫描正文，也不能用零字节记录冒充成功上传。
        raise FileContractError("file.no_extractable_text")
    if kind == "pdf" and not content.startswith(b"%PDF-"):
        # PDF 扩展名必须与固定魔数一致，防止伪装内容进入第三方解析器。
        raise FileContractError("file.unsupported_format")
    if kind == "docx" and not content.startswith(b"PK"):
        # DOCX 必须先证明是 ZIP 容器，深层 OOXML 结构随后再执行有界校验。
        raise FileContractError("file.unsupported_format")
    if kind == "txt":
        # TXT 没有稳定魔数；NUL 是二进制内容的强信号，完整 UTF-8 校验由解析阶段完成。
        if b"\x00" in content:
            # 含 NUL 的输入按二进制伪装处理，不能交给 UTF-8 文本链路继续解释。
            raise FileContractError("file.unsupported_format")
    return ValidatedUploadFormat(
        original_name=original_name,
        kind=kind,
        extension=kind,
        media_type=MEDIA_TYPE_BY_KIND[kind],
    )


def estimate_file_tokens(text: object) -> int:
    """按冻结的 CJK/非空字符规则估算完整文件 Token。

    参数：已完成大小校验的抽取文本。返回：非负整数 Token 估算。关键约束：不调用网络、
    厂商 Tokenizer 或浮点算法，确保 Control、测试和后续批任务得到相同结果。
    """

    if type(text) is not str:
        # Token 估算只接受已抽取文本，禁止把任意对象转换后泄露其 repr。
        raise FileContractError("file.parse_failed")
    cjk = 0
    other = 0
    for character in text:
        if character.isspace():
            continue
        if "\u4e00" <= character <= "\u9fff":
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)


def build_extracted_text(text: object, *, extractor_version: object) -> ExtractedText:
    """校验抽取正文边界并生成 FileExtract 的安全计数。

    参数：第三方解析器文本和冻结 extractor version。返回：关闭 repr 的正文 DTO。
    关键约束：空文本与超限文本必须失败关闭，不能用零 Token 冒充成功解析。
    """

    if type(text) is not str or type(extractor_version) is not str:
        # 解析结果与版本必须由受信解析器显式提供，不能从任意对象猜测。
        raise FileContractError("file.parse_failed")
    if extractor_version not in EXTRACTOR_VERSION_BY_KIND.values():
        # 未登记版本无法被持久状态和后续任务一致解释，因此必须失败关闭。
        raise FileContractError("file.parse_failed")
    if not text.strip():
        # 只有空白的抽取结果不具备 DLP 或模型处理价值，不能标记为 ready。
        raise FileContractError("file.no_extractable_text")
    encoded = text.encode("utf-8")
    if len(text) > MAX_EXTRACTED_CODE_POINTS or len(encoded) > MAX_EXTRACTED_TEXT_BYTES:
        # 文本和 UTF-8 两个维度都要受限，避免 CJK 与多字节字符绕过单一计数。
        raise FileContractError("file.extract_limit_exceeded")
    return ExtractedText(
        text=text,
        extractor_version=extractor_version,
        text_size_bytes=len(encoded),
        text_code_points=len(text),
        estimated_tokens=estimate_file_tokens(text),
    )
