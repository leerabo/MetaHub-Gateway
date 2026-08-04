"""PRD-067 Prompt 模板 HTTP 正文长度和传输异常的定向安全测试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from metahub_control.prompt_templates.routes import _read_bounded_body
from metahub_shared.runtime.errors import ApiError
from starlette.requests import Request


def _request(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    receive: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> Request:
    """构造只供有界读取器消费的最小 ASGI Request。

    参数：原始 Header、有序 receive 消息或自定义 receive。返回：未读取正文的 Starlette Request。
    关键约束：Header 保留重复项，测试不会经过会折叠 Content-Length 的 HTTP 客户端。
    """

    queue = list(messages or [])

    async def queued_receive() -> dict[str, Any]:
        """依次返回测试消息；耗尽后返回合法空终块，避免测试本身制造无限流。"""

        if queue:
            return queue.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/prompt-templates",
        "raw_path": b"/prompt-templates",
        "query_string": b"",
        "headers": list(headers or []),
        "client": ("127.0.0.1", 10000),
        "server": ("control", 8001),
    }
    return Request(scope, receive=receive or queued_receive)


@pytest.mark.anyio
async def test_bounded_body_accepts_exact_length_and_missing_declaration() -> None:
    """主流程：唯一规范长度和无声明的有界流均按原始 bytes 返回。"""

    exact = _request(
        headers=[(b"content-length", b"2")],
        messages=[{"type": "http.request", "body": b"{}", "more_body": False}],
    )
    undeclared = _request(
        messages=[
            {"type": "http.request", "body": b"{", "more_body": True},
            {"type": "http.request", "body": b"}", "more_body": False},
        ]
    )

    assert await _read_bounded_body(exact, maximum=2) == b"{}"
    assert await _read_bounded_body(undeclared, maximum=2) == b"{}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"2"), (b"Content-Length", b"2")],
        [(b"content-length", b"+2")],
        [(b"content-length", b"02")],
        [(b"content-length", b" 2")],
        [(b"content-length", b"\xff")],
    ],
)
async def test_bounded_body_rejects_duplicate_or_noncanonical_length(
    headers: list[tuple[bytes, bytes]],
) -> None:
    """异常：重复、带符号、前导零、空白和非 ASCII 长度统一为固定请求错误。"""

    request = _request(
        headers=headers,
        messages=[{"type": "http.request", "body": b"{}", "more_body": False}],
    )

    with pytest.raises(ApiError) as caught:
        await _read_bounded_body(request, maximum=2)
    assert caught.value.code == "prompt_template.request_invalid"


@pytest.mark.anyio
async def test_bounded_body_rejects_declared_mismatch_and_actual_overflow() -> None:
    """异常：声明与实际长度不一致，或无声明实际流超限，均不能返回部分正文。"""

    mismatch = _request(
        headers=[(b"content-length", b"3")],
        messages=[{"type": "http.request", "body": b"{}", "more_body": False}],
    )
    overflow = _request(
        messages=[{"type": "http.request", "body": b"abc", "more_body": False}]
    )

    for request in (mismatch, overflow):
        with pytest.raises(ApiError) as caught:
            await _read_bounded_body(request, maximum=2)
        assert caught.value.code == "prompt_template.request_invalid"


@pytest.mark.anyio
async def test_bounded_body_maps_stream_failure_and_invalid_chunk_without_detail() -> None:
    """安全：底层断流详情和非 bytes 流块只能映射固定错误，不能穿透传输异常文本。"""

    async def failing_receive() -> dict[str, Any]:
        """模拟可能含敏感传输详情的 ASGI receive 故障。"""

        # 自由异常文本是泄漏金丝雀，只能由生产读取器折叠，测试本身不得打印它。
        raise RuntimeError("transport-sensitive-detail-must-not-escape")

    failing = _request(receive=failing_receive)
    invalid_chunk = _request(
        messages=[{"type": "http.request", "body": "not-bytes", "more_body": False}]
    )

    for request in (failing, invalid_chunk):
        with pytest.raises(ApiError) as caught:
            await _read_bounded_body(request, maximum=16)
        assert caught.value.code == "prompt_template.request_invalid"
        assert "transport-sensitive-detail" not in str(caught.value)
