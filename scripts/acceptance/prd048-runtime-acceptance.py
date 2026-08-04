"""PRD-048 效果-成本路由基础的真实三服务运行验收。

脚本只允许操作 ``artifacts/prd048`` 下的独立 SQLite 验收库和本机回环服务。它通过
公开 Control API 创建两条 OpenAI-compatible 路线、临时账号、Virtual Key 和 Webhook，
再由 Gateway 发起真实调用。所有持久证据仅包含稳定 ID、版本、计数和布尔结论；一次性
密码、Token、Key、Webhook Secret、Prompt 与 Response 始终只存在于进程内存。

默认运行和任意失败路径都会禁用临时资源并撤销 Session。只有显式指定
``--retain-for-browser`` 且脱敏状态文件已经原子落盘时才暂留资源；浏览器验收结束后必须
使用 ``--cleanup-state <runtime-state.json>`` 执行可重复的最终清理。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, closing
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from metahub_shared.runtime.webhooks import verify_webhook_signature

# 管理请求、模型调用和异步事件均设有硬超时；运行验收不能靠无限等待掩盖服务死锁。
HTTP_TIMEOUT = httpx.Timeout(20.0)
EVENT_WAIT_SECONDS = 12.0
MAX_HTTP_BODY_BYTES = 1_048_576
QUALITY_CONFIG = {
    "quality_weight_bps": 9_000,
    "cost_weight_bps": 1_000,
    "latency_weight_bps": 0,
    "quality_floor_bps": 6_000,
    "minimum_sample_count": 20,
}
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
SAFE_SUFFIX_PATTERN = re.compile(r"^[a-z0-9]{8,32}$")
FORBIDDEN_STATE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "bearer",
        "key_hash",
        "password",
        "plain_value",
        "prompt",
        "provider_key",
        "response",
        "signing_secret",
        "token",
        "token_hash",
        "webhook_secret",
    }
)


class AcceptanceServer(ThreadingHTTPServer):
    """同时承载两个固定上游和一个只保存脱敏摘要的 Webhook 接收端。"""

    daemon_threads = True

    def __init__(self, prompt_sentinel: str) -> None:
        """绑定随机回环端口并初始化线程安全状态。

        参数：``prompt_sentinel`` 是只在内存中存在的随机正文哨兵。
        返回值：无。关键约束：共享列表不得保存原始 HTTP body 或请求头。
        """

        super().__init__(("127.0.0.1", 0), AcceptanceHandler)
        self.prompt_sentinel = prompt_sentinel
        self.signing_secret = ""
        self._provider_calls: list[dict[str, str]] = []
        self._webhook_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record_provider_call(self, provider: str, model: str) -> None:
        """记录上游标签和模型名，不保留包含 Prompt 的请求正文。

        参数：``provider`` 是 a/b 安全标签，``model`` 是公开上游模型名。
        返回值：无。关键约束：调用方必须先完成正文哨兵校验再调用本函数。
        """

        with self._lock:
            self._provider_calls.append({"provider": provider, "model": model})

    def provider_calls(self) -> list[dict[str, str]]:
        """返回上游调用摘要副本，避免主线程与 HTTP 工作线程共享可变对象。"""

        with self._lock:
            return [dict(item) for item in self._provider_calls]

    def record_webhook_event(self, event: dict[str, Any]) -> None:
        """保存已经验签且通过隐私扫描的 Webhook 白名单摘要。"""

        with self._lock:
            self._webhook_events.append(dict(event))

    def webhook_events(self) -> list[dict[str, Any]]:
        """返回 Webhook 安全摘要副本，供主线程按 request_id 有界等待。"""

        with self._lock:
            return [dict(item) for item in self._webhook_events]


class AcceptanceHandler(BaseHTTPRequestHandler):
    """提供固定 OpenAI-compatible 响应并验证评分事件 HMAC。"""

    def do_GET(self) -> None:  # noqa: N802
        """返回上游模型目录；未知路径只返回固定 404，不暴露内部状态。"""

        if self.path.endswith("/models"):
            self._json_response(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "prd048-quality-a"},
                        {"id": "prd048-cheap-b"},
                    ],
                },
            )
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """有界读取正文并分派 Provider 或 Webhook 请求。

        参数与返回值由 ``BaseHTTPRequestHandler`` 协议提供。关键约束：未知或过大正文
        不能进入 JSON 解析，也不能由默认访问日志输出。
        """

        raw = self._read_body()
        if raw is None:
            return
        if self.path == "/hook":
            self._handle_webhook(raw)
            return
        if self.path in {
            "/upstream-a/v1/chat/completions",
            "/upstream-b/v1/chat/completions",
        }:
            self._handle_provider(raw)
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, _format: str, *args: object) -> None:
        """关闭默认 HTTP 日志，防止未来 URL 或错误参数意外落盘。"""

        del args

    def _read_body(self) -> bytes | None:
        """按 Content-Length 有界读取正文，非法长度直接返回 413。

        返回值：合法时返回字节，拒绝时返回 ``None``。关键约束：一 MiB 是硬上限，
        不能因测试输入异常分配无界内存。
        """

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        # 非法声明不能进入正文读取，否则可能绕过一 MiB 的内存硬上限。
        except ValueError:
            length = -1
        if length < 1 or length > MAX_HTTP_BODY_BYTES:
            self._json_response(413, {"error": "body_too_large"})
            return None
        return self.rfile.read(length)

    def _handle_provider(self, raw: bytes) -> None:
        """验证真实 Prompt 已到达回环上游并返回稳定 Usage。

        参数：``raw`` 为仅在当前栈帧存在的请求正文。返回值：无。关键约束：解析完成后
        只登记 Provider 标签与模型名，不把正文或消息内容放入共享状态。
        """

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            body = json.loads(raw.decode("utf-8", errors="strict"))
        # 上游夹具只接受严格 UTF-8 JSON，错误响应不能回显可能含 Prompt 的原文。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        if not isinstance(body, dict):
            self._json_response(400, {"error": "invalid_object"})
            return
        if server.prompt_sentinel.encode("utf-8") not in raw:
            self._json_response(400, {"error": "prompt_missing"})
            return
        provider = "a" if "/upstream-a/" in self.path else "b"
        model = str(body.get("model", ""))
        server.record_provider_call(provider, model)
        self._json_response(
            200,
            {
                "id": f"chatcmpl_prd048_{provider}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"selected-{provider}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 8,
                    "total_tokens": 18,
                },
            },
        )

    def _handle_webhook(self, raw: bytes) -> None:
        """验签评分事件并只保存协议白名单字段。

        参数：``raw`` 是签名覆盖的原始字节。返回值：无。关键约束：Prompt 哨兵、
        Provider Key、Token 等敏感内容一旦出现即拒绝，原始信封不得进入共享状态。
        """

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            envelope = json.loads(raw.decode("utf-8", errors="strict"))
        # Webhook 解析失败只返回固定错误，禁止把签名覆盖正文拼入异常或日志。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        if not isinstance(envelope, dict) or server.prompt_sentinel.encode("utf-8") in raw:
            self._json_response(400, {"error": "unsafe_event"})
            return
        payload = envelope.get("payload")
        safe_payload = payload if isinstance(payload, dict) else {}
        signature = self.headers.get("X-MetaHub-Signature", "")
        signature_valid = bool(server.signing_secret) and verify_webhook_signature(
            signature,
            server.signing_secret,
            raw,
        )
        payload_keys = sorted(str(key) for key in safe_payload)
        forbidden = {
            "authorization",
            "base_url",
            "password",
            "prompt",
            "provider_key",
            "response",
            "token",
        }
        privacy_ok = forbidden.isdisjoint(set(payload_keys))
        server.record_webhook_event(
            {
                "event_id": str(envelope.get("event_id", "")),
                "event_type": str(envelope.get("event_type", "")),
                "object_id": str(envelope.get("object_id", "")),
                "request_id": safe_payload.get("request_id"),
                "model_id": safe_payload.get("model_id"),
                "feedback_version": safe_payload.get("feedback_version"),
                "statistics_version": safe_payload.get("statistics_version"),
                "trigger": safe_payload.get("trigger"),
                "payload_keys": payload_keys,
                "signature_valid": signature_valid,
                "privacy_ok": privacy_ok,
            }
        )
        self.send_response(204 if signature_valid and privacy_ok else 401)
        self.end_headers()

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        """写入固定 UTF-8 JSON 响应并显式设置长度，避免客户端等待连接关闭。"""

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class RunningAcceptanceServer(AbstractContextManager[AcceptanceServer]):
    """管理回环服务生命周期，确保成功和异常路径都关闭监听线程。"""

    def __init__(self, prompt_sentinel: str) -> None:
        """登记仅在内存中使用的 Prompt 哨兵，服务在进入上下文时才启动。"""

        self.prompt_sentinel = prompt_sentinel
        self.server: AcceptanceServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> AcceptanceServer:
        """启动 daemon HTTP 线程并返回可读取安全摘要的服务对象。"""

        self.server = AcceptanceServer(self.prompt_sentinel)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_exc_info: object) -> None:
        """关闭监听、等待线程退出，并立即清空一次性 HMAC Secret。"""

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server.signing_secret = ""
        if self.thread is not None:
            self.thread.join(timeout=5)


def _safe_diagnostic_scalar(value: object) -> str:
    """把响应字段压缩为可安全输出的协议标量。

    参数：不可信响应中的任意值。返回值：有界错误码/链路 ID 或固定 ``<redacted>``。
    关键约束：异常诊断不能因为服务端 wire 回归而把正文、Token 或 Key 打到终端。
    """

    if isinstance(value, str):
        safe_chain_id = value.startswith(("req_", "trc_")) and bool(
            re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
        )
        safe_error_code = bool(
            re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", value)
        )
        # 仅允许 MetaHub 链路 ID 或点分统一错误码，Key/Secret 前缀不会落入白名单。
        if safe_chain_id or safe_error_code:
            return value
    return "<redacted>"


def _content_type_classification(response: httpx.Response) -> str:
    """把 Content-Type 归类为固定枚举，避免异常头值进入验收输出。"""

    raw = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if raw == "application/json":
        return "application/json"
    if raw in {"text/html", "text/plain"}:
        return raw
    return "missing" if not raw else "other"


def expect_json(
    response: httpx.Response,
    expected_status: int,
    expected_request_id: str | None = None,
    expected_trace_id: str | None = None,
) -> dict[str, Any]:
    """断言统一 JSON wire、HTTP 状态以及 request_id/trace_id 回显。

    参数：响应、预期状态和可选链路 ID。返回值：JSON object。关键约束：错误信息只输出
    状态、统一错误码和安全链路 ID，不能回显可能含 Token、Key 或正文的完整响应。
    """

    try:
        body = response.json()
    # 解析失败仅报告状态和媒体类型，不能把代理 HTML 或敏感响应原文带到终端。
    except json.JSONDecodeError as exc:
        content_type = _content_type_classification(response)
        # 固定诊断字段足够定位 wire 回归，同时避免输出响应正文。
        raise AssertionError(
            "response is not JSON: "
            f"status={response.status_code}, content_type={content_type}"
        ) from exc
    if not isinstance(body, dict):
        # 登录和 Key 创建响应可能带一次性凭据，结构异常时也不能回显 body。
        raise AssertionError(f"response JSON must be object: status={response.status_code}")
    if response.status_code != expected_status:
        # 状态异常只暴露统一错误码与 request_id，不复制完整 JSON。
        raise AssertionError(
            "unexpected response status: "
            f"actual={response.status_code}, expected={expected_status}, "
            f"code={_safe_diagnostic_scalar(body.get('code'))}, "
            f"request_id={_safe_diagnostic_scalar(body.get('request_id'))}"
        )
    response_request_id = body.get("request_id") or response.headers.get("X-Request-ID")
    response_trace_id = body.get("trace_id") or response.headers.get("X-Trace-ID")
    assert isinstance(response_request_id, str) and response_request_id
    assert isinstance(response_trace_id, str) and response_trace_id
    # 调用方提供 request_id 时必须精确回显，证明请求、审计与错误可关联。
    if expected_request_id is not None:
        assert response_request_id == expected_request_id, (
            "request_id mismatch: "
            f"actual={_safe_diagnostic_scalar(response_request_id)}, "
            f"expected={_safe_diagnostic_scalar(expected_request_id)}"
        )
    # 显式 trace_id 同样必须原样回显，防止服务在链路中静默换号。
    if expected_trace_id is not None:
        assert response_trace_id == expected_trace_id, (
            "trace_id mismatch: "
            f"actual={_safe_diagnostic_scalar(response_trace_id)}, "
            f"expected={_safe_diagnostic_scalar(expected_trace_id)}"
        )
    return body


def quote_identifier(value: str) -> str:
    """安全引用来自 SQLite 元数据的标识符，供逐列隐私扫描使用。"""

    return '"' + value.replace('"', '""') + '"'


def utc_now() -> str:
    """返回数据库统一使用的 UTC 秒级时间文本。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_loopback_url(value: str, label: str) -> None:
    """限制服务 URL 为本机明文 HTTP 回环，防止脚本误操作外部部署。"""

    parsed = urlparse(value)
    assert parsed.scheme == "http", f"{label} must use http"
    assert parsed.hostname in {"127.0.0.1", "localhost"}, f"{label} must be loopback"


