"""惰性加载官方 ``openai`` 包并构建 OpenAI-compatible 客户端。"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from .config import ExampleConfig


class OpenAIClientUnavailable(RuntimeError):
    """表示官方 ``openai`` 依赖缺失或无法完成客户端初始化。

    调用方应把该异常映射为固定的本地错误码，不能直接打印底层 ImportError 或
    初始化异常，因为异常文本可能包含本机路径、代理地址或敏感参数。
    """

    def __init__(self, code: str) -> None:
        """使用固定本地错误码初始化异常，不保存底层错误文本。

        ``code`` 由本模块内部常量提供，用于区分依赖缺失与客户端初始化失败；
        原始异常只通过异常链保留给调试器，CLI 不会打印异常链。
        """

        super().__init__(code)
        self.code = code


def load_openai_module() -> ModuleType:
    """在真正执行示例时惰性导入官方 ``openai`` 顶层模块。

    包导入阶段不依赖 ``openai``，因此配置与安全格式化测试可在全局运行时未安装
    官方客户端时执行；真实安装后仍调用官方顶层公开类 ``openai.OpenAI``。
    """

    try:
        return importlib.import_module("openai")
    # 依赖缺失属于本地启动错误，不能把本机模块搜索路径带入公开输出。
    except (ImportError, ModuleNotFoundError) as exc:
        # 依赖错误不透传原始异常，避免泄露本机安装路径和 Python 环境细节。
        raise OpenAIClientUnavailable("client.dependency_missing") from exc


def build_openai_client(
    config: ExampleConfig,
    openai_module: ModuleType | Any | None = None,
) -> Any:
    """使用官方公开构造参数创建指向 MetaHub Gateway 的客户端。

    ``openai_module`` 仅用于测试注入假官方模块；生产调用不传该参数。构造器明确
    传入 ``base_url``、Virtual Key、总超时与最大重试次数，不修改官方客户端的
    私有字段，也不在失败路径序列化包含密钥的 ``config``。
    """

    module = load_openai_module() if openai_module is None else openai_module
    try:
        # Virtual Key 仅进入官方客户端 api_key 参数，绝不能写入终端或日志。
        return module.OpenAI(
            base_url=config.base_url,
            api_key=config.virtual_key,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )
    except Exception as exc:
        # 客户端初始化异常可能携带代理或认证配置，只返回固定安全异常。
        raise OpenAIClientUnavailable("client.initialization_failed") from exc
