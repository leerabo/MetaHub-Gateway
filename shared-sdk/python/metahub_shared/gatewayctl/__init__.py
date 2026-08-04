"""MetaHub 配置迁移命令行客户端。

该包只通过 Control API 导入、导出配置，不读取数据库，也不接受客户端指定租户。
公开入口采用惰性加载，避免 ``python -m metahub_shared.gatewayctl.cli`` 执行前重复导入
目标模块并产生 runpy 警告。
"""

from __future__ import annotations

from typing import Any

__all__ = ["main", "run_cli"]


def __getattr__(name: str) -> Any:
    """按需解析 CLI 公开入口，同时保持包级向后兼容导入。

    参数：``name`` 只能是 ``main`` 或 ``run_cli``。返回值：对应的 CLI 函数。
    关键约束：包初始化阶段不能提前导入 ``cli``，否则模块执行会出现重复加载警告。
    """

    if name in __all__:
        from . import cli

        return getattr(cli, name)
    # 未声明名称必须保持标准 AttributeError 语义，不能静默返回空值掩盖调用方拼写错误。
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
