"""PRD-071 本地真实 HTTP、持久化 owner 与脱敏证据验收器。

脚本只连接工作区开发库以及 ``127.0.0.1`` 上已经启动的 Gateway/Control。它在
``127.0.0.1:11434`` 临时提供 OpenAI-compatible 假上游，但不会启动或停止业务服务。
Provider Key、登录 Token、Virtual Key、Prompt、模型响应和 DLP pattern 只驻留内存；
正式证据只包含状态码、统一错误码、不可变 ID、枚举和计数。
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from metahub_shared.db.security import hash_password

# 本组常量冻结工作区路径、回环服务、合成场景与证据禁用字段，禁止运行时扩展到外部环境。
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = ROOT / ".data" / "metahub-dev-prd031.db"
DEFAULT_GATEWAY_LOG = ROOT / ".tmp" / "prd071-live" / "gateway.log"
DEFAULT_CONTROL_LOG = ROOT / ".tmp" / "prd071-live" / "control.log"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd071-runtime"

CONTROL_URL = "http://127.0.0.1:8001"
GATEWAY_URL = "http://127.0.0.1:8000"
ADMIN_TENANT_SLUG = "default"
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "MetaHubLocal!2026"
MODEL_ALIAS = "default-chat"
PROVIDER_CODE = "local_openai"
PROVIDER_HOST = "127.0.0.1"
PROVIDER_PORT = 11434

# 场景标记都是合成测试输入。它们仍按 Prompt 处理：只在调用栈中使用，并纳入日志/库扫描。
SCENARIO_CLEAN = "prd071-case-clean-"
SCENARIO_BLOCKED = "prd071-case-blocked-"
SCENARIO_MALFORMED = "prd071-case-malformed-"
SCENARIO_STREAM_BLOCKED = "prd071-case-stream-blocked-"
CLEAN_RESPONSE_TEXT = "prd071 synthetic clean response"
SAFE_STREAM_TEXT = "prd071 synthetic safe stream prefix"

# 证据结构禁止出现这些正文或凭据字段；带 ``_id`` 的资源身份仍是允许的安全元数据。
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "authorization",
        "password",
        "plain_value",
        "prompt",
        "provider_key",
        "response",
        "token",
        "virtual_key_plaintext",
        "dlp_pattern",
    }
)


class AcceptanceFailure(RuntimeError):
    """只携带固定阶段与分类码，避免异常消息拼入 HTTP 正文或敏感配置。"""

    def __init__(self, stage: str, code: str) -> None:
        """构造可公开的失败坐标，不接受自由异常消息。"""

        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；失败时只抛出固定阶段和分类。"""

    if not condition:
        raise AcceptanceFailure(stage, code)


