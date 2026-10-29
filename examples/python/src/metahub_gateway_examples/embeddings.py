"""通过官方 OpenAI 客户端执行 Embeddings，并限制向量与敏感错误的输出边界。"""

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
    """创建只接收待向量化文本的命令行解析器。

    输入文本只随本次请求传给 Gateway，不出现在失败输出；参数设为必填，避免源码
    内置业务数据或让空输入造成难以理解的上游错误。
    """

    parser = argparse.ArgumentParser(description="调用 MetaHub Gateway Embeddings")
    parser.add_argument("--input", required=True, help="需要生成向量的文本")
    return parser


def _embedding_dimension(response: Any) -> int | None:
    """从官方 CreateEmbeddingResponse 读取第一条向量的维度。

    示例只展示维度而不打印完整向量，避免大段数据污染终端；响应结构异常时返回
    ``None``，由调用方生成不包含原始响应的固定错误。
    """

    data = getattr(response, "data", None)
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)) or not data:
        return None
    embedding = getattr(data[0], "embedding", None)
    if not isinstance(embedding, Sequence) or isinstance(embedding, (str, bytes)):
        return None
    return len(embedding)


def run_embeddings(
    client: Any,
    config: ExampleConfig,
    input_text: str,
    openai_module: Any,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """执行一次 Embeddings 请求并输出向量维度及成功请求标识。

    调用使用官方 ``client.embeddings.create`` 公开接口并原样传递输入文本。失败时
    不打印输入、完整向量、原始异常或响应体，只输出安全分类字段和可用的服务端
    ``request_id``；返回值 ``0`` 表示成功，``1`` 表示失败。
    """

    try:
        response = client.embeddings.create(
            model=config.embedding_model,
            input=input_text,
        )
    except Exception as exc:
        # 原始异常可能含 Virtual Key、输入文本或响应体，必须走安全映射器。
        write_failure(stderr, failure_from_openai_error(exc, openai_module))
        return 1

    dimension = _embedding_dimension(response)
    if dimension is None:
        write_failure(stderr, local_failure("client.invalid_embedding_response"))
        return 1

    # 成功链路只打印经单行白名单校验的服务端 request_id。
    request_id = safe_request_id(getattr(response, "_request_id", None))
    print(f"Embedding 维度: {dimension}", file=stdout)
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
    """装配配置、官方客户端和 Embeddings 调用并返回进程状态码。

    参数注入让测试可以在未安装 ``openai`` 的全局环境中使用假官方模块验证完整
    入口；真实运行时仍惰性加载官方包。配置和初始化失败都不会输出异常堆栈。
    """

    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    args = create_parser().parse_args(argv)
    try:
        config = load_config(environ)
    # 配置分支可能处理 Virtual Key，只允许固定错误码离开该边界。
    except ExampleConfigError as exc:
        # 配置失败可能涉及 Virtual Key，终端只显示固定错误码。
        write_failure(error_output, local_failure(exc.code))
        return 2

    try:
        module = load_openai_module() if openai_module is None else openai_module
        client = build_openai_client(config, module)
    except OpenAIClientUnavailable as exc:
        # 不打印底层 ImportError 或初始化异常，只保留固定本地代码。
        write_failure(error_output, local_failure(exc.code))
        return 2
    return run_embeddings(
        client,
        config,
        args.input,
        module,
        stdout=output,
        stderr=error_output,
    )


def cli() -> None:
    """执行安装后的 Embeddings 控制台入口并设置进程退出码。

    入口不添加额外异常输出，确保所有可预期错误都保持 ``main`` 定义的安全格式。
    """

    raise SystemExit(main())


if __name__ == "__main__":
    cli()
