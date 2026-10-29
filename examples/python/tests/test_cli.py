"""验证 Chat、Embeddings 入口的主流程、异常分类与安全输出。"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import (
    FakeAPIConnectionError,
    FakeAPIStatusError,
    FakeAPITimeoutError,
    FakeClient,
    FakeCompletions,
    FakeEmbeddings,
    FakeOpenAIModule,
)
from metahub_gateway_examples import chat, embeddings


def valid_environment() -> dict[str, str]:
    """构造两个 CLI 入口共用的合法环境变量集合。

    测试密钥故意具有可搜索特征，便于确认成功和失败输出均未泄露该值。
    """

    return {
        "METAHUB_BASE_URL": "https://gateway.example.com/v1",
        "METAHUB_VIRTUAL_KEY": "vk_cli_must_not_leak",
        "METAHUB_CHAT_MODEL": "chat-alias",
        "METAHUB_EMBEDDING_MODEL": "embedding-alias",
        "METAHUB_TIMEOUT_SECONDS": "15",
        "METAHUB_MAX_RETRIES": "3",
    }


def output_streams() -> tuple[StringIO, StringIO]:
    """创建隔离的标准输出和错误输出缓冲区供安全断言使用。"""

    return StringIO(), StringIO()


def chat_response(*, text: str = "这是安全响应", request_id: str = "req_chat_123") -> Any:
    """构造符合官方 ChatCompletion 访问形状的最小成功响应。"""

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        _request_id=request_id,
    )


def embedding_response(*, request_id: str = "req_embedding_123") -> Any:
    """构造包含三维向量和官方成功请求标识的最小响应。"""

    return SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])],
        _request_id=request_id,
    )


def fake_module(
    *,
    chat_result: Any = None,
    chat_error: BaseException | None = None,
    embedding_result: Any = None,
    embedding_error: BaseException | None = None,
) -> tuple[FakeOpenAIModule, FakeCompletions, FakeEmbeddings]:
    """组合可分别控制 Chat 与 Embeddings 行为的假官方模块。

    返回资源对象让测试既能执行完整 ``main`` 入口，也能精确断言传给公开客户端
    方法的参数。
    """

    completions = FakeCompletions(response=chat_result, error=chat_error)
    embedding_resource = FakeEmbeddings(response=embedding_result, error=embedding_error)
    module = FakeOpenAIModule(FakeClient(completions, embedding_resource))
    return module, completions, embedding_resource


def test_chat_main_runs_non_streaming_request_and_prints_success_request_id() -> None:
    """Chat 完整入口应非流式调用官方接口并输出文本和成功 request_id。"""

    module, completions, _ = fake_module(chat_result=chat_response())
    stdout, stderr = output_streams()

    exit_code = chat.main(
        ["--prompt", "介绍 MetaHub"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert completions.calls == [
        {
            "model": "chat-alias",
            "messages": [{"role": "user", "content": "介绍 MetaHub"}],
            "stream": False,
        }
    ]
    assert "Chat 响应: 这是安全响应" in stdout.getvalue()
    assert "request_id: req_chat_123" in stdout.getvalue()
    assert "vk_cli_must_not_leak" not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_embeddings_main_passes_input_and_prints_dimension_and_request_id() -> None:
    """Embeddings 完整入口应透传输入且只展示向量维度与成功 request_id。"""

    module, _, embedding_resource = fake_module(embedding_result=embedding_response())
    stdout, stderr = output_streams()

    exit_code = embeddings.main(
        ["--input", "企业级 AI Gateway"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert embedding_resource.calls == [
        {"model": "embedding-alias", "input": "企业级 AI Gateway"}
    ]
    assert "Embedding 维度: 3" in stdout.getvalue()
    assert "request_id: req_embedding_123" in stdout.getvalue()
    assert "0.1" not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_gateway_status_error_keeps_code_and_request_id_without_sensitive_body() -> None:
    """Gateway 状态错误应保留公开错误码和 request_id，但隐藏异常及响应正文。"""

    secret = "vk_status_secret"
    prompt = "private prompt must not leak"
    error = FakeAPIStatusError(
        f"Authorization: Bearer {secret}; prompt={prompt}",
        status_code=429,
        request_id="req_rate_limit_456",
        body={
            "error": {
                "code": "quota.rate_limited",
                "message": f"response contains {secret} and {prompt}",
            }
        },
    )
    module, completions, _ = fake_module(chat_error=error)
    stdout, stderr = output_streams()

    exit_code = chat.main(
        ["--prompt", prompt],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert len(completions.calls) == 1
    assert stdout.getvalue() == ""
    assert "category=gateway_api" in stderr.getvalue()
    assert "code=quota.rate_limited" in stderr.getvalue()
    assert "status=429" in stderr.getvalue()
    assert "request_id=req_rate_limit_456" in stderr.getvalue()
    assert "retryable=true" in stderr.getvalue()
    assert secret not in stderr.getvalue()
    assert prompt not in stderr.getvalue()
    assert "Authorization" not in stderr.getvalue()


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
def test_retryable_gateway_statuses_match_official_client_policy(status_code: int) -> None:
    """官方默认重试的状态码应在安全输出中统一标记为可重试。"""

    error = FakeAPIStatusError(
        "response details must remain hidden",
        status_code=status_code,
        request_id="req_retryable_status",
        body={"error": {"code": "gateway.temporary_failure"}},
    )
    module, _, _ = fake_module(chat_error=error)
    stdout, stderr = output_streams()

    exit_code = chat.main(
        ["--prompt", "hello"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert f"status={status_code}" in stderr.getvalue()
    assert "retryable=true" in stderr.getvalue()


@pytest.mark.parametrize(
    ("error", "category", "code"),
    [
        (FakeAPITimeoutError("vk_timeout_secret private input"), "timeout", "client.timeout"),
        (
            FakeAPIConnectionError("Authorization: Bearer vk_connection_secret"),
            "connection",
            "client.connection_failed",
        ),
    ],
)
def test_transport_errors_are_retryable_without_request_id_or_raw_exception(
    error: BaseException,
    category: str,
    code: str,
) -> None:
    """超时和连接失败应可重试、request_id 为横线且不泄露原始异常。"""

    module, _, embedding_resource = fake_module(embedding_error=error)
    stdout, stderr = output_streams()

    exit_code = embeddings.main(
        ["--input", "private embedding input"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert len(embedding_resource.calls) == 1
    assert stdout.getvalue() == ""
    assert f"category={category}" in stderr.getvalue()
    assert f"code={code}" in stderr.getvalue()
    assert "request_id=-" in stderr.getvalue()
    assert "retryable=true" in stderr.getvalue()
    assert "secret" not in stderr.getvalue()
    assert "private" not in stderr.getvalue()
    assert "Authorization" not in stderr.getvalue()


def test_missing_virtual_key_stops_before_client_and_names_only_fixed_variable() -> None:
    """Virtual Key 缺失时入口应在建连前失败，只输出固定变量错误码。"""

    module, _, _ = fake_module(chat_result=chat_response())
    stdout, stderr = output_streams()

    exit_code = chat.main(
        ["--prompt", "prompt-secret"],
        environ={"OTHER_SECRET": "environment-secret"},
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert module.constructor_calls == []
    assert stdout.getvalue() == ""
    assert "code=config.METAHUB_VIRTUAL_KEY_missing" in stderr.getvalue()
    assert "request_id=-" in stderr.getvalue()
    assert "prompt-secret" not in stderr.getvalue()
    assert "environment-secret" not in stderr.getvalue()


def test_client_initialization_error_uses_fixed_code_without_secret_details() -> None:
    """官方客户端构造失败时 CLI 应隐藏原始异常中的密钥和代理地址。"""

    module = FakeOpenAIModule(
        None,
        initialization_error=RuntimeError(
            "Authorization Bearer vk_cli_must_not_leak through proxy-password"
        ),
    )
    stdout, stderr = output_streams()

    exit_code = embeddings.main(
        ["--input", "private input"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert "code=client.initialization_failed" in stderr.getvalue()
    assert "vk_cli_must_not_leak" not in stderr.getvalue()
    assert "proxy-password" not in stderr.getvalue()
    assert "private input" not in stderr.getvalue()


def test_invalid_remote_request_id_is_not_written_to_multiple_log_lines() -> None:
    """恶意 request_id 含换行时应降级为横线，避免终端日志注入。"""

    error = FakeAPIStatusError(
        "safe message",
        status_code=401,
        request_id="req_safe\nAuthorization: leaked",
        body={"error": {"code": "auth.token_invalid"}},
    )
    module, _, _ = fake_module(chat_error=error)
    stdout, stderr = output_streams()

    exit_code = chat.main(
        ["--prompt", "hello"],
        environ=valid_environment(),
        openai_module=module,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "request_id=-" in stderr.getvalue()
    assert stderr.getvalue().count("\n") == 1
    assert "Authorization" not in stderr.getvalue()


def test_malformed_success_responses_return_fixed_local_errors() -> None:
    """成功 HTTP 响应结构缺失时两个入口都应返回固定错误且不打印对象。"""

    module, _, _ = fake_module(
        chat_result=SimpleNamespace(choices=[], _request_id="req_bad_chat"),
        embedding_result=SimpleNamespace(data=[], _request_id="req_bad_embedding"),
    )
    chat_stdout, chat_stderr = output_streams()
    embedding_stdout, embedding_stderr = output_streams()

    chat_exit = chat.main(
        ["--prompt", "hello"],
        environ=valid_environment(),
        openai_module=module,
        stdout=chat_stdout,
        stderr=chat_stderr,
    )
    embedding_exit = embeddings.main(
        ["--input", "hello"],
        environ=valid_environment(),
        openai_module=module,
        stdout=embedding_stdout,
        stderr=embedding_stderr,
    )

    assert chat_exit == 1
    assert embedding_exit == 1
    assert "code=client.invalid_chat_response" in chat_stderr.getvalue()
    assert "code=client.invalid_embedding_response" in embedding_stderr.getvalue()
    assert chat_stdout.getvalue() == ""
    assert embedding_stdout.getvalue() == ""
