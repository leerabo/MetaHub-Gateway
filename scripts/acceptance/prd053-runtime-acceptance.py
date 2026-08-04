"""PRD-053 成本异常检测基础的真实运行验收器。

脚本只连接调用方已经启动的回环 Control API 与 Gateway，不启动业务服务、不执行迁移，
也不访问开发数据库。运行库必须位于 ``artifacts/prd053/<batch>/runtime.db`` 且已应用
0043。验收器经公开 API 创建角色、治理资源和回环假上游；正式成本只能由受控
``usage_ledger`` 写入经数据库触发器投影，再调用内部 detector 生成异常。

最终证据只保留安全资源 ID、HTTP 状态和布尔结论。管理员口令、Session Token、
Virtual Key、Provider Key、Webhook Secret、Prompt 与 Response 均只驻留当前进程内存，
不会写入证据、URL、日志或异常正文。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit

import httpx
from metahub_control.cost_anomalies.outbox import drain_cost_anomaly_event_once
from metahub_control.cost_anomalies.scheduler import run_cost_anomaly_detection_once
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.webhooks import (
    build_webhook_event,
    verify_webhook_signature,
)


# 下列常量共同定义验收器的网络边界、证据隐私合同、正式计费来源和运行库结构门禁。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")
CANONICAL_CNY_PATTERN = re.compile(r"^(0|[1-9][0-9]{0,14})\.[0-9]{6}$")
ANOMALY_ID_PATTERN = re.compile(r"^can_[0-9a-f]{32}$")
EVENT_ID_PATTERN = re.compile(r"^caevt_[0-9a-f]{32}$")

# 证据键和值同时执行隐私扫描；内部 ID 可以保留，但任何明文槽位都必须拒绝。
FORBIDDEN_EVIDENCE_KEY_PATTERN = re.compile(
    r"password|access_token|authorization|plain_value|prompt|response|secret",
    re.IGNORECASE,
)
FORBIDDEN_EVIDENCE_TEXT_PATTERN = re.compile(
    r"Bearer\s+|mhv_[A-Za-z0-9_-]{8,}|whsec_[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)
FORBIDDEN_WEBHOOK_KEYS = frozenset(
    {
        "authorization",
        "password",
        "plain_value",
        "prompt",
        "provider_key",
        "response",
        "secret",
        "token",
    }
)
WEBHOOK_ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
)
COST_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "anomaly_id",
        "rule_type",
        "severity",
        "status",
        "environment_id",
        "scope_type",
        "scope_id",
        "window_start",
        "window_end",
        "current_cost_micros",
        "baseline_total_micros",
        "observed_ratio_millis",
        "baseline_bucket_count",
        "threshold_multiplier_millis",
        "minimum_cost_micros",
        "source_entry_count",
        "estimated_entry_count",
        "affected_virtual_key_count",
        "recommended_action",
        "detection_version",
    }
)

FORMAL_USAGE_PRICE_SNAPSHOT_SOURCE = "finops_legacy_backfill"
FORMAL_USAGE_PRICE_SCHEMA_VERSION = "legacy-unversioned-v1"
PUBLIC_MODEL_ALIAS = "prd053-runtime-chat"
UPSTREAM_MODEL_ID = "prd053-runtime-model"
MAX_HTTP_BODY_BYTES = 1024 * 1024

REQUIRED_RUNTIME_TABLES = frozenset(
    {
        "applications",
        "cost_anomalies",
        "cost_anomaly_actions",
        "cost_anomaly_detection_runs",
        "environments",
        "finops_ledger_entries",
        "gateway_request_claims",
        "projects",
        "request_logs",
        "routing_attempts",
        "schema_migrations",
        "sessions",
        "temporary_rate_limit_overrides",
        "usage_ledger",
        "virtual_keys",
        "webhook_deliveries",
        "webhook_endpoints",
    }
)
REQUIRED_RUNTIME_TRIGGERS = frozenset(
    {
        "trg_usage_ledger_finops_projection_insert_prd051",
        "trg_cost_anomalies_owner_insert_prd053",
        "trg_cost_anomaly_actions_owner_insert_prd053",
        "trg_temporary_rate_limit_overrides_owner_insert_prd053",
    }
)


class AcceptanceFailure(RuntimeError):
    """只暴露固定 stage/code 的脱敏失败，不传播 HTTP、SQLite 或输入正文。"""

    def __init__(self, stage: str, code: str) -> None:
        """构造固定失败；调用点不得把外部异常文本拼入两个字段。"""

        super().__init__(f"prd053_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """真实运行验收配置；管理员口令只存在当前 Python 进程。"""

    control_origin: str
    gateway_origin: str
    artifact_directory: Path
    database_path: Path
    tenant_slug: str
    admin_email: str
    admin_password: str
    timeout_seconds: float


@dataclass
class SessionCredential:
    """短生命周期 Session 的内存状态，最终只公开撤销布尔结论。"""

    role: str
    tenant_id: str
    user_id: str
    session_id: str
    access_value: str
    password: str


def _fail(stage: str, code: str) -> None:
    """以固定阶段和错误码终止当前验收。

    参数只能使用调用点声明的脱敏标识；函数不返回，并禁止把外部异常、URL 或敏感正文
    拼入错误消息，避免失败路径反向泄露凭据和业务内容。
    """

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行布尔门禁，失败时只携带调用点声明的固定信息。"""

    if not condition:
        _fail(stage, code)


def _safe_identifier(value: object, stage: str, code: str) -> str:
    """读取可公开 ID，拒绝空值、超长值和可能承载正文的字符。"""

    _require(isinstance(value, str), stage, code)
    _require(SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None, stage, code)
    return value


