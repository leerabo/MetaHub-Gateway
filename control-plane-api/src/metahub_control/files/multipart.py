"""鉴权后执行的有界单文件 multipart 流解析器。

路由不能声明 ``UploadFile`` 或 ``File`` 参数，因为框架可能在认证依赖完成前把明文正文
spool 到系统临时目录。本模块直接消费 ``Request.stream()``，用 python-multipart callback
把唯一 ``file`` part 限制在内存硬上限内，并拒绝所有歧义 Header 和参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import Final

from fastapi import Request
from metahub_shared.runtime.errors import ApiError

# multipart envelope 只允许比真实文件多 64 KiB，用于 boundary 和两个必要 part Header。
MULTIPART_ENVELOPE_OVERHEAD: Final[int] = 64 * 1024
MAX_MULTIPART_HEADER_BYTES: Final[int] = 8 * 1024
_BOUNDARY_CHARACTERS: Final[frozenset[str]] = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'()+_,-./:=?"
)


@dataclass(frozen=True, slots=True)
class ParsedFilePart:
    """鉴权后解析得到的单个有界文件 part。

    文件名和正文关闭 repr；该对象只在当前上传调用栈内存在，不能进入日志、审计摘要或
    Webhook。服务层仍需执行扩展名、MIME、魔数和深层容器校验。
    """

    filename: str = field(repr=False)
    media_type: str
    content: bytes = field(repr=False)


class _MultipartCollector:
    """接收 python-multipart 回调并维护唯一文件 part 状态机。

    解析状态只在单次已鉴权请求内存活；任何额外字段、重复 Header 或回调顺序漂移都
    失败关闭，避免客户端借 multipart 歧义注入 tenant、owner 或额外明文内容。
    """

    def __init__(self, *, maximum_file_bytes: int) -> None:
        """初始化一次请求的有界解析状态。"""

        self.maximum_file_bytes = maximum_file_bytes
        self.part_count = 0
        self.current_header_name = bytearray()
        self.current_header_value = bytearray()
        self.headers: dict[str, bytes] = {}
        self.filename: str | None = None
        self.media_type: str | None = None
        self.file_bytes = bytearray()
        self.headers_finished = False
        self.part_finished = False
        self.stream_finished = False

    @property
    def callbacks(self) -> dict[str, object]:
        """返回解析器要求的固定回调映射。

        返回值只含 python-multipart 公共回调名，不允许调用方增删回调而绕过 part 数、
        Header 大小或正文大小门禁。
        """

        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        """开始一个 part，并在第二个 part 到达时立即失败关闭。"""

        self.part_count += 1
        if self.part_count != 1:
            # PRD-072 只允许一个 file part；普通字段也可能承载隐藏 owner，必须一律拒绝。
            raise ApiError("file.multipart_invalid")
        self.current_header_name.clear()
        self.current_header_value.clear()
        self.headers.clear()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        """累计当前 part Header 名称，并执行独立大小上限。"""

        self._append_header_bytes(self.current_header_name, data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        """累计当前 part Header 值，并执行独立大小上限。"""

        self._append_header_bytes(self.current_header_value, data, start, end)

    def on_header_end(self) -> None:
        """封印一条唯一 Header，拒绝大小写变体造成的重复解释。"""

        try:
            name = bytes(self.current_header_name).decode("ascii", errors="strict").lower()
        # Header 名必须是规范 ASCII；无法解码时不能让下游代理与应用产生不同解释。
        except UnicodeDecodeError:
            # 公共错误只暴露固定 multipart 失败码，不回显攻击者提供的原始 Header bytes。
            raise ApiError("file.multipart_invalid") from None
        if (
            not name
            or name not in {"content-disposition", "content-type"}
            or name in self.headers
        ):
            # 非必要或重复 Header 会扩大不同代理/解析器的解释差异，入口采用 exact-set。
            raise ApiError("file.multipart_invalid")
        self.headers[name] = bytes(self.current_header_value)
        self.current_header_name.clear()
        self.current_header_value.clear()

    def on_headers_finished(self) -> None:
        """在正文到达前严格解析文件名和声明 MIME。

        参数由此前 Header 回调累计；函数无返回值。只有 exact-set 的 disposition 与
        content-type 可进入正文阶段，tenant 和 owner 永远不能从 multipart 字段恢复。
        """

        if set(self.headers) != {"content-disposition", "content-type"}:
            # 缺失或额外 Header 会造成解析器解释漂移，不能猜测默认 MIME 或文件 owner。
            raise ApiError("file.multipart_invalid")
        self.filename = _parse_content_disposition(self.headers["content-disposition"])
        self.media_type = _parse_part_content_type(self.headers["content-type"])
        self.headers_finished = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """累计真实文件 bytes，并独立于 envelope/Header 执行硬上限。"""

        if not self.headers_finished or self.part_finished:
            # 正文只能出现在完成 Header 后且 part 封印前，乱序回调代表畸形输入。
            raise ApiError("file.multipart_invalid")
        chunk = data[start:end]
        if len(self.file_bytes) + len(chunk) > self.maximum_file_bytes:
            # 真实文件正文上限不能只依赖客户端 Content-Length 或解析器 envelope 配置，
            # 否则伪造长度仍可让单个租户请求无界占用服务内存。
            raise ApiError("file.too_large")
        self.file_bytes.extend(chunk)

    def on_part_end(self) -> None:
        """封印唯一文件 part 并拒绝重复或乱序结束。

        该回调没有参数和返回值；只有 Header 已完整且 part 尚未结束时才能推进状态，
        防止畸形 multipart 在同一缓冲区拼接第二份企业正文。
        """

        if not self.headers_finished or self.part_finished:
            # 未完成 Header 或重复结束都表示状态机失配，不能容错拼接后续正文。
            raise ApiError("file.multipart_invalid")
        self.part_finished = True

    def on_end(self) -> None:
        """记录完整 closing boundary 已被解析器消费。

        函数没有参数和返回值；该终态会在 finish 中与唯一 part 状态联合校验，避免断流
        请求被误认为一个完整且可以持久化的企业文件。
        """

        self.stream_finished = True

    def finish(self) -> ParsedFilePart:
        """验证完整状态并复制出调用栈内的有界 bytes。"""

        if (
            self.part_count != 1
            or not self.part_finished
            or not self.stream_finished
            or self.filename is None
            or self.media_type is None
        ):
            # 只有唯一 part、完整边界、文件名和 MIME 同时成立才允许复制不可变正文。
            raise ApiError("file.multipart_invalid")
        return ParsedFilePart(
            filename=self.filename,
            media_type=self.media_type,
            content=bytes(self.file_bytes),
        )

    def wipe(self) -> None:
        """尽力覆盖可变缓冲区并释放 Header/正文引用。"""

        # Python 不能保证复制出的 immutable bytes 被原地覆盖；仍清除唯一可变大缓冲区，
        # 缩短异常路径和 parser collector 对企业正文的驻留时间。
        self.file_bytes[:] = b"\x00" * len(self.file_bytes)
        self.file_bytes.clear()
        self.current_header_name.clear()
        self.current_header_value.clear()
        self.headers.clear()

    def _append_header_bytes(
        self,
        target: bytearray,
        data: bytes,
        start: int,
        end: int,
    ) -> None:
        """向当前 Header 缓冲追加切片，并阻断 Header 放大。"""

        chunk = data[start:end]
        if len(target) + len(chunk) > MAX_MULTIPART_HEADER_BYTES:
            # Header 超限可能放大内存或隐藏参数，统一按畸形 multipart 失败关闭。
            raise ApiError("file.multipart_invalid")
        target.extend(chunk)


async def read_single_file_part(
    request: Request,
    *,
    maximum_file_bytes: int,
) -> ParsedFilePart:
    """在鉴权依赖完成后流式读取一个 multipart 文件。

    参数：FastAPI Request 与部署级文件上限。返回：唯一有界文件 part。关键约束：不调用
    ``request.form()``，不创建系统明文临时文件，同时核对 Header 声明、envelope 和真实
    part 三个独立大小边界。
    """

    boundary = _request_boundary(request)
    maximum_envelope_bytes = maximum_file_bytes + MULTIPART_ENVELOPE_OVERHEAD
    declared_length = _content_length(request)
    if declared_length is not None and declared_length > maximum_envelope_bytes:
        # 声明长度已经越界时无需读取正文，及早拒绝可避免无授权资源消耗。
        raise ApiError("file.too_large")

    try:
        from python_multipart import MultipartParser
    # 解析依赖缺失属于部署故障，不能伪装成客户端格式问题并诱导重复上传。
    except ImportError:
        # 部署缺失解析依赖属于服务不可用，不应伪装成客户端 multipart 错误。
        raise ApiError("file.storage_unavailable") from None

    collector = _MultipartCollector(maximum_file_bytes=maximum_file_bytes)
    envelope_size = 0
    try:
        try:
            parser = MultipartParser(
                boundary,
                callbacks=collector.callbacks,
                max_size=maximum_envelope_bytes,
                max_header_count=2,
                max_header_size=MAX_MULTIPART_HEADER_BYTES,
            )
            async for chunk in request.stream():
                if type(chunk) is not bytes:
                    # ASGI 流合同漂移时不尝试字符串转换，避免编码过程复制或改写正文。
                    raise ApiError("file.multipart_invalid")
                envelope_size += len(chunk)
                if envelope_size > maximum_envelope_bytes:
                    # 逐块累计是对伪造或缺失 Content-Length 的权威 envelope 硬上限。
                    raise ApiError("file.too_large")
                consumed = parser.write(chunk)
                # python-multipart 超过 max_size 时会截断而不是强制抛错，必须核对消费长度。
                if consumed != len(chunk):
                    raise ApiError("file.too_large")
            parser.finalize()
        # 文件域已规范化错误包含准确业务含义，不能被外层解析异常覆盖成另一错误码。
        except ApiError:
            # 已规范化的文件错误必须原样传播，不能被包成不可诊断的通用解析失败。
            raise
        except Exception:
            # parser/断流异常可能携带 Header、偏移或正文片段，公共错误只保留固定码。
            raise ApiError("file.multipart_invalid") from None
        if declared_length is not None and declared_length != envelope_size:
            # 声明长度与实际流不一致可能是截断或请求走私信号，不能继续保存半文件。
            raise ApiError("file.multipart_invalid")
        return collector.finish()
    finally:
        collector.wipe()


def _request_boundary(request: Request) -> bytes:
    """从唯一 Content-Type Header 提取规范 multipart boundary。

    参数为已鉴权请求，返回仅含允许 ASCII 字符且长度 1..70 的 bytes。重复 Header、额外
    参数或编码歧义全部失败关闭，防止代理与应用选择不同边界。
    """

    values = _raw_header_values(request, b"content-type")
    if len(values) != 1:
        # Header 缺失或重复时不能由框架折叠后猜测一个边界值。
        raise ApiError("file.multipart_invalid")
    message = _header_message("content-type", values[0])
    params = message.get_params(header="content-type", unquote=True, failobj=[])
    if not params or str(params[0][0]).lower() != "multipart/form-data":
        # 文件入口只接受 multipart/form-data，不把 JSON 或任意二进制当作隐式文件。
        raise ApiError("file.multipart_invalid")
    parameter_items = [(str(name).lower(), value) for name, value in params[1:]]
    if len(parameter_items) != 1 or parameter_items[0][0] != "boundary":
        # exact-one boundary 可消除 charset 等多余参数造成的解析差异。
        raise ApiError("file.multipart_invalid")
    value = parameter_items[0][1]
    if type(value) is not str:
        # RFC2231 tuple 等扩展形式不属于 boundary 合同，不能隐式转换。
        raise ApiError("file.multipart_invalid")
    try:
        boundary = value.encode("ascii", errors="strict")
    # 非 ASCII 边界在不同 HTTP 组件中的规范化结果不稳定，必须拒绝。
    except UnicodeEncodeError:
        # 错误响应不得带回原 boundary，以免把攻击输入传播到日志或诊断。
        raise ApiError("file.multipart_invalid") from None
    if (
        not 1 <= len(boundary) <= 70
        or any(character not in _BOUNDARY_CHARACTERS for character in value)
    ):
        # 长度和字符白名单共同收敛 RFC 边界，排除空白、控制符和分隔歧义。
        raise ApiError("file.multipart_invalid")
    return boundary


def _content_length(request: Request) -> int | None:
    """读取唯一规范 Content-Length；缺失时仍由逐块累计执行真实上限。"""

    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        # 重复 Content-Length 可能在代理与 ASGI 服务器间产生请求边界分歧。
        raise ApiError("file.multipart_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # 长度字段只允许 ASCII 十进制，任何编码异常都不能容错转换。
    except UnicodeDecodeError:
        # 固定错误码避免回显原始 Header，并让调用方按格式错误处理。
        raise ApiError("file.multipart_invalid") from None
    if (
        not value
        or len(value) > 12
        or any(character not in "0123456789" for character in value)
        or (len(value) > 1 and value.startswith("0"))
    ):
        # 拒绝空值、超长数值和前导零，确保所有组件使用同一规范长度。
        raise ApiError("file.multipart_invalid")
    return int(value, 10)


def _parse_content_disposition(value: bytes) -> str:
    """解析唯一 form-data name/filename 参数并严格处理 UTF-8 扩展名。

    参数为有界 Header bytes，返回原始交互文件名；该名称只交给后续规范化与独立加密，
    不作为磁盘路径。重复参数、错误 owner 或未知参数均按畸形 multipart 拒绝。
    """

    message = _header_message("content-disposition", value)
    params = message.get_params(
        header="content-disposition",
        unquote=False,
        failobj=[],
    )
    if not params or str(params[0][0]).lower() != "form-data":
        # disposition 类型必须明确是 form-data，attachment 等类型不能进入上传合同。
        raise ApiError("file.multipart_invalid")
    parsed: dict[str, object] = {}
    for raw_name, raw_value in params[1:]:
        name = str(raw_name).lower()
        # email 会把 filename* 规范为 filename + RFC2231 tuple；重复 filename/filename*
        # 因而会在这里命中同一 key，不能由 get_filename 静默选择其中一个。
        if name not in {"name", "filename"} or name in parsed:
            # 未知或重复参数会造成文件名/owner 选择歧义，入口必须失败关闭。
            raise ApiError("file.multipart_invalid")
        parsed[name] = raw_value
    if set(parsed) != {"name", "filename"}:
        # name 与 filename 都是唯一文件 part 的必要字段，不能推断缺省值。
        raise ApiError("file.multipart_invalid")
    name = _decode_disposition_parameter(parsed["name"], allow_extended=False)
    filename = _decode_disposition_parameter(parsed["filename"], allow_extended=True)
    if name != "file" or not filename:
        # 字段 owner 固定为 file，禁止通过普通字段注入 tenant 或业务 owner。
        raise ApiError("file.multipart_invalid")
    return filename


def _parse_part_content_type(value: bytes) -> str:
    """解析文件 part 的无参数 MIME，格式白名单由 Shared 合同继续复核。"""

    message = _header_message("content-type", value)
    params = message.get_params(header="content-type", unquote=True, failobj=[])
    if not params or len(params) != 1:
        # 文件 part MIME 不接受 charset 等参数，避免扩展名和解析器语义漂移。
        raise ApiError("file.multipart_invalid")
    media_type = str(params[0][0]).lower()
    if not media_type or "/" not in media_type:
        # MIME 必须具备 type/subtype 结构，具体白名单由 Shared 格式合同二次复核。
        raise ApiError("file.multipart_invalid")
    return media_type


def _decode_disposition_parameter(value: object, *, allow_extended: bool) -> str:
    """解码普通 quoted-string 或严格 UTF-8 RFC2231 参数。

    参数是标准库结构化值和是否允许扩展编码的开关，返回单一字符串。这里只解码 Header，
    不清洗路径；后续文件合同仍会拒绝控制符、路径分隔符和危险后缀。
    """

    if isinstance(value, tuple):
        if not allow_extended or len(value) != 3:
            # 只有 filename 允许标准三元组扩展编码，name owner 必须保持普通字符串。
            raise ApiError("file.multipart_invalid")
        charset = str(value[0] or "").lower()
        if charset != "utf-8":
            # 单一 UTF-8 合同避免不同本地编码把同一字节解释成不同文件名。
            raise ApiError("file.multipart_invalid")
        try:
            decoded = collapse_rfc2231_value(value, errors="strict")
        # 未知字符集、非法转义或错误对象类型都代表不可消除的 Header 歧义。
        except (LookupError, UnicodeError, TypeError):
            # 统一错误码不携带原参数，防止文件名或 Header 明文进入异常链。
            raise ApiError("file.multipart_invalid") from None
        if type(decoded) is not str:
            # 标准库若返回非字符串即视为合同漂移，不能用 str() 猜测转换。
            raise ApiError("file.multipart_invalid")
        return decoded
    if type(value) is not str:
        # 普通参数只接受精确字符串，bytes 或自定义对象均不能隐式解码。
        raise ApiError("file.multipart_invalid")
    # unquote=False 保留普通引号；使用 email 的结构化 API解码一次，避免自制转义规则。
    holder = Message()
    holder["x-parameter"] = f"value={value}"
    decoded = holder.get_param("value", header="x-parameter", unquote=True)
    if type(decoded) is not str:
        # quoted-string 解码必须产生单一字符串，否则拒绝继续处理敏感文件名。
        raise ApiError("file.multipart_invalid")
    return decoded


def _header_message(name: str, value: bytes) -> Message:
    """把有界 Header bytes 转为标准库结构化消息对象。"""

    if b"\r" in value or b"\n" in value or len(value) > MAX_MULTIPART_HEADER_BYTES:
        # 换行可能注入第二条 Header，超限会放大内存；两者都在结构化解析前拒绝。
        raise ApiError("file.multipart_invalid")
    try:
        decoded = value.decode("utf-8", errors="strict")
    # Header 值编码错误时不使用替换字符，否则可能改变参数边界或文件名。
    except UnicodeDecodeError:
        # 响应只暴露固定码，避免底层解码错误包含敏感 Header 片段。
        raise ApiError("file.multipart_invalid") from None
    message = Message()
    message[name] = decoded
    return message


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """从 ASGI 原始 Header 列表读取全部同名值，避免框架折叠重复项。"""

    values: list[bytes] = []
    for raw_name, raw_value in request.scope.get("headers", ()):
        if type(raw_name) is bytes and raw_name.lower() == name:
            if type(raw_value) is not bytes:
                # ASGI Header 值必须是 bytes；合同漂移时不能将任意对象序列化进解析器。
                raise ApiError("file.multipart_invalid")
            values.append(raw_value)
    return values
