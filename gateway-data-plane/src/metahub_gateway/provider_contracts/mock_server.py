"""提供仅监听 loopback 的自包含 Provider 合同 mock server。

server 运行生产 Adapter 的真实 HTTP 路径，但只记录模型、stream、鉴权存在性等布尔合同，
不会保存 Authorization、请求正文或响应正文。每个上下文只服务一个受控 case，退出时关闭线程和 socket。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Mapping


# 合同请求上限足以容纳固定夹具，同时阻止测试失误把大型客户样本送入内存 server。
_MAX_REQUEST_BYTES = 64 * 1024


@dataclass(frozen=True)
class MockResponsePlan:
    """描述一次 loopback 响应的状态、类型和固定合成正文。"""

    status_code: int
    content_type: str
    body: bytes = field(repr=False)


@dataclass
class MockRequestObservation:
    """保存 mock server 可公开的请求合同，不保留 Header 值或正文。

    该对象由 server 线程写、runner 在请求完成后读；每个 server 只接受一次请求，因此无需把
    敏感 wire 数据复制到跨线程队列。
    """

    request_count: int = 0
    method: str = ""
    path: str = ""
    model: str = ""
    stream: bool = False
    include_usage: bool = False
    has_authorization: bool = False
    authorization_scheme: str | None = None
    parse_valid: bool = False


class _LoopbackServer(ThreadingHTTPServer):
    """绑定单个响应计划和安全观察对象的 loopback HTTP server。"""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, plan: MockResponsePlan, observation: MockRequestObservation) -> None:
        """只绑定 `127.0.0.1` 随机端口，避免 mock 被局域网其他主机访问。"""

        self.plan = plan
        self.observation = observation
        super().__init__(("127.0.0.1", 0), _ContractRequestHandler)


class _ContractRequestHandler(BaseHTTPRequestHandler):
    """消费一个有界 JSON 请求并返回 server 预置的固定合成响应。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 固定接口名
        """处理 Adapter POST，同时把敏感 Header 压缩为布尔/固定 scheme 观察值。"""

        server = self.server
        if not isinstance(server, _LoopbackServer):
            # 类型漂移表示测试框架内部错误，固定 500 且不输出请求数据。
            self.send_error(500)
            return
        observation = server.observation
        observation.request_count += 1
        observation.method = "POST"
        observation.path = self.path.split("?", 1)[0]
        raw_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_length)
        # 非法长度不能继续读取 socket，否则测试 server 可能被迫等待未知正文。
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
            self._write_response(413, "application/json", b"{}")
            return
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8"))
        # 非 UTF-8 或非法 JSON 只标记解析失败，不把原始正文送入日志或观察对象。
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, Mapping):
            observation.model = str(payload.get("model") or "")[:128]
            observation.stream = payload.get("stream") is True
            options = payload.get("stream_options")
            observation.include_usage = (
                isinstance(options, Mapping) and options.get("include_usage") is True
            )
            observation.parse_valid = True
        authorization = self.headers.get("Authorization")
        observation.has_authorization = bool(authorization)
        # 观察对象只记录固定 scheme，绝不能保存其后的合成或真实凭据字节。
        if authorization:
            # 只保留固定 scheme；凭据剩余部分永不离开 handler 当前栈。
            observation.authorization_scheme = authorization.partition(" ")[0][:16]
        self._write_response(
            server.plan.status_code,
            server.plan.content_type,
            server.plan.body,
        )

    def _write_response(self, status_code: int, content_type: str, body: bytes) -> None:
        """发送固定有界响应并强制关闭连接，保证 Windows 上下文退出可及时释放端口。"""

        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        """禁用 BaseHTTPRequestHandler 默认访问日志，避免 URL 或解析错误进入测试输出。"""

        return


@contextmanager
def run_mock_upstream(
    plan: MockResponsePlan,
) -> Iterator[tuple[str, MockRequestObservation]]:
    """启动一次性 loopback server，并在 finally 中关闭监听线程和 socket。

    返回值：安全 base URL 与观察对象。调用方必须在上下文内完成请求；退出后不能复用地址，
    防止并发 case 意外共享响应或认证状态。
    """

    observation = MockRequestObservation()
    server = _LoopbackServer(plan, observation)
    thread = threading.Thread(
        target=server.serve_forever,
        # 默认 0.5 秒轮询会在 45 个网络合同逐次关闭时累计几十秒；缩短轮询只影响测试
        # server 的退出响应，不改变生产 Adapter 的 HTTP 行为或请求超时语义。
        kwargs={"poll_interval": 0.01},
        name="metahub-provider-contract-mock",
        daemon=True,
    )
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}/v1", observation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
        if thread.is_alive():
            # 未停止线程会让后续 CI case 复用不确定资源，必须明确失败而不是静默遗留。
            raise RuntimeError("adapter.contract_mock_cleanup_failed")