def _utc_now() -> str:
    """返回 SQLite 和 JSON 都可稳定解析的 UTC 秒级时间。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _workspace_path(value: str | Path, boundary: Path, *, stage: str) -> Path:
    """解析路径并强制位于指定工作区目录，阻止验收器访问沙箱外文件。"""

    path = Path(value).expanduser().resolve()
    root = boundary.resolve()
    _require(path == root or root in path.parents, stage, "path_outside_workspace")
    return path


def _loopback_base_url(value: str, *, expected_port: int, stage: str) -> str:
    """校验无凭据、无查询的本机 HTTP 基址，避免验收器意外访问外网。"""

    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    # 非法端口或 IPv6 括号会由标准库抛 ValueError，统一收敛后不能暴露解析器细节。
    except ValueError as exc:
        raise AcceptanceFailure(stage, "url_invalid") from exc
    _require(parsed.scheme == "http", stage, "scheme_invalid")
    _require(parsed.hostname in {"127.0.0.1", "localhost"}, stage, "host_invalid")
    _require(parsed_port == expected_port, stage, "port_invalid")
    _require(parsed.username is None and parsed.password is None, stage, "credentials_forbidden")
    _require(not parsed.query and not parsed.fragment, stage, "suffix_invalid")
    _require(parsed.path in {"", "/"}, stage, "path_invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class RuntimeConfig:
    """本地验收配置；所有可写路径都被限制在当前工作区。"""

    database: Path
    gateway_log: Path
    control_log: Path
    artifact_root: Path
    gateway_url: str
    control_url: str
    provider_port: int

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        """读取少量显式环境覆盖，并重新执行路径与回环网络门禁。"""

        database = _workspace_path(
            os.getenv("METAHUB_PRD071_DATABASE", str(DEFAULT_DATABASE)),
            ROOT / ".data",
            stage="config.database",
        )
        gateway_log = _workspace_path(
            os.getenv("METAHUB_PRD071_GATEWAY_LOG", str(DEFAULT_GATEWAY_LOG)),
            ROOT / ".tmp",
            stage="config.gateway_log",
        )
        control_log = _workspace_path(
            os.getenv("METAHUB_PRD071_CONTROL_LOG", str(DEFAULT_CONTROL_LOG)),
            ROOT / ".tmp",
            stage="config.control_log",
        )
        artifact_root = _workspace_path(
            os.getenv("METAHUB_PRD071_ARTIFACT_ROOT", str(ARTIFACT_ROOT)),
            ROOT / ".artifacts",
            stage="config.artifact_root",
        )
        raw_port = os.getenv("METAHUB_PRD071_PROVIDER_PORT", str(PROVIDER_PORT))
        _require(raw_port.isascii() and raw_port.isdecimal(), "config.provider", "port_invalid")
        provider_port = int(raw_port)
        _require(provider_port == PROVIDER_PORT, "config.provider", "port_not_11434")
        return cls(
            database=database,
            gateway_log=gateway_log,
            control_log=control_log,
            artifact_root=artifact_root,
            gateway_url=_loopback_base_url(
                os.getenv("METAHUB_PRD071_GATEWAY_URL", GATEWAY_URL),
                expected_port=8000,
                stage="config.gateway",
            ),
            control_url=_loopback_base_url(
                os.getenv("METAHUB_PRD071_CONTROL_URL", CONTROL_URL),
                expected_port=8001,
                stage="config.control",
            ),
            provider_port=provider_port,
        )


@dataclass(frozen=True)
class RunSecrets:
    """本批验收的内存敏感值集合；repr 明确隐藏全部字段。"""

    block_marker: str = field(repr=False)
    provider_key: str = field(repr=False)
    clean_prompt: str = field(repr=False)
    blocked_prompt: str = field(repr=False)
    malformed_prompt: str = field(repr=False)
    stream_prompt: str = field(repr=False)

    @classmethod
    def create(cls, run_suffix: str) -> RunSecrets:
        """生成不会与历史规则或请求碰撞的一次性合成正文。"""

        return cls(
            block_marker=f"prd071-block-{secrets.token_hex(12)}",
            provider_key=f"sk-prd071-runtime-{secrets.token_urlsafe(24)}",
            clean_prompt=SCENARIO_CLEAN + run_suffix,
            blocked_prompt=SCENARIO_BLOCKED + run_suffix,
            malformed_prompt=SCENARIO_MALFORMED + run_suffix,
            stream_prompt=SCENARIO_STREAM_BLOCKED + run_suffix,
        )

    def sensitive_values(self) -> list[str]:
        """返回需要扫描的 Prompt、响应、规则和 Provider 凭据明文。"""

        return [
            self.block_marker,
            self.clean_prompt,
            self.blocked_prompt,
            self.malformed_prompt,
            self.stream_prompt,
            CLEAN_RESPONSE_TEXT,
            SAFE_STREAM_TEXT,
            self.provider_key,
            ADMIN_PASSWORD,
        ]


@dataclass
class ProviderObservation:
    """假 Provider 的安全计数；不保存 Header、请求正文或响应正文。"""

    calls: int = 0
    invalid_requests: int = 0
    invalid_authorization: int = 0
    clean_calls: int = 0
    blocked_calls: int = 0
    malformed_calls: int = 0
    stream_blocked_calls: int = 0
    cross_chunk_splits: int = 0

    def as_evidence(self) -> dict[str, int]:
        """投影只含固定计数的证据对象。"""

        return {
            "calls": self.calls,
            "invalid_requests": self.invalid_requests,
            "invalid_authorization": self.invalid_authorization,
            "clean_calls": self.clean_calls,
            "blocked_calls": self.blocked_calls,
            "malformed_calls": self.malformed_calls,
            "stream_blocked_calls": self.stream_blocked_calls,
            "cross_chunk_splits": self.cross_chunk_splits,
        }


def _chat_response(content: object) -> bytes:
    """构造有显式 Usage 的合成 OpenAI JSON；content 仅在返回缓冲区中存在。"""

    payload = {
        "id": "chatcmpl-prd071-runtime",
        "object": "chat.completion",
        "created": 1,
        "model": "local-chat-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _sse_event(content: str) -> bytes:
    """把合成模型片段编码成一个完整 OpenAI-compatible SSE event。"""

    payload = {
        "id": "chatcmpl-prd071-runtime-stream",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    return (
        b"data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )


def _stream_provider_chunks(block_marker: str) -> tuple[bytes, ...]:
    """构造跨 HTTP chunk 的命中 event，并保证 marker 后还有一个完整检查间隔。

    这里验证的是“当前命中 event 尚未发送”路径，因此 marker 后必须有足够文本触发周期检查；
    若把 marker 放在最后一个检查点之后，只能由 ``[DONE]`` 尾窗发现，那属于已发送内容不撤回的另一合同。
    """

    safe = _sse_event(SAFE_STREAM_TEXT)
    blocked = _sse_event("x" * 260 + block_marker + "z" * 32)
    marker_bytes = block_marker.encode("utf-8")
    marker_start = blocked.index(marker_bytes)
    split_at = marker_start + max(1, len(marker_bytes) // 2)
    # 第二个 SSE event 在 marker 内切开；event 结束符只在后一块中出现，验证 framer 不提前放行。
    return safe, blocked[:split_at], blocked[split_at:], b"data: [DONE]\n\n"


def _request_scenario(payload: object) -> str | None:
    """从当前调用栈中的合成 Prompt 识别场景，不把正文写入 Provider 状态。"""

    if not isinstance(payload, Mapping):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], Mapping):
        return None
    content = messages[-1].get("content")
    if not isinstance(content, str):
        return None
    for prefix, scenario in (
        (SCENARIO_CLEAN, "clean"),
        (SCENARIO_BLOCKED, "blocked"),
        (SCENARIO_MALFORMED, "malformed"),
        (SCENARIO_STREAM_BLOCKED, "stream_blocked"),
    ):
        if content.startswith(prefix):
            return scenario
    return None


class _ProviderServer(ThreadingHTTPServer):
    """绑定合成 marker、线程锁和安全计数的 loopback HTTP server。"""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, host: str, port: int, block_marker: str, provider_key: str) -> None:
        """只监听显式回环地址；block marker 和 Provider Key 不进入 observation 或日志。"""

        self.block_marker = block_marker
        self.provider_key = provider_key
        self.observation = ProviderObservation()
        self.observation_lock = threading.Lock()
        super().__init__((host, port), _ProviderHandler)


class _ProviderHandler(BaseHTTPRequestHandler):
    """处理受控 Chat 请求并禁用标准库默认访问日志。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - 标准库固定回调名。
        """校验路径、Key 和有界 JSON 后返回指定合成场景。"""

        server = self.server
        # Handler 只信任本验收器绑定的 server 类型，避免未知宿主绕过安全观察和响应限制。
        if not isinstance(server, _ProviderServer):
            self._json_response(500, b"{}")
            return
        length_text = self.headers.get("Content-Length", "")
        valid_length = length_text.isascii() and length_text.isdecimal()
        content_length = int(length_text) if valid_length else -1
        if self.path.split("?", 1)[0] != "/v1/chat/completions" or not 1 <= content_length <= 65536:
            self._record(server, "invalid_requests")
            self._json_response(400, b"{}")
            return
        body = self.rfile.read(content_length)
        # Provider Key 必须逐字匹配启动约定；失败只返回固定错误且绝不保存来访凭据。
        if self.headers.get("Authorization") != f"Bearer {server.provider_key}":
            self._record(server, "invalid_authorization")
            self._json_response(401, b'{"error":{"code":"invalid_api_key"}}')
            return
        try:
            payload = json.loads(body.decode("utf-8", errors="strict"))
        # 非 UTF-8 或非法 JSON 只进入固定无效请求分支，原始正文不得写入异常或观察对象。
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        scenario = _request_scenario(payload)
        is_stream = isinstance(payload, Mapping) and payload.get("stream") is True
        if scenario is None or (scenario == "stream_blocked") != is_stream:
            self._record(server, "invalid_requests")
            self._json_response(400, b"{}")
            return
        self._record(server, "calls")
        self._record(server, f"{scenario}_calls")
        if scenario == "clean":
            self._json_response(200, _chat_response(CLEAN_RESPONSE_TEXT))
        elif scenario == "blocked":
            self._json_response(200, _chat_response("blocked " + server.block_marker))
        elif scenario == "malformed":
            # content object 对 Adapter 是合法 JSON，但对输出正文投影是不可证明形状，必须固定 503。
            self._json_response(200, _chat_response({"invalid": True}))
        else:
            self._chunked_stream(server)

    def _record(self, server: _ProviderServer, field_name: str) -> None:
        """在线程锁内只增加预声明计数，不保存请求或 Header 值。"""

        with server.observation_lock:
            setattr(server.observation, field_name, getattr(server.observation, field_name) + 1)

    def _json_response(self, status: int, body: bytes) -> None:
        """写入固定有界 JSON，并关闭连接避免 Windows 退出时残留 socket。"""

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _chunked_stream(self, server: _ProviderServer) -> None:
        """按真实 HTTP/1.1 分块传输发送跨块命中 SSE，并保持正文只驻留 socket 写缓冲区。"""

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = _stream_provider_chunks(server.block_marker)
        try:
            for index, chunk in enumerate(chunks):
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk + b"\r\n")
                self.wfile.flush()
                if index == 2:
                    # 后半 marker 已真实写入 socket 后才登记跨块证据；仅构造 chunks 不算通过。
                    self._record(server, "cross_chunk_splits")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        # Gateway 命中后会主动关闭上游；这是预期安全行为，不能由标准库打印线程 traceback。
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        """关闭 BaseHTTPRequestHandler 日志，避免 URL 或解析异常扩散。"""

        return


class FakeProvider:
    """管理静默回环 Provider 的启动、停止和安全观察快照。"""

    def __init__(
        self,
        block_marker: str,
        provider_key: str,
        *,
        host: str = PROVIDER_HOST,
        port: int = PROVIDER_PORT,
    ) -> None:
        """保存当前运行所需 marker 和 Key；二者只驻留内存且 repr 不公开。"""

        self._block_marker = block_marker
        self._provider_key = provider_key
        self._host = host
        self._port = port
        self._server: _ProviderServer | None = None
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:
        """返回不含 marker 的固定描述，避免诊断时意外展开敏感规则。"""

        return "FakeProvider(loopback)"

    @property
    def base_url(self) -> str:
        """返回服务启动后的回环基址；未启动时失败关闭。"""

        _require(self._server is not None, "provider.base_url", "not_started")
        port = int(self._server.server_address[1])
        return f"http://{self._host}:{port}/v1"

    def start(self) -> None:
        """启动回环监听；端口冲突固定映射为前置条件失败。"""

        _require(self._server is None, "provider.start", "already_started")
        try:
            server = _ProviderServer(
                self._host,
                self._port,
                self._block_marker,
                self._provider_key,
            )
        # 固定端口被占用说明 Gateway 启动前置条件不成立，不能改用未知端口制造伪验收。
        except OSError as exc:
            raise AcceptanceFailure("provider.start", "port_unavailable") from exc
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="prd071-runtime-provider",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """关闭监听并等待线程退出，防止重复验收继承旧 Provider。"""

        server, thread = self._server, self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
            _require(not thread.is_alive(), "provider.stop", "thread_alive")
        self._server = None
        self._thread = None

    def observation(self) -> ProviderObservation:
        """复制当前安全计数，调用方无法修改 server 内部状态。"""

        _require(self._server is not None, "provider.observation", "not_started")
        server = self._server
        with server.observation_lock:
            return ProviderObservation(**server.observation.as_evidence())


