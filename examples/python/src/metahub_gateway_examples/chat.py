"""通过官方 OpenAI 客户端执行非流式 Chat Completions 示例。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from .client import OpenAIClientUnavailable, build_openai_client, load_openai_module
from .config import ExampleConfig, ExampleConfigError, load_config
from .errors import (
    failure_from_openai_error,
    local_failure,
    safe_request_id,
    write_failure,
)


def create_parser() -> argparse.ArgumentParser:
    """创建只接收 Chat 文本参数的命令行解析器。

    Prompt 仅用于本次请求，不写入环境变量、日志或失败输出；使用必填参数避免在
    示例源码中硬编码可能被误当成生产输入的数据。
    """

    parser = argparse.ArgumentParser(description="调用 MetaHub Gateway Chat Completions")
    parser.add_argument("--prompt", required=True, help="发送给 Chat 模型的文本")
    return parser


def _chat_text(response: Any) -> str | None:
    """从官方 ChatCompletion 响应对象读取第一条文本结果。

    仅访问公开的 ``choices``、``message`` 与 ``content`` 字段；响应结构不完整或
    内容不是字符串时返回 ``None``，由调用方输出固定错误，不打印响应对象。
    """

    choices = getattr(response, "choices", None)
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def run_chat(
    client: Any,
    config: ExampleConfig,
    prompt: str,
    openai_module: Any,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """执行一次非流式 Chat 请求并输出结果及成功请求标识。

    调用参数遵循官方 ``client.chat.completions.create`` 公开接口。异常路径只交给
    安全映射器，绝不输出可能包含 Virtual Key、Authorization、Prompt 或响应体
    的原始异常；返回值 ``0`` 表示成功，``1`` 表示调用或响应校验失败。
    """

    try:
        response = client.chat.completions.create(
            model=config.chat_model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
    except Exception as exc:
        # 原始异常可能含密钥、Prompt 或上游响应，只输出白名单安全字段。
        write_failure(stderr, failure_from_openai_error(exc, openai_module))
        return 1

    text = _chat_text(response)
    if text is None:
        write_failure(stderr, local_failure("client.invalid_chat_response"))
        return 1

    # 官方响应公开提供 _request_id；缺失或含异常字符时安全降级为 “-”。
    request_id = safe_request_id(getattr(response, "_request_id", None))
    print(f"Chat 响应: {text}", file=stdout)
    print(f"request_id: {request_id}", file=stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    openai_module: Any | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """装配配置、官方客户端和 Chat 调用，返回适合进程退出的状态码。

    可注入环境、模块和输出流以完成无网络单元测试；真实命令行不注入时惰性加载
    官方 ``openai`` 包。所有启动错误均转换为固定本地错误码，不打印异常堆栈。
    """

    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    args = create_parser().parse_args(argv)
    try:
        config = load_config(environ)
    # 配置分支可能处理 Virtual Key，只允许固定错误码离开该边界。
    except ExampleConfigError as exc:
        # 配置异常可能发生在密钥分支，统一提示变量清单而不回显任何环境值。
        write_failure(error_output, local_failure(exc.code))
        return 2

    try:
        module = load_openai_module() if openai_module is None else openai_module
        client = build_openai_client(config, module)
    except OpenAIClientUnavailable as exc:
        # 仅使用异常携带的固定代码，不输出底层导入或初始化异常。
        write_failure(error_output, local_failure(exc.code))
        return 2
    return run_chat(
        client,
        config,
        args.prompt,
        module,
        stdout=output,
        stderr=error_output,
    )


def cli() -> None:
    """执行控制台脚本入口并把 ``main`` 返回值转换为进程退出码。

    该函数供 ``pyproject.toml`` 的 console script 调用，不捕获或打印额外异常，
    保证错误输出仍由 ``main`` 的安全路径统一生成。
    """

    raise SystemExit(main())


if __name__ == "__main__":
    cli()
