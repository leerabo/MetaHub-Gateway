"""预算 Scope 的稳定身份与层级顺序共享工具。

该模块只处理已认证、已完成租户归属校验的 ID。它不查询数据库，也不把 ``budget_scope_id``
当成授权凭据；Control 和 Gateway 仍必须在自己的事务中按 tenant_id 验证资源归属。
"""

from __future__ import annotations

import hashlib


# 固定顺序同时用于 Gateway 逐层判定和 Control limiting rule 稳定排序，兼容既有 Application 层。
BUDGET_SCOPE_PRIORITIES = {
    "tenant": 0,
    "project": 1,
    "application": 2,
    "virtual_key": 3,
}

# 版本前缀中包含 NUL，避免未来协议名称与首个长度字段产生文本拼接歧义。
_BUDGET_SCOPE_CANONICAL_PREFIX = b"metahub:budget-scope:v1\0"


def build_budget_scope_id(
    *,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
) -> str:
    """生成 ``bsc_`` 稳定不可逆标识。

    参数：四个字段必须来自可信租户、Environment 和资源绑定；字段保持原始大小写与 Unicode。
    返回：前缀 ``bsc_`` 加 SHA-256 前 32 位小写十六进制。
    约束：长度按 UTF-8 字节计算并逐字段编码，不能用分隔符 join 或 locale 大小写归一化，
    否则包含冒号或中文的合法 ID 可能产生歧义或跨运行环境漂移。
    """

    values = (tenant_id, environment_id, scope_type, scope_id)
    # 任一可信身份为空时都不能生成可被误当作真实 Scope 的稳定标识。
    if any(type(value) is not str or not value for value in values):
        raise ValueError("budget.scope_invalid")
    # 未支持层级不能与合法类型共享摘要空间，调用方必须显式处理范围扩展。
    if scope_type not in BUDGET_SCOPE_PRIORITIES:
        raise ValueError("budget.scope_invalid")
    canonical = bytearray(_BUDGET_SCOPE_CANONICAL_PREFIX)
    for value in values:
        encoded = value.encode("utf-8")
        canonical.extend(str(len(encoded)).encode("ascii"))
        canonical.extend(b":")
        canonical.extend(encoded)
    return "bsc_" + hashlib.sha256(bytes(canonical)).hexdigest()[:32]


def budget_scope_priority(scope_type: str) -> int:
    """返回固定 Scope 优先级，未知类型失败关闭而不是默认为 Key 层。"""

    try:
        return BUDGET_SCOPE_PRIORITIES[scope_type]
    # 类型错误与未知层级统一收敛为固定原因，异常中不回显外部输入。
    except (KeyError, TypeError) as exc:
        raise ValueError("budget.scope_invalid") from exc