def _chain(run_id: str, label: str) -> tuple[str, str]:
    """为每个 HTTP 操作生成互不复用的 request_id/trace_id。"""

    suffix = secrets.token_hex(6)
    return f"req_{run_id}_{label}_{suffix}", f"trc_{run_id}_{label}_{suffix}"


def _headers(token: str | None, request_id: str, trace_id: str) -> dict[str, str]:
    """构造内存认证 Header；Token 不会进入返回证据或日志。"""

    result = {"X-Request-ID": request_id, "X-Trace-ID": trace_id}
    # 认证值仅在当前请求字典中短暂存在，匿名权限用例不得注入空 Bearer Header。
    if token is not None:
        result["Authorization"] = f"Bearer {token}"
    return result


def _json_object(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    """解析 JSON object；失败时不回显原始响应正文。"""

    try:
        payload = response.json()
    # JSON 解码错误不能拼接响应正文，否则 Provider 或认证错误内容可能进入测试输出。
    except (ValueError, UnicodeDecodeError) as exc:
        raise AcceptanceFailure(stage, "json_invalid") from exc
    _require(type(payload) is dict, stage, "payload_not_object")
    return payload


def _assert_chain(response: httpx.Response, request_id: str, trace_id: str, *, stage: str) -> None:
    """核对响应 Header 精确透传链路身份，防止 body 与 owner 使用不同 ID。"""

    _require(response.headers.get("x-request-id") == request_id, stage, "request_id_mismatch")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_id_mismatch")


def _expect_json(
    response: httpx.Response,
    status: int,
    request_id: str,
    trace_id: str,
    *,
    stage: str,
    error_code: str | None = None,
    private_no_store: bool = False,
) -> dict[str, Any]:
    """校验状态、链路、统一错误码和可选 RequestLog 私有缓存策略。"""

    _require(response.status_code == status, stage, "status_mismatch")
    _assert_chain(response, request_id, trace_id, stage=stage)
    payload = _json_object(response, stage=stage)
    # 只有错误合同要求 body 链路身份；普通成功响应仍必须通过响应 Header 精确核对。
    if error_code is not None:
        _require(payload.get("code") == error_code, stage, "error_code_mismatch")
        _require(payload.get("request_id") == request_id, stage, "body_request_id_mismatch")
        _require(payload.get("trace_id") == trace_id, stage, "body_trace_id_mismatch")
    if private_no_store:
        _require(
            response.headers.get("cache-control") == "no-store, private",
            stage,
            "cache_control_invalid",
        )
    return payload


def _error_evidence(payload: Mapping[str, object]) -> dict[str, str]:
    """从统一错误体抽取可写证据的 code/request_id。"""

    return {"code": str(payload["code"]), "request_id": str(payload["request_id"])}


def _login(
    client: httpx.Client,
    run_id: str,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    label: str,
) -> tuple[str, dict[str, Any]]:
    """执行真实登录并返回只驻留内存的 Token 与安全用户摘要。"""

    request_id, trace_id = _chain(run_id, label)
    response = client.post(
        "/auth/login",
        headers=_headers(None, request_id, trace_id),
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage=f"auth.{label}")
    token = payload.get("access_token")
    user = payload.get("user")
    _require(type(token) is str and len(token) >= 32, f"auth.{label}", "token_invalid")
    _require(type(user) is dict, f"auth.{label}", "user_invalid")
    # Token 返回后只交给当前调用栈，证据构造器和任何日志函数都不会接收该返回值。
    return token, user


@dataclass(frozen=True)
class ViewerIdentity:
    """一次性 viewer 身份；密码仅驻留内存并隐藏 repr。"""

    user_id: str
    email: str
    password: str = field(repr=False)


def _prepare_viewer(database: Path, tenant_id: str, run_id: str) -> ViewerIdentity:
    """直接创建最小 viewer 并撤销同 ID 残留 Session，供后端 403 验收。"""

    identity = ViewerIdentity(
        user_id="usr_prd071_runtime_" + secrets.token_hex(8),
        email=f"prd071-runtime-{secrets.token_hex(6)}@example.invalid",
        password="Prd071-Viewer-Aa1!" + secrets.token_urlsafe(18),
    )
    now = _utc_now()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,password_changed_at
            ) VALUES(?,?,?,?,?,'viewer','active',?,?,?)
            """,
            (
                identity.user_id,
                tenant_id,
                identity.email,
                f"PRD071 Runtime Viewer {run_id}",
                hash_password(identity.password),
                now,
                now,
                now,
            ),
        )
        connection.commit()
        return identity
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _disable_viewer(database: Path, identity: ViewerIdentity) -> dict[str, int]:
    """禁用一次性 viewer 并撤销全部 Session；用户行保留供审计引用。"""

    now = _utc_now()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        users = connection.execute(
            "UPDATE users SET status='disabled',updated_at=? WHERE user_id=? AND status='active'",
            (now, identity.user_id),
        ).rowcount
        sessions = connection.execute(
            """
            UPDATE sessions
            SET status='revoked',revoked_at=COALESCE(revoked_at,?),updated_at=?
            WHERE user_id=? AND status='active'
            """,
            (now, now, identity.user_id),
        ).rowcount
        active_sessions = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=? AND status='active'",
            (identity.user_id,),
        ).fetchone()[0]
        connection.commit()
        return {
            "viewer_users_disabled": users,
            "viewer_sessions_revoked_by_fallback": sessions,
            "viewer_active_sessions": int(active_sessions),
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _database_preflight(config: RuntimeConfig) -> dict[str, Any]:
    """确认开发库精确位于 0063，并返回默认租户和跨租户 404 测试目标。"""

    _require(config.database.is_file(), "database.preflight", "database_missing")
    connection = sqlite3.connect(config.database)
    try:
        schema = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        _require(schema == (63, "0063"), "database.preflight", "migration_not_0063")
        tenant = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug=? AND status='active'",
            (ADMIN_TENANT_SLUG,),
        ).fetchone()
        _require(tenant is not None, "database.preflight", "tenant_missing")
        tenant_id = str(tenant[0])
        route = connection.execute(
            """
            SELECT m.model_id,m.upstream_model_name,m.status,
                   p.provider_id,p.provider_code,p.base_url,p.auth_type,p.status
            FROM models m JOIN providers p
              ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            WHERE m.tenant_id=? AND m.model_alias=? AND p.provider_code=?
            """,
            (tenant_id, MODEL_ALIAS, PROVIDER_CODE),
        ).fetchall()
        _require(len(route) == 1, "database.preflight", "route_not_unique")
        row = route[0]
        _require(row[2] == "active" and row[7] == "active", "database.preflight", "route_disabled")
        _require(row[6] == "bearer", "database.preflight", "provider_auth_invalid")
        provider_url = urlsplit(str(row[5]))
        _require(
            provider_url.scheme == "http"
            and provider_url.hostname in {"127.0.0.1", "localhost"}
            and provider_url.port == config.provider_port
            and provider_url.path.rstrip("/") == "/v1",
            "database.preflight",
            "provider_base_url_invalid",
        )
        other_rows = connection.execute(
            """
            SELECT request_id FROM request_logs
            WHERE tenant_id<>? ORDER BY created_at DESC,request_id DESC LIMIT 100
            """,
            (tenant_id,),
        ).fetchall()
        other = next(
            (
                row
                for row in other_rows
                if 1 <= len(str(row[0])) <= 128
                and all(
                    character.isascii() and (character.isalnum() or character in "_.-")
                    for character in str(row[0])
                )
            ),
            None,
        )
        _require(other is not None, "database.preflight", "cross_tenant_request_missing")
        _require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.preflight",
            "integrity_failed",
        )
        _require(
            not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "database.preflight",
            "foreign_key_failed",
        )
        return {
            "schema": {"count": 63, "max_version": "0063"},
            "tenant_id": tenant_id,
            "model_id": str(row[0]),
            "provider_id": str(row[3]),
            "cross_tenant_request_id": str(other[0]),
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        }
    finally:
        connection.close()


def _health(client: httpx.Client, run_id: str, label: str) -> int:
    """核对单个业务服务健康接口和链路 Header。"""

    request_id, trace_id = _chain(run_id, f"health_{label}")
    response = client.get("/health", headers=_headers(None, request_id, trace_id))
    _expect_json(response, 200, request_id, trace_id, stage=f"health.{label}")
    return response.status_code


def _disable_stale_runtime_rules(control: httpx.Client, admin_token: str, run_id: str) -> list[str]:
    """仅禁用本验收器遗留规则；发现未知 active 输出规则时失败关闭。"""

    request_id, trace_id = _chain(run_id, "dlp_list")
    response = control.get(
        "/dlp/rules",
        params={"status": "active"},
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage="dlp.list")
    rules = payload.get("dlp_rules")
    _require(isinstance(rules, list), "dlp.list", "rules_invalid")
    active_output = [
        item
        for item in rules
        if isinstance(item, Mapping) and item.get("scan_scope") in {"output", "both"}
    ]
    unknown = [
        item
        for item in active_output
        if not str(item.get("name", "")).startswith("PRD071 Runtime ")
    ]
    _require(not unknown, "dlp.list", "foreign_active_output_rule")
    disabled: list[str] = []
    for item in active_output:
        rule_id = str(item.get("dlp_rule_id", ""))
        _require(rule_id.startswith("dlp_"), "dlp.cleanup", "rule_id_invalid")
        operation_id, operation_trace = _chain(run_id, "dlp_stale_disable")
        changed = control.patch(
            f"/dlp/rules/{rule_id}",
            json={"status": "disabled"},
            headers=_headers(admin_token, operation_id, operation_trace),
        )
        _expect_json(changed, 200, operation_id, operation_trace, stage="dlp.cleanup")
        disabled.append(rule_id)
    return disabled


def _create_runtime_rule(
    control: httpx.Client,
    admin_token: str,
    run_id: str,
    block_marker: str,
) -> str:
    """创建当前批唯一的 active 输出关键词阻断规则，并由控制面绑定认证租户 owner。"""

    request_id, trace_id = _chain(run_id, "dlp_create")
    response = control.post(
        "/dlp/rules",
        json={
            "name": f"PRD071 Runtime {run_id}",
            "rule_type": "keyword",
            "pattern": block_marker,
            "action": "block",
            "scan_scope": "output",
            "status": "active",
            "priority": 1,
            "description": "PRD-071 local runtime acceptance",
        },
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage="dlp.create")
    rule = payload.get("dlp_rule")
    _require(isinstance(rule, Mapping), "dlp.create", "rule_invalid")
    rule_id = str(rule.get("dlp_rule_id", ""))
    _require(rule_id.startswith("dlp_"), "dlp.create", "rule_id_invalid")
    _require(
        rule.get("scan_scope") == "output"
        and rule.get("action") == "block"
        and rule.get("status") == "active",
        "dlp.create",
        "rule_state_invalid",
    )
    return rule_id


def _create_provider_key(
    control: httpx.Client,
    admin_token: str,
    run_id: str,
    provider_id: str,
    plain_value: str,
) -> str:
    """通过控制面创建一次性 Provider Key，并拒绝任何明文回显。

    参数：``provider_id`` 必须来自开发库预检锁定的同租户本地 Provider；认证租户只由
    管理员 Session 恢复。返回值：可用于精确清理的脱敏 Provider Key ID。
    关键约束：Key 明文只进入本次 HTTP 请求和 Gateway 进程内存，响应、证据和日志都不得包含它。
    """

    request_id, trace_id = _chain(run_id, "provider_key_create")
    response = control.post(
        f"/providers/{provider_id}/keys",
        json={
            "name": f"PRD071 Runtime {run_id}",
            "plain_value": plain_value,
            # 新建未使用 Key 以最高权重排在开发库中不可解密的历史测试密文之前；选择器仍会
            # 按 tenant/provider/status 逐项校验，不能借验收绕过 KeyPool 治理。
            "weight": 10_000,
        },
        headers=_headers(admin_token, request_id, trace_id),
    )
    # 即使服务错误地返回 2xx，也不能让明文进入后续 JSON 证据或异常处理。
    _require(plain_value not in response.text, "provider_key.create", "plain_value_exposed")
    payload = _expect_json(response, 201, request_id, trace_id, stage="provider_key.create")
    key = payload.get("provider_key")
    _require(isinstance(key, Mapping), "provider_key.create", "key_invalid")
    key_id = str(key.get("provider_key_id", ""))
    _require(
        key_id
        and key.get("provider_id") == provider_id
        and key.get("status") == "active",
        "provider_key.create",
        "key_state_invalid",
    )
    return key_id


def _create_virtual_key(control: httpx.Client, admin_token: str, run_id: str) -> tuple[str, str]:
    """创建一次性 Virtual Key；明文只由调用方内存保存并在 finally 禁用。"""

    request_id, trace_id = _chain(run_id, "virtual_key_create")
    response = control.post(
        "/virtual-keys",
        json={
            "name": f"PRD071 Runtime {run_id}",
            "environment": "dev",
            "description": "PRD-071 local runtime acceptance",
        },
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 201, request_id, trace_id, stage="virtual_key.create")
    key = payload.get("virtual_key")
    plain = payload.get("plain_value")
    _require(isinstance(key, Mapping), "virtual_key.create", "key_invalid")
    key_id = str(key.get("virtual_key_id", ""))
    _require(
        key_id and type(plain) is str and plain.startswith("mhv_"),
        "virtual_key.create",
        "plain_invalid",
    )
    return key_id, plain


def _disable_provider_key(
    control: httpx.Client,
    admin_token: str,
    run_id: str,
    provider_key_id: str,
) -> bool:
    """通过正式控制面 API 精确禁用本批 Provider Key，不修改同 Provider 的其他凭据。"""

    request_id, trace_id = _chain(run_id, "provider_key_disable")
    response = control.patch(
        f"/provider-keys/{provider_key_id}",
        json={"status": "disabled"},
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage="cleanup.provider_key")
    key = payload.get("provider_key")
    return (
        isinstance(key, Mapping)
        and key.get("provider_key_id") == provider_key_id
        and key.get("status") == "disabled"
    )


def _disable_rule(control: httpx.Client, admin_token: str, run_id: str, rule_id: str) -> bool:
    """禁用本批 DLP 规则；tenant owner 仍由控制面服务层校验。"""

    request_id, trace_id = _chain(run_id, "dlp_disable")
    response = control.patch(
        f"/dlp/rules/{rule_id}",
        json={"status": "disabled"},
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage="cleanup.dlp")
    rule = payload.get("dlp_rule")
    return isinstance(rule, Mapping) and rule.get("status") == "disabled"


def _disable_virtual_key(
    control: httpx.Client, admin_token: str, run_id: str, virtual_key_id: str
) -> bool:
    """通过正式控制面 API 禁用一次性 Virtual Key。"""

    request_id, trace_id = _chain(run_id, "virtual_key_disable")
    response = control.delete(
        f"/virtual-keys/{virtual_key_id}",
        headers=_headers(admin_token, request_id, trace_id),
    )
    payload = _expect_json(response, 200, request_id, trace_id, stage="cleanup.virtual_key")
    key = payload.get("virtual_key")
    return isinstance(key, Mapping) and key.get("status") == "disabled"


def _logout(control: httpx.Client, token: str, run_id: str, label: str) -> bool:
    """撤销当前登录 Session；失败由调用方清理阶段统一收口。"""

    request_id, trace_id = _chain(run_id, f"logout_{label}")
    response = control.post("/auth/logout", headers=_headers(token, request_id, trace_id))
    _expect_json(response, 200, request_id, trace_id, stage=f"cleanup.logout_{label}")
    return True


def _gateway_body(prompt: str, *, stream: bool = False) -> dict[str, Any]:
    """构造最小 Chat 请求；正文只驻留 HTTP 调用栈。"""

    return {
        "model": MODEL_ALIAS,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }


def _run_non_stream_case(
    gateway: httpx.Client,
    run_id: str,
    virtual_key: str,
    *,
    label: str,
    prompt: str,
    expected_status: int,
    expected_code: str | None,
    forbidden_marker: str,
) -> tuple[str, str, dict[str, Any]]:
    """执行一个真实非流式 Chat 场景并返回安全结果摘要。"""

    request_id, trace_id = _chain(run_id, label)
    response = gateway.post(
        "/v1/chat/completions",
        headers=_headers(virtual_key, request_id, trace_id),
        json=_gateway_body(prompt),
    )
    _require(response.status_code == expected_status, f"gateway.{label}", "status_mismatch")
    _assert_chain(response, request_id, trace_id, stage=f"gateway.{label}")
    _require(
        response.headers.get("cache-control") == "no-store", f"gateway.{label}", "cache_invalid"
    )
    _require(
        forbidden_marker.encode("utf-8") not in response.content,
        f"gateway.{label}",
        "marker_exposed",
    )
    payload = _json_object(response, stage=f"gateway.{label}")
    if expected_code is None:
        choices = payload.get("choices")
        _require(isinstance(choices, list) and bool(choices), f"gateway.{label}", "choices_missing")
    else:
        _require(payload.get("code") == expected_code, f"gateway.{label}", "error_code_mismatch")
        _require(payload.get("request_id") == request_id, f"gateway.{label}", "body_owner_mismatch")
        _require("choices" not in payload, f"gateway.{label}", "choices_exposed")
    return (
        request_id,
        trace_id,
        {
            "status": response.status_code,
            "code": expected_code,
            "request_id": request_id,
            "trace_id": trace_id,
        },
    )


def _sse_blocks(wire: bytes) -> list[tuple[str | None, dict[str, Any] | None, bytes]]:
    """解析下游完整 SSE blocks，只反序列化 JSON data，不记录到证据。"""

    normalized = wire.replace(b"\r\n", b"\n")
    blocks: list[tuple[str | None, dict[str, Any] | None, bytes]] = []
    for raw in normalized.split(b"\n\n"):
        if not raw:
            continue
        event_name: str | None = None
        data_lines: list[bytes] = []
        for line in raw.split(b"\n"):
            if line.startswith(b"event: "):
                event_name = line[7:].decode("ascii", errors="strict")
            elif line.startswith(b"data: "):
                data_lines.append(line[6:])
        payload: dict[str, Any] | None = None
        if data_lines and b"\n".join(data_lines) != b"[DONE]":
            try:
                parsed = json.loads(b"\n".join(data_lines).decode("utf-8", errors="strict"))
            # SSE data 解析失败只能返回固定协议错误，禁止把可能含模型正文的 event 拼入异常。
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AcceptanceFailure("gateway.stream", "sse_json_invalid") from exc
            _require(type(parsed) is dict, "gateway.stream", "sse_payload_invalid")
            payload = parsed
        blocks.append((event_name, payload, raw))
    return blocks


def _stream_error(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    """兼容顶层与 OpenAI error 包装，只返回固定错误对象引用。"""

    nested = payload.get("error")
    if isinstance(nested, Mapping):
        return nested
    return payload if isinstance(payload.get("code"), str) else None


def _validate_stream_wire(
    wire: bytes,
    request_id: str,
    trace_id: str,
    forbidden_marker: str,
) -> dict[str, int | str]:
    """纯函数校验 blocked 流式 wire，并只返回计数与固定错误码。"""

    _require(forbidden_marker.encode("utf-8") not in wire, "gateway.stream", "marker_exposed")
    _require(b"data: [DONE]" not in wire, "gateway.stream", "done_exposed")
    blocks = _sse_blocks(wire)
    errors = [
        payload for event, payload, _raw in blocks if event == "error" and payload is not None
    ]
    _require(len(errors) == 1, "gateway.stream", "error_event_count_invalid")
    error = _stream_error(errors[0])
    _require(error is not None, "gateway.stream", "error_payload_invalid")
    _require(
        error.get("code") == "content_policy_violation", "gateway.stream", "error_code_mismatch"
    )
    _require(error.get("request_id") == request_id, "gateway.stream", "request_id_mismatch")
    _require(error.get("trace_id") == trace_id, "gateway.stream", "trace_id_mismatch")
    body_events = [
        payload for event, payload, _raw in blocks if event != "error" and payload is not None
    ]
    _require(len(body_events) == 1, "gateway.stream", "safe_event_count_invalid")
    return {
        "code": "content_policy_violation",
        "body_event_count": len(body_events),
        "error_event_count": len(errors),
        "done_count": 0,
    }


def _run_stream_case(
    gateway: httpx.Client,
    run_id: str,
    virtual_key: str,
    prompt: str,
    forbidden_marker: str,
) -> tuple[str, str, dict[str, Any]]:
    """消费真实流式响应并证明唯一安全 error、无命中 event、无 ``[DONE]``。"""

    request_id, trace_id = _chain(run_id, "stream_blocked")
    with gateway.stream(
        "POST",
        "/v1/chat/completions",
        headers=_headers(virtual_key, request_id, trace_id),
        json=_gateway_body(prompt, stream=True),
    ) as response:
        _require(response.status_code == 200, "gateway.stream", "status_mismatch")
        _assert_chain(response, request_id, trace_id, stage="gateway.stream")
        _require(
            response.headers.get("cache-control") == "no-store", "gateway.stream", "cache_invalid"
        )
        wire = b"".join(response.iter_raw())
    contract = _validate_stream_wire(wire, request_id, trace_id, forbidden_marker)
    return (
        request_id,
        trace_id,
        {
            "status": 200,
            "code": contract["code"],
            "request_id": request_id,
            "trace_id": trace_id,
            "body_event_count": contract["body_event_count"],
            "error_event_count": contract["error_event_count"],
            "done_count": contract["done_count"],
        },
    )


@dataclass(frozen=True)
class FactExpectation:
    """单个请求的无正文持久终态期望。"""

    request_status: str
    error_code: str | None
    scan_mode: str
    scan_status: str
    decision: str
    exposure_state: str
    billing_disposition: str
    dispute_count: int
    event_count: int
    stream_status: str | None = None
    failure_reason_code: str | None = None


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> list[sqlite3.Row]:
    """执行固定 SELECT 并返回行列表，集中支持精确计数断言。"""

    return list(connection.execute(sql, params).fetchall())


def _load_request_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> dict[str, Any]:
    """按租户和请求读取扫描、会话、用量、计费、争议及来源事件，并逐层复核 owner。"""

    request_logs = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id,status,error_code,stream_session_id,stream_status "
        "FROM request_logs WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    claims = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id FROM gateway_request_claims "
        "WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    scans = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id,output_safety_scan_id,stream_session_id,scan_mode,"
        "scan_status,decision,exposure_state,billing_disposition,failure_reason_code "
        "FROM output_safety_scans WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    streams = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id,stream_session_id,status,error_code "
        "FROM stream_sessions WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    usages = _rows(
        connection,
        "SELECT tenant_id,request_id,usage_ledger_id FROM usage_ledger "
        "WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    charges = _rows(
        connection,
        "SELECT tenant_id,source_request_id,source_trace_id,usage_ledger_id,"
        "finops_ledger_entry_id,entry_type FROM finops_ledger_entries "
        "WHERE tenant_id=? AND source_request_id=? AND entry_type='usage_charge'",
        (tenant_id, request_id),
    )
    disputes = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id,output_safety_scan_id,usage_ledger_id,"
        "finops_ledger_entry_id,review_status,reversal_eligible "
        "FROM finops_ledger_disputes WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    events = _rows(
        connection,
        "SELECT tenant_id,request_id,trace_id,output_safety_scan_id,event_id,event_type "
        "FROM output_safety_events WHERE tenant_id=? AND request_id=?",
        (tenant_id, request_id),
    )
    _require(len(request_logs) == 1, "database.facts", "request_log_count_invalid")
    _require(len(claims) == 1, "database.facts", "claim_count_invalid")
    _require(len(scans) == 1, "database.facts", "scan_count_invalid")
    _require(len(usages) == 1, "database.facts", "usage_count_invalid")
    _require(len(charges) == 1, "database.facts", "charge_count_invalid")
    log, claim, scan, usage, charge = request_logs[0], claims[0], scans[0], usages[0], charges[0]
    trace_id = str(log["trace_id"])
    owner_rows = [claim, scan, usage, charge, *streams, *disputes, *events]
    for row in owner_rows:
        keys = set(row.keys())
        _require(row["tenant_id"] == tenant_id, "database.facts", "tenant_owner_mismatch")
        row_request = row["request_id"] if "request_id" in keys else row["source_request_id"]
        _require(row_request == request_id, "database.facts", "request_owner_mismatch")
        row_trace = (
            row["trace_id"]
            if "trace_id" in keys
            else (row["source_trace_id"] if "source_trace_id" in keys else None)
        )
        if row_trace is not None:
            _require(row_trace == trace_id, "database.facts", "trace_owner_mismatch")
    usage_id = str(usage["usage_ledger_id"])
    charge_id = str(charge["finops_ledger_entry_id"])
    scan_id = str(scan["output_safety_scan_id"])
    _require(charge["usage_ledger_id"] == usage_id, "database.facts", "charge_usage_mismatch")
    for row in disputes:
        _require(
            row["usage_ledger_id"] == usage_id
            and row["finops_ledger_entry_id"] == charge_id
            and row["output_safety_scan_id"] == scan_id,
            "database.facts",
            "dispute_owner_mismatch",
        )
    for row in events:
        _require(row["output_safety_scan_id"] == scan_id, "database.facts", "event_owner_mismatch")
    if streams:
        _require(len(streams) == 1, "database.facts", "stream_count_invalid")
        _require(
            streams[0]["stream_session_id"] == scan["stream_session_id"]
            and streams[0]["stream_session_id"] == log["stream_session_id"],
            "database.facts",
            "stream_owner_mismatch",
        )
    else:
        _require(
            scan["stream_session_id"] is None and log["stream_session_id"] is None,
            "database.facts",
            "unexpected_stream_owner",
        )
    return {
        "request_id": request_id,
        "trace_id": trace_id,
        "request_log": {
            "count": 1,
            "status": str(log["status"]),
            "error_code": log["error_code"],
            "stream_status": log["stream_status"],
        },
        "scan": {
            "count": 1,
            "output_safety_scan_id": scan_id,
            "scan_mode": str(scan["scan_mode"]),
            "scan_status": str(scan["scan_status"]),
            "decision": str(scan["decision"]),
            "exposure_state": str(scan["exposure_state"]),
            "billing_disposition": str(scan["billing_disposition"]),
            "failure_reason_code": scan["failure_reason_code"],
        },
        "stream": {
            "count": len(streams),
            "status": str(streams[0]["status"]) if streams else None,
            "error_code": streams[0]["error_code"] if streams else None,
        },
        "usage": {"count": 1, "usage_ledger_id": usage_id},
        "charge": {"count": 1, "finops_ledger_entry_id": charge_id},
        "dispute": {"count": len(disputes)},
        "source_event": {"count": len(events)},
    }


def _validate_request_fact(fact: Mapping[str, Any], expected: FactExpectation) -> None:
    """核对无正文持久事实状态矩阵与 blocked 的争议/source event 门禁。"""

    log = fact["request_log"]
    scan = fact["scan"]
    stream = fact["stream"]
    _require(
        log["status"] == expected.request_status and log["error_code"] == expected.error_code,
        "database.facts",
        "request_terminal_invalid",
    )
    _require(
        scan["scan_mode"] == expected.scan_mode
        and scan["scan_status"] == expected.scan_status
        and scan["decision"] == expected.decision
        and scan["exposure_state"] == expected.exposure_state
        and scan["billing_disposition"] == expected.billing_disposition
        and scan["failure_reason_code"] == expected.failure_reason_code,
        "database.facts",
        "scan_terminal_invalid",
    )
    _require(
        fact["dispute"]["count"] == expected.dispute_count,
        "database.facts",
        "dispute_count_invalid",
    )
    _require(
        fact["source_event"]["count"] == expected.event_count,
        "database.facts",
        "event_count_invalid",
    )
    expected_stream_count = 1 if expected.stream_status is not None else 0
    _require(stream["count"] == expected_stream_count, "database.facts", "stream_count_invalid")
    if expected.stream_status is not None:
        _require(
            stream["status"] == expected.stream_status
            and log["stream_status"] == expected.stream_status,
            "database.facts",
            "stream_terminal_invalid",
        )


def _wait_for_facts(
    database: Path,
    tenant_id: str,
    request_expectations: Mapping[str, FactExpectation],
    *,
    timeout_seconds: float = 15.0,
) -> dict[str, dict[str, Any]]:
    """轮询异步 FinOps 落账，直到全部请求 owner 完整或达到有界超时。"""

    deadline = time.monotonic() + timeout_seconds
    last_error: AcceptanceFailure | None = None
    while time.monotonic() < deadline:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            facts: dict[str, dict[str, Any]] = {}
            for request_id, expected in request_expectations.items():
                fact = _load_request_fact(connection, tenant_id, request_id)
                _validate_request_fact(fact, expected)
                facts[request_id] = fact
            return facts
        # 异步 FinOps 可能稍后提交；轮询只保留固定分类，不保留 SQLite 异常正文或 SQL。
        except (AcceptanceFailure, sqlite3.Error) as exc:
            last_error = (
                exc
                if isinstance(exc, AcceptanceFailure)
                else AcceptanceFailure("database.facts", "query_failed")
            )
        finally:
            connection.close()
        time.sleep(0.1)
    # 达到有界等待后只返回最后固定分类，不能把数据库路径、锁信息或查询文本向外扩散。
    raise AcceptanceFailure("database.facts", last_error.code if last_error else "timeout")


def _request_log_permissions(
    control: httpx.Client,
    run_id: str,
    admin_token: str,
    viewer_token: str,
    cross_tenant_request_id: str,
) -> dict[str, dict[str, str]]:
    """验证匿名 401、viewer 403 以及跨租户/不存在 404 同形。"""

    request_id, trace_id = _chain(run_id, "request_log_anonymous")
    anonymous_response = control.get("/request-logs", headers=_headers(None, request_id, trace_id))
    anonymous = _expect_json(
        anonymous_response,
        401,
        request_id,
        trace_id,
        stage="permission.anonymous",
        error_code="auth.token_missing",
        private_no_store=True,
    )
    request_id, trace_id = _chain(run_id, "request_log_viewer")
    viewer_response = control.get(
        "/request-logs", headers=_headers(viewer_token, request_id, trace_id)
    )
    viewer = _expect_json(
        viewer_response,
        403,
        request_id,
        trace_id,
        stage="permission.viewer",
        error_code="auth.permission_denied",
        private_no_store=True,
    )
    request_id, trace_id = _chain(run_id, "request_log_cross")
    cross_response = control.get(
        f"/request-logs/{cross_tenant_request_id}",
        headers=_headers(admin_token, request_id, trace_id),
    )
    cross = _expect_json(
        cross_response,
        404,
        request_id,
        trace_id,
        stage="permission.cross_tenant",
        error_code="request_log.not_found",
        private_no_store=True,
    )
    missing_id = "req_prd071_missing_" + secrets.token_hex(10)
    request_id, trace_id = _chain(run_id, "request_log_missing")
    missing_response = control.get(
        f"/request-logs/{missing_id}",
        headers=_headers(admin_token, request_id, trace_id),
    )
    missing = _expect_json(
        missing_response,
        404,
        request_id,
        trace_id,
        stage="permission.missing",
        error_code="request_log.not_found",
        private_no_store=True,
    )
    _require(
        cross_response.status_code == missing_response.status_code
        and cross.get("code") == missing.get("code"),
        "permission.not_found_shape",
        "shape_mismatch",
    )
    return {
        "anonymous": _error_evidence(anonymous),
        "viewer": _error_evidence(viewer),
        "cross_tenant": _error_evidence(cross),
        "missing": _error_evidence(missing),
    }


def _control_read_models(
    control: httpx.Client,
    admin_token: str,
    run_id: str,
    facts_by_label: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """通过真实控制面详情、四类筛选和 FinOps 投影复核安全只读合同。"""

    details: dict[str, dict[str, object]] = {}
    for label, fact in facts_by_label.items():
        request_id = str(fact["request_id"])
        operation_id, operation_trace = _chain(run_id, f"detail_{label}")
        response = control.get(
            f"/request-logs/{request_id}",
            headers=_headers(admin_token, operation_id, operation_trace),
        )
        payload = _expect_json(
            response,
            200,
            operation_id,
            operation_trace,
            stage=f"control.detail.{label}",
            private_no_store=True,
        )
        detail = payload.get("request_log")
        _require(isinstance(detail, Mapping), f"control.detail.{label}", "detail_invalid")
        log = detail.get("log")
        safety = detail.get("output_safety")
        _require(
            isinstance(log, Mapping) and isinstance(safety, Mapping),
            f"control.detail.{label}",
            "projection_invalid",
        )
        _require(
            log.get("request_id") == request_id, f"control.detail.{label}", "request_owner_mismatch"
        )
        _require(
            safety.get("output_safety_scan_id") == fact["scan"]["output_safety_scan_id"]
            and safety.get("scan_status") == fact["scan"]["scan_status"]
            and safety.get("decision") == fact["scan"]["decision"],
            f"control.detail.{label}",
            "scan_projection_mismatch",
        )
        dispute = safety.get("dispute")
        _require(
            (dispute is not None) == (fact["dispute"]["count"] == 1),
            f"control.detail.{label}",
            "dispute_projection_mismatch",
        )
        details[label] = {
            "request_id": request_id,
            "scan_status": safety.get("scan_status"),
            "decision": safety.get("decision"),
            "dispute_present": dispute is not None,
        }

    filter_cases = {
        "status": ("output_safety_status", "completed", {"clean", "blocked", "stream_blocked"}),
        "mode": ("output_safety_mode", "stream_window", {"stream_blocked"}),
        "exposure": ("output_safety_exposure", "none", {"blocked", "unavailable"}),
        "billing": ("output_safety_billing", "dispute_pending", {"blocked", "stream_blocked"}),
    }
    filter_counts: dict[str, int] = {}
    for name, (field_name, value, labels) in filter_cases.items():
        operation_id, operation_trace = _chain(run_id, f"filter_{name}")
        response = control.get(
            "/request-logs",
            params={field_name: value, "limit": "100"},
            headers=_headers(admin_token, operation_id, operation_trace),
        )
        payload = _expect_json(
            response,
            200,
            operation_id,
            operation_trace,
            stage=f"control.filter.{name}",
            private_no_store=True,
        )
        rows = payload.get("request_logs")
        _require(isinstance(rows, list), f"control.filter.{name}", "rows_invalid")
        returned = {str(row.get("request_id")) for row in rows if isinstance(row, Mapping)}
        expected = {str(facts_by_label[label]["request_id"]) for label in labels}
        _require(expected <= returned, f"control.filter.{name}", "expected_request_missing")
        filter_counts[name] = len(expected)

    finops: dict[str, bool] = {}
    for label in ("blocked", "stream_blocked", "clean", "unavailable"):
        source_request = str(facts_by_label[label]["request_id"])
        operation_id, operation_trace = _chain(run_id, f"finops_{label}")
        response = control.get(
            "/finops/ledger",
            params={"request_id": source_request},
            headers=_headers(admin_token, operation_id, operation_trace),
        )
        payload = _expect_json(
            response,
            200,
            operation_id,
            operation_trace,
            stage=f"control.finops.{label}",
            private_no_store=True,
        )
        entries = payload.get("ledger_entries")
        _require(
            isinstance(entries, list) and len(entries) == 1,
            f"control.finops.{label}",
            "entry_count_invalid",
        )
        entry = entries[0]
        _require(isinstance(entry, Mapping), f"control.finops.{label}", "entry_invalid")
        has_dispute = entry.get("output_safety_dispute") is not None
        _require(
            has_dispute == (facts_by_label[label]["dispute"]["count"] == 1),
            f"control.finops.{label}",
            "dispute_projection_mismatch",
        )
        finops[label] = has_dispute
    return {"details": details, "filters": filter_counts, "finops_dispute_present": finops}


def _privacy_scan(
    config: RuntimeConfig,
    sensitive_values: list[str],
    *,
    database_allowed_values: set[str],
) -> dict[str, int]:
    """扫描开发库/WAL 与服务日志；DLP pattern 仅允许存在于权威数据库。"""

    _require(config.gateway_log.is_file(), "privacy.scan", "gateway_log_missing")
    _require(config.control_log.is_file(), "privacy.scan", "control_log_missing")
    targets = [
        (config.database, database_allowed_values),
        (config.database.with_name(config.database.name + "-wal"), database_allowed_values),
        (config.gateway_log, set()),
        (config.control_log, set()),
    ]
    scanned = 0
    for path, allowed in targets:
        if not path.is_file():
            continue
        content = path.read_bytes()
        scanned += 1
        for value in sensitive_values:
            if value in allowed:
                continue
            _require(value.encode("utf-8") not in content, "privacy.scan", "sensitive_match")
    _require(scanned >= 3, "privacy.scan", "scan_target_count_invalid")
    return {"files_scanned": scanned, "matches": 0}


def _validate_evidence(value: object, sensitive_values: list[str]) -> bytes:
    """递归拒绝敏感字段名，再扫描序列化 JSON 中的全部内存敏感值。"""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                _require(type(key) is str, "evidence.validate", "key_not_string")
                _require(
                    key.lower() not in FORBIDDEN_EVIDENCE_KEYS, "evidence.validate", "forbidden_key"
                )
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
        else:
            _require(
                current is None or type(current) in {str, int, float, bool},
                "evidence.validate",
                "value_type_invalid",
            )
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    for sensitive in sensitive_values:
        _require(sensitive.encode("utf-8") not in encoded, "evidence.validate", "sensitive_match")
    return encoded


def _write_evidence(
    config: RuntimeConfig,
    run_id: str,
    evidence: dict[str, Any],
    sensitive_values: list[str],
) -> Path:
    """在工作区 artifact 目录原子写入已通过字段和明文门禁的 JSON。"""

    encoded = _validate_evidence(evidence, sensitive_values)
    directory = _workspace_path(
        config.artifact_root / run_id, ROOT / ".artifacts", stage="evidence.path"
    )
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / "runtime-acceptance.json"
    temporary = directory / "runtime-acceptance.json.tmp"
    temporary.write_bytes(encoded + b"\n")
    os.replace(temporary, target)
    return target


def _expectations(requests: Mapping[str, tuple[str, str]]) -> dict[str, FactExpectation]:
    """把四个 Gateway request_id 映射为冻结的 PRD-071 状态矩阵。"""

    return {
        requests["clean"][0]: FactExpectation(
            "success",
            None,
            "non_stream_full",
            "completed",
            "clean",
            "complete",
            "not_required",
            0,
            0,
        ),
        requests["blocked"][0]: FactExpectation(
            "failed",
            "content_policy_violation",
            "non_stream_full",
            "completed",
            "blocked",
            "none",
            "dispute_pending",
            1,
            1,
        ),
        requests["unavailable"][0]: FactExpectation(
            "failed",
            "gateway.output_safety_unavailable",
            "non_stream_full",
            "unavailable",
            "unknown",
            "none",
            "not_required",
            0,
            0,
            failure_reason_code="output_safety.response_shape_invalid",
        ),
        requests["stream_blocked"][0]: FactExpectation(
            "failed",
            "content_policy_violation",
            "stream_window",
            "completed",
            "blocked",
            "partial",
            "dispute_pending",
            1,
            1,
            stream_status="output_blocked",
        ),
    }


def main() -> int:
    """执行真实 HTTP、RBAC、输出安全、账单 owner、清理和隐私闭环。"""

    config = RuntimeConfig.from_environment()
    run_id = "prd071_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)
    secrets_value = RunSecrets.create(secrets.token_hex(8))
    sensitive_values = secrets_value.sensitive_values()
    preflight = _database_preflight(config)
    tenant_id = str(preflight["tenant_id"])
    provider = FakeProvider(
        secrets_value.block_marker,
        secrets_value.provider_key,
        port=config.provider_port,
    )
    viewer_identity: ViewerIdentity | None = None
    admin_token = viewer_token = ""
    provider_key_id = virtual_key_id = virtual_key_plaintext = rule_id = ""
    cleanup: dict[str, object] = {
        "provider_key_disabled": False,
        "runtime_rule_disabled": False,
        "virtual_key_disabled": False,
        "viewer_users_disabled": 0,
        "viewer_sessions_revoked_by_fallback": 0,
        "viewer_active_sessions": 0,
        "admin_session_revoked": False,
        "viewer_session_revoked": False,
    }
    evidence: dict[str, Any] = {
        "status": "passed",
        "prd_id": "PRD-071",
        "run_id": run_id,
        "started_at": _utc_now(),
        "database": {
            "schema": preflight["schema"],
            "tenant_id": tenant_id,
            "integrity_check": preflight["integrity_check"],
            "foreign_key_violations": preflight["foreign_key_violations"],
        },
    }
    failure: BaseException | None = None

    provider.start()
    try:
        viewer_identity = _prepare_viewer(config.database, tenant_id, run_id)
        sensitive_values.append(viewer_identity.password)
        with (
            httpx.Client(base_url=config.control_url, timeout=20.0, trust_env=False) as control,
            httpx.Client(base_url=config.gateway_url, timeout=30.0, trust_env=False) as gateway,
        ):
            try:
                evidence["health"] = {
                    "control": _health(control, run_id, "control"),
                    "gateway": _health(gateway, run_id, "gateway"),
                }
                admin_token, admin_user = _login(
                    control,
                    run_id,
                    tenant_slug=ADMIN_TENANT_SLUG,
                    email=ADMIN_EMAIL,
                    password=ADMIN_PASSWORD,
                    label="admin",
                )
                viewer_token, viewer_user = _login(
                    control,
                    run_id,
                    tenant_slug=ADMIN_TENANT_SLUG,
                    email=viewer_identity.email,
                    password=viewer_identity.password,
                    label="viewer",
                )
                sensitive_values.extend([admin_token, viewer_token])
                _require(admin_user.get("tenant_id") == tenant_id, "auth.admin", "tenant_mismatch")
                _require(viewer_user.get("role") == "viewer", "auth.viewer", "role_mismatch")
                permissions = _request_log_permissions(
                    control,
                    run_id,
                    admin_token,
                    viewer_token,
                    str(preflight["cross_tenant_request_id"]),
                )
                evidence["permission"] = permissions

                stale_rules = _disable_stale_runtime_rules(control, admin_token, run_id)
                provider_key_id = _create_provider_key(
                    control,
                    admin_token,
                    run_id,
                    str(preflight["provider_id"]),
                    secrets_value.provider_key,
                )
                rule_id = _create_runtime_rule(
                    control, admin_token, run_id, secrets_value.block_marker
                )
                virtual_key_id, virtual_key_plaintext = _create_virtual_key(
                    control, admin_token, run_id
                )
                sensitive_values.append(virtual_key_plaintext)
                evidence["setup"] = {
                    "stale_runtime_rules_disabled": len(stale_rules),
                    "provider_key_id": provider_key_id,
                    "runtime_rule_id": rule_id,
                    "virtual_key_id": virtual_key_id,
                }

                requests: dict[str, tuple[str, str]] = {}
                gateway_results: dict[str, dict[str, Any]] = {}
                request_id, trace_id, summary = _run_non_stream_case(
                    gateway,
                    run_id,
                    virtual_key_plaintext,
                    label="clean",
                    prompt=secrets_value.clean_prompt,
                    expected_status=200,
                    expected_code=None,
                    forbidden_marker=secrets_value.block_marker,
                )
                requests["clean"] = (request_id, trace_id)
                gateway_results["clean"] = summary
                request_id, trace_id, summary = _run_non_stream_case(
                    gateway,
                    run_id,
                    virtual_key_plaintext,
                    label="blocked",
                    prompt=secrets_value.blocked_prompt,
                    expected_status=403,
                    expected_code="content_policy_violation",
                    forbidden_marker=secrets_value.block_marker,
                )
                requests["blocked"] = (request_id, trace_id)
                gateway_results["blocked"] = summary
                request_id, trace_id, summary = _run_non_stream_case(
                    gateway,
                    run_id,
                    virtual_key_plaintext,
                    label="unavailable",
                    prompt=secrets_value.malformed_prompt,
                    expected_status=503,
                    expected_code="gateway.output_safety_unavailable",
                    forbidden_marker=secrets_value.block_marker,
                )
                requests["unavailable"] = (request_id, trace_id)
                gateway_results["unavailable"] = summary
                request_id, trace_id, summary = _run_stream_case(
                    gateway,
                    run_id,
                    virtual_key_plaintext,
                    secrets_value.stream_prompt,
                    secrets_value.block_marker,
                )
                requests["stream_blocked"] = (request_id, trace_id)
                gateway_results["stream_blocked"] = summary
                evidence["gateway"] = gateway_results

                expectations = _expectations(requests)
                facts_by_request = _wait_for_facts(config.database, tenant_id, expectations)
                facts_by_label = {
                    label: facts_by_request[identity[0]] for label, identity in requests.items()
                }
                evidence["persistence"] = facts_by_label
                evidence["control_read_models"] = _control_read_models(
                    control, admin_token, run_id, facts_by_label
                )
                observation = provider.observation()
                _require(
                    observation.as_evidence()
                    == {
                        "calls": 4,
                        "invalid_requests": 0,
                        "invalid_authorization": 0,
                        "clean_calls": 1,
                        "blocked_calls": 1,
                        "malformed_calls": 1,
                        "stream_blocked_calls": 1,
                        "cross_chunk_splits": 1,
                    },
                    "provider.observation",
                    "count_mismatch",
                )
                evidence["provider"] = observation.as_evidence()
            except BaseException as exc:
                failure = exc
            finally:
                cleanup_failures: list[BaseException] = []
                # 只清理本批创建且已记录 ID 的规则，禁止根据名称批量修改用户安全策略。
                if rule_id and admin_token:
                    try:
                        cleanup["runtime_rule_disabled"] = _disable_rule(
                            control, admin_token, run_id, rule_id
                        )
                    except BaseException as exc:
                        cleanup_failures.append(exc)
                # Provider Key 必须通过后端权限和审计链禁用；只使用本批精确 ID，禁止批量修改
                # 开发库中属于用户或历史验收的其他 Provider 凭据。
                if provider_key_id and admin_token:
                    try:
                        cleanup["provider_key_disabled"] = _disable_provider_key(
                            control, admin_token, run_id, provider_key_id
                        )
                    except BaseException as exc:
                        cleanup_failures.append(exc)
                # Virtual Key 通过正式租户 API 禁用，不能直接改库绕过审计和权限校验。
                if virtual_key_id and admin_token:
                    try:
                        cleanup["virtual_key_disabled"] = _disable_virtual_key(
                            control, admin_token, run_id, virtual_key_id
                        )
                    except BaseException as exc:
                        cleanup_failures.append(exc)
                # viewer Session 先走公开 logout；数据库 fallback 仅负责证明最终无 active Session。
                if viewer_token:
                    try:
                        cleanup["viewer_session_revoked"] = _logout(
                            control, viewer_token, run_id, "viewer"
                        )
                    except BaseException as exc:
                        cleanup_failures.append(exc)
                # 管理员 Token 同样必须撤销，验收成功不能留下可继续使用的控制面 Session。
                if admin_token:
                    try:
                        cleanup["admin_session_revoked"] = _logout(
                            control, admin_token, run_id, "admin"
                        )
                    except BaseException as exc:
                        cleanup_failures.append(exc)
                if cleanup_failures and failure is None:
                    failure = AcceptanceFailure("cleanup.http", "operation_failed")
    finally:
        try:
            provider.stop()
        except BaseException as exc:
            if failure is None:
                failure = exc
        if viewer_identity is not None:
            try:
                cleanup.update(_disable_viewer(config.database, viewer_identity))
            except BaseException as exc:
                if failure is None:
                    failure = exc

    if failure is not None:
        raise failure
    _require(
        cleanup["provider_key_disabled"] is True
        and cleanup["runtime_rule_disabled"] is True
        and cleanup["virtual_key_disabled"] is True
        and cleanup["admin_session_revoked"] is True
        and cleanup["viewer_session_revoked"] is True
        and cleanup["viewer_users_disabled"] == 1
        and cleanup["viewer_active_sessions"] == 0,
        "cleanup.final",
        "incomplete",
    )
    evidence["cleanup"] = cleanup
    evidence["privacy"] = _privacy_scan(
        config,
        sensitive_values,
        database_allowed_values={secrets_value.block_marker},
    )
    evidence["completed_at"] = _utc_now()
    evidence_path = _write_evidence(config, run_id, evidence, sensitive_values)
    print(
        json.dumps(
            {
                "status": "passed",
                "prd_id": "PRD-071",
                "run_id": run_id,
                "evidence_path": str(evidence_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