def _safe_resource_ids(
    resource_ids: object,
    *,
    require_complete: bool,
) -> dict[str, str]:
    """校验清理账本只含有界稳定 ID，拒绝空值、SQL 片段和未知复合结构。

    参数：来自当前进程或脱敏状态文件的资源映射。返回值：字符串副本。关键约束：
    清理 SQL 只消费本函数验证后的值，并继续用 tenant_id 二次限定 owner。
    """

    assert isinstance(resource_ids, dict), "resource_ids must be object"
    cleaned: dict[str, str] = {}
    for key, value in resource_ids.items():
        assert isinstance(key, str) and SAFE_ID_PATTERN.fullmatch(key), "invalid resource key"
        assert isinstance(value, str) and SAFE_ID_PATTERN.fullmatch(value), (
            f"invalid resource ID for {key}"
        )
        cleaned[key] = value
    required = {
        "main_tenant_id",
        "admin_session_id",
        "developer_session_id",
        "viewer_session_id",
        "developer_user_id",
        "viewer_user_id",
        "provider_a_id",
        "provider_b_id",
        "model_a_id",
        "model_b_id",
        "cross_tenant_id",
        "cross_tenant_environment_id",
        "cross_tenant_provider_id",
        "cross_tenant_model_id",
        "cross_tenant_score_id",
        "alias_id",
        "routing_policy_id",
        "virtual_key_id",
        "webhook_endpoint_id",
    }
    if require_complete:
        assert required.issubset(cleaned), "cleanup resource ledger is incomplete"
    return cleaned


def _update_one(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
    *,
    label: str,
) -> int:
    """执行单 owner 状态更新并拒绝多行变更。

    参数：连接、固定 SQL、绑定值和安全标签。返回值：0/1 变更数。关键约束：任何多行
    结果都代表资源账本或唯一约束损坏，必须让整个清理事务回滚。
    """

    cursor = connection.execute(query, parameters)
    changed = max(cursor.rowcount, 0)
    assert changed in {0, 1}, f"{label} cleanup changed unexpected rows"
    return changed