def normalize_loopback_origin(value: str, *, stage: str) -> str:
    """规范化无凭据、无路径且显式端口的 HTTP 回环 origin。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 解析异常可能携带调用方原始输入，因此统一折叠为固定配置错误。
    except (TypeError, ValueError):
        _fail(stage, "invalid_url")
    hostname = (parsed.hostname or "").lower()
    _require(parsed.scheme == "http", stage, "scheme_not_http")
    _require(hostname in LOOPBACK_HOSTS, stage, "host_not_loopback")
    _require(parsed.username is None and parsed.password is None, stage, "url_credentials")
    _require(parsed.path in {"", "/"}, stage, "url_path")
    _require(not parsed.query and not parsed.fragment, stage, "url_suffix")
    _require(port is not None, stage, "port_missing")
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{host}:{port}"


def _workspace_root() -> Path:
    """从脚本物理位置恢复 MetaHub 根，避免依赖调用方当前目录。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def resolve_artifact_directory(
    value: str | Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """把证据目录限制为 ``artifacts/prd053`` 下恰好一个批次层级。"""

    root = (workspace_root or _workspace_root()).resolve()
    artifact_root = (root / "artifacts" / "prd053").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(artifact_root)
    # 越界路径不得通过异常正文暴露主机目录，只返回稳定的验收错误码。
    except ValueError:
        _fail("config.artifact_directory", "outside_workspace")
    _require(len(relative.parts) == 1, "config.artifact_directory", "batch_depth")
    _require(
        BATCH_NAME_PATTERN.fullmatch(relative.name) is not None,
        "config.artifact_directory",
        "batch_name",
    )
    return candidate


def sqlite_url_for_path(path: Path) -> str:
    """把绝对运行库路径转换为无查询参数的 canonical SQLite URL。"""

    resolved = path.resolve()
    _require(resolved.is_absolute(), "config.database_url", "path_not_absolute")
    return f"sqlite:///{resolved.as_posix()}"


def resolve_runtime_database(value: str, *, artifact_directory: Path) -> Path:
    """解析并绑定当前验收批次唯一允许使用的 SQLite 运行库。

    ``value`` 必须是无查询参数的绝对 SQLite URL，``artifact_directory`` 是已校验的批次目录；
    返回规范化 ``runtime.db`` 路径，拒绝开发库、符号链接和批次目录外的任何数据库。
    """

    try:
        parsed = urlsplit(str(value))
    # 数据库 URL 解析失败时不回显路径，避免把本机目录写入终端或证据。
    except (TypeError, ValueError):
        _fail("config.database_url", "invalid_url")
    _require(parsed.scheme == "sqlite", "config.database_url", "scheme")
    _require(not parsed.netloc, "config.database_url", "authority")
    _require(not parsed.query and not parsed.fragment, "config.database_url", "url_suffix")
    _require(parsed.path not in {"", "/"}, "config.database_url", "path_missing")
    _require("%" not in parsed.path and "\\" not in parsed.path, "config.database_url", "noncanonical")
    raw_path = parsed.path
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    source = Path(raw_path)
    _require(source.is_absolute(), "config.database_url", "path_not_absolute")
    candidate = source.resolve()
    expected = (artifact_directory / "runtime.db").resolve()
    _require(candidate == expected, "config.database_url", "not_batch_runtime")
    _require(not source.is_symlink(), "config.database_url", "database_symlink")
    return candidate


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeConfig:
    """解析回环地址和批次路径，并主动移除环境中的管理员口令。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(
        description="PRD-053 成本异常真实运行验收",
        epilog=(
            "Gateway 由调用方预先启动；隔离验收可在 listen 前向 app.state 注入仓库现有 "
            "InMemoryRateLimitStore，不要求外部 Redis。"
        ),
    )
    parser.add_argument(
        "--control-url",
        default=env.get("METAHUB_PRD053_CONTROL_URL", "http://localhost:8001"),
    )
    parser.add_argument(
        "--gateway-url",
        default=env.get("METAHUB_PRD053_GATEWAY_URL", "http://localhost:8000"),
    )
    parser.add_argument("--artifact-dir", default=env.get("METAHUB_PRD053_ARTIFACT_DIR", ""))
    parser.add_argument("--database-url", default=env.get("METAHUB_PRD053_DATABASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    specific_password = env.pop("METAHUB_PRD053_ADMIN_PASSWORD", "")
    fallback_password = env.pop("METAHUB_ADMIN_PASSWORD", "")
    admin_password = specific_password or fallback_password
    _require(bool(admin_password), "config.credentials", "admin_password_missing")
    _require(bool(args.artifact_dir), "config.artifact_directory", "missing")
    _require(bool(args.database_url), "config.database_url", "missing")
    _require(1.0 <= args.timeout <= 120.0, "config.timeout", "out_of_range")

    artifact_directory = resolve_artifact_directory(args.artifact_dir)
    database_path = resolve_runtime_database(
        args.database_url,
        artifact_directory=artifact_directory,
    )
    control_origin = normalize_loopback_origin(args.control_url, stage="config.control_origin")
    gateway_origin = normalize_loopback_origin(args.gateway_url, stage="config.gateway_origin")
    _require(control_origin != gateway_origin, "config.origins", "must_be_distinct")
    tenant_slug = env.get("METAHUB_PRD053_TENANT_SLUG") or env.get(
        "METAHUB_TENANT_SLUG", "default"
    )
    admin_email = env.get("METAHUB_PRD053_ADMIN_EMAIL") or env.get(
        "METAHUB_ADMIN_EMAIL", "admin@metahub.local"
    )
    _require(bool(tenant_slug.strip()), "config.credentials", "tenant_slug_missing")
    _require("@" in admin_email, "config.credentials", "admin_email_invalid")
    return RuntimeConfig(
        control_origin=control_origin,
        gateway_origin=gateway_origin,
        artifact_directory=artifact_directory,
        database_path=database_path,
        tenant_slug=tenant_slug.strip(),
        admin_email=admin_email.strip().lower(),
        admin_password=admin_password,
        timeout_seconds=args.timeout,
    )


def _request_identity(action: str, suffix: str) -> tuple[str, str]:
    """为真实调用生成只含安全标识的 request_id 与 trace_id。"""

    request_id = f"req_prd053_accept_{action}_{suffix}"
    trace_id = f"trc_prd053_accept_{action}_{suffix}"
    _require(len(request_id) <= 128, "request.identity", "request_id_too_long")
    _require(len(trace_id) <= 128, "request.identity", "trace_id_too_long")
    _safe_identifier(request_id, "request.identity", "request_id_invalid")
    _safe_identifier(trace_id, "request.identity", "trace_id_invalid")
    return request_id, trace_id


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """读取 JSON object；解码失败不能回显代理页或上游正文。"""

    try:
        payload = response.json()
    # 响应可能是代理页或上游正文，解码失败时只能返回固定错误而不能回显内容。
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _fail(stage, "response_not_json")
    _require(isinstance(payload, dict), stage, "response_not_object")
    return payload


def verify_response_contract(
    response: httpx.Response,
    *,
    stage: str,
    expected_status: int,
    request_id: str,
    trace_id: str,
    expected_code: str | None = None,
    require_no_store: bool = False,
    require_body_chain: bool = True,
) -> dict[str, Any]:
    """核对状态、统一链路、错误码和成本接口 ``no-store``。

    Control API 与统一错误体默认要求 Header/正文双重链路；OpenAI-compatible 成功正文
    必须保持上游协议，只在调用点显式关闭正文链路要求，响应 Header 仍不可缺失。
    """

    _require(response.status_code == expected_status, stage, "status_mismatch")
    payload = _json_object(response, stage)
    _require(response.headers.get("x-request-id") == request_id, stage, "request_header")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_header")
    if require_body_chain:
        _require(payload.get("request_id") == request_id, stage, "request_body")
        _require(payload.get("trace_id") == trace_id, stage, "trace_body")
    if expected_code is not None:
        _require(payload.get("code") == expected_code, stage, "error_code")
    if require_no_store:
        directives = {
            item.strip().lower()
            for item in response.headers.get("cache-control", "").split(",")
            if item.strip()
        }
        _require("no-store" in directives, stage, "cache_control")
    return payload


def _safe_http_call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    stage: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """只向客户端预绑定 origin 发送相对路径，并折叠全部传输异常。"""

    _require(path.startswith("/") and not path.startswith("//"), stage, "path_invalid")
    _require("\r" not in path and "\n" not in path, stage, "path_invalid")
    try:
        return client.request(method, path, headers=dict(headers), json=body)
    # 传输异常可能包含 URL 或认证头上下文，统一折叠后才能进入验收输出。
    except httpx.HTTPError:
        _fail(stage, "transport_failed")


def _public_http_evidence(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    include_code: bool = False,
) -> dict[str, Any]:
    """把 HTTP 结果压缩为不含 URL、Header 和正文的安全证据。"""

    # OpenAI-compatible 成功正文不注入 MetaHub 扩展字段；证据复用已由上层严格核验的 Header。
    result: dict[str, Any] = {
        "status": response.status_code,
        "request_id": str(payload.get("request_id") or response.headers.get("x-request-id", "")),
        "trace_id": str(payload.get("trace_id") or response.headers.get("x-trace-id", "")),
    }
    if include_code:
        result["code"] = str(payload.get("code", ""))
    return result


def align_detection_window(value: datetime) -> datetime:
    """把带时区时刻向下对齐到 UTC 五分钟桶。"""

    _require(isinstance(value, datetime) and value.tzinfo is not None, "detector.window", "clock")
    current = value.astimezone(UTC)
    epoch = int(current.timestamp())
    return datetime.fromtimestamp(epoch - epoch % 300, tz=UTC)


def _contains_forbidden_webhook_key(value: object) -> bool:
    """递归检查完整 Webhook 信封，不能先投影后再做隐私判断。"""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                if normalized in FORBIDDEN_WEBHOOK_KEYS:
                    return True
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return False


def validate_cost_anomaly_webhook(
    body: bytes,
    *,
    signature: str,
    signing_secret: str,
    now_seconds: int | None = None,
) -> dict[str, Any]:
    """验签并校验精确 8 字段信封与 20 字段成本异常 payload。"""

    _require(0 < len(body) <= MAX_HTTP_BODY_BYTES, "webhook.receive", "body_size")
    try:
        envelope = json.loads(body.decode("utf-8", errors="strict"))
    # 原始 Webhook 正文属于不可信输入，解析失败不得把正文或解码细节带入错误。
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("webhook.receive", "invalid_json")
    _require(isinstance(envelope, dict), "webhook.receive", "envelope_type")
    _require(set(envelope) == WEBHOOK_ENVELOPE_FIELDS, "webhook.receive", "envelope_fields")
    payload = envelope.get("payload")
    _require(isinstance(payload, dict), "webhook.receive", "payload_type")
    _require(set(payload) == COST_EVENT_PAYLOAD_FIELDS, "webhook.receive", "payload_fields")
    _require(not _contains_forbidden_webhook_key(envelope), "webhook.receive", "privacy")
    _require(
        verify_webhook_signature(
            signature,
            signing_secret,
            body,
            now_seconds=now_seconds,
        ),
        "webhook.receive",
        "signature",
    )
    _require(envelope.get("event_type") == "cost.anomaly_detected", "webhook.receive", "event_type")
    _require(envelope.get("actor_id") is None, "webhook.receive", "actor")
    event_id = _safe_identifier(envelope.get("event_id"), "webhook.receive", "event_id")
    anomaly_id = _safe_identifier(payload.get("anomaly_id"), "webhook.receive", "anomaly_id")
    _require(EVENT_ID_PATTERN.fullmatch(event_id) is not None, "webhook.receive", "event_id")
    _require(ANOMALY_ID_PATTERN.fullmatch(anomaly_id) is not None, "webhook.receive", "anomaly_id")
    _require(envelope.get("object_id") == anomaly_id, "webhook.receive", "object_id")
    try:
        timestamp = datetime.fromisoformat(str(envelope.get("timestamp", "")).replace("Z", "+00:00"))
        event = build_webhook_event(
            tenant_id=str(envelope.get("tenant_id", "")),
            event_type="cost.anomaly_detected",
            payload=dict(payload),
            actor_id=None,
            object_id=anomaly_id,
            trace_id=str(envelope.get("trace_id", "")),
            event_id=event_id,
            timestamp=timestamp,
        )
    # 共享合同校验可能引用事件字段，验收器只保留稳定错误码以维持证据隐私。
    except (TypeError, ValueError):
        _fail("webhook.receive", "shared_contract")
    _require(event.to_payload() == envelope, "webhook.receive", "canonical_envelope")
    return {
        "event_id": event_id,
        "anomaly_id": anomaly_id,
        "payload_field_count": len(payload),
        "signature_valid": True,
        "privacy_verified": True,
    }


class AcceptanceServer(ThreadingHTTPServer):
    """同时承载回环假上游与 HMAC Webhook，不保存原始请求或响应正文。"""

    daemon_threads = True

    def __init__(self) -> None:
        """绑定随机回环端口，并初始化仅存在内存的敏感材料。"""

        super().__init__(("127.0.0.1", 0), AcceptanceHandler)
        self.provider_key = ""
        self.gateway_prompt = ""
        self.webhook_secret = ""
        self._provider_call_count = 0
        self._webhook_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def install_provider(self, provider_key: str, gateway_prompt: str) -> None:
        """安装本轮 Provider Key 和 Prompt 哨兵；两者只用于恒定时间比较。"""

        _require(bool(provider_key and gateway_prompt), "receiver.provider", "secret_missing")
        with self._lock:
            self.provider_key = provider_key
            self.gateway_prompt = gateway_prompt

    def install_webhook_secret(self, value: str) -> None:
        """安装一次性 Webhook Secret，只供当前回环请求验签。"""

        _require(bool(value), "receiver.webhook", "secret_missing")
        with self._lock:
            self.webhook_secret = value

    def accept_provider_call(self, authorization: str, prompt: object, model: object) -> bool:
        """验证本轮凭据和输入后只增加安全计数，不保存 Header 或 Prompt。"""

        with self._lock:
            expected_auth = f"Bearer {self.provider_key}"
            valid = (
                bool(self.provider_key)
                and hmac.compare_digest(authorization, expected_auth)
                and isinstance(prompt, str)
                and hmac.compare_digest(prompt, self.gateway_prompt)
                and model == UPSTREAM_MODEL_ID
            )
            if valid:
                self._provider_call_count += 1
            return valid

    def provider_call_count(self) -> int:
        """返回假上游已接受调用数，不暴露调用内容。"""

        with self._lock:
            return self._provider_call_count

    def record_webhook(self, summary: Mapping[str, Any]) -> None:
        """保存已经验签和隐私校验的安全事件摘要。"""

        with self._lock:
            self._webhook_events.append(dict(summary))

    def webhook_events(self) -> list[dict[str, Any]]:
        """返回安全事件摘要副本，避免 HTTP 线程共享可变对象。"""

        with self._lock:
            return [dict(item) for item in self._webhook_events]

    def clear_sensitive(self) -> None:
        """清空回环服务器持有的全部明文材料。"""

        with self._lock:
            self.provider_key = ""
            self.gateway_prompt = ""
            self.webhook_secret = ""


class AcceptanceHandler(BaseHTTPRequestHandler):
    """实现最小 OpenAI-compatible Provider 与成本异常 Webhook 接收协议。"""

    def do_GET(self) -> None:  # noqa: N802
        """只为 Provider 可达性检查返回固定模型目录。"""

        if self.path == "/v1/models":
            self._json_response(200, {"object": "list", "data": [{"id": UPSTREAM_MODEL_ID}]})
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """按固定路径处理 Provider 或 Webhook，请求体始终有界。"""

        raw = self._read_body()
        if raw is None:
            return
        if self.path == "/v1/chat/completions":
            self._handle_provider(raw)
            return
        if self.path == "/hook":
            self._handle_webhook(raw)
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, _format: str, *args: object) -> None:
        """关闭标准库访问日志，避免 URL、Header 或错误参数进入终端。"""

        del args

    def _read_body(self) -> bytes | None:
        """按 Content-Length 有界读取正文，非法长度固定返回 413。"""

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        # 非法长度头按越界正文处理，不能把攻击者提供的 Header 写入响应或日志。
        except ValueError:
            length = -1
        if length < 1 or length > MAX_HTTP_BODY_BYTES:
            self._json_response(413, {"error": "body_too_large"})
            return None
        return self.rfile.read(length)

    def _handle_provider(self, raw: bytes) -> None:
        """验证凭据、模型和单条 user Prompt，再返回固定成功响应。"""

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            payload = json.loads(raw.decode("utf-8", errors="strict"))
        # Provider 请求可能携带 Prompt，解析失败仅返回固定状态且绝不记录原始正文。
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._json_response(400, {"error": "invalid_json"})
            return
        messages = payload.get("messages") if isinstance(payload, dict) else None
        prompt = None
        if isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict):
            # 只接收唯一的 user 消息，避免其他角色内容绕过 Prompt 哨兵校验。
            if messages[0].get("role") == "user":
                prompt = messages[0].get("content")
        if not server.accept_provider_call(
            self.headers.get("Authorization", ""),
            prompt,
            payload.get("model") if isinstance(payload, dict) else None,
        ):
            self._json_response(401, {"error": "unauthorized"})
            return
        self._json_response(
            200,
            {
                "id": "chatcmpl_prd053_runtime",
                "object": "chat.completion",
                "model": UPSTREAM_MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "acceptance-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            },
        )

    def _handle_webhook(self, raw: bytes) -> None:
        """验签成本异常事件并只保存稳定 ID 与布尔隐私结论。"""

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            summary = validate_cost_anomaly_webhook(
                raw,
                signature=self.headers.get("X-MetaHub-Signature", ""),
                signing_secret=server.webhook_secret,
            )
        except AcceptanceFailure:
            self._json_response(401, {"error": "unsafe_event"})
            return
        server.record_webhook(summary)
        self.send_response(204)
        self.end_headers()

    def _json_response(self, status: int, payload: Mapping[str, Any]) -> None:
        """写入固定 UTF-8 JSON 与长度，防止客户端等待连接关闭。"""

        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class RunningAcceptanceServer(AbstractContextManager[AcceptanceServer]):
    """管理回环接收器线程，确保异常路径也关闭端口并清空明文。"""

    def __init__(self) -> None:
        """初始化尚未绑定端口的上下文。"""

        self.server: AcceptanceServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> AcceptanceServer:
        """绑定随机回环端口并启动 daemon 线程。"""

        self.server = AcceptanceServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """先停线程再清空内存凭据，避免在途请求读取已清空状态。"""

        del exc_type, exc, traceback
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.server is not None:
            self.server.clear_sensitive()


class RuntimeAcceptance:
    """通过真实 Control/Gateway 与隔离运行库验证成本异常端到端合同。"""

    def __init__(
        self,
        config: RuntimeConfig,
        control_client: httpx.Client,
        gateway_client: httpx.Client,
    ) -> None:
        """保存客户端和随机后缀；所有敏感值只存在当前实例内存。"""

        self.config = config
        self.control = control_client
        self.gateway = gateway_client
        self.suffix = secrets.token_hex(6)
        self.sessions: dict[str, SessionCredential] = {}
        self.resource_ids: dict[str, str] = {}
        self.http_evidence: dict[str, dict[str, Any]] = {}
        self.known_sensitive: list[str] = [config.admin_password]
        self.virtual_key_value = ""
        self.provider_key_value = ""
        self.webhook_secret = ""
        self.gateway_prompt = f"prd053-private-runtime-input-{self.suffix}"
        self.known_sensitive.append(self.gateway_prompt)

    def _connect_runtime(self) -> sqlite3.Connection:
        """以读写模式连接已存在运行库，禁止 SQLite 隐式创建其他文件。"""

        _require(self.config.database_path.is_file(), "database.connect", "runtime_missing")
        _require(not self.config.database_path.is_symlink(), "database.connect", "database_symlink")
        try:
            connection = sqlite3.connect(
                self.config.database_path,
                timeout=self.config.timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        # SQLite 原始错误可能暴露运行库路径和结构，统一转换为固定连接错误。
        except sqlite3.Error:
            _fail("database.connect", "open_failed")

    def _internal_config(self) -> AppConfig:
        """为内部 detector/outbox 复制配置，只把数据库绑定到已验证运行库。"""

        try:
            base = load_app_config("control-plane-api")
            return base.model_copy(
                update={
                    "database_url": sqlite_url_for_path(self.config.database_path),
                    "cost_anomaly_enabled": False,
                }
            )
        # 配置校验异常可能包含数据库 URL，内部调用只能向外暴露固定错误码。
        except (TypeError, ValueError):
            _fail("internal.config", "invalid")

    def verify_runtime_database_ready(self) -> dict[str, Any]:
        """确认 0043、必需表/触发器和 SQLite 完整性均已由调用方准备。"""

        connection = self._connect_runtime()
        try:
            migration = connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0043'"
            ).fetchone()
            _require(
                migration is not None and migration["status"] == "applied",
                "database.readiness",
                "migration_0043_missing",
            )
            rows = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchall()
            tables = {str(row["name"]) for row in rows if row["type"] == "table"}
            triggers = {str(row["name"]) for row in rows if row["type"] == "trigger"}
            _require(REQUIRED_RUNTIME_TABLES <= tables, "database.readiness", "table_missing")
            _require(REQUIRED_RUNTIME_TRIGGERS <= triggers, "database.readiness", "trigger_missing")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            _require(
                len(integrity) == 1 and str(integrity[0][0]).lower() == "ok",
                "database.readiness",
                "integrity_failed",
            )
            _require(
                not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "database.readiness",
                "foreign_key_failed",
            )
        # 就绪检查失败不得泄露表结构和运行库路径，只报告稳定查询错误。
        except sqlite3.Error:
            _fail("database.readiness", "query_failed")
        finally:
            connection.close()
        return {
            "database_in_batch": True,
            "migration_0043_applied": True,
            "integrity_verified": True,
        }

    def _headers(
        self,
        action: str,
        *,
        role: str | None,
        include_access: bool = True,
    ) -> tuple[dict[str, str], str, str]:
        """为指定动作构造认证头和可追踪的链路标识。

        ``role`` 必须对应已登记 Session；返回 Header、request_id 和 trace_id。认证明文只交给
        当前 ``httpx`` 调用，不得写入证据字典或异常消息。
        """

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {
            "Accept": "application/json",
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
        }
        if include_access:
            _require(role in self.sessions, "auth.headers", "session_missing")
            access_value = self.sessions[str(role)].access_value
            _require(bool(access_value), "auth.headers", "session_empty")
            headers["Authorization"] = f"Bearer {access_value}"
        return headers, request_id, trace_id

    def _control_call(
        self,
        *,
        action: str,
        role: str | None,
        method: str,
        path: str,
        expected_status: int,
        body: Mapping[str, Any] | None = None,
        expected_code: str | None = None,
        require_no_store: bool = False,
        idempotency_key: str | None = None,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        """调用 Control 接口并验证统一链路与可选领域错误。"""

        headers, request_id, trace_id = self._headers(
            action,
            role=role,
            include_access=role is not None,
        )
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = _safe_http_call(
            self.control,
            method,
            path,
            stage=f"control.{action}",
            headers=headers,
            body=body,
        )
        payload = verify_response_contract(
            response,
            stage=f"control.{action}",
            expected_status=expected_status,
            request_id=request_id,
            trace_id=trace_id,
            expected_code=expected_code,
            require_no_store=require_no_store,
        )
        self.http_evidence[action] = _public_http_evidence(
            response,
            payload,
            include_code=expected_code is not None,
        )
        return response, payload

    def _login(
        self,
        *,
        role: str,
        email: str,
        password: str,
        expected_user_id: str | None = None,
    ) -> dict[str, Any]:
        """创建短生命周期 Session，并验证角色、租户和数据库绑定身份。"""

        action = f"login_{role}"
        headers, request_id, trace_id = self._headers(action, role=None, include_access=False)
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/login",
            stage=f"auth.{action}",
            headers={**headers, "Content-Type": "application/json"},
            body={
                "tenant_slug": self.config.tenant_slug,
                "email": email,
                "password": password,
            },
        )
        provisional = _json_object(response, f"auth.{action}")
        access = provisional.get("access_token")
        user = provisional.get("user")
        if isinstance(access, str) and access and isinstance(user, dict):
            self.sessions[role] = SessionCredential(
                role=role,
                tenant_id=str(user.get("tenant_id", "")),
                user_id=str(user.get("user_id", "")),
                session_id=str(provisional.get("session_id", "")),
                access_value=access,
                password=password,
            )
            self.known_sensitive.extend((password, access))
        payload = verify_response_contract(
            response,
            stage=f"auth.{action}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        user = payload.get("user")
        _require(isinstance(user, dict), f"auth.{action}", "user_missing")
        _require(user.get("role") == role, f"auth.{action}", "role_mismatch")
        tenant_id = _safe_identifier(user.get("tenant_id"), f"auth.{action}", "tenant_id")
        user_id = _safe_identifier(user.get("user_id"), f"auth.{action}", "user_id")
        if expected_user_id is not None:
            _require(user_id == expected_user_id, f"auth.{action}", "user_id_mismatch")
        # 后续角色必须与管理员首次绑定的租户一致，防止跨租户 Session 混入验收链路。
        if self.resource_ids.get("tenant_id"):
            _require(
                tenant_id == self.resource_ids["tenant_id"],
                f"auth.{action}",
                "tenant_mismatch",
            )
        access = payload.get("access_token")
        _require(isinstance(access, str) and bool(access), f"auth.{action}", "access_missing")
        session_id = _safe_identifier(payload.get("session_id"), f"auth.{action}", "session_id")
        self.sessions[role] = SessionCredential(
            role=role,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            access_value=access,
            password=password,
        )
        self.resource_ids.setdefault("tenant_id", tenant_id)
        self.resource_ids[f"{role}_user_id"] = user_id
        evidence = _public_http_evidence(response, payload)
        self.http_evidence[action] = evidence
        return evidence

    def verify_session_database_binding(self, role: str) -> bool:
        """用 Session ID 和不可逆 Token 哈希证明 Control 绑定当前 runtime.db。"""

        _require(role in self.sessions, "database.session_binding", "session_missing")
        credential = self.sessions[role]
        expected_hash = hashlib.sha256(credential.access_value.encode("utf-8")).hexdigest()
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                """
                SELECT s.tenant_id, s.user_id, s.token_hash, s.status, u.role
                FROM sessions s
                JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.user_id
                WHERE s.session_id=?
                """,
                (credential.session_id,),
            ).fetchone()
        # Session 查询异常不能暴露 Token 哈希、用户归属或运行库结构。
        except sqlite3.Error:
            _fail("database.session_binding", "query_failed")
        finally:
            connection.close()
        _require(row is not None, "database.session_binding", "session_not_found")
        _require(row["tenant_id"] == credential.tenant_id, "database.session_binding", "tenant")
        _require(row["user_id"] == credential.user_id, "database.session_binding", "user")
        _require(row["role"] == role, "database.session_binding", "role")
        _require(row["status"] == "active", "database.session_binding", "status")
        _require(
            hmac.compare_digest(str(row["token_hash"]), expected_hash),
            "database.session_binding",
            "token_hash",
        )
        return True

    def create_role_users(self) -> dict[str, bool]:
        """经公开管理员 API 创建 developer/viewer，并登录形成真实攻击 Session。"""

        for role in ("developer", "viewer"):
            password = f"Prd053!{secrets.token_urlsafe(18)}"
            email = f"prd053-{role}-{self.suffix}@metahub.local"
            _, payload = self._control_call(
                action=f"create_{role}",
                role="admin",
                method="POST",
                path="/users",
                expected_status=201,
                body={
                    "email": email,
                    "display_name": f"PRD053 runtime {role}",
                    "password": password,
                    "role": role,
                    "status": "active",
                },
            )
            user = payload.get("user")
            _require(isinstance(user, dict), f"users.create_{role}", "user_missing")
            user_id = _safe_identifier(user.get("user_id"), f"users.create_{role}", "user_id")
            _require(user.get("role") == role, f"users.create_{role}", "role_mismatch")
            self._login(role=role, email=email, password=password, expected_user_id=user_id)
            _require(
                self.verify_session_database_binding(role),
                f"users.create_{role}",
                "database_binding",
            )
        # 对外证据只声明两种低权限 Session 已绑定，绝不包含登录凭据或 Token。
        return {"developer_session_bound": True, "viewer_session_bound": True}

    def create_isolated_route(self, receiver: AcceptanceServer) -> dict[str, bool]:
        """经公开 API 创建只指向本机接收器的 Provider、Model、Environment 与 Alias。"""

        host, port = receiver.server_address
        provider_code = f"prd053_{self.suffix}"
        _, provider_payload = self._control_call(
            action="provider_create",
            role="admin",
            method="POST",
            path="/providers",
            expected_status=201,
            body={
                "provider_code": provider_code,
                "name": f"PRD053 runtime provider {self.suffix}",
                "provider_type": "openai_compatible",
                "base_url": f"http://{host}:{port}/v1",
                "auth_type": "bearer",
                "description": "PRD-053 isolated runtime acceptance",
            },
        )
        provider = provider_payload.get("provider")
        _require(isinstance(provider, dict), "provider.create", "provider_missing")
        provider_id = _safe_identifier(provider.get("provider_id"), "provider.create", "provider_id")
        _require(provider.get("tenant_id") == self.resource_ids["tenant_id"], "provider.create", "tenant")
        self.resource_ids["provider_id"] = provider_id

        self.provider_key_value = f"sk-prd053-{secrets.token_urlsafe(24)}"
        self.known_sensitive.append(self.provider_key_value)
        receiver.install_provider(self.provider_key_value, self.gateway_prompt)
        _, key_payload = self._control_call(
            action="provider_key_create",
            role="admin",
            method="POST",
            path=f"/providers/{provider_id}/keys",
            expected_status=201,
            body={
                "name": f"PRD053 runtime provider key {self.suffix}",
                "plain_value": self.provider_key_value,
                "weight": 100,
            },
        )
        provider_key = key_payload.get("provider_key")
        _require(isinstance(provider_key, dict), "provider_key.create", "key_missing")
        self.resource_ids["provider_key_id"] = _safe_identifier(
            provider_key.get("provider_key_id"), "provider_key.create", "provider_key_id"
        )

        _, model_payload = self._control_call(
            action="model_create",
            role="admin",
            method="POST",
            path="/models",
            expected_status=201,
            body={
                "internal_model_id": f"prd053-runtime-{self.suffix}",
                "provider_id": provider_id,
                "provider_model_id": UPSTREAM_MODEL_ID,
                "display_name": f"PRD053 runtime model {self.suffix}",
                "capabilities": {"chat": True},
                "context_window": 4096,
                "input_price_per_1k": 0,
                "output_price_per_1k": 0,
                "currency": "CNY",
                "price_schema_version": "prd053-runtime-v1",
                "description": "PRD-053 isolated runtime acceptance",
                "status": "active",
            },
        )
        model = model_payload.get("model")
        _require(isinstance(model, dict), "model.create", "model_missing")
        model_id = _safe_identifier(model.get("model_id"), "model.create", "model_id")
        self.resource_ids["model_id"] = model_id

        _, environment_payload = self._control_call(
            action="environment_create",
            role="admin",
            method="POST",
            path="/environments",
            expected_status=201,
            body={
                "code": "test",
                "name": f"PRD053 runtime test {self.suffix}",
                "description": "PRD-053 isolated runtime acceptance",
            },
        )
        environment = environment_payload.get("environment")
        _require(isinstance(environment, dict), "environment.create", "environment_missing")
        environment_id = _safe_identifier(
            environment.get("environment_id"), "environment.create", "environment_id"
        )
        _require(environment.get("tenant_id") == self.resource_ids["tenant_id"], "environment.create", "tenant")
        self.resource_ids["environment_id"] = environment_id

        _, alias_payload = self._control_call(
            action="alias_create",
            role="admin",
            method="POST",
            path="/model-aliases",
            expected_status=201,
            body={
                "alias": PUBLIC_MODEL_ALIAS,
                "environment_id": environment_id,
                "default_model_id": model_id,
                "description": "PRD-053 isolated runtime acceptance",
                "status": "active",
            },
        )
        alias = alias_payload.get("model_alias")
        _require(isinstance(alias, dict), "alias.create", "alias_missing")
        self.resource_ids["model_alias_id"] = _safe_identifier(
            alias.get("model_alias_id"), "alias.create", "model_alias_id"
        )
        _require(alias.get("default_model_id") == model_id, "alias.create", "model")
        return {
            "environment_created_by_public_api": True,
            "provider_is_loopback": True,
            "model_route_active": True,
        }

    def create_scope_resources(self) -> dict[str, bool]:
        """经公开 API 创建成本异常作用域所需的完整资源链。

        返回值只包含创建和归属校验结论；每一级资源都必须匹配管理员 Session 的租户、环境
        及父资源，Virtual Key 明文只保存在当前进程内存中。
        """

        tenant_id = self.resource_ids["tenant_id"]
        environment_id = self.resource_ids["environment_id"]
        _, project_payload = self._control_call(
            action="project_create",
            role="admin",
            method="POST",
            path="/projects",
            expected_status=201,
            body={
                "project_code": f"prd053-{self.suffix}",
                "name": f"PRD053 runtime project {self.suffix}",
                "description": "PRD-053 isolated runtime acceptance",
                "owner_user_id": self.resource_ids["admin_user_id"],
            },
        )
        project = project_payload.get("project")
        _require(isinstance(project, dict), "project.create", "project_missing")
        project_id = _safe_identifier(project.get("project_id"), "project.create", "project_id")
        _require(project.get("tenant_id") == tenant_id, "project.create", "tenant")
        self.resource_ids["project_id"] = project_id

        _, application_payload = self._control_call(
            action="application_create",
            role="admin",
            method="POST",
            path="/applications",
            expected_status=201,
            body={
                "project_id": project_id,
                "application_code": f"prd053-{self.suffix}",
                "name": f"PRD053 runtime application {self.suffix}",
                "description": "PRD-053 isolated runtime acceptance",
            },
        )
        application = application_payload.get("application")
        _require(isinstance(application, dict), "application.create", "application_missing")
        application_id = _safe_identifier(
            application.get("application_id"), "application.create", "application_id"
        )
        _require(application.get("tenant_id") == tenant_id, "application.create", "tenant")
        _require(application.get("project_id") == project_id, "application.create", "project")
        self.resource_ids["application_id"] = application_id

        _, key_payload = self._control_call(
            action="virtual_key_create",
            role="admin",
            method="POST",
            path="/virtual-keys",
            expected_status=201,
            body={
                "name": f"PRD053 runtime key {self.suffix}",
                "environment_id": environment_id,
                "project_id": project_id,
                "application_id": application_id,
                "description": "PRD-053 isolated runtime acceptance",
            },
        )
        key = key_payload.get("virtual_key")
        plain_value = key_payload.get("plain_value")
        _require(isinstance(key, dict), "virtual_key.create", "key_missing")
        _require(
            isinstance(plain_value, str) and plain_value.startswith("mhv_"),
            "virtual_key.create",
            "plain_value_missing",
        )
        virtual_key_id = _safe_identifier(
            key.get("virtual_key_id"), "virtual_key.create", "virtual_key_id"
        )
        _require(key.get("tenant_id") == tenant_id, "virtual_key.create", "tenant")
        _require(key.get("environment_id") == environment_id, "virtual_key.create", "environment")
        _require(key.get("project_id") == project_id, "virtual_key.create", "project")
        _require(key.get("application_id") == application_id, "virtual_key.create", "application")
        self.resource_ids["virtual_key_id"] = virtual_key_id
        self.virtual_key_value = plain_value
        self.known_sensitive.append(plain_value)
        return {"owner_chain_verified": True, "resources_created_by_public_api": True}

    def create_webhook(self, receiver: AcceptanceServer) -> dict[str, bool]:
        """经公开 API 创建只订阅成本异常的回环 HMAC Endpoint。"""

        host, port = receiver.server_address
        _, payload = self._control_call(
            action="webhook_create",
            role="admin",
            method="POST",
            path="/webhooks",
            expected_status=200,
            body={
                "name": f"PRD053 runtime webhook {self.suffix}",
                "endpoint_url": f"http://{host}:{port}/hook",
                "event_types": ["cost.anomaly_detected"],
                "timeout_ms": 2000,
                "status": "active",
            },
        )
        webhook = payload.get("webhook")
        secret = payload.get("signing_secret")
        _require(isinstance(webhook, dict), "webhook.create", "webhook_missing")
        _require(isinstance(secret, str) and bool(secret), "webhook.create", "secret_missing")
        endpoint_id = _safe_identifier(
            webhook.get("webhook_endpoint_id"), "webhook.create", "endpoint_id"
        )
        _require(webhook.get("tenant_id") == self.resource_ids["tenant_id"], "webhook.create", "tenant")
        _require(webhook.get("event_types") == ["cost.anomaly_detected"], "webhook.create", "events")
        self.resource_ids["webhook_endpoint_id"] = endpoint_id
        self.webhook_secret = secret
        self.known_sensitive.append(secret)
        receiver.install_webhook_secret(secret)
        # Webhook 证据仅保留创建与事件订阅结论，签名密钥不能进入返回结构。
        return {"webhook_created_by_public_api": True, "cost_event_subscribed": True}

    def insert_formal_usage(
        self,
        *,
        label: str,
        amount_cny: str,
        created_at: datetime,
    ) -> dict[str, Any]:
        """写入受控 Usage，并验证 0041 触发器自动生成正式 posted usage_charge。"""

        _require(re.fullmatch(r"[a-z][a-z0-9_]{2,20}", label) is not None, "database.formal_usage", "label")
        _require(CANONICAL_CNY_PATTERN.fullmatch(amount_cny) is not None, "database.formal_usage", "amount")
        _require(created_at.tzinfo is not None, "database.formal_usage", "created_at")
        whole, fraction = amount_cny.split(".", 1)
        micros = int(whole) * 1_000_000 + int(fraction)
        _require(0 <= micros <= 1_000_000_000_000_000, "database.formal_usage", "amount_range")
        request_id, trace_id = _request_identity(f"formal_{label}", self.suffix)
        usage_id = f"usg_prd053_{label}_{self.suffix}"
        snapshot_id = f"prs_prd053_{label}_{self.suffix}"
        timestamp = created_at.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        normalized_usage = json.dumps(
            {
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "cache_tokens": 0,
                "cost_amount": amount_cny,
                "cost_cny": amount_cny,
                "currency": "CNY",
                "posting_source": "direct",
                "price_snapshot_id": snapshot_id,
                "price_schema_version": FORMAL_USAGE_PRICE_SCHEMA_VERSION,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = self._connect_runtime()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                """
                SELECT vk.tenant_id, vk.environment_id, vk.project_id, vk.application_id,
                       vk.status, e.status AS environment_status,
                       p.status AS project_status, a.status AS application_status
                FROM virtual_keys vk
                JOIN environments e
                  ON e.tenant_id=vk.tenant_id AND e.environment_id=vk.environment_id
                JOIN projects p
                  ON p.tenant_id=vk.tenant_id AND p.project_id=vk.project_id
                JOIN applications a
                  ON a.tenant_id=vk.tenant_id AND a.application_id=vk.application_id
                 AND a.project_id=vk.project_id
                WHERE vk.tenant_id=? AND vk.virtual_key_id=?
                """,
                (self.resource_ids["tenant_id"], self.resource_ids["virtual_key_id"]),
            ).fetchone()
            _require(owner is not None, "database.formal_usage", "owner_missing")
            _require(
                owner["environment_id"] == self.resource_ids["environment_id"]
                and owner["project_id"] == self.resource_ids["project_id"]
                and owner["application_id"] == self.resource_ids["application_id"],
                "database.formal_usage",
                "owner_mismatch",
            )
            _require(
                owner["status"] == "active"
                and owner["environment_status"] == "active"
                and owner["project_status"] == "active"
                and owner["application_status"] == "active",
                "database.formal_usage",
                "owner_inactive",
            )
            connection.execute(
                """
                INSERT INTO gateway_request_claims(
                  tenant_id, request_id, trace_id, virtual_key_id,
                  snapshot_version, policy_snapshot_status, claimed_at
                ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
                """,
                (
                    self.resource_ids["tenant_id"],
                    request_id,
                    trace_id,
                    self.resource_ids["virtual_key_id"],
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id, tenant_id, trace_id, virtual_key_id, environment_id,
                  project_id, application_id, endpoint, request_model, status,
                  input_tokens, output_tokens, total_tokens, estimated_cost_amount,
                  currency, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 'chat/completions', ?, 'success',
                         1, 0, 1, ?, 'CNY', ?, ?)
                """,
                (
                    request_id,
                    self.resource_ids["tenant_id"],
                    trace_id,
                    self.resource_ids["virtual_key_id"],
                    self.resource_ids["environment_id"],
                    self.resource_ids["project_id"],
                    self.resource_ids["application_id"],
                    PUBLIC_MODEL_ALIAS,
                    amount_cny,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO price_snapshots(
                  price_snapshot_id, tenant_id, request_id, currency,
                  input_price_per_1k, output_price_per_1k, price_schema_version,
                  billing_dimensions, provider_raw_mapping, source,
                  created_at, updated_at
                ) VALUES(?, ?, ?, 'CNY', 0, 0, ?, ?, '{}', ?, ?, ?)
                """,
                (
                    snapshot_id,
                    self.resource_ids["tenant_id"],
                    request_id,
                    FORMAL_USAGE_PRICE_SCHEMA_VERSION,
                    json.dumps(
                        {"finops_usage_ledger_id": usage_id},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    FORMAL_USAGE_PRICE_SNAPSHOT_SOURCE,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO usage_ledger(
                  usage_ledger_id, tenant_id, request_id,
                  input_tokens, output_tokens, total_tokens, cache_tokens,
                  raw_usage, normalized_usage, cost_amount, cost_cny, cost_usd,
                  currency, price_snapshot_id, price_schema_version,
                  virtual_key_id, environment_id, project_id, application_id,
                  endpoint, request_model, created_at, updated_at
                ) VALUES(?, ?, ?, 1, 0, 1, 0, '{}', ?, ?, ?, 0, 'CNY', ?,
                         ?, ?, ?, ?, ?, 'chat/completions', ?, ?, ?)
                """,
                (
                    usage_id,
                    self.resource_ids["tenant_id"],
                    request_id,
                    normalized_usage,
                    amount_cny,
                    amount_cny,
                    snapshot_id,
                    FORMAL_USAGE_PRICE_SCHEMA_VERSION,
                    self.resource_ids["virtual_key_id"],
                    self.resource_ids["environment_id"],
                    self.resource_ids["project_id"],
                    self.resource_ids["application_id"],
                    PUBLIC_MODEL_ALIAS,
                    timestamp,
                    timestamp,
                ),
            )
            entry = connection.execute(
                """
                SELECT finops_ledger_entry_id, entry_type, direction, status,
                       normalized_cny_micros, cny_normalization_status,
                       price_evidence_status, virtual_key_id, environment_id,
                       project_id, application_id
                FROM finops_ledger_entries
                WHERE tenant_id=? AND usage_ledger_id=?
                """,
                (self.resource_ids["tenant_id"], usage_id),
            ).fetchone()
            _require(entry is not None, "database.formal_usage", "entry_missing")
            _require(
                entry["entry_type"] == "usage_charge"
                and entry["direction"] == "debit"
                and entry["status"] == "posted"
                and entry["normalized_cny_micros"] == micros
                and entry["cny_normalization_status"] == "normalized"
                and entry["price_evidence_status"] == "legacy_amount_only",
                "database.formal_usage",
                "entry_contract",
            )
            _require(
                entry["virtual_key_id"] == self.resource_ids["virtual_key_id"]
                and entry["environment_id"] == self.resource_ids["environment_id"]
                and entry["project_id"] == self.resource_ids["project_id"]
                and entry["application_id"] == self.resource_ids["application_id"],
                "database.formal_usage",
                "entry_scope",
            )
            entry_id = _safe_identifier(
                entry["finops_ledger_entry_id"], "database.formal_usage", "entry_id"
            )
            connection.commit()
        except AcceptanceFailure:
            connection.rollback()
            raise
        # 事务错误可能包含 SQL 与字段值，回滚后只转换为固定的数据库阶段错误。
        except sqlite3.Error:
            connection.rollback()
            _fail("database.formal_usage", "transaction_failed")
        finally:
            connection.close()
        self.resource_ids[f"{label}_usage_ledger_id"] = usage_id
        self.resource_ids[f"{label}_finops_ledger_entry_id"] = entry_id
        return {
            "usage_ledger_id": usage_id,
            "finops_ledger_entry_id": entry_id,
            "posted_usage_charge_verified": True,
        }

    def run_detection_until_idle(
        self,
        *,
        now: datetime,
        maximum_iterations: int = 16,
    ) -> dict[str, Any]:
        """有界调用内部单次 detector，直到当前运行库没有可领取候选。"""

        _require(1 <= maximum_iterations <= 64, "detector.run", "iteration_limit")
        config = self._internal_config()
        worked = 0
        for _ in range(maximum_iterations):
            # 单次检测返回假值即表示候选已清空，有界循环避免调度异常导致验收失控。
            if not run_cost_anomaly_detection_once(config, now=now):
                break
            worked += 1
        else:
            _fail("detector.run", "did_not_become_idle")
        _require(worked >= 1, "detector.run", "no_internal_work")
        connection = self._connect_runtime()
        try:
            rows = connection.execute(
                """
                SELECT cost_anomaly_id, event_id, rule_type, scope_type, scope_id,
                       tenant_id, environment_id, status
                FROM cost_anomalies
                WHERE tenant_id=? AND environment_id=?
                ORDER BY anomaly_sequence
                """,
                (self.resource_ids["tenant_id"], self.resource_ids["environment_id"]),
            ).fetchall()
        # 查询错误不得泄露租户作用域和异常表结构，只返回 detector 固定错误。
        except sqlite3.Error:
            _fail("detector.run", "query_failed")
        finally:
            connection.close()
        matching = [
            row
            for row in rows
            if row["rule_type"] == "virtual_key_spend_spike"
            and row["scope_type"] == "virtual_key"
            and row["scope_id"] == self.resource_ids["virtual_key_id"]
            and row["status"] == "open"
        ]
        _require(len(matching) == 1, "detector.run", "key_spike_missing")
        anomaly_id = _safe_identifier(matching[0]["cost_anomaly_id"], "detector.run", "anomaly_id")
        event_id = _safe_identifier(matching[0]["event_id"], "detector.run", "event_id")
        self.resource_ids["cost_anomaly_id"] = anomaly_id
        self.resource_ids["cost_anomaly_event_id"] = event_id
        return {
            "internal_detection_iterations": worked,
            "detector_generated_anomaly": True,
            "cost_anomaly_count": len(rows),
        }

    def verify_permissions_and_list(self) -> dict[str, bool]:
        """真实验证 admin GET 与匿名/developer/viewer 的后端拒绝。"""

        _, admin_payload = self._control_call(
            action="anomalies_admin_list",
            role="admin",
            method="GET",
            path="/cost-anomalies?limit=100",
            expected_status=200,
            require_no_store=True,
        )
        items = admin_payload.get("cost_anomalies")
        _require(isinstance(items, list) and bool(items), "anomalies.list", "items_missing")
        _require(
            all(isinstance(item, dict) and item.get("tenant_id") == self.resource_ids["tenant_id"] for item in items),
            "anomalies.list",
            "tenant_mismatch",
        )
        _require(
            any(item.get("cost_anomaly_id") == self.resource_ids["cost_anomaly_id"] for item in items),
            "anomalies.list",
            "target_missing",
        )
        self._control_call(
            action="anomalies_anonymous_list",
            role=None,
            method="GET",
            path="/cost-anomalies",
            expected_status=401,
            expected_code="auth.token_missing",
        )
        self._control_call(
            action="anomalies_developer_list",
            role="developer",
            method="GET",
            path="/cost-anomalies",
            expected_status=403,
            expected_code="auth.permission_denied",
        )
        self._control_call(
            action="anomalies_viewer_list",
            role="viewer",
            method="GET",
            path="/cost-anomalies",
            expected_status=403,
            expected_code="auth.permission_denied",
        )
        path = f"/cost-anomalies/{self.resource_ids['cost_anomaly_id']}/action"
        for role, status, code in (
            (None, 401, "auth.token_missing"),
            ("developer", 403, "auth.permission_denied"),
            ("viewer", 403, "auth.permission_denied"),
        ):
            role_name = role or "anonymous"
            self._control_call(
                action=f"anomaly_{role_name}_patch",
                role=role,
                method="PATCH",
                path=path,
                expected_status=status,
                expected_code=code,
                body={"action": "acknowledge"},
                idempotency_key=f"idem-prd053-denied-{role_name}-{self.suffix}",
            )
        return {
            "admin_read_allowed": True,
            "anonymous_denied": True,
            "developer_denied": True,
            "viewer_denied": True,
        }

    def dispatch_and_verify_webhook(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """有界清空本轮 source outbox，并核对 HMAC、20 字段和稳定 event ID。"""

        config = self._internal_config()
        worked = 0
        for _ in range(16):
            if not drain_cost_anomaly_event_once(
                config,
                worker_id=f"prd053-accept-{self.suffix}",
            ):
                break
            worked += 1
        else:
            _fail("webhook.dispatch", "did_not_become_idle")
        _require(worked >= 1, "webhook.dispatch", "no_work")
        deadline = time.monotonic() + self.config.timeout_seconds
        target = self.resource_ids["cost_anomaly_event_id"]
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            events = receiver.webhook_events()
            if any(item.get("event_id") == target for item in events):
                break
            time.sleep(0.02)
        matching = [item for item in events if item.get("event_id") == target]
        _require(len(matching) == 1, "webhook.dispatch", "target_missing")
        _require(
            matching[0].get("anomaly_id") == self.resource_ids["cost_anomaly_id"],
            "webhook.dispatch",
            "anomaly_mismatch",
        )
        connection = self._connect_runtime()
        try:
            delivery = connection.execute(
                """
                SELECT event_id, event_type, status, attempt_count
                FROM webhook_deliveries
                WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id=?
                  AND delivery_kind='original'
                """,
                (
                    self.resource_ids["tenant_id"],
                    self.resource_ids["webhook_endpoint_id"],
                    target,
                ),
            ).fetchone()
        # 投递记录包含租户与事件关联，查询失败时只暴露固定验收错误。
        except sqlite3.Error:
            _fail("webhook.dispatch", "query_failed")
        finally:
            connection.close()
        _require(delivery is not None, "webhook.dispatch", "delivery_missing")
        _require(
            delivery["event_type"] == "cost.anomaly_detected"
            and delivery["status"] == "success"
            and int(delivery["attempt_count"]) == 1,
            "webhook.dispatch",
            "delivery_contract",
        )
        return {
            "stable_event_id_verified": True,
            "webhook_hmac_valid": True,
            "webhook_payload_field_count": 20,
            "webhook_privacy_verified": True,
        }

    def apply_temporary_limit_idempotently(self) -> dict[str, bool]:
        """admin 真实 apply 并重放同一意图，验证只生成一个 action/overlay。"""

        anomaly_id = self.resource_ids["cost_anomaly_id"]
        path = f"/cost-anomalies/{anomaly_id}/action"
        idempotency_key = f"idem-prd053-apply-{self.suffix}"
        self.known_sensitive.append(idempotency_key)
        body = {
            "action": "apply_temporary_rate_limit",
            "duration_minutes": 30,
            # 单次请求的估算 Token 必然大于 1，因此无需先放行一次就能真实得到 429。
            "tpm_limit": 1,
        }
        _, first = self._control_call(
            action="anomaly_apply",
            role="admin",
            method="PATCH",
            path=path,
            expected_status=200,
            body=body,
            idempotency_key=idempotency_key,
            require_no_store=True,
        )
        _, replay = self._control_call(
            action="anomaly_apply_replay",
            role="admin",
            method="PATCH",
            path=path,
            expected_status=200,
            body=body,
            idempotency_key=idempotency_key,
            require_no_store=True,
        )
        first_action = first.get("action")
        replay_action = replay.get("action")
        override = first.get("temporary_rate_limit_override")
        _require(isinstance(first_action, dict) and isinstance(replay_action, dict), "action.apply", "action_missing")
        _require(isinstance(override, dict), "action.apply", "override_missing")
        _require(first.get("idempotent_replay") is False, "action.apply", "first_replay")
        _require(replay.get("idempotent_replay") is True, "action.apply", "replay_flag")
        _require(
            replay_action.get("cost_anomaly_action_id") == first_action.get("cost_anomaly_action_id"),
            "action.apply",
            "action_id_changed",
        )
        _require(override.get("effective_status") == "active", "action.apply", "override_inactive")
        _require(override.get("tpm_limit") == 1, "action.apply", "limit_mismatch")
        self.resource_ids["apply_action_id"] = _safe_identifier(
            first_action.get("cost_anomaly_action_id"), "action.apply", "action_id"
        )
        self.resource_ids["temporary_rate_limit_override_id"] = _safe_identifier(
            override.get("temporary_rate_limit_override_id"), "action.apply", "override_id"
        )
        connection = self._connect_runtime()
        try:
            rows = connection.execute(
                """
                SELECT idempotency_key_sha256 FROM cost_anomaly_actions
                WHERE tenant_id=? AND cost_anomaly_id=?
                  AND action_type='apply_temporary_rate_limit'
                """,
                (self.resource_ids["tenant_id"], anomaly_id),
            ).fetchall()
        # 幂等键只允许验证哈希结果，数据库异常不得泄露原始键或动作记录。
        except sqlite3.Error:
            _fail("action.apply", "query_failed")
        finally:
            connection.close()
        _require(len(rows) == 1, "action.apply", "duplicate_action")
        _require(
            len(str(rows[0]["idempotency_key_sha256"])) == 64
            and str(rows[0]["idempotency_key_sha256"]) != idempotency_key,
            "action.apply",
            "idempotency_not_hashed",
        )
        return {"temporary_limit_active": True, "idempotent_replay_verified": True}

    def verify_gateway_rate_limited(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """调用真实 Gateway，证明临时 TPM 覆盖在 Provider 前返回统一 429。"""

        _require(bool(self.virtual_key_value), "gateway.block", "key_missing")
        before_calls = receiver.provider_call_count()
        headers, request_id, trace_id = self._headers(
            "gateway_rate_limited", role=None, include_access=False
        )
        headers["Authorization"] = f"Bearer {self.virtual_key_value}"
        headers["Content-Type"] = "application/json"
        response = _safe_http_call(
            self.gateway,
            "POST",
            "/v1/chat/completions",
            stage="gateway.rate_limited",
            headers=headers,
            body={
                "model": PUBLIC_MODEL_ALIAS,
                "messages": [{"role": "user", "content": self.gateway_prompt}],
                "max_tokens": 1,
            },
        )
        payload = verify_response_contract(
            response,
            stage="gateway.rate_limited",
            expected_status=429,
            request_id=request_id,
            trace_id=trace_id,
            expected_code="gateway.rate_limit_exceeded",
        )
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                """
                SELECT status, error_code FROM request_logs
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()
            attempts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM routing_attempts
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()["count"]
        # 限流审计查询失败时不能回显租户、请求标识或内部路由表信息。
        except sqlite3.Error:
            _fail("gateway.rate_limited", "query_failed")
        finally:
            connection.close()
        _require(
            row is not None and row["status"] == "failed" and row["error_code"] == "gateway.rate_limit_exceeded",
            "gateway.rate_limited",
            "request_log_contract",
        )
        _require(attempts == 0, "gateway.rate_limited", "provider_attempted")
        _require(receiver.provider_call_count() == before_calls, "gateway.rate_limited", "receiver_called")
        evidence = _public_http_evidence(response, payload, include_code=True)
        evidence.update(
            {
                "provider_attempt_count_zero": True,
                "provider_receiver_call_count_unchanged": True,
                "gateway_database_binding_verified": True,
            }
        )
        self.http_evidence["gateway_rate_limited"] = evidence
        return evidence

    def release_temporary_limit(self) -> dict[str, bool]:
        """admin 真实释放当前 anomaly 的 overlay，并保持 anomaly mitigated 历史。"""

        anomaly_id = self.resource_ids["cost_anomaly_id"]
        idempotency_key = f"idem-prd053-release-{self.suffix}"
        self.known_sensitive.append(idempotency_key)
        _, payload = self._control_call(
            action="anomaly_release",
            role="admin",
            method="PATCH",
            path=f"/cost-anomalies/{anomaly_id}/action",
            expected_status=200,
            body={"action": "release_temporary_rate_limit"},
            idempotency_key=idempotency_key,
            require_no_store=True,
        )
        anomaly = payload.get("cost_anomaly")
        override = payload.get("temporary_rate_limit_override")
        action = payload.get("action")
        _require(isinstance(anomaly, dict), "action.release", "anomaly_missing")
        _require(isinstance(override, dict), "action.release", "override_missing")
        _require(isinstance(action, dict), "action.release", "action_missing")
        _require(anomaly.get("status") == "mitigated", "action.release", "history_status")
        _require(override.get("effective_status") == "released", "action.release", "override_status")
        _require(
            action.get("target_override_id") == self.resource_ids["temporary_rate_limit_override_id"],
            "action.release",
            "target_override",
        )
        self.resource_ids["release_action_id"] = _safe_identifier(
            action.get("cost_anomaly_action_id"), "action.release", "action_id"
        )
        return {"temporary_limit_released": True, "mitigated_history_preserved": True}

    def verify_gateway_restored(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """释放后真实请求命中本机 Provider，证明相同 Key 不再被临时覆盖阻断。"""

        before_calls = receiver.provider_call_count()
        headers, request_id, trace_id = self._headers(
            "gateway_after_release", role=None, include_access=False
        )
        headers["Authorization"] = f"Bearer {self.virtual_key_value}"
        headers["Content-Type"] = "application/json"
        response = _safe_http_call(
            self.gateway,
            "POST",
            "/v1/chat/completions",
            stage="gateway.after_release",
            headers=headers,
            body={
                "model": PUBLIC_MODEL_ALIAS,
                "messages": [{"role": "user", "content": self.gateway_prompt}],
                "max_tokens": 1,
            },
        )
        payload = verify_response_contract(
            response,
            stage="gateway.after_release",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            # 成功正文保持 OpenAI-compatible；MetaHub 调用链由响应 Header 提供。
            require_body_chain=False,
        )
        _require(receiver.provider_call_count() == before_calls + 1, "gateway.after_release", "provider_not_called")
        connection = self._connect_runtime()
        try:
            attempts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM routing_attempts
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()["count"]
            request_log = connection.execute(
                """
                SELECT status, error_code FROM request_logs
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()
        # 恢复链路的数据库异常统一脱敏，避免暴露租户请求和 Provider 路由细节。
        except sqlite3.Error:
            _fail("gateway.after_release", "query_failed")
        finally:
            connection.close()
        _require(attempts >= 1, "gateway.after_release", "routing_attempt_missing")
        _require(
            request_log is not None and request_log["status"] == "success" and request_log["error_code"] is None,
            "gateway.after_release",
            "request_log_contract",
        )
        evidence = _public_http_evidence(response, payload)
        evidence.update(
            {
                "release_restored_gateway": True,
                "provider_attempt_recorded": True,
                "loopback_provider_called_once": True,
            }
        )
        self.http_evidence["gateway_after_release"] = evidence
        return evidence

    def run(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """执行完整 PRD-053 聚焦验收，返回尚未发布且不含清理结论的证据。"""

        database = self.verify_runtime_database_ready()
        admin_login = self._login(
            role="admin",
            email=self.config.admin_email,
            password=self.config.admin_password,
        )
        _require(self.verify_session_database_binding("admin"), "auth.admin", "database_binding")
        roles = self.create_role_users()
        route = self.create_isolated_route(receiver)
        scope = self.create_scope_resources()
        webhook = self.create_webhook(receiver)

        now = datetime.now(UTC).replace(second=0, microsecond=0)
        current_window = align_detection_window(now - timedelta(minutes=10))
        baseline = self.insert_formal_usage(
            label="baseline",
            amount_cny="0.001000",
            created_at=current_window - timedelta(hours=24) + timedelta(seconds=10),
        )
        current = self.insert_formal_usage(
            label="current",
            amount_cny="1.000000",
            created_at=current_window + timedelta(seconds=10),
        )
        detection = self.run_detection_until_idle(now=now)
        permissions = self.verify_permissions_and_list()
        event = self.dispatch_and_verify_webhook(receiver)
        apply = self.apply_temporary_limit_idempotently()
        blocked = self.verify_gateway_rate_limited(receiver)
        release = self.release_temporary_limit()
        restored = self.verify_gateway_restored(receiver)
        return {
            "schema_version": 1,
            "prd_id": "PRD-053",
            "status": "passed",
            "generated_resources": {
                key: value
                for key, value in self.resource_ids.items()
                if key
                not in {
                    "admin_user_id",
                    "developer_user_id",
                    "viewer_user_id",
                }
            },
            "checks": {
                "database": database,
                "admin_login": admin_login,
                "roles": roles,
                "route": route,
                "scope": scope,
                "webhook": webhook,
                "formal_usage": {
                    "baseline_posted": baseline["posted_usage_charge_verified"],
                    "current_posted": current["posted_usage_charge_verified"],
                    "anomaly_not_inserted_directly": True,
                },
                "detection": detection,
                "permissions": permissions,
                "event": event,
                "apply": apply,
                "gateway_blocked": blocked,
                "release": release,
                "gateway_restored": restored,
            },
            "http": dict(self.http_evidence),
        }

    def _logout_one(self, role: str) -> bool:
        """注销一个已登记 Session，并通过同库状态证明服务端撤销。"""

        credential = self.sessions.get(role)
        if credential is None or not credential.access_value:
            return False
        headers, request_id, trace_id = self._headers(f"logout_{role}", role=role)
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/logout",
            stage=f"auth.logout_{role}",
            headers=headers,
        )
        verify_response_contract(
            response,
            stage=f"auth.logout_{role}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                "SELECT status FROM sessions WHERE session_id=?",
                (credential.session_id,),
            ).fetchone()
        # 注销核验失败不得把 Session 标识或数据库状态带入验收错误正文。
        except sqlite3.Error:
            _fail(f"auth.logout_{role}", "query_failed")
        finally:
            connection.close()
        return row is not None and row["status"] == "revoked"

    def logout_all(self) -> dict[str, bool]:
        """按低权限到高权限撤销全部 Session，并给 Browser 兼容聚合结论。"""

        results: dict[str, bool] = {}
        for role in ("viewer", "developer", "admin"):
            # 只注销本轮确实建立的 Session，兼容前序步骤失败后的 finally 清理路径。
            if role in self.sessions:
                results[f"{role}_session_revoked"] = self._logout_one(role)
        expected = {f"{role}_session_revoked" for role in self.sessions}
        results["session_revoked"] = bool(expected) and all(results.get(key) is True for key in expected)
        return results

    def sensitive_values(self) -> tuple[str, ...]:
        """返回当前内存中所有已知明文，用于最终证据等值扫描。"""

        return tuple(value for value in self.known_sensitive if value)

    def clear_sensitive_memory(self) -> None:
        """清空验收器持有的口令、Token、Key、Secret、Prompt 引用。"""

        for credential in self.sessions.values():
            credential.access_value = ""
            credential.password = ""
        self.virtual_key_value = ""
        self.provider_key_value = ""
        self.webhook_secret = ""
        self.gateway_prompt = ""
        self.known_sensitive.clear()


def assert_safe_evidence(value: Any, *, sensitive_values: tuple[str, ...]) -> None:
    """递归拒绝敏感字段、非 JSON 类型、明文格式和本轮已知敏感值。"""

    known = tuple(item for item in sensitive_values if item)

    def walk(node: Any) -> None:
        """递归扫描证据节点；字段名和值都不能绕过隐私与 JSON 类型门禁。"""

        if isinstance(node, Mapping):
            for key, child in node.items():
                _require(isinstance(key, str), "evidence.privacy", "non_string_key")
                _require(
                    FORBIDDEN_EVIDENCE_KEY_PATTERN.search(key) is None,
                    "evidence.privacy",
                    "forbidden_key",
                )
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        _require(
            node is None or isinstance(node, (str, int, bool)),
            "evidence.privacy",
            "non_json_value",
        )

    walk(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    _require(
        FORBIDDEN_EVIDENCE_TEXT_PATTERN.search(encoded) is None,
        "evidence.privacy",
        "forbidden_text",
    )
    for secret in known:
        _require(secret not in encoded, "evidence.privacy", "known_secret")

def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """以独占创建方式发布最终 JSON，同一批次重跑不得覆盖既有事实。"""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "runtime-acceptance.json"
    encoded = (
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        with target.open("xb") as handle:
            handle.write(encoded)
    # 同一批次证据必须只写一次，拒绝覆盖可防止重跑改写既有验收事实。
    except FileExistsError:
        _fail("evidence.publish", "already_exists")
    # 其他文件系统错误也只映射固定错误码，不能暴露主机路径和权限细节。
    except OSError:
        _fail("evidence.publish", "write_failed")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """执行真实验收、强制注销 Session、执行隐私扫描并发布一次证据。"""

    acceptance: RuntimeAcceptance | None = None
    candidate: dict[str, Any] | None = None
    cleanup: dict[str, bool] = {"session_revoked": False}
    sensitive: tuple[str, ...] = (config.admin_password,)
    with RunningAcceptanceServer() as receiver:
        with (
            httpx.Client(
                base_url=config.control_origin,
                timeout=config.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as control,
            httpx.Client(
                base_url=config.gateway_origin,
                timeout=config.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as gateway,
        ):
            acceptance = RuntimeAcceptance(config, control, gateway)
            try:
                candidate = acceptance.run(receiver)
            finally:
                sensitive = acceptance.sensitive_values()
                cleanup = acceptance.logout_all()
                acceptance.clear_sensitive_memory()
                receiver.clear_sensitive()
    _require(candidate is not None, "runtime", "candidate_missing")
    _require(cleanup.get("session_revoked") is True, "auth.cleanup", "session_not_revoked")
    evidence = dict(candidate)
    checks = dict(evidence.get("checks", {}))
    checks["cleanup"] = cleanup
    checks["all_sessions_revoked"] = True
    evidence["checks"] = checks
    assert_safe_evidence(evidence, sensitive_values=sensitive)
    publish_evidence(config.artifact_directory, evidence)
    return evidence


def main(argv: list[str] | None = None) -> int:
    """命令行入口；终端只输出固定状态，不打印证据正文或异常详情。"""

    try:
        config = parse_config(argv)
        execute_runtime(config)
    except AcceptanceFailure as exc:
        print(str(exc))
        return 1
    except Exception:
        print("prd053_runtime_acceptance_failed stage=runtime code=unexpected")
        return 1
    print("prd053_runtime_acceptance_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
