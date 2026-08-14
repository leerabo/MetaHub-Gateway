"""套餐额度的精确金额、顺序和幂等合同。

金额跨 Gateway、Control、SQLite 与 Console 时只使用 CNY 微元整数或固定六位小数
文本，禁止经过二进制浮点。规范摘要只覆盖调用意图，不包含幂等键、调整原因之外的
任意请求正文、凭据或租户外部数据。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Final, Literal

QuotaType = Literal["paid", "free", "trial", "gift"]
AllocationClass = Literal["paid", "free", "trial", "gift", "mixed", "unallocated"]

# 额度类型、消费顺序、合同版本和金额上限共同构成跨服务冻结合同。
QUOTA_TYPES: Final[tuple[QuotaType, ...]] = ("paid", "free", "trial", "gift")
QUOTA_CONSUMPTION_ORDER: Final[tuple[QuotaType, ...]] = (
    "trial",
    "free",
    "gift",
    "paid",
)
QUOTA_ORDER_VERSION: Final[str] = "trial_free_gift_paid_v1"
MAX_CNY_MICROS: Final[int] = 1_000_000_000_000_000


class QuotaContractError(ValueError):
    """携带稳定错误码且不暴露原始输入的共享额度合同异常。"""

    def __init__(self, code: str) -> None:
        """保存已校验错误码，供各服务映射统一公开错误响应。"""

        super().__init__(code)
        self.code = code


def parse_cny_micros(value: object, *, allow_zero: bool = True) -> int:
    """把普通 ASCII 十进制金额严格转换为 CNY 微元整数。

    最多六位小数，不接受符号、指数、前导零、JSON number、NaN/Infinity 或空小数。
    """

    # 金额只能来自有界 ASCII 文本，拒绝浮点和非标准字符进入财务合同。
    if type(value) is not str or not value or not value.isascii():
        raise QuotaContractError("quota.amount_invalid")
    integer, separator, fraction = value.partition(".")
    if (
        not 1 <= len(integer) <= 10
        or not integer.isdecimal()
        or (len(integer) > 1 and integer.startswith("0"))
        or (
            separator
            and (not 1 <= len(fraction) <= 6 or not fraction.isdecimal())
        )
    ):
        # 结构或精度非法时只返回稳定错误码，不回显可能敏感的原始输入。
        raise QuotaContractError("quota.amount_invalid")
    micros = int(integer) * 1_000_000 + int(fraction.ljust(6, "0") if separator else "0")
    # 有界微元避免数据库溢出；需要正额的调用方不能用零额绕过校验。
    if micros > MAX_CNY_MICROS or (micros == 0 and not allow_zero):
        raise QuotaContractError("quota.amount_invalid")
    return micros


def format_cny_micros(value: object) -> str:
    """把有界非负微元整数格式化为固定六位小数文本。"""

    # 格式化入口同样拒绝布尔值、负数和越界整数，保持公开金额可信。
    if type(value) is not int or value < 0 or value > MAX_CNY_MICROS:
        raise QuotaContractError("quota.amount_invalid")
    integer, fraction = divmod(value, 1_000_000)
    return f"{integer}.{fraction:06d}"


def classify_allocations(quota_types: Iterable[str], *, amount_micros: int) -> AllocationClass:
    """从真实正额 allocation 类型推导稳定分类；零额或无分摊显式为 unallocated。"""

    # 分摊分类只接受非负整数微元，避免浮点或负额改变额度归属统计。
    if type(amount_micros) is not int or amount_micros < 0:
        raise QuotaContractError("quota.allocation_invalid")
    values = tuple(quota_types)
    # 未知额度类型不得回退为付费额度，否则会污染财务和用量审计。
    if any(value not in QUOTA_TYPES for value in values):
        raise QuotaContractError("quota.allocation_invalid")
    unique = set(values)
    if amount_micros == 0 or not unique:
        return "unallocated"
    if len(unique) == 1:
        return next(iter(unique))  # type: ignore[return-value]
    return "mixed"


def canonical_quota_intent_sha256(value: Mapping[str, object]) -> str:
    """计算有界额度调整意图的稳定 SHA-256，不接受隐式或递归 JSON 类型。"""

    # 幂等意图必须是非空普通字典，拒绝自定义映射产生隐式序列化行为。
    if type(value) is not dict or not value:
        raise QuotaContractError("quota.intent_invalid")
    canonical = _canonical_value(value, depth=1)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    # 摘要输入设置严格字节上限，防止无界内容进入审计与幂等路径。
    if len(encoded) > 4096:
        raise QuotaContractError("quota.intent_invalid")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: object, *, depth: int) -> object:
    """递归复制规范 JSON 子集，拒绝 float、bytes、空键和无界层级。"""

    # 深度和容器规模均受限，避免递归载荷消耗过多计算与内存资源。
    if depth > 8:
        raise QuotaContractError("quota.intent_invalid")
    if value is None or type(value) in {str, int, bool}:
        # 字符串不得为空或携带控制字符，避免污染规范摘要和审计展示。
        if type(value) is str and (not value or any(ord(character) < 32 for character in value)):
            raise QuotaContractError("quota.intent_invalid")
        return value
    if type(value) is list:
        # 列表长度必须有界，防止恶意意图对象放大递归处理成本。
        if len(value) > 32:
            raise QuotaContractError("quota.intent_invalid")
        return [_canonical_value(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        # 对象必须非空且字段数量有界，维持跨语言规范化的确定性。
        if not value or len(value) > 32:
            raise QuotaContractError("quota.intent_invalid")
        result: dict[str, object] = {}
        for key, child in value.items():
            # 字段名只接受非空安全文本，拒绝控制字符破坏日志和摘要边界。
            if type(key) is not str or not key or any(ord(character) < 32 for character in key):
                raise QuotaContractError("quota.intent_invalid")
            result[key] = _canonical_value(child, depth=depth + 1)
        return result
    # 其余对象类型可能携带隐式执行或不稳定表示，必须统一拒绝。
    raise QuotaContractError("quota.intent_invalid")
