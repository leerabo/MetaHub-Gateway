"""PRD-070 生产 Redis Store 的真实回环 TCP/RESP 合同测试。

测试只启动进程内回环 socket，不访问外部网络或真实 Redis。它验证生产 ``_command`` 确实
发送 AUTH/SELECT/PING RESP2 帧，并证明异常正文、连接密码和错误端点细节不会进入公开异常。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from metahub_gateway.session_risk import (
    RedisSessionRiskStore,
    SessionRiskAssessment,
    SessionRiskScope,
    SessionRiskStoreUnavailable,
)


async def _read_resp_command(reader: asyncio.StreamReader) -> tuple[str, ...]:
    """从真实 TCP 流严格读取一条测试所需的 RESP2 字符串数组命令。"""

    array_header = await reader.readline()
    # 生产客户端必须以 RESP 数组发送命令；其他形状可能掩盖拼帧或命令注入问题。
    if not array_header.startswith(b"*") or not array_header.endswith(b"\r\n"):
        raise AssertionError("expected RESP array")
    item_count = int(array_header[1:-2].decode("ascii"))
    # 有界元素数防止损坏长度让测试探针无界读取内存或错误接受截断命令。
    if not 1 <= item_count <= 128:
        raise AssertionError("unexpected RESP array size")
    parts: list[str] = []
    for _index in range(item_count):
        bulk_header = await reader.readline()
        # EVAL 脚本和全部参数都必须使用 bulk string，禁止 simple string 改变二进制边界。
        if not bulk_header.startswith(b"$") or not bulk_header.endswith(b"\r\n"):
            raise AssertionError("expected RESP bulk string")
        byte_count = int(bulk_header[1:-2].decode("ascii"))
        # 单字段上限只保护验收服务，生产 Store 仍由自身模型进一步限制每个业务参数。
        if not 0 <= byte_count <= 1_048_576:
            raise AssertionError("unexpected RESP bulk size")
        raw = await reader.readexactly(byte_count)
        # 长度后缺少 CRLF 表示帧不完整，不能继续把下一字段字节误当成本字段内容。
        if await reader.readexactly(2) != b"\r\n":
            raise AssertionError("invalid RESP bulk terminator")
        parts.append(raw.decode("utf-8"))
    return tuple(parts)


async def _start_resp_probe(
    responses: Sequence[bytes],
    *,
    response_delay_seconds: float = 0.0,
) -> tuple[asyncio.AbstractServer, int, list[tuple[str, ...]], asyncio.Event]:
    """启动一次性回环 RESP 探针并返回服务、端口、已收命令及连接完成事件。

    参数：按命令顺序返回的完整 RESP 字节。返回值：只绑定 ``127.0.0.1`` 的临时服务。
    探针不解析 Lua、不保存业务正文，只验证生产客户端真实网络帧和错误收口。
    """

    commands: list[tuple[str, ...]] = []
    completed = asyncio.Event()

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """按固定响应序列处理一个短连接，确保测试结束后关闭 socket。"""

        try:
            for response in responses:
                commands.append(await _read_resp_command(reader))
                # 可控延迟只用于验证生产读取 deadline，不模拟外部网络或依赖真实 Redis。
                if response_delay_seconds:
                    await asyncio.sleep(response_delay_seconds)
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            # 客户端超时后会先关闭连接；探针清理异常不能遮蔽待断言的生产超时结果。
            except (ConnectionError, OSError):
                pass
            finally:
                completed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    socket = server.sockets[0] if server.sockets else None
    if socket is None:
        server.close()
        await server.wait_closed()
        # 未取得真实回环端口就不能宣称覆盖 TCP 路径，必须让测试立即失败。
        raise AssertionError("loopback RESP server did not expose a socket")
    return server, int(socket.getsockname()[1]), commands, completed


async def _start_multi_connection_probe(
    response_chunks: Sequence[Sequence[bytes]],
) -> tuple[asyncio.AbstractServer, int, list[tuple[str, ...]], asyncio.Event]:
    """为每个短连接读取一条命令，并按小块写回 RESP 半包。

    参数：每个连接对应的响应字节块。返回值：回环服务、端口、完整命令和全部连接完成事件。
    该探针用于复核 EVAL 后 HGETALL 的真实两连接流程，不保存原始会话或正文。
    """

    commands: list[tuple[str, ...]] = []
    completed = asyncio.Event()
    connection_index = 0
    index_lock = asyncio.Lock()

    async def handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """为当前连接领取唯一脚本，读取命令后逐块返回并可靠清理 socket。"""

        nonlocal connection_index
        async with index_lock:
            current_index = connection_index
            connection_index += 1
        try:
            # 多余连接表示生产调用次数漂移，不能静默复用最后一份响应掩盖行为变化。
            if current_index >= len(response_chunks):
                raise AssertionError("unexpected RESP connection")
            commands.append(await _read_resp_command(reader))
            for chunk in response_chunks[current_index]:
                writer.write(chunk)
                await writer.drain()
                # 主动让出事件循环，使数组头、Bulk 头和正文可作为真实 TCP 半包到达客户端。
                await asyncio.sleep(0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            # 被测客户端先关闭时只影响探针资源回收，不改变已捕获命令证据。
            except (ConnectionError, OSError):
                pass
            finally:
                if connection_index >= len(response_chunks):
                    completed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    socket = server.sockets[0] if server.sockets else None
    if socket is None:
        server.close()
        await server.wait_closed()
        # 未取得回环端口就没有覆盖真实 TCP，必须明确失败而非降级为内存 mock。
        raise AssertionError("loopback RESP server did not expose a socket")
    return server, int(socket.getsockname()[1]), commands, completed


async def _close_resp_probe(server: asyncio.AbstractServer, completed: asyncio.Event) -> None:
    """等待当前短连接完成并关闭监听器，避免测试遗留后台任务或端口。"""

    await asyncio.wait_for(completed.wait(), timeout=1.0)
    server.close()
    await server.wait_closed()


@pytest.mark.anyio
async def test_real_tcp_resp_auth_select_and_ping_are_exact() -> None:
    """生产客户端必须在同一真实 TCP 连接依次完成 AUTH、SELECT 和 PING。"""

    server, port, commands, completed = await _start_resp_probe(
        (b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n")
    )
    try:
        store = RedisSessionRiskStore(
            f"redis://resp-user:synthetic-secret@127.0.0.1:{port}/7",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=1.0,
        )
        assert await store.ping() is True
    finally:
        await _close_resp_probe(server, completed)

    assert commands == [
        ("AUTH", "resp-user", "synthetic-secret"),
        ("SELECT", "7"),
        ("PING",),
    ]


@pytest.mark.anyio
async def test_real_tcp_resp_rejects_non_ok_auth_before_select_or_ping() -> None:
    """AUTH 的无关 simple-string 不能被当作成功，也不能继续发送后续命令。"""

    server, port, commands, completed = await _start_resp_probe((b"+PONG\r\n",))
    try:
        store = RedisSessionRiskStore(
            f"redis://resp-user:synthetic-secret@127.0.0.1:{port}/7",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=1.0,
        )
        with pytest.raises(SessionRiskStoreUnavailable) as caught:
            await store.ping()
    finally:
        await _close_resp_probe(server, completed)

    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
    assert commands == [("AUTH", "resp-user", "synthetic-secret")]


@pytest.mark.anyio
async def test_real_tcp_resp_error_body_is_not_exposed() -> None:
    """Redis error 正文可能含部署细节，公开异常必须只保留固定文本且没有 cause。"""

    sensitive_marker = "synthetic-secret-host-detail"
    server, port, commands, completed = await _start_resp_probe(
        (f"-ERR {sensitive_marker}\r\n".encode("ascii"),)
    )
    try:
        store = RedisSessionRiskStore(
            f"redis://127.0.0.1:{port}/0",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=1.0,
        )
        with pytest.raises(SessionRiskStoreUnavailable) as caught:
            await store.ping()
    finally:
        await _close_resp_probe(server, completed)

    assert commands == [("PING",)]
    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
    assert sensitive_marker not in repr(caught.value)


def test_redis_acl_username_without_password_is_rejected() -> None:
    """ACL 用户名缺少非空密码时启动即失败，不能静默退化为匿名连接。"""

    with pytest.raises(SessionRiskStoreUnavailable):
        RedisSessionRiskStore("redis://resp-user@127.0.0.1:6379/0")


@pytest.mark.anyio
async def test_real_tcp_resp_eval_uses_exact_23_arguments_and_accepts_chunked_arrays() -> None:
    """完整 EVAL 必须发送 23 个 ARGV，并能解析真实数组半包及后续空 HGETALL。"""

    server, port, commands, completed = await _start_multi_connection_probe(
        (
            (b"*1\r\n", b"$5\r\nCL", b"EAN\r\n"),
            (b"*0\r\n",),
        )
    )
    try:
        store = RedisSessionRiskStore(
            f"redis://127.0.0.1:{port}/0",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=1.0,
        )
        scope = SessionRiskScope(
            tenant_id="tenant-resp",
            virtual_key_id="vk_resp",
            scope_type="api_session",
            scope_key="srh_" + "a" * 64,
            owner_slot="srh_" + "b" * 64,
            hmac_key_version=3,
        )
        assessment = SessionRiskAssessment(
            turn_key_digest="srh_" + "c" * 64,
            turn_intent_digest="srh_" + "d" * 64,
            jailbreak_count_delta=0,
            sensitive_count_delta=0,
            tool_inducement_count_delta=0,
            turn_delta=0,
            input_truncated=False,
            detector_version="intent-v1",
            ruleset_version="session-risk-ruleset-v1",
            threshold_version="session-risk-threshold-v1",
            source_request_id="req-resp-070",
            source_trace_id="trace-resp-070",
        )
        result = await store.accumulate(scope, assessment)
    finally:
        await _close_resp_probe(server, completed)

    assert result.snapshot is None
    assert result.pending is None
    assert result.duplicate is False
    assert len(commands) == 2
    eval_command, load_command = commands
    assert eval_command[:3] == ("EVAL", eval_command[1], "2")
    assert len(eval_command) == 28
    assert eval_command[3] == f"metahub:sr:v1:{{{scope.owner_slot}}}:score"
    assert eval_command[4] == (
        f"metahub:sr:v1:{{{scope.owner_slot}}}:turn:{assessment.turn_key_digest}"
    )
    arguments = eval_command[5:]
    assert len(arguments) == 23
    assert arguments[:8] == (
        assessment.turn_key_digest,
        assessment.turn_intent_digest,
        "0",
        "0",
        "0",
        "0",
        "session-risk-threshold-v1",
        "3",
    )
    assert arguments[8].isascii() and arguments[8].isdecimal()
    assert arguments[9:16] == (
        "req-resp-070",
        "trace-resp-070",
        "0",
        "intent-v1",
        "session-risk-ruleset-v1",
        "",
        "",
    )
    assert arguments[16].startswith("srs_") and len(arguments[16]) == 36
    assert arguments[17:] == ("1800", "none", "0", "allow", "0", "0")
    assert load_command == ("HGETALL", f"metahub:sr:v1:{{{scope.owner_slot}}}:score")


@pytest.mark.anyio
async def test_real_tcp_resp_disconnect_fails_closed_without_endpoint_details() -> None:
    """服务端读完 PING 后直接断连时，生产 Store 必须固定失败且不泄露端点。"""

    server, port, commands, completed = await _start_resp_probe((b"",))
    try:
        store = RedisSessionRiskStore(
            f"redis://127.0.0.1:{port}/0",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=1.0,
        )
        with pytest.raises(SessionRiskStoreUnavailable) as caught:
            await store.ping()
    finally:
        await _close_resp_probe(server, completed)

    assert commands == [("PING",)]
    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
    assert str(port) not in repr(caught.value)


@pytest.mark.anyio
async def test_real_tcp_resp_read_timeout_is_bounded_and_fails_closed() -> None:
    """回环端点不及时响应时，读取 deadline 必须生效并返回无细节固定异常。"""

    server, port, commands, completed = await _start_resp_probe(
        (b"+PONG\r\n",),
        response_delay_seconds=0.2,
    )
    try:
        store = RedisSessionRiskStore(
            f"redis://127.0.0.1:{port}/0",
            connect_timeout_seconds=1.0,
            io_timeout_seconds=0.05,
        )
        with pytest.raises(SessionRiskStoreUnavailable) as caught:
            await store.ping()
    finally:
        await _close_resp_probe(server, completed)

    assert commands == [("PING",)]
    assert str(caught.value) == "session risk store unavailable"
    assert caught.value.__cause__ is None
