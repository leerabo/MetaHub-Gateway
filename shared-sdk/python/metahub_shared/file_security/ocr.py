"""PRD-073 有界离线 OCR 适配器。

OCR 只处理内存中的受限 RGB 图片，不访问网络、不下载模型，也不保存识别文本。正式后端使用
``rapidocr-onnxruntime``；测试可注入同一 Protocol 的纯内存替身验证安全状态机。
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Protocol

from .contracts import FileSecurityError

# OCR 版本、默认预算和发布包硬上限集中冻结；请求参数不得覆盖这些资源与文本边界。
OCR_ENGINE_VERSION: Final[str] = "rapidocr-onnxruntime-1.4.4"
DEFAULT_MAX_IMAGE_PIXELS: Final[int] = 12_000_000
DEFAULT_MAX_TOTAL_PIXELS: Final[int] = 50_000_000
HARD_MAX_IMAGE_PIXELS: Final[int] = 50_000_000
HARD_MAX_TOTAL_PIXELS: Final[int] = 200_000_000
MAX_OCR_BOXES_PER_IMAGE: Final[int] = 2_000
MAX_OCR_TEXT_PER_BOX: Final[int] = 2_048
MAX_OCR_TOTAL_TEXT: Final[int] = 100_000


class OcrFailure(FileSecurityError):
    """携带固定内部 reason 的 OCR 失败；reason 只能用于 coverage HMAC，不进入响应。"""

    def __init__(self, reason: str) -> None:
        """保存静态失败原因并对外统一为安全扫描不可用。"""

        self.reason = reason
        super().__init__("file.security_scan_unavailable")


class OcrBackend(Protocol):
    """离线 OCR 后端最小合同；实现不得自行联网、持久化或记录图片/文本。"""

    engine_version: str

    def recognize(self, image: object) -> Iterable[str]:
        """识别一张已完成像素门禁的 RGB PIL 图片并返回文本段。"""


@dataclass(slots=True)
class OcrBudget:
    """单次文件扫描共享的像素、图片和文本预算。"""

    max_images: int
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    max_total_pixels: int = DEFAULT_MAX_TOTAL_PIXELS
    consumed_images: int = 0
    consumed_pixels: int = 0
    consumed_text: int = 0

    def __post_init__(self) -> None:
        """冻结部署预算的合法范围，调用方不能按请求绕过发布包硬上限。"""

        if (
            type(self.max_images) is not int
            or not 1 <= self.max_images <= 200
            or type(self.max_image_pixels) is not int
            or not 1 <= self.max_image_pixels <= HARD_MAX_IMAGE_PIXELS
            or type(self.max_total_pixels) is not int
            or not self.max_image_pixels
            <= self.max_total_pixels
            <= HARD_MAX_TOTAL_PIXELS
        ):
            raise OcrFailure("ocr_budget_invalid")

    def consume_image(self, *, width: int, height: int) -> None:
        """在解码和 OCR 前预扣图片/像素预算，超限立即失败关闭。"""

        if type(width) is not int or type(height) is not int or width < 1 or height < 1:
            raise OcrFailure("ocr_image_dimensions_invalid")
        pixels = width * height
        if pixels > self.max_image_pixels:
            raise OcrFailure("ocr_image_pixel_limit")
        if self.consumed_images >= self.max_images:
            raise OcrFailure("ocr_image_count_limit")
        if self.consumed_pixels + pixels > self.max_total_pixels:
            raise OcrFailure("ocr_total_pixel_limit")
        self.consumed_images += 1
        self.consumed_pixels += pixels

    def consume_text(self, value: str) -> None:
        """逐段累计 OCR 字符，禁止先拼接无界结果再截断。"""

        if len(value) > MAX_OCR_TEXT_PER_BOX:
            raise OcrFailure("ocr_box_text_limit")
        if self.consumed_text + len(value) > MAX_OCR_TOTAL_TEXT:
            raise OcrFailure("ocr_total_text_limit")
        self.consumed_text += len(value)


@dataclass(frozen=True, slots=True)
class OcrImageResult:
    """单张图片 OCR 的有界文本段；正文关闭 repr 且不得持久化。"""

    texts: tuple[str, ...] = field(repr=False)
    engine_version: str
    width: int
    height: int


class RapidOcrBackend:
    """懒加载 rapidocr-onnxruntime 的离线后端，禁止运行期下载模型。"""

    engine_version = OCR_ENGINE_VERSION

    def __init__(self) -> None:
        """创建尚未加载模型的后端；真实初始化推迟到第一张候选图片。"""

        self._engine: object | None = None

    def recognize(self, image: object) -> Iterable[str]:
        """调用本地 RapidOCR 并只投影文本列，第三方异常统一折叠为静态 reason。"""

        try:
            if self._engine is None:
                from rapidocr_onnxruntime import RapidOCR

                # wheel 内模型由部署依赖提供；此处不传 URL 或下载目录，运行期不能访问网络。
                self._engine = RapidOCR()
            result, _elapsed = self._engine(image)
        # 部署包缺少本地模型或运行库时明确失败，绝不尝试联网下载后继续处理企业图片。
        except (ImportError, ModuleNotFoundError):
            raise OcrFailure("ocr_engine_unavailable") from None
        except Exception:
            # ONNX、模型或解码异常可能携带宿主路径，不能原样抛给服务层。
            raise OcrFailure("ocr_engine_failed") from None
        if result is None:
            return ()
        texts: list[str] = []
        total_text = 0
        try:
            for index, item in enumerate(result):
                if index >= MAX_OCR_BOXES_PER_IMAGE:
                    # 在构造第二份 Python 列表前执行 box 上限，避免恶意结果先被无界复制。
                    raise OcrFailure("ocr_box_count_limit")
                if not isinstance(item, (list, tuple)) or len(item) < 2 or type(item[1]) is not str:
                    raise OcrFailure("ocr_result_invalid")
                if len(item[1]) > MAX_OCR_TEXT_PER_BOX:
                    raise OcrFailure("ocr_box_text_limit")
                total_text += len(item[1])
                if total_text > MAX_OCR_TOTAL_TEXT:
                    raise OcrFailure("ocr_total_text_limit")
                texts.append(item[1])
        except OcrFailure:
            raise
        except Exception:
            raise OcrFailure("ocr_result_invalid") from None
        return tuple(texts)


def scan_image_bytes(
    content: object,
    *,
    backend: OcrBackend,
    budget: OcrBudget,
) -> OcrImageResult:
    """校验 header/像素后离线识别一张内存图片并返回有界文本段。"""

    if type(content) is not bytes or not content:
        raise OcrFailure("ocr_image_invalid")
    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as source:
            width, height = source.size
            budget.consume_image(width=width, height=height)
            # 在像素预算通过后才触发完整 decode；不修改 Pillow 进程级 DecompressionBomb 配置。
            source.load()
            image = source.convert("RGB")
    except OcrFailure:
        raise
    except Exception:
        raise OcrFailure("ocr_image_invalid") from None
    return _recognize_rgb(image, backend=backend, budget=budget, width=width, height=height)


def scan_pdf_page(
    content: object,
    page_index: int,
    *,
    backend: OcrBackend,
    budget: OcrBudget,
    scale: float = 2.0,
) -> OcrImageResult:
    """使用 pypdfium2 有界渲染单个候选页，再交给同一离线 OCR 门禁。"""

    if (
        type(content) is not bytes
        or type(page_index) is not int
        or page_index < 0
        or type(scale) not in {int, float}
        or not math.isfinite(float(scale))
        or float(scale) != 2.0
    ):
        raise OcrFailure("ocr_pdf_page_invalid")
    try:
        import pypdfium2

        document = pypdfium2.PdfDocument(content)
        try:
            if page_index >= len(document):
                raise OcrFailure("ocr_pdf_page_invalid")
            page = document[page_index]
            page_width, page_height = page.get_size()
            if (
                not isinstance(page_width, (int, float))
                or isinstance(page_width, bool)
                or not isinstance(page_height, (int, float))
                or isinstance(page_height, bool)
                or not math.isfinite(float(page_width))
                or not math.isfinite(float(page_height))
                or page_width <= 0
                or page_height <= 0
            ):
                raise OcrFailure("ocr_pdf_page_dimensions_invalid")
            reserved_width = math.ceil(float(page_width) * float(scale))
            reserved_height = math.ceil(float(page_height) * float(scale))
            # MediaBox 可以声明极大页面；必须在 PDFium 分配 bitmap 之前预扣像素预算。
            budget.consume_image(width=reserved_width, height=reserved_height)
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil().convert("RGB")
        finally:
            document.close()
        width, height = image.size
        if width > reserved_width or height > reserved_height or width < 1 or height < 1:
            # 渲染器输出不得超过预先证明的尺寸，否则前置预算不再可信。
            raise OcrFailure("ocr_pdf_render_dimensions_invalid")
    except OcrFailure:
        raise
    # PDFium 是离线渲染必需依赖；缺失时不能跳过图片页后伪装完整覆盖。
    except (ImportError, ModuleNotFoundError):
        # PDFium 是离线渲染必需依赖；缺失时不能降级跳过图片页并伪装 coverage complete。
        raise OcrFailure("ocr_pdf_renderer_unavailable") from None
    except Exception:
        raise OcrFailure("ocr_pdf_render_failed") from None
    return _recognize_rgb(image, backend=backend, budget=budget, width=width, height=height)


def _recognize_rgb(
    image: object,
    *,
    backend: OcrBackend,
    budget: OcrBudget,
    width: int,
    height: int,
) -> OcrImageResult:
    """调用受信 OCR backend 并逐段验证类型、box 数和累计文本体积。"""

    # Protocol 含 ``engine_version`` 数据属性时，Python 不保证运行期 isinstance 安全；这里只校验
    # 当前调用真正依赖的可调用能力，版本字段在识别完成后另做严格类型和长度校验。
    if not callable(getattr(backend, "recognize", None)):
        raise OcrFailure("ocr_engine_unavailable")
    try:
        values = backend.recognize(image)
        texts: list[str] = []
        for index, value in enumerate(values):
            if index >= MAX_OCR_BOXES_PER_IMAGE:
                raise OcrFailure("ocr_box_count_limit")
            if type(value) is not str:
                raise OcrFailure("ocr_result_invalid")
            normalized = value.strip()
            if not normalized:
                continue
            budget.consume_text(normalized)
            texts.append(normalized)
    except OcrFailure:
        raise
    except Exception:
        raise OcrFailure("ocr_engine_failed") from None
    version = getattr(backend, "engine_version", "")
    if type(version) is not str or not version or len(version) > 64:
        raise OcrFailure("ocr_engine_version_invalid")
    return OcrImageResult(
        texts=tuple(texts),
        engine_version=version,
        width=width,
        height=height,
    )