def cleanup_runtime_resources(
    database_path: Path,
    resource_ids: object,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """按 tenant-qualified 稳定 ID 禁用资源、撤销 Session 并复核终态。

    参数：独立验收库与脱敏资源账本。返回值：各类实际变更计数及 ``verified``。
    关键约束：默认管理员只撤销本脚本登记的 Session；临时 developer/viewer 的全部
    Session 都会撤销，以覆盖浏览器异常退出未能调用 logout 的情况。
    """

    ids = _safe_resource_ids(resource_ids, require_complete=require_complete)
    tenant_id = ids.get("main_tenant_id")
    now = utc_now()
    counts: dict[str, int] = {
        "sessions": 0,
        "users": 0,
        "tenants": 0,
        "environments": 0,
        "aliases": 0,
        "models": 0,
        "providers": 0,
        "virtual_keys": 0,
        "webhooks": 0,
    }
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")

        # 管理员可能同时有不属于本次验收的其他会话，因此只撤销本脚本登记的 Session。
        # 浏览器验收必须把自己的 admin logout 作为成功硬门禁；这里不能按 admin user 批量
        # 撤销，否则 cleanup-state 会误伤同一管理员的日常控制台会话。若认证在 /auth/me 前
        # 失败，tenant 尚未知，但 session_id 仍是全局唯一稳定 ID，可精确撤销。
        admin_session_id = ids.get("admin_session_id")
        if admin_session_id:
            if tenant_id:
                query = (
                    "UPDATE sessions SET status='revoked', "
                    "revoked_at=COALESCE(revoked_at, ?), updated_at=? "
                    "WHERE tenant_id=? AND session_id=? AND status!='revoked'"
                )
                parameters: tuple[object, ...] = (
                    now,
                    now,
                    tenant_id,
                    admin_session_id,
                )
            else:
                query = (
                    "UPDATE sessions SET status='revoked', "
                    "revoked_at=COALESCE(revoked_at, ?), updated_at=? "
                    "WHERE session_id=? AND status!='revoked'"
                )
                parameters = (now, now, admin_session_id)
            counts["sessions"] += _update_one(
                connection,
                query,
                parameters,
                label="admin session",
            )
        # 临时账号没有复用价值；按 tenant+user 撤销全部会话，可兜住 Browser logout 失败。
        for role in ("developer", "viewer"):
            user_id = ids.get(f"{role}_user_id")
            session_id = ids.get(f"{role}_session_id")
            # owner 已知时撤销临时账号全部会话，覆盖 Browser logout 失败的残留。
            if tenant_id and user_id:
                cursor = connection.execute(
                    """
                    UPDATE sessions
                    SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
                    WHERE tenant_id=? AND user_id=? AND status!='revoked'
                    """,
                    (now, now, tenant_id, user_id),
                )
                changed = max(cursor.rowcount, 0)
                # 一个角色可能同时存在 runtime 与浏览器 Session，因此这里允许有限多行清理。
                assert changed <= 16, f"{role} session cleanup exceeded safety bound"
                counts["sessions"] += changed
            elif session_id:
                counts["sessions"] += _update_one(
                    connection,
                    """
                    UPDATE sessions
                    SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
                    WHERE session_id=? AND status!='revoked'
                    """,
                    (now, now, session_id),
                    label=f"{role} session",
                )

        # 业务资源只在 tenant owner 已冻结后创建；若出现无 tenant 的业务 ID，拒绝扩大清理。
        business_keys = {
            "webhook_endpoint_id",
            "virtual_key_id",
            "alias_id",
            "model_a_id",
            "model_b_id",
            "provider_a_id",
            "provider_b_id",
            "cross_tenant_id",
            "cross_tenant_environment_id",
            "cross_tenant_provider_id",
            "cross_tenant_model_id",
            "cross_tenant_score_id",
            "developer_user_id",
            "viewer_user_id",
        }
        assert tenant_id or business_keys.isdisjoint(ids), (
            "business cleanup requires main_tenant_id"
        )
        # Webhook 是异步外发入口，必须最先禁用，避免接收器关闭后继续重试。
        if tenant_id and ids.get("webhook_endpoint_id"):
            counts["webhooks"] += _update_one(
                connection,
                """
                UPDATE webhook_endpoints SET status='disabled', updated_at=?
                WHERE tenant_id=? AND webhook_endpoint_id=? AND status!='disabled'
                """,
                (now, tenant_id, ids["webhook_endpoint_id"]),
                label="webhook",
            )
        # Virtual Key 在路由图之前失效，确保清理期间不能再发起新调用。
        if tenant_id and ids.get("virtual_key_id"):
            counts["virtual_keys"] += _update_one(
                connection,
                """
                UPDATE virtual_keys
                SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                WHERE tenant_id=? AND virtual_key_id=? AND status!='disabled'
                """,
                (now, now, tenant_id, ids["virtual_key_id"]),
                label="virtual key",
            )
        # Alias 禁用后保留不可变 Decision/Candidate 历史，仅关闭新的解析入口。
        if tenant_id and ids.get("alias_id"):
            counts["aliases"] += _update_one(
                connection,
                """
                UPDATE model_aliases
                SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                WHERE tenant_id=? AND model_alias_id=? AND status!='disabled'
                """,
                (now, now, tenant_id, ids["alias_id"]),
                label="model alias",
            )
        for label in ("a", "b"):
            # 模型只按 tenant+稳定 ID 逐条禁用，不能按名称或前缀批量匹配。
            if tenant_id and ids.get(f"model_{label}_id"):
                counts["models"] += _update_one(
                    connection,
                    """
                    UPDATE models
                    SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                    WHERE tenant_id=? AND model_id=? AND status!='disabled'
                    """,
                    (now, now, tenant_id, ids[f"model_{label}_id"]),
                    label=f"model {label}",
                )
            # Provider 与模型分别取证，部分创建路径仍能精确收口已有对象。
            if tenant_id and ids.get(f"provider_{label}_id"):
                counts["providers"] += _update_one(
                    connection,
                    """
                    UPDATE providers
                    SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                    WHERE tenant_id=? AND provider_id=? AND status!='disabled'
                    """,
                    (now, now, tenant_id, ids[f"provider_{label}_id"]),
                    label=f"provider {label}",
                )
        cross_tenant_id = ids.get("cross_tenant_id")
        cross_environment_id = ids.get("cross_tenant_environment_id")
        cross_provider_id = ids.get("cross_tenant_provider_id")
        cross_model_id = ids.get("cross_tenant_model_id")
        cross_score_id = ids.get("cross_tenant_score_id")
        cross_values = (
            cross_tenant_id,
            cross_environment_id,
            cross_provider_id,
            cross_model_id,
            cross_score_id,
        )
        # 第二租户夹具必须整组出现，防止残缺状态文件把清理扩大成仅凭单个全局 ID 的操作。
        assert not any(cross_values) or all(cross_values), "cross-tenant cleanup ledger is partial"
        if all(cross_values):
            assert cross_tenant_id != tenant_id, "cross-tenant fixture must use a distinct tenant"
            # Score 是 Model trigger 生成的隔离证据且禁止删除；清理前先证明账本四个 ID
            # 仍组成唯一 owner 图，再只禁用可变的 Model、Provider 和 Tenant 入口。
            score_owner_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM model_quality_scores
                    WHERE tenant_id=? AND model_id=? AND model_quality_score_id=?
                    """,
                    (cross_tenant_id, cross_model_id, cross_score_id),
                ).fetchone()[0]
            )
            assert score_owner_count == 1, "cross-tenant score owner graph is invalid"
            counts["models"] += _update_one(
                connection,
                """
                UPDATE models
                SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                WHERE tenant_id=? AND model_id=? AND status!='disabled'
                """,
                (now, now, cross_tenant_id, cross_model_id),
                label="cross-tenant model",
            )
            counts["providers"] += _update_one(
                connection,
                """
                UPDATE providers
                SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                WHERE tenant_id=? AND provider_id=? AND status!='disabled'
                """,
                (now, now, cross_tenant_id, cross_provider_id),
                label="cross-tenant provider",
            )
            counts["environments"] += _update_one(
                connection,
                """
                UPDATE environments SET status='disabled', updated_at=?
                WHERE tenant_id=? AND environment_id=? AND status!='disabled'
                """,
                (now, cross_tenant_id, cross_environment_id),
                label="cross-tenant environment",
            )
            counts["tenants"] += _update_one(
                connection,
                """
                UPDATE tenants SET status='disabled', updated_at=?
                WHERE tenant_id=? AND status!='disabled'
                """,
                (now, cross_tenant_id),
                label="cross-tenant tenant",
            )
        for role in ("developer", "viewer"):
            user_id = ids.get(f"{role}_user_id")
            # 临时账号在 Session 撤销后软禁用；默认管理员永远不进入该循环。
            if tenant_id and user_id:
                counts["users"] += _update_one(
                    connection,
                    """
                    UPDATE users SET status='disabled', updated_at=?
                    WHERE tenant_id=? AND user_id=? AND status!='disabled'
                    """,
                    (now, tenant_id, user_id),
                    label=f"{role} user",
                )

        verification_queries: list[tuple[str, tuple[object, ...]]] = []
        # 管理员仅核对本次 Session；不会误伤同一用户的其他管理会话。
        if admin_session_id:
            # 正常路径继续绑定 tenant，只有 /auth/me 前失败才使用全局唯一 Session ID。
            if tenant_id:
                verification_queries.append(
                    (
                        "SELECT COUNT(*) FROM sessions WHERE tenant_id=? AND session_id=? "
                        "AND status!='revoked'",
                        (tenant_id, admin_session_id),
                    )
                )
            else:
                verification_queries.append(
                    (
                        "SELECT COUNT(*) FROM sessions WHERE session_id=? AND status!='revoked'",
                        (admin_session_id,),
                    )
                )
        for role in ("developer", "viewer"):
            user_id = ids.get(f"{role}_user_id")
            session_id = ids.get(f"{role}_session_id")
            # 完整 owner 可同时验证账号和全部会话，部分路径只验证已登记 Session。
            if tenant_id and user_id:
                verification_queries.append(
                    (
                        "SELECT COUNT(*) FROM sessions WHERE tenant_id=? AND user_id=? "
                        "AND status!='revoked'",
                        (tenant_id, user_id),
                    )
                )
                verification_queries.append(
                    (
                        "SELECT COUNT(*) FROM users WHERE tenant_id=? AND user_id=? "
                        "AND status!='disabled'",
                        (tenant_id, user_id),
                    )
                )
            # 登录已提交但用户 owner 尚未完整登记时，仍按唯一 Session ID 兜底复核。
            elif session_id:
                verification_queries.append(
                    (
                        "SELECT COUNT(*) FROM sessions WHERE session_id=? AND status!='revoked'",
                        (session_id,),
                    )
                )
        # 业务对象使用同一白名单生成只读终态查询，不接受状态文件提供表名或列名。
        for table, column, key in (
            ("providers", "provider_id", "provider_a_id"),
            ("providers", "provider_id", "provider_b_id"),
            ("models", "model_id", "model_a_id"),
            ("models", "model_id", "model_b_id"),
            ("model_aliases", "model_alias_id", "alias_id"),
            ("virtual_keys", "virtual_key_id", "virtual_key_id"),
            ("webhook_endpoints", "webhook_endpoint_id", "webhook_endpoint_id"),
        ):
            # 只有本轮确实登记的对象才进入验证，失败路径不会猜测缺失资源。
            if tenant_id and ids.get(key):
                verification_queries.append(
                    (
                        f"SELECT COUNT(*) FROM {table} WHERE tenant_id=? AND {column}=? "
                        "AND status!='disabled'",
                        (tenant_id, ids[key]),
                    )
                )
        if all(cross_values):
            # 第二租户只能按自己的 tenant owner 验证，不能复用 main_tenant_id 造成假阳性。
            verification_queries.extend(
                [
                    (
                        "SELECT COUNT(*) FROM tenants WHERE tenant_id=? AND status!='disabled'",
                        (cross_tenant_id,),
                    ),
                    (
                        "SELECT COUNT(*) FROM providers WHERE tenant_id=? AND provider_id=? "
                        "AND status!='disabled'",
                        (cross_tenant_id, cross_provider_id),
                    ),
                    (
                        "SELECT COUNT(*) FROM environments "
                        "WHERE tenant_id=? AND environment_id=? AND status!='disabled'",
                        (cross_tenant_id, cross_environment_id),
                    ),
                    (
                        "SELECT COUNT(*) FROM models WHERE tenant_id=? AND model_id=? "
                        "AND status!='disabled'",
                        (cross_tenant_id, cross_model_id),
                    ),
                ]
            )
        assert all(
            int(connection.execute(query, parameters).fetchone()[0]) == 0
            for query, parameters in verification_queries
        ), "runtime cleanup verification failed"
    return {**counts, "verified": True}


def assert_redacted_state(state: dict[str, Any], sensitive_values: list[str]) -> None:
    """拒绝状态树中的敏感键或任一已知敏感明文。

    参数：待写 JSON 与本进程内凭据列表。返回值：无。关键约束：检查发生在写盘和终端
    输出之前；空敏感值被忽略，避免对所有字符串产生无意义命中。
    """

    stack: list[object] = [state]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                normalized = str(key).lower()
                assert normalized not in FORBIDDEN_STATE_KEYS, (
                    f"forbidden state key: {normalized}"
                )
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
    for value in sensitive_values:
        if value:
            assert value not in encoded, "sensitive value reached runtime state"


def write_state_atomically(path: Path, state: dict[str, Any]) -> None:
    """以 UTF-8 原子替换方式写入脱敏状态，失败时不留下半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    encoded = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RuntimeAcceptance:
    """编排 PRD-048 主流程、异常、权限、统计冻结、租户和隐私验收。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """创建本地 HTTP 客户端并初始化只在内存中持有的凭据。

        参数：已完成路径与回环校验的命令行参数。返回值：无。关键约束：Token、Key、
        Secret 与密码不会加入资源账本，``close`` 会主动清空长生命周期引用。
        """

        self.args = args
        self.suffix = args.suffix
        self.prompt_sentinel = f"prd048-prompt-{secrets.token_urlsafe(24)}"
        self.control = httpx.Client(base_url=args.control_url, timeout=HTTP_TIMEOUT)
        self.gateway = httpx.Client(base_url=args.gateway_url, timeout=HTTP_TIMEOUT)
        self.admin_token = ""
        self.role_tokens: dict[str, str] = {}
        self.virtual_key = ""
        self.webhook_secret = ""
        self.resource_ids: dict[str, str] = {}
        self.request_ids: dict[str, str] = {}
        self.feedback_request_ids: list[str] = []

    def close(self) -> None:
        """关闭连接池并清空所有进程内凭据。

        返回值：无。关键约束：该函数不负责持久资源清理；调用方必须先执行 ``cleanup``
        或已经以显式浏览器保留模式原子写出脱敏状态。
        """

        self.control.close()
        self.gateway.close()
        self.admin_token = ""
        self.role_tokens.clear()
        self.virtual_key = ""
        self.webhook_secret = ""
        self.args.admin_password = ""
        self.args.developer_password = ""
        self.args.viewer_password = ""

    def sensitive_values(self) -> list[str]:
        """返回写状态前必须排除的内存敏感值列表，不对外序列化该列表。"""

        return [
            self.prompt_sentinel,
            self.args.admin_password,
            self.args.developer_password,
            self.args.viewer_password,
            self.admin_token,
            *self.role_tokens.values(),
            self.virtual_key,
            self.webhook_secret,
            # 固定 Response 哨兵也视为敏感正文，数据库和状态均不得持久化。
            "selected-a",
            "selected-b",
        ]

    def run(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """执行完整运行验收并返回只含稳定事实的脱敏摘要。

        参数：同时承载本地 Provider 与 Webhook 的回环服务。返回值：待写状态对象。
        关键约束：任一断言失败都由外层 finally 清理；本方法不会吞掉异常或提前标记通过。
        """

        self._verify_service_health()
        self.admin_token = self._login(
            self.args.admin_email,
            self.args.admin_password,
            role="admin",
        )
        self._current_identity()
        self._create_cross_tenant_score_fixture()
        environment = self._default_environment()
        self.role_tokens = self._create_role_users()
        route = self._create_route(environment, receiver)
        self.virtual_key = self._create_virtual_key(environment["environment_id"])
        self._create_webhook(receiver)

        score_setup = self._configure_initial_scores(route, receiver)
        policy = self._enable_quality_cost(route)
        rbac = self._verify_score_rbac(route, score_setup)
        cross_tenant = self._verify_cross_tenant_score_isolation()
        quality = self._verify_quality_wins(route, policy, receiver)
        low_quality = self._verify_all_low(route, receiver)
        score_events = self._verify_final_score_events(route, receiver)
        self._disable_webhook_after_evidence()
        privacy = self._verify_privacy()

        checks = {
            "webhook_hmac_valid": True,
            "webhook_event_count": score_events,
            "selected_upstream_model": "prd048-quality-a",
            "selected_frozen_statistics_version": quality["frozen_statistics_version"],
            "current_statistics_version": quality["current_statistics_version"],
            "quality_weighted_score_gt_cheap": True,
            "quality_cost_gt_cheap": True,
            "all_low_attempt_count": low_quality["attempt_count"],
            "all_low_provider_call_count": low_quality["provider_call_count"],
            "request_log_cache_control": "no-store, private",
            "developer_read": rbac["developer_read"],
            "viewer_read_denied": rbac["viewer_read_denied"],
            "developer_write_denied": rbac["developer_write_denied"],
            "cross_tenant_get_hidden": cross_tenant["get_hidden"],
            "cross_tenant_patch_hidden": cross_tenant["patch_hidden"],
            "cross_tenant_score_unchanged": cross_tenant["score_unchanged"],
            "tenant_id_client_input_absent": cross_tenant["tenant_input_absent"],
            "score_idempotent_retry": score_setup["idempotent_retry"],
            "score_version_conflict": score_setup["version_conflict"],
            "score_request_conflict": score_setup["request_conflict"],
            "score_event_count": score_events,
            "database_prompt_hits": privacy["database_prompt_hits"],
        }
        return {
            "status": "passed",
            "prd_id": "PRD-048",
            "suffix": self.suffix,
            "database_file": self.args.database_path.name,
            "resource_ids": dict(self.resource_ids),
            "request_ids": dict(self.request_ids),
            "checks": checks,
        }

    def cleanup(self, *, require_complete: bool) -> dict[str, Any]:
        """清理当前已登记资源；失败路径允许部分账本，成功终清理要求账本完整。

        参数：``require_complete`` 控制是否强制所有预期 ID 已登记。返回值：安全计数摘要。
        关键约束：SQL 仍逐对象 tenant-qualified，部分清理不能扩大为名称或租户级批量禁用。
        """

        return cleanup_runtime_resources(
            self.args.database_path,
            self.resource_ids,
            require_complete=require_complete,
        )

    def _verify_service_health(self) -> None:
        """确认 Gateway、Control 和 Console 三个本机服务均真实返回 HTTP 200。"""

        for client, path in ((self.gateway, "/health"), (self.control, "/health")):
            response = client.get(path)
            assert response.status_code == 200, f"service health failed: {path}"
        console = httpx.get(self.args.console_url, timeout=HTTP_TIMEOUT)
        assert console.status_code == 200, "console health failed"

    def _login(self, email: str, password: str, *, role: str) -> str:
        """登录当前租户并只返回进程内 Token。

        参数：账号、密码和安全角色标签。返回值：Bearer Token。关键约束：Session ID 在
        响应出现后立即登记，即使后续 Token 断言失败也能由 finally 撤销。
        """

        request_id = f"req_prd048_runtime_login_{role}_{self.suffix}"
        trace_id = f"trc_prd048_runtime_login_{role}_{self.suffix}"
        body = expect_json(
            self.control.post(
                "/auth/login",
                headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
                json={
                    "tenant_slug": self.args.tenant_slug,
                    "email": email,
                    "password": password,
                },
            ),
            200,
            request_id,
            trace_id,
        )
        session_id = str(body.get("session_id", ""))
        token = str(body.get("access_token", ""))
        # Session ID 一出现立即登记，后续 Token/角色断言失败时仍可由 finally 撤销。
        if session_id:
            self.resource_ids[f"{role}_session_id"] = session_id
        assert session_id and token
        user = body.get("user")
        assert isinstance(user, dict) and user.get("role") == role
        # 明文 Token 只返回当前编排对象内存，绝不进入资源账本或运行状态。
        return token

    def _headers(
        self,
        token: str,
        request_id: str,
        trace_id: str | None = None,
    ) -> dict[str, str]:
        """构造认证和链路头；tenant_id 永远不接受客户端注入。"""

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": request_id,
        }
        # 仅需要验证显式 trace 透传的攻击请求携带该字段，其他请求由中间件生成。
        if trace_id:
            headers["X-Trace-ID"] = trace_id
        return headers

    def _current_identity(self) -> dict[str, str]:
        """从真实 ``/auth/me`` 冻结默认租户与管理员 owner，不猜测种子 ID。"""

        request_id = f"req_prd048_runtime_identity_{self.suffix}"
        body = expect_json(
            self.control.get(
                "/auth/me",
                headers=self._headers(self.admin_token, request_id),
            ),
            200,
            request_id,
        )
        user = body.get("user")
        session = body.get("session")
        assert isinstance(user, dict) and isinstance(session, dict)
        tenant_id = str(user.get("tenant_id", ""))
        admin_user_id = str(user.get("user_id", ""))
        assert SAFE_ID_PATTERN.fullmatch(tenant_id)
        assert SAFE_ID_PATTERN.fullmatch(admin_user_id)
        assert user.get("role") == "admin"
        assert session.get("session_id") == self.resource_ids["admin_session_id"]
        self.resource_ids["main_tenant_id"] = tenant_id
        self.resource_ids["admin_user_id"] = admin_user_id
        # 返回值只含稳定 owner ID，供环境和后续资源图做租户一致性校验。
        return {"tenant_id": tenant_id, "admin_user_id": admin_user_id}

    def _create_cross_tenant_score_fixture(self) -> dict[str, str]:
        """在隔离验收库创建真实第二租户 Model，并冻结 trigger 生成的 Score owner 图。

        参数：无。返回值：第二租户、默认 Environment、Provider、Model 与 Score 的稳定 ID。
        关键约束：这里只在 ``artifacts/prd048`` 独立库内写入最小跨租户夹具；不创建第二租户
        凭据，也不通过请求传入 tenant_id。Model 与默认 Score 必须在同一事务由 0037 trigger
        原子生成。
        """

        tenant_id = f"tenant_prd048_cross_{self.suffix}"
        provider_id = f"provider_prd048_cross_{self.suffix}"
        model_id = f"model_prd048_cross_{self.suffix}"
        created_at = utc_now()
        assert tenant_id != self.resource_ids["main_tenant_id"]
        # 独立事务确保只有完整 tenant/provider/model/score 图会进入资源账本；任一 INSERT 或
        # trigger 失败都会整体回滚，外层部分清理不会面对半个跨租户图。
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id, slug, name, service_mode, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'internal_only', 'active', ?, ?)
                """,
                (
                    tenant_id,
                    f"prd048-cross-{self.suffix}",
                    f"PRD048 cross tenant {self.suffix}",
                    created_at,
                    created_at,
                ),
            )
            environment = connection.execute(
                """
                SELECT environment_id FROM environments
                WHERE tenant_id=? AND code='dev' AND status='active'
                """,
                (tenant_id,),
            ).fetchone()
            assert environment is not None, "cross-tenant default Environment trigger did not run"
            environment_id = str(environment["environment_id"])
            assert SAFE_ID_PATTERN.fullmatch(environment_id)
            connection.execute(
                """
                INSERT INTO providers(
                  provider_id, tenant_id, provider_code, name, base_url, status,
                  provider_type, auth_type, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'http://127.0.0.1:9/v1', 'active',
                         'openai_compatible', 'none', ?, ?)
                """,
                (
                    provider_id,
                    tenant_id,
                    f"prd048_cross_{self.suffix}",
                    f"PRD048 cross provider {self.suffix}",
                    created_at,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO models(
                  model_id, tenant_id, provider_id, model_alias, upstream_model_name,
                  capabilities, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'prd048-cross-upstream', '{"chat":true}',
                         'active', ?, ?)
                """,
                (
                    model_id,
                    tenant_id,
                    provider_id,
                    f"prd048-cross-model-{self.suffix}",
                    created_at,
                    created_at,
                ),
            )
            score = connection.execute(
                """
                SELECT model_quality_score_id, tenant_id, model_id, status,
                       sample_count, statistics_version, feedback_version,
                       manual_quality_bps
                FROM model_quality_scores
                WHERE tenant_id=? AND model_id=?
                """,
                (tenant_id, model_id),
            ).fetchone()
            assert score is not None, "cross-tenant ModelQualityScore trigger did not run"
            assert tuple(score)[1:] == (
                tenant_id,
                model_id,
                "active",
                0,
                0,
                1,
                None,
            )
            score_id = str(score["model_quality_score_id"])
            assert SAFE_ID_PATTERN.fullmatch(score_id)
        fixture = {
            "cross_tenant_id": tenant_id,
            "cross_tenant_environment_id": environment_id,
            "cross_tenant_provider_id": provider_id,
            "cross_tenant_model_id": model_id,
            "cross_tenant_score_id": score_id,
        }
        self.resource_ids.update(fixture)
        return fixture

    def _default_environment(self) -> dict[str, str]:
        """读取当前租户 dev Environment，避免硬编码或跨租户复用环境 ID。"""

        request_id = f"req_prd048_runtime_environment_{self.suffix}"
        body = expect_json(
            self.control.get(
                "/environments?code=dev",
                headers=self._headers(self.admin_token, request_id),
            ),
            200,
            request_id,
        )
        environments = body.get("environments")
        assert isinstance(environments, list) and len(environments) == 1
        item = environments[0]
        assert isinstance(item, dict) and item.get("code") == "dev"
        return {
            "environment_id": str(item["environment_id"]),
            "code": "dev",
        }

    def _create_role_users(self) -> dict[str, str]:
        """通过公开 API 创建 developer/viewer 并实际登录用于后端权限攻击。"""

        tokens: dict[str, str] = {}
        for role in ("developer", "viewer"):
            password = getattr(self.args, f"{role}_password")
            request_id = f"req_prd048_runtime_user_{role}_{self.suffix}"
            body = expect_json(
                self.control.post(
                    "/users",
                    headers=self._headers(self.admin_token, request_id),
                    json={
                        "email": f"prd048-{role}-{self.suffix}@metahub.local",
                        "display_name": f"PRD048 runtime {role}",
                        "password": password,
                        "role": role,
                    },
                ),
                201,
                request_id,
            )
            user = body.get("user")
            assert isinstance(user, dict) and user.get("role") == role
            self.resource_ids[f"{role}_user_id"] = str(user["user_id"])
            tokens[role] = self._login(
                f"prd048-{role}-{self.suffix}@metahub.local",
                password,
                role=role,
            )
        # Token 映射只在 RuntimeAcceptance 内存中使用，close 会主动清空。
        return tokens

    def _create_route(
        self,
        environment: dict[str, str],
        receiver: AcceptanceServer,
    ) -> dict[str, Any]:
        """创建昂贵高质量 A 与便宜低质量 B 的 Provider/Model/Alias 图。

        参数：当前租户环境和回环服务。返回值：路由稳定 ID 与初始版本。关键约束：价格
        统一使用 CNY 版本目录，任何对象成功后立即登记 ID 供失败清理。
        """

        host, port = receiver.server_address
        route: dict[str, Any] = {}
        for label, price, provider_model in (
            ("a", 9, "prd048-quality-a"),
            ("b", 1, "prd048-cheap-b"),
        ):
            provider_request_id = f"req_prd048_runtime_provider_{label}_{self.suffix}"
            provider_body = expect_json(
                self.control.post(
                    "/providers",
                    headers=self._headers(self.admin_token, provider_request_id),
                    json={
                        "provider_code": f"prd048_runtime_{label}_{self.suffix}",
                        "name": f"PRD048 runtime Provider {label.upper()}",
                        "provider_type": "openai_compatible",
                        "base_url": f"http://{host}:{port}/upstream-{label}/v1",
                        "auth_type": "none",
                        "status": "active",
                    },
                ),
                201,
                provider_request_id,
            )
            provider_id = str(provider_body["provider"]["provider_id"])
            self.resource_ids[f"provider_{label}_id"] = provider_id
            model_request_id = f"req_prd048_runtime_model_{label}_{self.suffix}"
            model_body = expect_json(
                self.control.post(
                    "/models",
                    headers=self._headers(self.admin_token, model_request_id),
                    json={
                        "internal_model_id": f"prd048-runtime-{label}-{self.suffix}",
                        "provider_id": provider_id,
                        "provider_model_id": provider_model,
                        "display_name": f"PRD048 runtime Model {label.upper()}",
                        "capabilities": {
                            "chat": True,
                            "stream": True,
                            "embeddings": False,
                        },
                        "context_window": 8192,
                        "input_price_per_1k": price,
                        "output_price_per_1k": price,
                        "currency": "CNY",
                        "price_schema_version": f"prd048-runtime-{label}-v1",
                        "description": "PRD-048 isolated runtime acceptance",
                        "status": "active",
                    },
                ),
                201,
                model_request_id,
            )
            model_id = str(model_body["model"]["model_id"])
            self.resource_ids[f"model_{label}_id"] = model_id
            route[f"provider_{label}_id"] = provider_id
            route[f"model_{label}_id"] = model_id

        alias_request_id = f"req_prd048_runtime_alias_{self.suffix}"
        alias_body = expect_json(
            self.control.post(
                "/model-aliases",
                headers=self._headers(self.admin_token, alias_request_id),
                json={
                    "alias": f"prd048-runtime-{self.suffix}",
                    "environment_id": environment["environment_id"],
                    "default_model_id": route["model_a_id"],
                    "fallback_model_id": route["model_b_id"],
                    "description": "PRD-048 quality-cost runtime acceptance",
                    "status": "active",
                },
            ),
            201,
            alias_request_id,
        )
        alias = alias_body.get("model_alias")
        assert isinstance(alias, dict)
        policy = alias.get("routing_policy")
        assert isinstance(policy, dict) and policy.get("routing_mode") == "fixed"
        route.update(
            {
                "alias_id": str(alias["model_alias_id"]),
                "alias_name": str(alias["alias"]),
                "routing_policy_id": str(policy["routing_policy_id"]),
                "routing_policy_version": int(policy["version"]),
                "environment_id": environment["environment_id"],
            }
        )
        self.resource_ids.update(
            {
                "alias_id": route["alias_id"],
                "alias_name": route["alias_name"],
                "routing_policy_id": route["routing_policy_id"],
            }
        )
        return route

    def _create_virtual_key(self, environment_id: str) -> str:
        """创建一次性 Gateway Key；明文只返回当前调用栈与对象内存。"""

        request_id = f"req_prd048_runtime_key_{self.suffix}"
        body = expect_json(
            self.control.post(
                "/virtual-keys",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "name": f"PRD048 runtime {self.suffix}",
                    "environment_id": environment_id,
                    "description": "PRD-048 isolated runtime acceptance",
                },
            ),
            201,
            request_id,
        )
        key_info = body.get("virtual_key")
        assert isinstance(key_info, dict)
        self.resource_ids["virtual_key_id"] = str(key_info["virtual_key_id"])
        plain_value = str(body.get("plain_value", ""))
        assert plain_value.startswith("mhv_")
        return plain_value

    def _create_webhook(self, receiver: AcceptanceServer) -> None:
        """创建评分事件 Endpoint，并只把一次性 Secret 交给回环接收器内存。"""

        host, port = receiver.server_address
        request_id = f"req_prd048_runtime_webhook_{self.suffix}"
        body = expect_json(
            self.control.post(
                "/webhooks",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "name": f"PRD048 runtime {self.suffix}",
                    "endpoint_url": f"http://{host}:{port}/hook",
                    "event_types": ["model.feedback_recorded"],
                    "timeout_ms": 2000,
                    "status": "active",
                },
            ),
            200,
            request_id,
        )
        webhook = body.get("webhook")
        assert isinstance(webhook, dict)
        self.resource_ids["webhook_endpoint_id"] = str(webhook["webhook_endpoint_id"])
        self.webhook_secret = str(body.get("signing_secret", ""))
        assert self.webhook_secret
        receiver.signing_secret = self.webhook_secret

    def _list_scores(
        self,
        token: str,
        *,
        label: str,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """通过评分 GET 读取当前租户快照并核对 no-store 与 owner。

        参数：角色 Token、安全请求标签和可选模型过滤。返回值：评分对象列表。关键约束：
        响应中每一行必须属于认证租户，客户端不发送或信任 tenant_id 参数。
        """

        request_id = f"req_prd048_runtime_{label}_{self.suffix}"
        path = "/model-quality-scores?limit=200&offset=0"
        # 单模型查询仍只发送 model_id，不允许客户端拼入 tenant_id。
        if model_id is not None:
            assert SAFE_ID_PATTERN.fullmatch(model_id)
            path = f"/model-quality-scores?model_id={model_id}&limit=10&offset=0"
        response = self.control.get(path, headers=self._headers(token, request_id))
        body = expect_json(response, 200, request_id)
        assert response.headers.get("cache-control", "").startswith("no-store")
        scores = body.get("model_quality_scores")
        assert isinstance(scores, list)
        tenant_id = self.resource_ids["main_tenant_id"]
        assert all(
            isinstance(item, dict) and item.get("tenant_id") == tenant_id
            for item in scores
        )
        return scores

    @staticmethod
    def _score_by_model(
        scores: list[dict[str, Any]],
        model_id: str,
    ) -> dict[str, Any]:
        """从租户评分快照中取得唯一模型行，缺失或重复都视为数据合同损坏。"""

        matches = [item for item in scores if item.get("model_id") == model_id]
        assert len(matches) == 1, "model quality score owner is not unique"
        return matches[0]

    def _patch_score(
        self,
        model_id: str,
        manual_quality_bps: int,
        expected_feedback_version: int,
        *,
        request_id: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        """提交一次严格评分 CAS 并返回统一响应。

        参数：模型、人工基点、期望版本、稳定 request_id 和可选角色 Token。返回值：响应
        object。关键约束：正文只包含三字段，不包含 tenant_id 或未来 PRD 字段。
        """

        response = self.control.patch(
            "/model-quality-scores",
            headers=self._headers(token or self.admin_token, request_id),
            json={
                "model_id": model_id,
                "manual_quality_bps": manual_quality_bps,
                "expected_feedback_version": expected_feedback_version,
            },
        )
        return expect_json(response, 200, request_id)

    def _configure_initial_scores(
        self,
        route: dict[str, Any],
        receiver: AcceptanceServer,
    ) -> dict[str, bool]:
        """设置 A=10000、B=6500，并验证评分幂等、版本冲突与 request 冲突。

        参数：隔离路由图和 Webhook 接收器。返回值：三项 CAS 布尔证据。关键约束：
        调用流量尚未发生，两个 Score 的自动统计版本必须仍为 0。
        """

        scores = self._list_scores(
            self.role_tokens["developer"],
            label="score_list",
        )
        initial: dict[str, dict[str, Any]] = {}
        for label in ("a", "b"):
            item = self._score_by_model(scores, route[f"model_{label}_id"])
            assert item["manual_quality_bps"] is None
            assert item["sample_count"] == 0
            assert item["statistics_version"] == 0
            assert item["feedback_version"] == 1
            initial[label] = item

        request_a = f"req_prd048_runtime_score_a_{self.suffix}"
        changed_a = self._patch_score(
            route["model_a_id"],
            10_000,
            int(initial["a"]["feedback_version"]),
            request_id=request_a,
        )
        assert changed_a.get("changed") is True
        score_a = changed_a.get("model_quality_score")
        assert isinstance(score_a, dict)
        assert score_a["manual_quality_bps"] == 10_000
        assert score_a["feedback_version"] == 2
        # 网络结果未知时复用同一 request_id，服务必须恢复同一已提交人工 mutation。
        repeated_a = self._patch_score(
            route["model_a_id"],
            10_000,
            int(initial["a"]["feedback_version"]),
            request_id=request_a,
        )
        assert repeated_a == changed_a
        self.feedback_request_ids.append(request_a)

        request_b = f"req_prd048_runtime_score_b_{self.suffix}"
        changed_b = self._patch_score(
            route["model_b_id"],
            6_500,
            int(initial["b"]["feedback_version"]),
            request_id=request_b,
        )
        assert changed_b.get("changed") is True
        score_b = changed_b.get("model_quality_score")
        assert isinstance(score_b, dict)
        assert score_b["manual_quality_bps"] == 6_500
        assert score_b["feedback_version"] == 2
        self.feedback_request_ids.append(request_b)

        request_conflict = self.control.patch(
            "/model-quality-scores",
            headers=self._headers(self.admin_token, request_a),
            json={
                "model_id": route["model_a_id"],
                "manual_quality_bps": 9_900,
                "expected_feedback_version": 1,
            },
        )
        assert expect_json(request_conflict, 409, request_a)["code"] == (
            "model_quality_score.request_conflict"
        )
        stale_id = f"req_prd048_runtime_score_stale_{self.suffix}"
        stale = self.control.patch(
            "/model-quality-scores",
            headers=self._headers(self.admin_token, stale_id),
            json={
                "model_id": route["model_a_id"],
                "manual_quality_bps": 9_900,
                "expected_feedback_version": 1,
            },
        )
        assert expect_json(stale, 409, stale_id)["code"] == (
            "model_quality_score.version_conflict"
        )
        self._wait_feedback_event(receiver, request_a, route["model_a_id"], 2, 0)
        self._wait_feedback_event(receiver, request_b, route["model_b_id"], 2, 0)
        return {
            "idempotent_retry": True,
            "version_conflict": True,
            "request_conflict": True,
        }

    def _enable_quality_cost(self, route: dict[str, Any]) -> dict[str, Any]:
        """以 Policy CAS 启用 ``quality_cost_v1`` 并验证相同 request 的幂等恢复。"""

        request_id = f"req_prd048_runtime_policy_{self.suffix}"
        payload = {
            "routing_mode": "quality_cost",
            "expected_version": route["routing_policy_version"],
            "quality_cost_config": dict(QUALITY_CONFIG),
        }
        path = f"/model-aliases/{route['alias_id']}/routing-mode"
        changed = expect_json(
            self.control.patch(
                path,
                headers=self._headers(self.admin_token, request_id),
                json=payload,
            ),
            200,
            request_id,
        )
        repeated = expect_json(
            self.control.patch(
                path,
                headers=self._headers(self.admin_token, request_id),
                json=payload,
            ),
            200,
            request_id,
        )
        assert repeated == changed
        policy = changed.get("routing_policy")
        assert isinstance(policy, dict)
        assert policy["routing_policy_id"] == route["routing_policy_id"]
        assert policy["routing_mode"] == "quality_cost"
        assert policy["quality_cost_config"] == QUALITY_CONFIG
        assert int(policy["version"]) == int(route["routing_policy_version"]) + 1
        stale_id = f"req_prd048_runtime_policy_stale_{self.suffix}"
        stale = self.control.patch(
            path,
            headers=self._headers(self.admin_token, stale_id),
            json=payload,
        )
        assert expect_json(stale, 409, stale_id)["code"] == (
            "model_alias.routing_policy_version_conflict"
        )
        return policy

    def _verify_score_rbac(
        self,
        route: dict[str, Any],
        _score_setup: dict[str, bool],
    ) -> dict[str, bool]:
        """验证 developer 可读不可写、viewer 不可读，并确认列表不泄露第二租户模型。

        参数：当前路由与评分准备摘要。返回值：三项后端权限结论。关键约束：developer
        使用超限正文攻击，必须先得到 403，证明权限校验发生在正文解析之前。
        """

        developer_scores = self._list_scores(
            self.role_tokens["developer"],
            label="developer_read",
        )
        assert {
            route["model_a_id"],
            route["model_b_id"],
        }.issubset({str(item["model_id"]) for item in developer_scores})
        assert self.resource_ids["cross_tenant_model_id"] not in {
            str(item["model_id"]) for item in developer_scores
        }

        viewer_id = f"req_prd048_runtime_viewer_read_{self.suffix}"
        viewer = self.control.get(
            "/model-quality-scores?limit=10&offset=0",
            headers=self._headers(self.role_tokens["viewer"], viewer_id),
        )
        assert expect_json(viewer, 403, viewer_id)["code"] == "auth.permission_denied"

        developer_id = f"req_prd048_runtime_developer_write_{self.suffix}"
        developer = self.control.request(
            "PATCH",
            "/model-quality-scores",
            headers={
                **self._headers(self.role_tokens["developer"], developer_id),
                "Content-Type": "application/json",
            },
            content=b"{" + b"x" * 9_000 + b"}",
        )
        assert expect_json(developer, 403, developer_id)["code"] == (
            "auth.permission_denied"
        )

        return {
            "developer_read": True,
            "viewer_read_denied": True,
            "developer_write_denied": True,
        }

    def _verify_cross_tenant_score_isolation(self) -> dict[str, bool]:
        """用真实第二租户 Score 验证当前租户 admin 的 GET/PATCH 均按 404 防枚举。

        参数：无。返回值：GET 隐藏、PATCH 隐藏、Score 未变和客户端未传 tenant_id 四项证据。
        关键约束：目标 model_id 在数据库中真实存在，但 tenant_id 只从 admin Session 恢复；
        URL、正文和 Header 均不得提供第二租户 owner，也不能借 404 修改其人工评分版本。
        """

        model_id = self.resource_ids["cross_tenant_model_id"]
        tenant_id = self.resource_ids["cross_tenant_id"]
        score_id = self.resource_ids["cross_tenant_score_id"]
        assert SAFE_ID_PATTERN.fullmatch(model_id)
        get_request_id = f"req_prd048_runtime_cross_tenant_get_{self.suffix}"
        get_path = f"/model-quality-scores?model_id={model_id}&limit=10&offset=0"
        assert "tenant_id" not in get_path
        hidden_get = self.control.get(
            get_path,
            headers=self._headers(self.admin_token, get_request_id),
        )
        assert expect_json(hidden_get, 404, get_request_id)["code"] == (
            "model_quality_score.not_found"
        )

        patch_request_id = f"req_prd048_runtime_cross_tenant_patch_{self.suffix}"
        patch_payload = {
            "model_id": model_id,
            "manual_quality_bps": 7_777,
            "expected_feedback_version": 1,
        }
        assert set(patch_payload) == {
            "model_id",
            "manual_quality_bps",
            "expected_feedback_version",
        }
        hidden_patch = self.control.patch(
            "/model-quality-scores",
            headers=self._headers(self.admin_token, patch_request_id),
            json=patch_payload,
        )
        assert expect_json(hidden_patch, 404, patch_request_id)["code"] == (
            "model_quality_score.not_found"
        )
        # 404 之后直接在独立库按真实 owner 图复核默认 Score 未被串租户请求推进。
        with closing(self._connect()) as connection:
            score = connection.execute(
                """
                SELECT manual_quality_bps, feedback_version, statistics_version, sample_count
                FROM model_quality_scores
                WHERE tenant_id=? AND model_id=? AND model_quality_score_id=?
                """,
                (tenant_id, model_id, score_id),
            ).fetchone()
        assert score is not None and tuple(score) == (None, 1, 0, 0)
        return {
            "get_hidden": True,
            "patch_hidden": True,
            "score_unchanged": True,
            "tenant_input_absent": True,
        }

    def _gateway_chat(
        self,
        route: dict[str, Any],
        request_id: str,
    ) -> httpx.Response:
        """发送真实非流式 Gateway 请求；Prompt 哨兵只存在于内存和回环网络。"""

        trace_id = request_id.replace("req_", "trc_", 1)
        return self.gateway.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.virtual_key}",
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
            },
            json={
                "model": route["alias_name"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.prompt_sentinel},
                            {"type": "text", "text": " structured"},
                        ],
                    }
                ],
                "max_completion_tokens": 8,
            },
        )

    def _request_log_detail(self, request_id: str, *, label: str) -> dict[str, Any]:
        """由 developer 读取冻结 RequestLog，并扫描返回体中的本轮敏感明文。"""

        detail_request_id = f"req_prd048_runtime_log_{label}_{self.suffix}"
        response = self.control.get(
            f"/request-logs/{request_id}",
            headers=self._headers(self.role_tokens["developer"], detail_request_id),
        )
        body = expect_json(response, 200, detail_request_id)
        cache_control = response.headers.get("cache-control", "")
        assert cache_control == "no-store, private"
        for sensitive in self.sensitive_values():
            if sensitive:
                assert sensitive not in response.text
        detail = body.get("request_log")
        assert isinstance(detail, dict)
        assert isinstance(detail.get("routing_candidates"), list)
        return detail

    def _verify_quality_wins(
        self,
        route: dict[str, Any],
        policy: dict[str, Any],
        receiver: AcceptanceServer,
    ) -> dict[str, int]:
        """证明昂贵高质量 A 凭综合分击败便宜 B，并冻结选择前统计版本。

        参数：路由图、已提交 Policy 与回环服务。返回值：冻结和当前统计版本。关键约束：
        RequestLog/Candidate 必须使用不可变事实，不能在成功后 JOIN 当前 Score 回算历史。
        """

        request_id = f"req_prd048_runtime_quality_wins_{self.suffix}"
        self.request_ids["quality_wins"] = request_id
        before_calls = len(receiver.provider_calls())
        response = self._gateway_chat(route, request_id)
        body = expect_json(
            response,
            200,
            request_id,
            request_id.replace("req_", "trc_", 1),
        )
        # Response 仅在当前栈帧断言，不进入返回摘要或状态文件。
        assert body["choices"][0]["message"]["content"] == "selected-a"
        calls = receiver.provider_calls()[before_calls:]
        assert calls == [{"provider": "a", "model": "prd048-quality-a"}]

        detail = self._request_log_detail(request_id, label="quality_wins")
        decision = detail.get("routing_decision")
        candidates = detail.get("routing_candidates")
        assert isinstance(decision, dict) and isinstance(candidates, list)
        assert decision["routing_policy_id"] == route["routing_policy_id"]
        assert decision["routing_policy_version"] == policy["version"]
        assert decision["routing_mode"] == "quality_cost"
        assert decision["selected_model_id"] == route["model_a_id"]
        assert decision["decision_reason"] == "routing.quality_cost_highest_score"
        assert decision["quality_formula_version"] == "quality_cost_v1"
        assert decision["quality_scoring_performed"] is True
        assert decision["quality_as_of"] == decision["price_as_of"]
        for field, expected in QUALITY_CONFIG.items():
            assert decision[field] == expected
        assert len(candidates) == 2
        candidate_a = next(item for item in candidates if item["model_id"] == route["model_a_id"])
        candidate_b = next(item for item in candidates if item["model_id"] == route["model_b_id"])
        assert candidate_a["status"] == "selected" and candidate_a["rank"] == 1
        assert candidate_b["status"] == "standby" and candidate_b["rank"] == 2
        assert candidate_a["manual_quality_bps"] == 10_000
        assert candidate_b["manual_quality_bps"] == 6_500
        assert candidate_a["quality_statistics_version"] == 0
        assert candidate_b["quality_statistics_version"] == 0
        assert candidate_a["quality_feedback_version"] == 2
        assert candidate_b["quality_feedback_version"] == 2
        assert int(candidate_a["weighted_score_bps"]) > int(candidate_b["weighted_score_bps"])
        assert Decimal(candidate_a["estimated_cost_amount"]) > Decimal(
            candidate_b["estimated_cost_amount"]
        )

        attempt = self._attempt_summary(request_id)
        assert attempt == {
            "attempt_count": 1,
            "succeeded_count": 1,
            "model_id": route["model_a_id"],
        }
        current_a = self._wait_for_statistics(route["model_a_id"], minimum_version=1)
        current_b = self._score_by_model(
            self._list_scores(
                self.role_tokens["developer"],
                label="score_after_b",
                model_id=route["model_b_id"],
            ),
            route["model_b_id"],
        )
        assert current_a["statistics_version"] == 1
        assert current_a["sample_count"] == 1
        assert current_a["success_count"] == 1
        assert current_a["error_count"] == 0
        assert current_b["statistics_version"] == 0
        assert candidate_a["quality_statistics_version"] < current_a["statistics_version"]
        return {
            "frozen_statistics_version": int(candidate_a["quality_statistics_version"]),
            "current_statistics_version": int(current_a["statistics_version"]),
        }

    def _wait_for_statistics(
        self,
        model_id: str,
        *,
        minimum_version: int,
    ) -> dict[str, Any]:
        """有界等待 Gateway 自动统计可见，避免用固定 sleep 制造偶发验收。"""

        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            scores = self._list_scores(
                self.role_tokens["developer"],
                label="score_after",
                model_id=model_id,
            )
            last = self._score_by_model(scores, model_id)
            if int(last["statistics_version"]) >= minimum_version:
                return last
            time.sleep(0.1)
        actual = None if last is None else last.get("statistics_version")
        # 超时诊断只包含整数版本，不输出 Score 响应、模型配置或调用正文。
        raise AssertionError(
            f"quality statistics did not advance: actual={actual}, expected={minimum_version}"
        )

    def _attempt_summary(self, request_id: str) -> dict[str, Any]:
        """按 tenant+request 查询上游 attempt，返回不含错误正文的计数摘要。"""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT status, model_id FROM routing_attempts
                WHERE tenant_id=? AND request_id=? ORDER BY attempt_sequence
                """,
                (self.resource_ids["main_tenant_id"], request_id),
            ).fetchall()
        return {
            "attempt_count": len(rows),
            "succeeded_count": sum(row["status"] == "succeeded" for row in rows),
            "model_id": rows[-1]["model_id"] if rows else None,
        }

    def _verify_all_low(
        self,
        route: dict[str, Any],
        receiver: AcceptanceServer,
    ) -> dict[str, int]:
        """把两模型降到门槛下，验证 no-route、零 Provider 调用和零 attempt。

        参数：隔离路由和回环服务。返回值：attempt/Provider 计数。关键约束：取证后恢复
        浏览器验收所需的 A=10000/B=6500，但失败 Decision/候选必须保留低质冻结事实。
        """

        low_a_id = f"req_prd048_runtime_low_a_{self.suffix}"
        low_a = self._patch_score(
            route["model_a_id"],
            5_000,
            2,
            request_id=low_a_id,
        )
        assert low_a["model_quality_score"]["feedback_version"] == 3
        self.feedback_request_ids.append(low_a_id)
        low_b_id = f"req_prd048_runtime_low_b_{self.suffix}"
        low_b = self._patch_score(
            route["model_b_id"],
            5_500,
            2,
            request_id=low_b_id,
        )
        assert low_b["model_quality_score"]["feedback_version"] == 3
        self.feedback_request_ids.append(low_b_id)
        self._wait_feedback_event(receiver, low_a_id, route["model_a_id"], 3, 1)
        self._wait_feedback_event(receiver, low_b_id, route["model_b_id"], 3, 0)

        request_id = f"req_prd048_runtime_all_low_{self.suffix}"
        self.request_ids["all_low"] = request_id
        before_calls = len(receiver.provider_calls())
        response = self._gateway_chat(route, request_id)
        body = expect_json(
            response,
            503,
            request_id,
            request_id.replace("req_", "trc_", 1),
        )
        assert body["code"] == "gateway.no_route_available"
        assert len(receiver.provider_calls()) == before_calls

        detail = self._request_log_detail(request_id, label="all_low")
        log = detail.get("log")
        decision = detail.get("routing_decision")
        candidates = detail.get("routing_candidates")
        assert isinstance(log, dict) and isinstance(decision, dict)
        assert isinstance(candidates, list) and len(candidates) == 2
        assert log["status"] == "failed"
        assert log["error_code"] == "gateway.no_route_available"
        assert int(log["routing_attempt_count"]) == 0
        assert decision["decision_status"] == "failed"
        assert decision["decision_reason"] == "routing.no_available_candidate"
        assert decision["error_code"] == "gateway.no_route_available"
        assert decision["quality_scoring_performed"] is False
        assert decision["price_as_of"] is None
        assert all(item["status"] == "rejected" for item in candidates)
        assert all(item["rank"] is None for item in candidates)
        assert all(
            item["reason_code"] == "routing.candidate.quality_below_floor"
            for item in candidates
        )
        assert all(item["weighted_score_bps"] is None for item in candidates)
        attempt = self._attempt_summary(request_id)
        assert attempt == {"attempt_count": 0, "succeeded_count": 0, "model_id": None}

        restore_a_id = f"req_prd048_runtime_restore_a_{self.suffix}"
        restored_a = self._patch_score(
            route["model_a_id"],
            10_000,
            3,
            request_id=restore_a_id,
        )
        assert restored_a["model_quality_score"]["feedback_version"] == 4
        self.feedback_request_ids.append(restore_a_id)
        restore_b_id = f"req_prd048_runtime_restore_b_{self.suffix}"
        restored_b = self._patch_score(
            route["model_b_id"],
            6_500,
            3,
            request_id=restore_b_id,
        )
        assert restored_b["model_quality_score"]["feedback_version"] == 4
        self.feedback_request_ids.append(restore_b_id)
        self._wait_feedback_event(receiver, restore_a_id, route["model_a_id"], 4, 1)
        self._wait_feedback_event(receiver, restore_b_id, route["model_b_id"], 4, 0)
        return {"attempt_count": 0, "provider_call_count": 0}

    def _wait_feedback_event(
        self,
        receiver: AcceptanceServer,
        request_id: str,
        model_id: str,
        feedback_version: int,
        statistics_version: int,
    ) -> dict[str, Any]:
        """有界等待指定评分事件并核对 HMAC、版本、触发器和隐私字段。"""

        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            event = next(
                (
                    item
                    for item in receiver.webhook_events()
                    if item.get("request_id") == request_id
                ),
                None,
            )
            if event is not None:
                assert event["event_type"] == "model.feedback_recorded"
                assert str(event["event_id"]).startswith("mqe_")
                assert str(event["object_id"]).startswith("mqs_")
                assert event["model_id"] == model_id
                assert event["feedback_version"] == feedback_version
                assert event["statistics_version"] == statistics_version
                assert event["trigger"] == "manual_score_updated"
                assert event["signature_valid"] is True
                assert event["privacy_ok"] is True
                return event
            time.sleep(0.1)
        # 事件超时只报告安全 request_id，不复制可能含治理字段的原始信封。
        raise AssertionError(f"model.feedback_recorded not received for {request_id}")

    def _verify_final_score_events(
        self,
        route: dict[str, Any],
        receiver: AcceptanceServer,
    ) -> int:
        """核对六次真实评分变化各有唯一事件，失败/CAS 重试没有制造额外事件。"""

        assert len(self.feedback_request_ids) == 6
        captured = receiver.webhook_events()
        for request_id in self.feedback_request_ids:
            matches = [item for item in captured if item.get("request_id") == request_id]
            assert len(matches) == 1
            assert matches[0]["signature_valid"] is True
            assert matches[0]["privacy_ok"] is True
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT request_id FROM model_quality_score_events
                WHERE tenant_id=? AND model_id IN (?, ?)
                ORDER BY created_at, request_id
                """,
                (
                    self.resource_ids["main_tenant_id"],
                    route["model_a_id"],
                    route["model_b_id"],
                ),
            ).fetchall()
        assert len(rows) == 6
        assert {str(row["request_id"]) for row in rows} == set(self.feedback_request_ids)
        final_scores = self._list_scores(
            self.role_tokens["developer"],
            label="score_final",
        )
        final_a = self._score_by_model(final_scores, route["model_a_id"])
        final_b = self._score_by_model(final_scores, route["model_b_id"])
        assert (final_a["manual_quality_bps"], final_a["feedback_version"]) == (10_000, 4)
        assert (final_b["manual_quality_bps"], final_b["feedback_version"]) == (6_500, 4)
        return 6

    def _disable_webhook_after_evidence(self) -> None:
        """在接收器关闭前禁用临时 Endpoint，避免浏览器阶段向失效端口投递。

        返回值：无。关键约束：只按 tenant+稳定 ID 更新当前 Endpoint；评分事件与投递证据
        保持不可变，最终 cleanup 再次执行时应得到零 Webhook 变更。
        """

        with closing(self._connect()) as connection, connection:
            changed = _update_one(
                connection,
                """
                UPDATE webhook_endpoints SET status='disabled', updated_at=?
                WHERE tenant_id=? AND webhook_endpoint_id=? AND status!='disabled'
                """,
                (
                    utc_now(),
                    self.resource_ids["main_tenant_id"],
                    self.resource_ids["webhook_endpoint_id"],
                ),
                label="webhook evidence",
            )
            assert changed == 1
            row = connection.execute(
                """
                SELECT status FROM webhook_endpoints
                WHERE tenant_id=? AND webhook_endpoint_id=?
                """,
                (
                    self.resource_ids["main_tenant_id"],
                    self.resource_ids["webhook_endpoint_id"],
                ),
            ).fetchone()
        assert row is not None and row["status"] == "disabled"

    def _verify_privacy(self) -> dict[str, int]:
        """扫描全部业务表文本面，证明本轮敏感明文未进入 SQLite。

        返回值：Prompt 和全部已知凭据命中数。关键约束：查询参数绑定敏感值，SQL 与断言
        失败消息都不拼接明文；合法的哈希、密文和 key_prefix 不会与原值相等。
        """

        sensitive_values = [value for value in self.sensitive_values() if value]
        prompt_hits = 0
        sensitive_hits = 0
        with closing(self._connect()) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table in tables:
                columns = [
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA table_info({quote_identifier(table)})"
                    ).fetchall()
                ]
                if not columns:
                    continue
                predicates = " OR ".join(
                    f"instr(CAST({quote_identifier(column)} AS TEXT), ?) > 0"
                    for column in columns
                )
                query = f"SELECT COUNT(*) FROM {quote_identifier(table)} WHERE {predicates}"
                prompt_hits += int(
                    connection.execute(
                        query,
                        tuple(self.prompt_sentinel for _ in columns),
                    ).fetchone()[0]
                )
                for value in sensitive_values:
                    sensitive_hits += int(
                        connection.execute(
                            query,
                            tuple(value for _ in columns),
                        ).fetchone()[0]
                    )
        assert prompt_hits == 0
        assert sensitive_hits == 0
        return {
            "database_prompt_hits": prompt_hits,
            "database_sensitive_hits": sensitive_hits,
        }

    def _connect(self) -> sqlite3.Connection:
        """连接显式独立验收库并启用外键，禁止退回开发库或内存库。"""

        connection = sqlite3.connect(self.args.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _artifact_root() -> Path:
    """返回当前仓库内 PRD-048 验收制品根目录的规范绝对路径。"""

    return (Path.cwd().resolve() / "artifacts" / "prd048").resolve()


def _path_inside(path: Path, root: Path, *, label: str) -> Path:
    """规范化路径并强制其位于指定制品根目录下。

    参数：待校验路径、根目录和安全标签。返回值：绝对路径。关键约束：路径本身不能等于
    根目录，避免状态文件或数据库参数指向目录后触发非预期替换。
    """

    resolved = path.resolve()
    assert root in resolved.parents, f"{label} must be inside artifacts/prd048"
    return resolved


def load_cleanup_state(state_path: Path) -> tuple[dict[str, Any], Path]:
    """读取并严格校验可用于终清理的脱敏状态。

    参数：``runtime-state.json`` 路径。返回值：状态对象与同目录验收库路径。关键约束：
    数据库文件只允许使用状态中的 basename，不能借由 JSON 注入绝对路径或 ``..``。
    """

    root = _artifact_root()
    resolved_state = _path_inside(state_path, root, label="cleanup state")
    assert resolved_state.is_file(), "cleanup state does not exist"
    try:
        state = json.loads(resolved_state.read_text(encoding="utf-8").lstrip("\ufeff"))
    # 解析失败不能尝试从残缺 JSON 猜测数据库或资源 ID，直接失败关闭。
    except (OSError, json.JSONDecodeError) as exc:
        # 固定错误消息不回显状态正文或本机绝对路径。
        raise AssertionError("cleanup state is not valid JSON") from exc
    assert isinstance(state, dict) and state.get("status") == "passed"
    assert state.get("prd_id") == "PRD-048"
    suffix = state.get("suffix")
    assert isinstance(suffix, str) and SAFE_SUFFIX_PATTERN.fullmatch(suffix)
    database_file = state.get("database_file")
    assert isinstance(database_file, str) and Path(database_file).name == database_file
    assert database_file.endswith(".db")
    database_path = _path_inside(
        resolved_state.parent / database_file,
        root,
        label="cleanup database",
    )
    assert database_path.is_file(), "cleanup database does not exist"
    _safe_resource_ids(state.get("resource_ids"), require_complete=True)
    assert_redacted_state(state, [])
    return state, database_path


def cleanup_from_state(state_path: Path) -> dict[str, Any]:
    """依据脱敏状态执行可重复终清理，并原子写回验证结果。

    参数：运行状态路径。返回值：更新后的状态对象。关键约束：第二次及后续调用必须得到
    零变更但继续 ``verified=true``，不能因资源已经禁用而报错或扩大扫描范围。
    """

    state, database_path = load_cleanup_state(state_path)
    cleanup = cleanup_runtime_resources(
        database_path,
        state["resource_ids"],
        require_complete=True,
    )
    state["lifecycle"] = {
        "retained_for_browser": False,
        "cleanup_completed": True,
        "cleanup_verified": cleanup["verified"],
        "cleanup_counts": {
            key: value for key, value in cleanup.items() if key != "verified"
        },
        "cleanup_run_at": utc_now(),
    }
    assert_redacted_state(state, [])
    write_state_atomically(state_path.resolve(), state)
    return state


def execute_runtime(
    args: argparse.Namespace,
    *,
    acceptance_factory: Callable[[argparse.Namespace], RuntimeAcceptance] = RuntimeAcceptance,
    server_factory: Callable[[str], AbstractContextManager[AcceptanceServer]] = (
        RunningAcceptanceServer
    ),
) -> dict[str, Any]:
    """执行一次 runtime、应用默认/保留生命周期并原子输出状态。

    参数：已校验参数，以及供无服务单测注入的两个工厂。返回值：最终脱敏状态。关键约束：
    仅当 ``retain_for_browser`` 为真且状态已经成功替换时跳过 finally 清理；任何更早失败
    都按部分资源账本收口。
    """

    args.state_output.unlink(missing_ok=True)
    acceptance = acceptance_factory(args)
    state_written = False
    cleanup_done = False
    try:
        with server_factory(acceptance.prompt_sentinel) as receiver:
            state = acceptance.run(receiver)
        assert state.get("status") == "passed"
        assert state.get("database_file") == args.database_path.name
        if args.retain_for_browser:
            state["lifecycle"] = {
                "retained_for_browser": True,
                "cleanup_completed": False,
                "cleanup_verified": False,
            }
        else:
            cleanup = acceptance.cleanup(require_complete=True)
            cleanup_done = True
            state["lifecycle"] = {
                "retained_for_browser": False,
                "cleanup_completed": True,
                "cleanup_verified": cleanup["verified"],
                "cleanup_counts": {
                    key: value for key, value in cleanup.items() if key != "verified"
                },
                "cleanup_run_at": utc_now(),
            }
        assert_redacted_state(state, acceptance.sensitive_values())
        write_state_atomically(args.state_output, state)
        state_written = True
        return state
    finally:
        # 显式保留只有在完整成功状态可供 Browser 消费后才成立；写盘失败仍必须立即清理。
        if not cleanup_done and not (args.retain_for_browser and state_written):
            acceptance.cleanup(require_complete=False)
        acceptance.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析普通运行或 ``--cleanup-state`` 模式并执行本地路径/凭据约束。

    参数：可选 argv，省略时读取进程命令行。返回值：规范化 Namespace。关键约束：
    浏览器保留模式必须显式提供三个账号密码；密码不会写入状态或 argparse 错误文本。
    """

    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup-state", type=Path)
    parser.add_argument("--retain-for-browser", action="store_true")
    parser.add_argument(
        "--control-url",
        default=os.getenv("METAHUB_CONTROL_API_URL", "http://127.0.0.1:8001"),
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("METAHUB_GATEWAY_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--console-url",
        default=os.getenv("METAHUB_CONSOLE_URL", "http://127.0.0.1:5173"),
    )
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--state-output", type=Path)
    parser.add_argument(
        "--suffix",
        default=datetime.now(UTC).strftime("%Y%m%dt%H%M%S").lower()
        + secrets.token_hex(3),
    )
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument(
        "--admin-password",
        default=os.getenv("METAHUB_PRD048_ADMIN_PASSWORD", ""),
    )
    parser.add_argument(
        "--developer-password",
        default=os.getenv("METAHUB_PRD048_DEVELOPER_PASSWORD", ""),
    )
    parser.add_argument(
        "--viewer-password",
        default=os.getenv("METAHUB_PRD048_VIEWER_PASSWORD", ""),
    )
    args = parser.parse_args(argv)
    root = _artifact_root()

    if args.cleanup_state is not None:
        assert not args.retain_for_browser, (
            "--cleanup-state cannot be combined with --retain-for-browser"
        )
        args.cleanup_state = _path_inside(
            args.cleanup_state,
            root,
            label="cleanup state",
        )
        # 清理模式完全依赖脱敏状态，不读取或要求任何账号密码和服务 URL。
        return args

    assert args.database_path is not None, "--database-path is required"
    assert args.state_output is not None, "--state-output is required"
    assert isinstance(args.suffix, str) and SAFE_SUFFIX_PATTERN.fullmatch(args.suffix), (
        "suffix must contain 8-32 lowercase letters or digits"
    )
    for label in ("control_url", "gateway_url", "console_url"):
        assert_loopback_url(getattr(args, label), label)
    database_path = _path_inside(args.database_path, root, label="acceptance database")
    state_output = _path_inside(args.state_output, root, label="state output")
    assert database_path.is_file(), "acceptance database does not exist"
    assert database_path.suffix == ".db", "acceptance database must use .db suffix"
    assert state_output.suffix == ".json", "state output must use .json suffix"
    assert database_path.parent == state_output.parent, (
        "database and state output must share one isolated batch directory"
    )
    assert database_path != state_output
    assert args.admin_password, "METAHUB_PRD048_ADMIN_PASSWORD or --admin-password is required"
    if args.retain_for_browser:
        assert args.developer_password and args.viewer_password, (
            "browser retention requires developer/viewer passwords from process environment"
        )
    else:
        # 默认运行无需把临时密码交给后续进程；缺失时在内存生成高熵值并在 close 清空。
        args.developer_password = args.developer_password or (
            f"Prd048Developer!{secrets.token_urlsafe(18)}"
        )
        args.viewer_password = args.viewer_password or (
            f"Prd048Viewer!{secrets.token_urlsafe(18)}"
        )
    args.database_path = database_path
    args.state_output = state_output
    return args


def main(argv: list[str] | None = None) -> int:
    """运行真实验收或终清理，并只把脱敏 JSON 摘要输出到终端。"""

    args = parse_args(argv)
    if args.cleanup_state is not None:
        state = cleanup_from_state(args.cleanup_state)
    else:
        state = execute_runtime(args)
    encoded = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
