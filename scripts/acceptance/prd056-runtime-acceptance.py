"""PRD-056 Provider 错误归一化规则库真实运行验收器。

验收器只访问调用方已启动的本机 Gateway、Control API 和 Console，并在当前进程内启动
仅监听回环地址的受控 OpenAI-compatible 上游与 Webhook 接收器。测试数据库、服务日志和
证据统一位于 ``.artifacts/prd056/<batch>``；密码、Token、Prompt、原始 Provider 正文和 Endpoint
仅在内存或其合法配置 owner 中短暂出现，禁止进入服务日志和最终证据。
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
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.webhooks import verify_webhook_signature

# 验收入口和公开标识使用白名单，防止命令行参数扩大到工作区外或携带正文。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
ERROR_MAP_ID_PATTERN = re.compile(r"^pem_[0-9a-f]{32}$")
MIGRATION_FILENAME = "0046_provider_error_normalization_rules.sql"
PRIVATE_PROVIDER_SENTINEL = "prd056-private-upstream-body-sentinel"
PRIVATE_PROMPT_SENTINEL = "prd056-private-prompt-sentinel"
MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_LOG_FILE_BYTES = 64 * 1024 * 1024
WEBHOOK_EVENT_ID_PATTERN = re.compile(r"^pemevt_[0-9a-f]{32}$")
WEBHOOK_DELIVERY_ID_PATTERN = re.compile(r"^whd_[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
ERROR_MAP_EVENT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "provider_error_map_id",
        "rule_version",
        "head_revision",
        "provider_scope",
        "status",
        "priority",
        "output_error_code",
        "pattern_hash",
        "request_id",
    }
)

# 这些对象共同证明规则、不可变版本、热更新、事实和 Source Outbox 已落到同一运行库。
REQUIRED_TABLES = frozenset(
    {
        "provider_error_builtin_catalog_versions",
        "provider_error_builtin_rule_versions",
        "provider_error_maps",
        "provider_error_map_versions",
        "provider_error_map_heads",
        "provider_error_map_head_versions",
        "provider_error_map_events",
        "provider_error_map_operations",
        "provider_error_facts",
    }
)

# 最终 JSON 不允许携带认证材料、原始正文或可重放的上游配置。
FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "credential_id",
        "encrypted_key",
        "endpoint",
        "endpoint_url",
        "field_pattern",
        "message_pattern",
        "messages",
        "password",
        "payload",
        "prompt",
        "provider_key",
        "raw_body",
        "response",
        "response_body",
        "secret",
        "session_token",
        "token",
    }
)
FORBIDDEN_VALUE_PATTERN = re.compile(
    r"Bearer\s+[^\s\"']{8,}|\bmhv_[A-Za-z0-9_-]{12,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)


class AcceptanceFailure(RuntimeError):
    """只携带固定 stage/code 的验收异常，避免底层响应或 SQL 内容进入日志。"""

    def __init__(self, stage: str, code: str) -> None:
        """保存稳定失败分类；参数不得来自 Provider 正文或外部异常文本。"""

        super().__init__(f"prd056_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """描述同一 PRD-056 批次的服务地址、数据库和短期管理员身份。"""

    control_origin: str
    gateway_origin: str
    console_origin: str
    artifact_directory: Path
    database_path: Path
    tenant_slug: str
    admin_email: str
    admin_password: str
    virtual_key: str
    timeout_seconds: float
    log_paths: tuple[Path, ...] = ()


@dataclass
class SessionCredential:
    """保存一次真实 Session；注销后所有敏感字段必须立即清空。"""

    key: str
    role: str
    tenant_slug: str
    tenant_id: str
    user_id: str
    session_id: str
    access_token: str
    email: str
    password: str


@dataclass
class MockProviderState:
    """保存受控上游当前错误形态；锁保证并发请求读取一致快照。"""

    status: int = 404
    error_type: str = "resource_absent"
    error_code: str = "resource_absent"
    message: str = "resource unavailable"

    def __post_init__(self) -> None:
        """为状态切换建立线程锁；锁本身不进入证据或序列化。"""

        self.lock = threading.Lock()

    def set(self, *, status: int, error_type: str, error_code: str, message: str) -> None:
        """原子替换下一批响应，参数仅由验收器内部固定用例生成。"""

        with self.lock:
            self.status = status
            self.error_type = error_type
            self.error_code = error_code
            self.message = message

    def snapshot(self) -> tuple[int, str, str, str]:
        """读取一次不可变响应快照，防止状态切换产生混合正文。"""

        with self.lock:
            return self.status, self.error_type, self.error_code, self.message


def _fail(stage: str, code: str) -> None:
    """用固定分类终止验收，不拼接任何不可信输入。"""

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言，确保异常出口保持统一且不泄露细节。"""

    if not condition:
        _fail(stage, code)


def _workspace_root() -> Path:
    """从脚本物理位置恢复唯一 MetaHub 根，拒绝环境变量改写信任边界。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def _path_contains_link(path: Path, *, boundary: Path) -> bool:
    """检查批次路径任一节点是否为符号链接或 Windows junction。"""

    current = Path(os.path.abspath(path))
    trusted = Path(os.path.abspath(boundary))
    try:
        current.relative_to(trusted)
    # 越出工作区信任边界即视为含链接风险，不能继续解析候选证据路径。
    except ValueError:
        return True
    while True:
        try:
            is_junction = getattr(current, "is_junction", None)
            if current.is_symlink() or (callable(is_junction) and bool(is_junction())):
                return True
        # 任一节点元数据不可读时失败关闭，避免把权限错误误判成普通目录。
        except OSError:
            return True
        if current == trusted:
            return False
        if current.parent == current:
            return True
        current = current.parent


def normalize_loopback_origin(value: str, *, stage: str) -> str:
    """规范化带显式端口的 HTTP 回环 origin，拒绝凭据、路径和 URL 后缀。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 类型或端口解析失败统一视为配置错误，绝不回显可能携带凭据的原值。
    except (TypeError, ValueError):
        _fail(stage, "invalid_url")
    host = (parsed.hostname or "").lower()
    _require(parsed.scheme == "http", stage, "scheme_not_http")
    _require(host in LOOPBACK_HOSTS, stage, "host_not_loopback")
    _require(parsed.username is None and parsed.password is None, stage, "credentials")
    _require(parsed.path in {"", "/"} and not parsed.query and not parsed.fragment, stage, "suffix")
    _require(port is not None, stage, "port_missing")
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:{port}"


def resolve_artifact_directory(value: str, *, workspace_root: Path | None = None) -> Path:
    """把证据目录硬限制为 ``.artifacts/prd056/<batch>`` 的普通单层目录。"""

    root = (workspace_root or _workspace_root()).resolve(strict=True)
    supplied = Path(value)
    _require(not supplied.is_absolute(), "config.artifact", "absolute")
    _require(
        len(supplied.parts) == 3 and supplied.parts[:2] == (".artifacts", "prd056"),
        "config.artifact",
        "outside_root",
    )
    artifact_root = root / ".artifacts" / "prd056"
    candidate = Path(os.path.abspath(root / supplied))
    _require(not _path_contains_link(candidate.parent, boundary=root), "config.artifact", "link")
    try:
        relative = candidate.relative_to(Path(os.path.abspath(artifact_root)))
    # 证据目录必须位于冻结根目录内，越界路径只返回固定失败分类。
    except ValueError:
        _fail("config.artifact", "outside_root")
    _require(len(relative.parts) == 1, "config.artifact", "batch_depth")
    _require(
        BATCH_NAME_PATTERN.fullmatch(relative.name) is not None, "config.artifact", "batch_name"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)
    candidate.mkdir(exist_ok=True)
    _require(not _path_contains_link(candidate, boundary=root), "config.artifact", "link")
    resolved = candidate.resolve(strict=True)
    _require(
        resolved.parent == artifact_root.resolve(strict=True), "config.artifact", "outside_root"
    )
    return resolved


def resolve_runtime_database(value: str, *, artifact_directory: Path) -> Path:
    """解析同批次 ``runtime.db``，拒绝其他文件名、链接和真实路径逃逸。"""

    _require(value.startswith("sqlite:///"), "config.database", "scheme")
    raw = value[len("sqlite:///") :]
    _require(
        bool(raw)
        and "\\" not in raw
        and not any(character in raw for character in ("?", "#", "%", "\x00")),
        "config.database",
        "url_shape",
    )
    candidate = Path(raw)
    _require(candidate.is_absolute(), "config.database", "not_absolute")
    lexical = Path(os.path.abspath(candidate))
    _require(lexical.name == "runtime.db", "config.database", "filename")
    _require(lexical.parent == artifact_directory, "config.database", "outside_batch")
    _require(
        not _path_contains_link(lexical, boundary=artifact_directory), "config.database", "link"
    )
    _require(lexical.is_file(), "config.database", "missing")
    resolved = lexical.resolve(strict=True)
    _require(resolved == artifact_directory / "runtime.db", "config.database", "outside_batch")
    return resolved


def resolve_service_log(value: str, *, artifact_directory: Path) -> Path:
    """解析同批次普通服务日志，拒绝越界、链接、非日志后缀和缺失文件。"""

    supplied = Path(value)
    candidate = Path(
        os.path.abspath(supplied if supplied.is_absolute() else _workspace_root() / supplied)
    )
    _require(candidate.parent == artifact_directory, "config.log", "outside_batch")
    _require(candidate.suffix.lower() == ".log", "config.log", "suffix")
    _require(not _path_contains_link(candidate, boundary=artifact_directory), "config.log", "link")
    _require(candidate.is_file(), "config.log", "missing")
    # 日志扫描必须有显式上限，避免异常服务输出令验收器无界占用内存或跳过隐私门禁。
    _require(candidate.stat().st_size <= MAX_LOG_FILE_BYTES, "config.log", "too_large")
    return candidate.resolve(strict=True)


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> RuntimeConfig:
    """读取非敏感 CLI 与敏感环境变量，并立即移除全部凭据别名。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--control-url", default="http://localhost:8001")
    parser.add_argument("--gateway-url", default="http://localhost:8000")
    parser.add_argument("--console-url", default="http://localhost:5173")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--log-file", action="append", required=True)
    args = parser.parse_args(argv)
    artifact = resolve_artifact_directory(args.artifact_dir)
    admin_password = str(
        env.get("METAHUB_PRD056_ADMIN_PASSWORD") or env.get("METAHUB_ADMIN_PASSWORD") or ""
    )
    virtual_key = str(
        env.get("METAHUB_PRD056_VIRTUAL_KEY") or env.get("METAHUB_SEED_VIRTUAL_KEY") or ""
    )
    tenant_slug = str(env.get("METAHUB_PRD056_TENANT_SLUG") or "default").strip()
    admin_email = (
        str(env.get("METAHUB_PRD056_ADMIN_EMAIL") or "admin@metahub.local").strip().lower()
    )
    for name in (
        "METAHUB_PRD056_ADMIN_PASSWORD",
        "METAHUB_ADMIN_PASSWORD",
        "METAHUB_PRD056_VIRTUAL_KEY",
        "METAHUB_SEED_VIRTUAL_KEY",
        "METAHUB_PRD056_TENANT_SLUG",
        "METAHUB_PRD056_ADMIN_EMAIL",
    ):
        env.pop(name, None)
    _require(len(admin_password) >= 8, "config.identity", "admin_password")
    _require(len(virtual_key) >= 16, "config.identity", "virtual_key")
    _require(bool(tenant_slug) and "@" in admin_email, "config.identity", "identity")
    _require(1.0 <= args.timeout_seconds <= 30.0, "config.timeout", "range")
    log_paths = tuple(
        resolve_service_log(value, artifact_directory=artifact) for value in args.log_file
    )
    _require(len(log_paths) >= 2, "config.log", "count")
    _require(len(set(log_paths)) == len(log_paths), "config.log", "duplicate")
    origins = (
        normalize_loopback_origin(args.control_url, stage="config.control"),
        normalize_loopback_origin(args.gateway_url, stage="config.gateway"),
        normalize_loopback_origin(args.console_url, stage="config.console"),
    )
    _require(len(set(origins)) == 3, "config.origins", "not_distinct")
    return RuntimeConfig(
        control_origin=origins[0],
        gateway_origin=origins[1],
        console_origin=origins[2],
        artifact_directory=artifact,
        database_path=resolve_runtime_database(args.database_url, artifact_directory=artifact),
        tenant_slug=tenant_slug,
        admin_email=admin_email,
        admin_password=admin_password,
        virtual_key=virtual_key,
        timeout_seconds=args.timeout_seconds,
        log_paths=log_paths,
    )


def _normalize_key(value: str) -> str:
    """规范化 JSON 字段名，使 camelCase 和标点无法绕过隐私门禁。"""

    return (
        re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)).strip("_").lower()
    )


def assert_safe_payload(value: Any, *, sensitive_values: Iterable[str] = ()) -> None:
    """递归验证最终证据不含敏感字段、凭据形态或本批次已知明文。"""

    def walk(node: Any) -> None:
        """遍历只读证据树并检查键名、标量类型与已知敏感值，拒绝任意对象表示。"""

        if isinstance(node, Mapping):
            for key, child in node.items():
                _require(
                    _normalize_key(str(key)) not in FORBIDDEN_KEYS,
                    "evidence.privacy",
                    "forbidden_key",
                )
                walk(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
        else:
            _require(
                node is None or type(node) in {str, int, float, bool}, "evidence.privacy", "type"
            )

    walk(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    _require(
        FORBIDDEN_VALUE_PATTERN.search(encoded) is None, "evidence.privacy", "credential_pattern"
    )
    _require(PRIVATE_PROVIDER_SENTINEL not in encoded, "evidence.privacy", "provider_body")
    for item in sensitive_values:
        if item:
            _require(item not in encoded, "evidence.privacy", "known_sensitive")


def _migration_checksum() -> str:
    """按迁移器规则计算正式 0046 文件的 UTF-8 SHA-256。"""

    migration = _workspace_root() / "infra" / "migrations" / MIGRATION_FILENAME
    try:
        sql = migration.read_text(encoding="utf-8")
    # 正式迁移文件不可读时只返回固定分类，绝不把工作机物理路径写进证据。
    except OSError:
        _fail("database.migration", "source_unavailable")
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _request_identity(action: str, suffix: str) -> tuple[str, str]:
    """为每个真实 HTTP 操作生成可追踪且不含业务数据的 request_id/trace_id。"""

    safe_action = re.sub(r"[^a-z0-9_]+", "_", action.lower()).strip("_")[:48]
    return f"req_p56_{safe_action}_{suffix}", f"trc_p56_{safe_action}_{suffix}"


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """严格读取 JSON object；解析失败仅暴露固定分类。"""

    try:
        payload = response.json()
    # 原始响应可能包含内部错误正文，解析失败时只暴露固定阶段分类。
    except (ValueError, json.JSONDecodeError):
        _fail(stage, "invalid_json")
    _require(isinstance(payload, dict), stage, "not_object")
    return payload


def verify_response(
    response: httpx.Response,
    *,
    stage: str,
    status: int,
    request_id: str,
    trace_id: str,
    code: str | None = None,
) -> dict[str, Any]:
    """核验统一 HTTP 状态、request_id/trace_id 与可选错误码合同。"""

    payload = _json_object(response, stage)
    _require(response.status_code == status, stage, "status")
    _require(payload.get("request_id") == request_id, stage, "request_id")
    _require(payload.get("trace_id") == trace_id, stage, "trace_id")
    if code is not None:
        _require(payload.get("code") == code, stage, "code")
    return payload


class _MockProviderHandler(BaseHTTPRequestHandler):
    """返回有界错误 JSON 的本地上游；默认关闭访问日志以保护 Prompt 和 Header。"""

    server: MockProviderServer

    def do_POST(self) -> None:
        """只接受 chat/completions，并在丢弃请求体后返回当前错误快照。"""

        if self.path != "/v1/chat/completions":
            self._empty_response(404)
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            transfers = self.headers.get_all("Transfer-Encoding") or []
            if len(lengths) != 1 or transfers or re.fullmatch(r"[0-9]{1,9}", lengths[0]) is None:
                self._empty_response(413)
                return
            declared = int(lengths[0])
        # 非法长度固定进入超限分支，不能把缺失或歧义长度当作可读取正文。
        except ValueError:
            declared = MAX_HTTP_BODY_BYTES + 1
        if declared < 1 or declared > MAX_HTTP_BODY_BYTES:
            self._empty_response(413)
            return
        raw = self.rfile.read(declared)
        if len(raw) != declared:
            self._empty_response(400)
            return
        del raw
        status, error_type, error_code, message = self.server.state.snapshot()
        body = json.dumps(
            {"error": {"type": error_type, "code": error_code, "message": message}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty_response(self, status: int) -> None:
        """返回无正文固定状态，避免标准库错误页扩散请求信息。"""

        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        """禁用 BaseHTTPRequestHandler 默认日志，避免 URL 或 Header 被输出。"""


class MockProviderServer(ThreadingHTTPServer):
    """绑定 127.0.0.1 随机端口的短生命周期受控上游。"""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, state: MockProviderState) -> None:
        """在系统分配端口上启动服务，端口只写入批次数据库而不进入证据。"""

        self.state = state
        super().__init__(("127.0.0.1", 0), _MockProviderHandler)


def validate_error_map_webhook(
    body: bytes,
    *,
    signature: str,
    signing_secret: str,
    event_header: str,
    delivery_header: str,
    now_seconds: int | None = None,
) -> dict[str, Any]:
    """验签 error_map.updated，并只返回稳定 ID、revision 和隐私结论。"""

    _require(0 < len(body) <= MAX_HTTP_BODY_BYTES, "webhook.receive", "body_size")
    try:
        envelope = json.loads(body.decode("utf-8", errors="strict"))
    # 非法 UTF-8、畸形 JSON 或递归过深均统一拒绝，不能回显 Webhook 原始正文。
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail("webhook.receive", "invalid_json")
    _require(isinstance(envelope, dict), "webhook.receive", "envelope_type")
    _require(set(envelope) == WEBHOOK_ENVELOPE_FIELDS, "webhook.receive", "envelope_fields")
    payload = envelope.get("payload")
    _require(isinstance(payload, dict), "webhook.receive", "payload_type")
    _require(set(payload) == ERROR_MAP_EVENT_PAYLOAD_FIELDS, "webhook.receive", "payload_fields")
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    _require(PRIVATE_PROVIDER_SENTINEL not in encoded, "webhook.receive", "provider_body")
    _require(PRIVATE_PROMPT_SENTINEL not in encoded, "webhook.receive", "prompt")
    _require(event_header == "error_map.updated", "webhook.receive", "event_header")
    _require(
        WEBHOOK_DELIVERY_ID_PATTERN.fullmatch(delivery_header) is not None,
        "webhook.receive",
        "delivery_id",
    )
    _require(bool(signing_secret), "webhook.receive", "secret_missing")
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

    event_id = str(envelope.get("event_id", ""))
    map_id = str(payload.get("provider_error_map_id", ""))
    tenant_id = str(envelope.get("tenant_id", ""))
    actor_id = str(envelope.get("actor_id", ""))
    trace_id = str(envelope.get("trace_id", ""))
    request_id = str(payload.get("request_id", ""))
    _require(
        WEBHOOK_EVENT_ID_PATTERN.fullmatch(event_id) is not None, "webhook.receive", "event_id"
    )
    _require(ERROR_MAP_ID_PATTERN.fullmatch(map_id) is not None, "webhook.receive", "map_id")
    _require(envelope.get("event_type") == "error_map.updated", "webhook.receive", "event_type")
    _require(envelope.get("object_id") == map_id, "webhook.receive", "object_id")
    _require(
        all(
            SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None
            for value in (tenant_id, actor_id, trace_id, request_id)
        ),
        "webhook.receive",
        "identity",
    )
    operation = payload.get("operation")
    rule_version = payload.get("rule_version")
    head_revision = payload.get("head_revision")
    priority = payload.get("priority")
    _require(operation in {"create", "update", "enable", "disable"}, "webhook.receive", "operation")
    _require(type(rule_version) is int and rule_version >= 1, "webhook.receive", "rule_version")
    _require(type(head_revision) is int and head_revision >= 1, "webhook.receive", "head_revision")
    _require(type(priority) is int and 1 <= priority <= 1000, "webhook.receive", "priority")
    _require(payload.get("schema_version") == 1, "webhook.receive", "schema_version")
    _require(payload.get("status") in {"active", "disabled"}, "webhook.receive", "status")
    _require(
        payload.get("output_error_code")
        in {
            "gateway.upstream_rate_limited",
            "gateway.upstream_quota_exceeded",
            "gateway.upstream_model_not_found",
            "gateway.upstream_resource_not_found",
            "gateway.upstream_request_invalid",
        },
        "webhook.receive",
        "output_code",
    )
    _require(
        SHA256_PATTERN.fullmatch(str(payload.get("pattern_hash", ""))) is not None,
        "webhook.receive",
        "pattern_hash",
    )
    provider_scope = str(payload.get("provider_scope", ""))
    _require(
        provider_scope == "all" or SAFE_IDENTIFIER_PATTERN.fullmatch(provider_scope) is not None,
        "webhook.receive",
        "provider_scope",
    )
    return {
        "event_id": event_id,
        "delivery_id": delivery_header,
        "provider_error_map_id": map_id,
        "operation": str(operation),
        "rule_version": int(rule_version),
        "head_revision": int(head_revision),
        "signature_valid": True,
        "privacy_verified": True,
    }


def verify_source_event_bindings(
    event_rows: Iterable[Mapping[str, Any]],
    summaries: Iterable[Mapping[str, Any]],
    *,
    provider_error_map_id: str,
) -> int:
    """把每个 Source Event ID 与数据库冻结的操作、版本和 revision 逐项绑定。

    Webhook 可能因提交后重试出现重复 HTTP 投递，因此不能只比较两个 ID 集合，也不能假设
    每个 ID 恰好出现一次；所有属于本批事件的验签摘要都必须与同 ID 数据库事实完全一致。
    返回通过绑定检查的真实 HTTP 投递数量，供最终证据记录。
    """

    rows = tuple(event_rows)
    expected = {
        str(row["provider_error_map_event_id"]): (
            str(row["operation"]),
            int(row["rule_version"]),
            int(row["head_revision"]),
        )
        for row in rows
    }
    _require(
        bool(expected) and len(expected) == len(rows),
        "webhook.delivery",
        "event_identity",
    )
    accepted = [item for item in summaries if str(item.get("event_id", "")) in expected]
    observed_ids = {str(item.get("event_id", "")) for item in accepted}
    _require(
        set(expected) <= observed_ids
        and all(
            item.get("provider_error_map_id") == provider_error_map_id
            and item.get("signature_valid") is True
            and item.get("privacy_verified") is True
            and (
                item.get("operation"),
                item.get("rule_version"),
                item.get("head_revision"),
            )
            == expected[str(item.get("event_id", ""))]
            for item in accepted
        ),
        "webhook.delivery",
        "event_binding",
    )
    return len(accepted)


class WebhookReceiverServer(ThreadingHTTPServer):
    """在回环随机端口接收并验签本轮 error_map.updated，不保存原始正文。"""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(self) -> None:
        """初始化空 Secret 与安全摘要列表；Secret 由创建 Endpoint 响应一次性安装。"""

        self._secret = ""
        self._summaries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _WebhookReceiverHandler)

    def install_secret(self, value: str) -> None:
        """安装当前 Endpoint signing secret，只在内存中用于恒定时间验签。"""

        _require(bool(value), "webhook.receiver", "secret_missing")
        with self._lock:
            self._secret = value

    def signing_secret(self) -> str:
        """返回当前内存 Secret 给接收线程；调用方不得记录或序列化该值。"""

        with self._lock:
            # Secret 只跨线程短暂传递给验签器，调用方禁止记录或序列化返回值。
            return self._secret

    def record(self, summary: Mapping[str, Any]) -> None:
        """保存已验签的安全摘要副本，禁止保存 Webhook 原始正文或 Header。"""

        with self._lock:
            self._summaries.append(dict(summary))

    def summaries(self) -> list[dict[str, Any]]:
        """返回安全摘要副本，供主线程按 source event ID 核对实际投递。"""

        with self._lock:
            return [dict(item) for item in self._summaries]

    def clear_sensitive(self) -> None:
        """关闭接收器后清空唯一一次返回的签名密钥。"""

        with self._lock:
            self._secret = ""


class _WebhookReceiverHandler(BaseHTTPRequestHandler):
    """实现有界 HMAC Webhook 接收协议，所有异常只返回固定状态。"""

    server: WebhookReceiverServer

    def do_POST(self) -> None:
        """只接受 /hook，并在验签后保存不含正文的安全摘要。"""

        if self.path != "/hook":
            self._empty_response(404)
            return
        body = self._read_bounded_body()
        if body is None:
            return
        try:
            summary = validate_error_map_webhook(
                body,
                signature=self.headers.get("X-MetaHub-Signature", ""),
                signing_secret=self.server.signing_secret(),
                event_header=self.headers.get("X-MetaHub-Event", ""),
                delivery_header=self.headers.get("X-MetaHub-Delivery", ""),
            )
        except AcceptanceFailure:
            self._empty_response(401)
            return
        self.server.record(summary)
        self._empty_response(204)

    def _read_bounded_body(self) -> bytes | None:
        """拒绝 chunked、重复长度、零长度和超限正文，避免接收端无界缓冲。"""

        lengths = self.headers.get_all("Content-Length") or []
        transfers = self.headers.get_all("Transfer-Encoding") or []
        if len(lengths) != 1 or transfers or re.fullmatch(r"[0-9]{1,9}", lengths[0]) is None:
            self._empty_response(413)
            return None
        length = int(lengths[0])
        if length < 1 or length > MAX_HTTP_BODY_BYTES:
            self._empty_response(413)
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self._empty_response(400)
            return None
        return body

    def _empty_response(self, status: int) -> None:
        """发送无正文固定状态，并主动关闭连接避免歧义的请求复用。"""

        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        """关闭标准库访问日志，避免签名、Endpoint 或请求元数据进入终端。"""


class RuntimeAcceptance:
    """执行迁移、RBAC、租户、热更新、真实失败和冻结事实验收。"""

    def __init__(
        self,
        config: RuntimeConfig,
        control: httpx.Client,
        gateway: httpx.Client,
        console: httpx.Client,
    ) -> None:
        """初始化内存账本；任何敏感值都不进入最终证据映射。"""

        self.config = config
        self.control = control
        self.gateway = gateway
        self.console = console
        self.sessions: dict[str, SessionCredential] = {}
        self.suffix = secrets.token_hex(6)
        self.resource_ids: dict[str, str] = {}
        self.temporary_user_ids: list[tuple[str, str]] = []
        self.known_sensitive: list[str] = [
            config.admin_password,
            config.virtual_key,
            PRIVATE_PROVIDER_SENTINEL,
            PRIVATE_PROMPT_SENTINEL,
        ]
        # storage_sensitive 只含不得明文持久化的值；邮箱和 Endpoint 有合法 owner，不能误报。
        self.storage_sensitive: list[str] = list(self.known_sensitive)
        self.failure_request_ids: list[str] = []
        self.original_provider_base_url: str | None = None
        self.provider_endpoint_changed = False

    def _connect(self) -> sqlite3.Connection:
        """打开同批次 SQLite，并强制外键与命名列访问。"""

        connection = sqlite3.connect(self.config.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def verify_database_ready(self) -> dict[str, Any]:
        """验证 0046 checksum、核心对象、builtin 目录和完整性。"""

        connection = self._connect()
        try:
            migration = connection.execute(
                "SELECT status,checksum FROM schema_migrations WHERE version='0046'"
            ).fetchone()
            _require(
                migration is not None and migration["status"] == "applied",
                "database.migration",
                "missing",
            )
            _require(
                hmac.compare_digest(str(migration["checksum"]), _migration_checksum()),
                "database.migration",
                "checksum",
            )
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            _require(REQUIRED_TABLES <= tables, "database.objects", "table_missing")
            catalog = connection.execute(
                "SELECT builtin_catalog_version,catalog_hash "
                "FROM provider_error_builtin_catalog_versions"
            ).fetchall()
            rules = connection.execute(
                "SELECT COUNT(*) FROM provider_error_builtin_rule_versions"
            ).fetchone()[0]
            _require(
                len(catalog) == 1
                and catalog[0]["builtin_catalog_version"] == "provider-error-builtin-v1"
                and SHA256_PATTERN.fullmatch(str(catalog[0]["catalog_hash"])) is not None,
                "database.catalog",
                "version",
            )
            _require(rules == 13, "database.catalog", "rule_count")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            _require(
                len(integrity) == 1 and str(integrity[0][0]).lower() == "ok",
                "database.integrity",
                "failed",
            )
            _require(not foreign_keys, "database.foreign_keys", "failed")
            return {
                "migration": "0046",
                "migration_checksum_verified": True,
                "required_tables_verified": len(REQUIRED_TABLES),
                "builtin_catalog_version": "provider-error-builtin-v1",
                "builtin_rule_count": 13,
                "integrity_check": "ok",
                "foreign_key_check": "ok",
            }
        # 数据库异常可能包含路径、SQL 或持久值，Runtime 证据只记录固定失败阶段。
        except sqlite3.Error:
            _fail("database.ready", "query_failed")
        finally:
            connection.close()

    def verify_services(self) -> dict[str, bool]:
        """核验三个本机进程的真实健康入口和 Console HTML。"""

        result: dict[str, bool] = {}
        for name, client, path in (
            ("control", self.control, "/health"),
            ("gateway", self.gateway, "/health"),
            ("console", self.console, "/"),
        ):
            try:
                response = client.get(path)
            # 本机服务连接异常不写入证据正文，只标记对应健康阶段不可达。
            except httpx.HTTPError:
                _fail(f"services.{name}", "unreachable")
            _require(response.status_code == 200, f"services.{name}", "status")
            result[f"{name}_healthy"] = True
        return result

    def _headers(
        self, action: str, *, session_key: str | None = None
    ) -> tuple[dict[str, str], str, str]:
        """构造一次真实请求的链路头；Token 仅在指定 Session 时附加。"""

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {"Accept": "application/json", "X-Request-ID": request_id, "X-Trace-ID": trace_id}
        # Authorization 仅在显式 Session 场景附加，匿名和内部检查不得意外复用 Token。
        if session_key is not None:
            session = self.sessions.get(session_key)
            _require(
                session is not None and bool(session.access_token),
                "auth.headers",
                "session_missing",
            )
            headers["Authorization"] = f"Bearer {session.access_token}"
        return headers, request_id, trace_id

    def _control_call(
        self,
        action: str,
        method: str,
        path: str,
        *,
        session_key: str | None,
        status: int,
        body: Mapping[str, Any] | None = None,
        code: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """调用 Control API 并核验统一响应；原始响应正文不进入证据。"""

        headers, request_id, trace_id = self._headers(action, session_key=session_key)
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.control.request(method, path, headers=headers, json=body)
        # 请求异常可能携带 URL 或 Header，验收输出只保留固定动作阶段和失败分类。
        except httpx.HTTPError:
            _fail(f"control.{action}", "request_failed")
        return verify_response(
            response,
            stage=f"control.{action}",
            status=status,
            request_id=request_id,
            trace_id=trace_id,
            code=code,
        ), request_id

    def _login(
        self,
        key: str,
        *,
        tenant_slug: str,
        email: str,
        password: str,
        role: str,
    ) -> None:
        """通过公开登录建立真实 Session，并与同库 owner 和 token_hash 交叉核对。"""

        headers, request_id, trace_id = self._headers(f"login_{key}")
        response = self.control.post(
            "/auth/login",
            headers={**headers, "Content-Type": "application/json"},
            json={"tenant_slug": tenant_slug, "email": email, "password": password},
        )
        payload = verify_response(
            response,
            stage=f"auth.login_{key}",
            status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        user = payload.get("user")
        _require(isinstance(user, dict) and user.get("role") == role, f"auth.login_{key}", "role")
        fields = (
            user.get("tenant_id"),
            user.get("user_id"),
            payload.get("session_id"),
            payload.get("access_token"),
        )
        _require(
            all(isinstance(item, str) and item for item in fields), f"auth.login_{key}", "fields"
        )
        session = SessionCredential(
            key,
            role,
            tenant_slug,
            str(fields[0]),
            str(fields[1]),
            str(fields[2]),
            str(fields[3]),
            email,
            password,
        )
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT tenant_id,user_id,token_hash,status FROM sessions WHERE session_id=?",
                (session.session_id,),
            ).fetchone()
        finally:
            connection.close()
        expected_hash = hashlib.sha256(session.access_token.encode("utf-8")).hexdigest()
        _require(
            row is not None
            and row["tenant_id"] == session.tenant_id
            and row["user_id"] == session.user_id
            and row["status"] == "active"
            and hmac.compare_digest(str(row["token_hash"]), expected_hash),
            f"auth.login_{key}",
            "database_binding",
        )
        self.sessions[key] = session
        self.known_sensitive.extend((email, password, session.access_token))
        self.storage_sensitive.extend((password, session.access_token))

    def prepare_identities(self) -> dict[str, bool]:
        """创建 developer/viewer 与第二租户管理员，形成真实权限和横向攻击身份。"""

        self._login(
            "admin",
            tenant_slug=self.config.tenant_slug,
            email=self.config.admin_email,
            password=self.config.admin_password,
            role="admin",
        )
        for role in ("developer", "viewer"):
            email = f"prd056-{role}-{self.suffix}@metahub.local"
            password = f"Prd056!{secrets.token_urlsafe(18)}"
            payload, _ = self._control_call(
                f"create_{role}",
                "POST",
                "/users",
                session_key="admin",
                status=201,
                body={
                    "email": email,
                    "display_name": f"PRD056 runtime {role}",
                    "password": password,
                    "role": role,
                    "status": "active",
                },
            )
            user = payload.get("user")
            _require(
                isinstance(user, dict) and user.get("role") == role, f"users.{role}", "response"
            )
            user_id = str(user.get("user_id", ""))
            _require(
                SAFE_IDENTIFIER_PATTERN.fullmatch(user_id) is not None, f"users.{role}", "user_id"
            )
            self.temporary_user_ids.append((str(user.get("tenant_id")), user_id))
            self._login(
                role, tenant_slug=self.config.tenant_slug, email=email, password=password, role=role
            )

        other_tenant_id = f"tenant_p56_{self.suffix}"
        other_slug = f"p56-{self.suffix}"
        other_user_id = f"user_p56_{self.suffix}"
        other_email = f"prd056-admin-{self.suffix}@other.metahub.local"
        other_password = f"Prd056!{secrets.token_urlsafe(18)}"
        timestamp = "2026-07-29T00:00:00Z"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO tenants("
                "tenant_id,slug,name,service_mode,status,created_at,updated_at) "
                "VALUES(?,?,?,'internal_only','active',?,?)",
                (other_tenant_id, other_slug, "PRD056 isolated tenant", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,"
                "created_at,updated_at,failed_login_count,locked_until) "
                "VALUES(?,?,?,?,?,'admin','active',?,?,0,NULL)",
                (
                    other_user_id,
                    other_tenant_id,
                    other_email,
                    "PRD056 isolated admin",
                    hash_password(other_password),
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        # 夹具事务失败必须回滚且只记录固定分类，避免路径、邮箱或 SQL 进入证据。
        except sqlite3.Error:
            connection.rollback()
            _fail("fixtures.other_tenant", "insert_failed")
        finally:
            connection.close()
        self.temporary_user_ids.append((other_tenant_id, other_user_id))
        self._login(
            "other_admin",
            tenant_slug=other_slug,
            email=other_email,
            password=other_password,
            role="admin",
        )
        return {
            "admin_session_bound": True,
            "developer_session_bound": True,
            "viewer_session_bound": True,
            "secondary_tenant_session_bound": True,
        }

    def verify_permissions(self) -> dict[str, Any]:
        """验证 RBAC 先于 wire，并冻结后续上游验收使用的同租户 Provider。"""

        admin_list, _ = self._control_call(
            "admin_list", "GET", "/error-maps", session_key="admin", status=200
        )
        developer_list, _ = self._control_call(
            "developer_list", "GET", "/error-maps", session_key="developer", status=200
        )
        self._control_call(
            "viewer_denied",
            "GET",
            "/error-maps?unknown=1",
            session_key="viewer",
            status=403,
            code="auth.permission_denied",
        )
        self._control_call(
            "anonymous_denied",
            "GET",
            "/error-maps",
            session_key=None,
            status=401,
            code="auth.token_missing",
        )
        admin_maps = admin_list.get("error_maps")
        developer_maps = developer_list.get("error_maps")
        _require(
            isinstance(admin_maps, list)
            and isinstance(developer_maps, list)
            and admin_maps == developer_maps,
            "error_maps.list",
            "response_key",
        )
        self._control_call(
            "developer_write_denied",
            "POST",
            "/error-maps",
            session_key="developer",
            status=403,
            body={"tenant_id": "must-not-be-parsed"},
            code="auth.permission_denied",
        )
        options = admin_list.get("provider_options")
        _require(isinstance(options, list) and options, "error_maps.list", "provider_options")
        provider_id = str(options[0].get("provider_id", ""))
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(provider_id) is not None,
            "error_maps.list",
            "provider_id",
        )
        _require(
            developer_list.get("head_revision") == admin_list.get("head_revision"),
            "error_maps.list",
            "role_drift",
        )
        _require(admin_list.get("head_revision") == 0, "error_maps.list", "database_not_fresh")
        self.resource_ids["provider_id"] = provider_id
        return {
            "admin_read": True,
            "developer_read_only": True,
            "viewer_backend_denied": True,
            "anonymous_denied": True,
            "wire_not_parsed_before_rbac": True,
        }

    def create_runtime_webhook(
        self,
        receiver: WebhookReceiverServer,
    ) -> dict[str, bool]:
        """经真实管理 API 创建本轮回环 Endpoint，并把一次性 Secret 交给验签器。"""

        host, port = receiver.server_address
        endpoint_url = f"http://{host}:{port}/hook"
        payload, _ = self._control_call(
            "webhook_create",
            "POST",
            "/webhooks",
            session_key="admin",
            status=200,
            body={
                "name": f"PRD056 runtime webhook {self.suffix}",
                "endpoint_url": endpoint_url,
                "event_types": ["error_map.updated"],
                "status": "active",
                "timeout_ms": 2000,
            },
        )
        webhook = payload.get("webhook")
        signing_secret = payload.get("signing_secret")
        _require(isinstance(webhook, dict), "webhook.create", "webhook")
        _require(
            isinstance(signing_secret, str) and signing_secret.startswith("whsec_"),
            "webhook.create",
            "secret",
        )
        endpoint_id = str(webhook.get("webhook_endpoint_id", ""))
        _require(
            SAFE_IDENTIFIER_PATTERN.fullmatch(endpoint_id) is not None,
            "webhook.create",
            "endpoint_id",
        )
        _require(
            webhook.get("tenant_id") == self.sessions["admin"].tenant_id
            and webhook.get("event_types") == ["error_map.updated"],
            "webhook.create",
            "owner",
        )
        self.resource_ids["webhook_endpoint_id"] = endpoint_id
        self.known_sensitive.extend((signing_secret, endpoint_url))
        self.storage_sensitive.append(signing_secret)
        receiver.install_secret(signing_secret)
        return {
            "created_by_public_api": True,
            "error_map_updated_subscribed": True,
        }

    def create_rule_and_verify_tenant(self) -> dict[str, Any]:
        """创建 provider-specific overlay，并验证跨租户、版本和 stale CAS 边界。"""

        provider_id = self.resource_ids["provider_id"]
        rule = {
            "provider_id": provider_id,
            "priority": 900,
            "http_statuses": [404],
            "http_status_class": None,
            "error_field": "error.type",
            "field_operator": "equals",
            "field_pattern": "tenant_catalog_absent",
            "message_operator": None,
            "message_pattern": None,
            "output_error_code": "gateway.upstream_model_not_found",
            "status": "active",
        }
        created, create_request_id = self._control_call(
            "create_rule", "POST", "/error-maps", session_key="admin", status=201, body=rule
        )
        item = created.get("error_map")
        _require(isinstance(item, dict), "error_maps.create", "item")
        error_map_id = str(item.get("error_map_id", ""))
        _require(
            ERROR_MAP_ID_PATTERN.fullmatch(error_map_id) is not None, "error_maps.create", "id"
        )
        _require(
            item.get("version") == 1 and created.get("head_revision") >= 1,
            "error_maps.create",
            "version",
        )
        self.resource_ids.update(
            {
                "provider_id": provider_id,
                "error_map_id": error_map_id,
                "create_request_id": create_request_id,
                "head_revision": str(created["head_revision"]),
            }
        )
        self._control_call(
            "other_exact_list",
            "GET",
            f"/error-maps?error_map_id={error_map_id}",
            session_key="other_admin",
            status=404,
            code="error_map.not_found",
        )
        self._control_call(
            "other_patch_denied",
            "PATCH",
            f"/error-maps/{error_map_id}",
            session_key="other_admin",
            status=404,
            body={"expected_version": 1, "priority": 901},
            code="error_map.not_found",
        )
        changed, _ = self._control_call(
            "patch_rule_v2",
            "PATCH",
            f"/error-maps/{error_map_id}",
            session_key="admin",
            status=200,
            body={"expected_version": 1, "priority": 901},
        )
        _require(
            changed.get("head_revision") == 2,
            "error_maps.patch",
            "head_revision",
        )
        _require(changed.get("error_map", {}).get("version") == 2, "error_maps.patch", "version")
        self._control_call(
            "stale_patch",
            "PATCH",
            f"/error-maps/{error_map_id}",
            session_key="admin",
            status=409,
            body={"expected_version": 1, "priority": 902},
            code="error_map.version_conflict",
        )
        self.resource_ids["head_revision"] = str(changed["head_revision"])
        self.resource_ids["version"] = "2"
        return {
            "admin_write": True,
            "developer_read_only": True,
            "viewer_backend_denied": True,
            "anonymous_denied": True,
            "tenant_read_isolated": True,
            "tenant_write_isolated": True,
            "stale_cas_rejected": True,
            "created_version": 1,
            "current_version": 2,
        }

    def verify_runtime_webhook(
        self,
        receiver: WebhookReceiverServer,
    ) -> dict[str, Any]:
        """等待两条 source event 真实投递，并交叉核对验签摘要与数据库终态。"""

        map_id = self.resource_ids["error_map_id"]
        deadline = time.monotonic() + 15.0
        event_rows: list[sqlite3.Row] = []
        summaries: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            connection = self._connect()
            try:
                event_rows = connection.execute(
                    "SELECT provider_error_map_event_id,operation,rule_version,head_revision,"
                    "dispatch_status,dispatch_attempts FROM provider_error_map_events "
                    "WHERE tenant_id=? AND provider_error_map_id=? "
                    "ORDER BY head_revision",
                    (self.sessions["admin"].tenant_id, map_id),
                ).fetchall()
            finally:
                connection.close()
            summaries = receiver.summaries()
            delivered_ids = {str(item.get("event_id", "")) for item in summaries}
            expected_ids = {str(row["provider_error_map_event_id"]) for row in event_rows}
            if (
                len(event_rows) == 2
                and all(row["dispatch_status"] == "dispatched" for row in event_rows)
                and expected_ids <= delivered_ids
            ):
                break
            time.sleep(0.25)
        _require(
            len(event_rows) == 2 and [int(row["head_revision"]) for row in event_rows] == [1, 2],
            "webhook.delivery",
            "source_events",
        )
        _require(
            all(
                row["dispatch_status"] == "dispatched" and 1 <= int(row["dispatch_attempts"]) <= 10
                for row in event_rows
            ),
            "webhook.delivery",
            "source_terminal",
        )
        accepted_count = verify_source_event_bindings(
            (dict(row) for row in event_rows), summaries, provider_error_map_id=map_id
        )
        return {
            "source_events_dispatched": 2,
            "source_event_revisions": [1, 2],
            "real_http_deliveries_verified": accepted_count,
            "event_payload_binding_verified": True,
            "signature_verified": True,
            "payload_privacy_verified": True,
        }

    def verify_builtin_baseline(self) -> dict[str, bool]:
        """在创建 tenant overlay 前冻结 revision 0，并区分普通 404 与模型不存在。"""

        state = MockProviderState()
        server = MockProviderServer(state)
        worker = threading.Thread(
            target=server.serve_forever,
            name="prd056-baseline-provider",
            daemon=True,
        )
        worker.start()
        try:
            self._install_mock_endpoint(server)
            state.set(
                status=404,
                error_type="resource_absent",
                error_code="resource_absent",
                message=PRIVATE_PROVIDER_SENTINEL,
            )
            generic_request_id = self._gateway_failure(
                "builtin_generic_404",
                stream=False,
                expected_code="gateway.upstream_resource_not_found",
            )
            state.set(
                status=404,
                error_type="model_not_found",
                error_code="model_not_found",
                message=PRIVATE_PROVIDER_SENTINEL,
            )
            model_request_id = self._gateway_failure(
                "builtin_model_not_found",
                stream=False,
                expected_code="gateway.upstream_model_not_found",
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        _require(not worker.is_alive(), "gateway.baseline", "thread_alive")
        self.resource_ids["baseline_generic_request_id"] = generic_request_id
        self.resource_ids["baseline_model_request_id"] = model_request_id
        return {
            "revision_zero_loaded": True,
            "generic_404_is_resource_not_found": True,
            "model_signal_is_model_not_found": True,
        }

    def _install_mock_endpoint(self, server: MockProviderServer) -> None:
        """把同租户 Provider 临时指向回环上游，并首次冻结原 Endpoint 供最终恢复。"""

        provider_id = self.resource_ids["provider_id"]
        tenant_id = self.sessions["admin"].tenant_id
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT base_url FROM providers WHERE tenant_id=? AND provider_id=?",
                (tenant_id, provider_id),
            ).fetchone()
            _require(owner is not None, "mock_provider.install", "provider_missing")
            # 仅第一次替换时冻结原 Endpoint，后续受控请求不得覆盖最终恢复基线。
            if self.original_provider_base_url is None:
                self.original_provider_base_url = str(owner["base_url"])
                self.known_sensitive.append(self.original_provider_base_url)
            cursor = connection.execute(
                "UPDATE providers SET base_url=?,updated_at=datetime('now') "
                "WHERE tenant_id=? AND provider_id=?",
                (base_url, tenant_id, provider_id),
            )
            _require(cursor.rowcount == 1, "mock_provider.install", "provider_missing")
            connection.commit()
            self.provider_endpoint_changed = True
            self.known_sensitive.append(base_url)
        # 安装事务失败必须整体回滚，并把可能含路径的 SQLite 异常折叠为固定分类。
        except sqlite3.Error:
            connection.rollback()
            _fail("mock_provider.install", "database")
        finally:
            connection.close()

    def restore_provider_endpoint(self) -> bool:
        """按 tenant_id+provider_id 恢复原 Endpoint，并读取回验最终值。"""

        # 从未替换过 Endpoint 时无需写库，保持验收器可重复执行清理。
        if not self.provider_endpoint_changed:
            return True
        provider_id = self.resource_ids.get("provider_id", "")
        tenant_id = self.sessions.get("admin").tenant_id if self.sessions.get("admin") else ""
        original = self.original_provider_base_url
        # 缺少任一 tenant-qualified owner 字段都失败关闭，禁止按 provider_id 单独恢复。
        if not provider_id or not tenant_id or original is None:
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE providers SET base_url=?,updated_at=datetime('now') "
                "WHERE tenant_id=? AND provider_id=?",
                (original, tenant_id, provider_id),
            )
            if changed.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            restored = connection.execute(
                "SELECT base_url FROM providers WHERE tenant_id=? AND provider_id=?",
                (tenant_id, provider_id),
            ).fetchone()
            success = restored is not None and str(restored["base_url"]) == original
            if success:
                self.provider_endpoint_changed = False
            return success
        # 恢复事务异常只返回失败布尔值，不能让数据库正文覆盖主验收结论。
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

    def restore_provider_key_cooldown(self) -> bool:
        """恢复受控 429 引入的唯一托管 Key 冷却，并保持历史错误事实不变。

        返回：当前管理员租户与 Provider 恰有一把托管 Key，且最终可再次参与选择时返回真。
        约束：只允许把 ``rate_limited + 非空截止时间`` 恢复为 active；禁用、损坏、多 Key 或
        owner 缺失一律失败关闭，且不清除 last_error_code/last_error_message 追踪事实。
        """

        session = self.sessions.get("admin")
        provider_id = self.resource_ids.get("provider_id", "")
        # 缺少可信租户 owner 或 Provider ID 形态异常时禁止进入恢复事务。
        if session is None or SAFE_IDENTIFIER_PATTERN.fullmatch(provider_id) is None:
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT provider_credential_id,status,rate_limited_until "
                "FROM provider_credentials WHERE tenant_id=? AND provider_id=? "
                "AND encrypted_key LIKE 'aesgcm-v1:%'",
                (session.tenant_id, provider_id),
            ).fetchall()
            # fresh Runtime 只允许唯一托管 Key；多 Key 时无法证明是哪一把由本用例冷却。
            if len(rows) != 1:
                connection.rollback()
                return False
            row = rows[0]
            # 已恢复状态允许幂等清理，但 active 与残留截止时间混合时仍视为损坏。
            if row["status"] == "active" and row["rate_limited_until"] is None:
                connection.commit()
                return True
            # 只恢复本轮真实 429 产生的精确状态，不能把 disabled 等治理状态改回 active。
            if row["status"] != "rate_limited" or row["rate_limited_until"] is None:
                connection.rollback()
                return False
            updated = connection.execute(
                "UPDATE provider_credentials SET status='active',rate_limited_until=NULL,"
                "updated_at=datetime('now') WHERE tenant_id=? AND provider_id=? "
                "AND provider_credential_id=? AND status='rate_limited' "
                "AND rate_limited_until IS NOT NULL",
                (session.tenant_id, provider_id, row["provider_credential_id"]),
            )
            # CAS 未命中说明状态在事务边界内不符合预期，禁止报告假恢复。
            if updated.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            restored = connection.execute(
                "SELECT status,rate_limited_until FROM provider_credentials "
                "WHERE tenant_id=? AND provider_id=? AND provider_credential_id=?",
                (session.tenant_id, provider_id, row["provider_credential_id"]),
            ).fetchone()
            return (
                restored is not None
                and restored["status"] == "active"
                and restored["rate_limited_until"] is None
            )
        # 恢复异常只返回失败布尔值，SQLite 细节和 Credential 身份不得进入验收输出。
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()


    def _gateway_failure(
        self,
        action: str,
        *,
        stream: bool,
        expected_code: str,
    ) -> str:
        """发起真实 Gateway 调用并验证统一错误、request_id 和 trace_id。"""

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {
            "Authorization": f"Bearer {self.config.virtual_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
        }
        try:
            response = self.gateway.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": PRIVATE_PROMPT_SENTINEL}],
                    "stream": stream,
                },
            )
        # Gateway 网络异常不保存 URL、Virtual Key 或响应正文，只标记动作失败。
        except httpx.HTTPError:
            _fail(f"gateway.{action}", "request_failed")
        payload = _json_object(response, f"gateway.{action}")
        actual_code = payload.get("code") or payload.get("error", {}).get("code")
        actual_request_id = payload.get("request_id") or payload.get("error", {}).get("request_id")
        actual_trace_id = payload.get("trace_id") or payload.get("error", {}).get("trace_id")
        _require(response.status_code >= 400, f"gateway.{action}", "status")
        _require(
            actual_request_id == request_id and actual_trace_id == trace_id,
            f"gateway.{action}",
            "identity",
        )
        # 即使热更新轮询暂时命中旧 revision，该真实失败也必须进入后续 Fact/RequestLog 追踪。
        self.failure_request_ids.append(request_id)
        _require(actual_code == expected_code, f"gateway.{action}", "code")
        return request_id

    def verify_gateway_and_facts(self) -> dict[str, Any]:
        """验证热更新矩阵，并证明每个真实失败都具有 Fact 与 RequestLog 追踪。"""

        state = MockProviderState()
        server = MockProviderServer(state)
        worker = threading.Thread(
            target=server.serve_forever,
            name="prd056-mock-provider",
            daemon=True,
        )
        worker.start()
        matrix_request_ids: dict[str, str] = {}
        try:
            self._install_mock_endpoint(server)
            state.set(
                status=404,
                error_type="tenant_catalog_absent",
                error_code="tenant_catalog_absent",
                message=PRIVATE_PROVIDER_SENTINEL,
            )
            deadline = time.monotonic() + 10.0
            hot_request_id = ""
            while time.monotonic() < deadline:
                try:
                    hot_request_id = self._gateway_failure(
                        f"hot_update_{secrets.token_hex(2)}",
                        stream=False,
                        expected_code="gateway.upstream_model_not_found",
                    )
                    break
                except AcceptanceFailure as exc:
                    if exc.code != "code":
                        raise
                    time.sleep(0.5)
            _require(bool(hot_request_id), "gateway.hot_update", "not_visible")

            matrix = (
                ("auth401", 401, "auth_error", "auth_error", "gateway.upstream_auth_failed"),
                ("auth403", 403, "auth_error", "auth_error", "gateway.upstream_auth_failed"),
                (
                    "generic404",
                    404,
                    "resource_absent",
                    "resource_absent",
                    "gateway.upstream_resource_not_found",
                ),
                (
                    "rate429",
                    429,
                    "rate_limit_error",
                    "rate_limit_error",
                    "gateway.upstream_rate_limited",
                ),
                (
                    "balance429",
                    429,
                    "quota_error",
                    "insufficient_balance",
                    "gateway.upstream_quota_exceeded",
                ),
                ("server503", 503, "server_error", "server_error", "gateway.upstream_error"),
            )
            observed: list[str] = []
            for action, status, error_type, error_code, expected in matrix:
                state.set(
                    status=status,
                    error_type=error_type,
                    error_code=error_code,
                    message=PRIVATE_PROVIDER_SENTINEL,
                )
                request_id = self._gateway_failure(
                    action,
                    stream=False,
                    expected_code=expected,
                )
                matrix_request_ids[action] = request_id
                observed.append(expected)
                if status == 429:
                    _require(
                        self.restore_provider_key_cooldown(),
                        "gateway.key_cooldown",
                        action,
                    )
            state.set(
                status=429,
                error_type="rate_limit_error",
                error_code="rate_limit_error",
                message=PRIVATE_PROVIDER_SENTINEL,
            )
            stream_request_id = self._gateway_failure(
                "stream_handshake_429",
                stream=True,
                expected_code="gateway.upstream_rate_limited",
            )
            _require(
                self.restore_provider_key_cooldown(),
                "gateway.key_cooldown",
                "stream_handshake_429",
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        _require(not worker.is_alive(), "mock_provider.cleanup", "thread_alive")

        nonstream_request_id = matrix_request_ids["rate429"]
        self.resource_ids["mapped_request_id"] = hot_request_id
        self.resource_ids["nonstream_request_id"] = nonstream_request_id
        self.resource_ids["stream_request_id"] = stream_request_id
        expected_request_ids = tuple(self.failure_request_ids)
        _require(
            len(expected_request_ids) >= 10
            and len(expected_request_ids) == len(set(expected_request_ids)),
            "facts.database",
            "request_ledger",
        )
        placeholders = ",".join("?" for _ in expected_request_ids)
        connection = self._connect()
        try:
            deadline = time.monotonic() + 10.0
            facts: list[sqlite3.Row] = []
            while time.monotonic() < deadline:
                facts = connection.execute(
                    "SELECT * FROM provider_error_facts "
                    f"WHERE request_id IN ({placeholders}) ORDER BY created_at",
                    expected_request_ids,
                ).fetchall()
                # 只有本批全部 request_id 均出现 Fact 才结束轮询，避免迟到写入造成假通过。
                if {str(row["request_id"]) for row in facts} >= set(expected_request_ids):
                    break
                time.sleep(0.25)
            facts_by_request: dict[str, list[sqlite3.Row]] = {}
            for row in facts:
                facts_by_request.setdefault(str(row["request_id"]), []).append(row)
            _require(
                set(expected_request_ids) <= set(facts_by_request),
                "facts.database",
                "missing",
            )

            mapped_fact = next(
                (
                    row
                    for row in facts_by_request[hot_request_id]
                    if row["matched_rule_source"] == "tenant"
                ),
                None,
            )
            _require(
                mapped_fact is not None
                and mapped_fact["routing_attempt_id"] is not None
                and mapped_fact["matched_error_map_id"] == self.resource_ids["error_map_id"]
                and int(mapped_fact["matched_rule_version"]) == int(self.resource_ids["version"])
                and int(mapped_fact["error_map_revision"])
                == int(self.resource_ids["head_revision"]),
                "facts.database",
                "mapped_identity",
            )
            baseline_generic = facts_by_request[self.resource_ids["baseline_generic_request_id"]]
            baseline_model = facts_by_request[self.resource_ids["baseline_model_request_id"]]
            _require(
                any(
                    row["matched_rule_source"] == "builtin"
                    and row["matched_error_map_id"] == "builtin_http_404"
                    and int(row["error_map_revision"]) == 0
                    for row in baseline_generic
                ),
                "facts.database",
                "baseline_generic",
            )
            _require(
                any(
                    row["matched_rule_source"] == "builtin"
                    and row["matched_error_map_id"] == "builtin_model_not_found_signal"
                    and int(row["error_map_revision"]) == 0
                    for row in baseline_model
                ),
                "facts.database",
                "baseline_model",
            )
            _require(
                any(
                    row["matched_error_map_id"] == "builtin_quota_signal"
                    and row["normalized_output_error_code"] == "gateway.upstream_quota_exceeded"
                    for row in facts_by_request[matrix_request_ids["balance429"]]
                ),
                "facts.database",
                "insufficient_balance",
            )
            _require(
                any(
                    row["routing_attempt_id"] is not None
                    for row in facts_by_request[nonstream_request_id]
                )
                and any(
                    row["stream_session_id"] is not None
                    for row in facts_by_request[stream_request_id]
                ),
                "facts.database",
                "owner",
            )
            for row in facts:
                _require(
                    row["tenant_id"] == self.sessions["admin"].tenant_id,
                    "facts.database",
                    "tenant",
                )
                _require(
                    (row["routing_attempt_id"] is None) != (row["stream_session_id"] is None),
                    "facts.database",
                    "owner_exclusive",
                )
                projection = str(row["redacted_projection_json"])
                _require(
                    PRIVATE_PROVIDER_SENTINEL not in projection
                    and PRIVATE_PROMPT_SENTINEL not in projection,
                    "facts.database",
                    "raw_body",
                )
                if int(row["message_present"]) == 1:
                    _require("[REDACTED]" in projection, "facts.database", "projection")
            raw_database = self.config.database_path.read_bytes()
            _require(
                PRIVATE_PROVIDER_SENTINEL.encode("utf-8") not in raw_database
                and PRIVATE_PROMPT_SENTINEL.encode("utf-8") not in raw_database,
                "facts.database",
                "sentinel_persisted",
            )
        # Fact 查询失败只暴露稳定阶段码，SQL、数据库路径和投影正文均不得外泄。
        except sqlite3.Error:
            _fail("facts.database", "query_failed")
        finally:
            connection.close()

        detail_results: dict[str, bool] = {}
        for index, request_id in enumerate(expected_request_ids, start=1):
            payload, _ = self._control_call(
                f"detail_{index}",
                "GET",
                f"/request-logs/{request_id}",
                session_key="developer",
                status=200,
            )
            request_log = payload.get("request_log")
            _require(
                isinstance(request_log, dict),
                "request_log.all_failures",
                "payload",
            )
            request_facts = facts_by_request[request_id]
            # owner 二选一决定 DTO 读取位置，流式 Fact 不能退回 RoutingAttempt 列表查找。
            if any(row["stream_session_id"] is not None for row in request_facts):
                owner = request_log.get("stream_session")
                has_fact = isinstance(owner, dict) and bool(owner.get("provider_error_fact"))
            else:
                owners = request_log.get("routing_attempts")
                has_fact = isinstance(owners, list) and any(
                    item.get("provider_error_fact") for item in owners if isinstance(item, dict)
                )
            _require(has_fact, "request_log.all_failures", "fact")
            encoded = json.dumps(payload, ensure_ascii=False)
            _require(
                PRIVATE_PROVIDER_SENTINEL not in encoded and PRIVATE_PROMPT_SENTINEL not in encoded,
                "request_log.all_failures",
                "privacy",
            )
        detail_results["all_failed_requests_renderable"] = True
        return {
            "hot_update_without_gateway_restart": True,
            "normalized_codes": sorted(set(observed)),
            "tracked_failure_requests": len(expected_request_ids),
            "mapped_tenant_fact_committed": True,
            "nonstream_fact_committed": True,
            "stream_handshake_fact_committed": True,
            "fact_owner_exclusive": True,
            "raw_provider_body_absent": True,
            "prompt_absent": True,
            "insufficient_balance_normalized": True,
            **detail_results,
        }

    def verify_database_terminal(self) -> dict[str, Any]:
        """核验规则版本、Source Outbox 终态、完整性与 WAL checkpoint。"""

        connection = self._connect()
        try:
            map_id = self.resource_ids["error_map_id"]
            tenant_id = self.sessions["admin"].tenant_id
            counts = {
                "maps": connection.execute(
                    "SELECT COUNT(*) FROM provider_error_maps "
                    "WHERE tenant_id=? AND provider_error_map_id=?",
                    (tenant_id, map_id),
                ).fetchone()[0],
                "versions": connection.execute(
                    "SELECT COUNT(*) FROM provider_error_map_versions "
                    "WHERE tenant_id=? AND provider_error_map_id=?",
                    (tenant_id, map_id),
                ).fetchone()[0],
                "events": connection.execute(
                    "SELECT COUNT(*) FROM provider_error_map_events "
                    "WHERE tenant_id=? AND provider_error_map_id=?",
                    (tenant_id, map_id),
                ).fetchone()[0],
            }
            _require(
                counts == {"maps": 1, "versions": 2, "events": 2},
                "database.terminal",
                "counts",
            )
            event_rows = connection.execute(
                "SELECT head_revision,dispatch_status,dispatch_attempts "
                "FROM provider_error_map_events "
                "WHERE tenant_id=? AND provider_error_map_id=? "
                "ORDER BY head_revision",
                (tenant_id, map_id),
            ).fetchall()
            _require(
                [int(row["head_revision"]) for row in event_rows] == [1, 2]
                and all(row["dispatch_status"] == "dispatched" for row in event_rows),
                "database.terminal",
                "event_state",
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            _require(
                len(integrity) == 1 and str(integrity[0][0]).lower() == "ok",
                "database.terminal",
                "integrity",
            )
            _require(
                not foreign_keys and checkpoint is not None and int(checkpoint[0]) == 0,
                "database.terminal",
                "storage",
            )
            return {
                "rule_versions": 2,
                "source_events": 2,
                "source_event_revisions": [1, 2],
                "source_events_dispatched": True,
                "source_event_attempts_bounded": all(
                    1 <= int(row["dispatch_attempts"]) <= 10 for row in event_rows
                ),
                "integrity_check": "ok",
                "foreign_key_check": "ok",
                "wal_checkpoint_executed": True,
            }
        # 终态审计查询失败统一折叠，避免 SQLite/WAL 细节进入验收输出。
        except sqlite3.Error:
            _fail("database.terminal", "query_failed")
        finally:
            connection.close()

    def verify_persisted_privacy(self) -> dict[str, Any]:
        """扫描同批数据库与服务日志，拒绝正文、Prompt、Token、密码和签名 Secret。"""

        markers = tuple(
            value.encode("utf-8") for value in dict.fromkeys(self.storage_sensitive) if value
        )
        _require(bool(markers), "privacy.scan", "markers")
        # SQLite 在 WAL 模式下可能把刚提交的请求事实留在伴生文件中；只扫描主库会产生隐私
        # 假阴性。伴生文件仍必须是批次目录内的普通节点，不能借链接把扫描扩大到工作区外。
        database_candidates = (
            self.config.database_path,
            Path(f"{self.config.database_path}-wal"),
            Path(f"{self.config.database_path}-shm"),
            Path(f"{self.config.database_path}-journal"),
        )
        database_paths = tuple(path for path in database_candidates if path.exists())
        for path in database_paths:
            _require(
                not _path_contains_link(path, boundary=self.config.artifact_directory),
                "privacy.scan",
                "database_link",
            )
        paths = (*database_paths, *self.config.log_paths)
        for path in paths:
            try:
                size = path.stat().st_size
                if path in self.config.log_paths:
                    _require(size <= MAX_LOG_FILE_BYTES, "privacy.scan", "log_too_large")
                content = path.read_bytes()
            # 日志或数据库不可读时只返回固定分类，禁止输出文件路径或已读内容。
            except OSError:
                _fail("privacy.scan", "read_failed")
            _require(
                all(marker not in content for marker in markers),
                "privacy.scan",
                "sensitive_value",
            )
        return {
            "database_scanned": True,
            "database_files_scanned": len(database_paths),
            "service_logs_scanned": len(self.config.log_paths),
            "sensitive_markers_checked": len(markers),
            "provider_body_absent": True,
            "prompt_absent": True,
            "credentials_absent": True,
        }

    def run(self) -> dict[str, Any]:
        """按数据库、身份、RBAC、Webhook、Gateway 与终态顺序执行验收。"""

        database = self.verify_database_ready()
        services = self.verify_services()
        identities = self.prepare_identities()
        permissions = self.verify_permissions()
        baseline = self.verify_builtin_baseline()

        receiver = WebhookReceiverServer()
        receiver_worker = threading.Thread(
            target=receiver.serve_forever,
            name="prd056-webhook-receiver",
            daemon=True,
        )
        receiver_worker.start()
        try:
            webhook_config = self.create_runtime_webhook(receiver)
            mutation = self.create_rule_and_verify_tenant()
            webhook_delivery = self.verify_runtime_webhook(receiver)
            gateway = self.verify_gateway_and_facts()
        finally:
            receiver.shutdown()
            receiver.server_close()
            receiver_worker.join(timeout=5)
            receiver.clear_sensitive()
        _require(
            not receiver_worker.is_alive(),
            "webhook.receiver",
            "thread_alive",
        )
        terminal = self.verify_database_terminal()
        return {
            "schema_version": 1,
            "prd_id": "PRD-056",
            "status": "passed",
            "generated_resources": {
                "error_map_id": self.resource_ids["error_map_id"],
                "error_map_version": int(self.resource_ids["version"]),
                "head_revision": int(self.resource_ids["head_revision"]),
                "mapped_request_id": self.resource_ids["mapped_request_id"],
                "nonstream_request_id": self.resource_ids["nonstream_request_id"],
                "stream_request_id": self.resource_ids["stream_request_id"],
            },
            "checks": {
                "database": database,
                "services": services,
                "identities": identities,
                "permissions": permissions,
                "baseline": baseline,
                "webhook_config": webhook_config,
                "mutation": mutation,
                "webhook_delivery": webhook_delivery,
                "gateway": gateway,
                "terminal": terminal,
            },
        }

    def logout_all(self) -> dict[str, bool]:
        """逐个注销所有验收 Session；单个失败不能阻止其他身份回收。"""

        results: dict[str, bool] = {}
        for key in tuple(self.sessions):
            session = self.sessions[key]
            # 没有活动 Token 的身份无需出网注销，仍会在内存清理阶段清空其他敏感字段。
            if not session.access_token:
                continue
            try:
                headers, request_id, trace_id = self._headers(f"logout_{key}", session_key=key)
                response = self.control.post("/auth/logout", headers=headers)
                verify_response(
                    response,
                    stage=f"auth.logout_{key}",
                    status=200,
                    request_id=request_id,
                    trace_id=trace_id,
                )
                results[f"{key}_session_revoked"] = True
            except Exception:
                results[f"{key}_session_revoked"] = False
        expected = {f"{key}_session_revoked" for key in self.sessions}
        results["all_sessions_revoked"] = bool(expected) and all(
            results.get(key) is True for key in expected
        )
        return results

    def disable_temporary_users(self) -> bool:
        """按 tenant_id+user_id 精确停用临时账号，绝不修改种子管理员。"""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for tenant_id, user_id in self.temporary_user_ids:
                connection.execute(
                    "UPDATE users SET status='disabled',updated_at=datetime('now') "
                    "WHERE tenant_id=? AND user_id=?",
                    (tenant_id, user_id),
                )
                connection.execute(
                    "UPDATE sessions SET status='revoked',"
                    "revoked_at=COALESCE(revoked_at,datetime('now')) "
                    "WHERE tenant_id=? AND user_id=? AND status='active'",
                    (tenant_id, user_id),
                )
            connection.commit()
            remaining = sum(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM users "
                        "WHERE tenant_id=? AND user_id=? AND status='active'",
                        (tenant_id, user_id),
                    ).fetchone()[0]
                )
                for tenant_id, user_id in self.temporary_user_ids
            )
            return remaining == 0
        # 清理失败时回滚并返回布尔结果，原始数据库异常不能覆盖主验收结论或泄露路径。
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

    def disable_runtime_webhook(self) -> bool:
        """按管理员租户与 Endpoint ID 禁用本批 Webhook，避免接收器关闭后继续投递。"""

        session = self.sessions.get("admin")
        endpoint_id = self.resource_ids.get("webhook_endpoint_id", "")
        # 缺少可信管理员 owner 或 Endpoint ID 形态异常时失败关闭，禁止退化为无租户限定的清理。
        # 只有当前批次已认证管理员和受限 Endpoint ID 同时存在时才允许进入回收事务，
        # 避免异常路径用空身份或未校验对象 ID 修改任意租户的 Webhook 配置。
        if session is None or SAFE_IDENTIFIER_PATTERN.fullmatch(endpoint_id) is None:
            return False
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Webhook API 当前没有通用禁用 mutation；验收回收直接写 owner 表，但必须同时绑定
            # tenant_id、Endpoint ID 和可信管理员 user_id，不能只凭全局 ID 修改其他租户资源。
            updated = connection.execute(
                "UPDATE webhook_endpoints SET status='disabled',updated_by=?,"
                "updated_at=datetime('now') WHERE tenant_id=? AND webhook_endpoint_id=?",
                (session.user_id, session.tenant_id, endpoint_id),
            )
            row = connection.execute(
                "SELECT status FROM webhook_endpoints WHERE tenant_id=? AND webhook_endpoint_id=?",
                (session.tenant_id, endpoint_id),
            ).fetchone()
            connection.commit()
            return updated.rowcount == 1 and row is not None and row["status"] == "disabled"
        # 清理失败必须回滚并由最终门禁报告布尔失败，不能泄露 SQL 或宿主路径。
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

    def sensitive_values(self) -> tuple[str, ...]:
        """返回仍在内存中的已知明文，供最终证据做等值扫描。"""

        return tuple(value for value in self.known_sensitive if value)

    def clear_sensitive_memory(self) -> None:
        """清空验收器持有的密码、Token、邮箱、Secret 与 Virtual Key 引用。"""

        for session in self.sessions.values():
            session.access_token = ""
            session.password = ""
            session.email = ""
        self.known_sensitive.clear()
        self.storage_sensitive.clear()
        self.original_provider_base_url = None


def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """独占创建 Runtime 证据，禁止重跑覆盖既有事实。"""

    target = directory / "runtime-acceptance.json"
    encoded = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    try:
        with target.open("xb") as handle:
            handle.write(encoded)
    # 同批证据不可覆盖，重复运行必须使用新批次目录以保留既有验收事实。
    except FileExistsError:
        _fail("evidence.publish", "already_exists")
    # 文件系统异常只暴露固定写入分类，不把工作机路径写入最终证据。
    except OSError:
        _fail("evidence.publish", "write_failed")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """执行验收、恢复 Provider、回收身份、扫描隐私并发布同批次证据。"""

    acceptance: RuntimeAcceptance | None = None
    candidate: dict[str, Any] | None = None
    privacy: dict[str, Any] | None = None
    cleanup = {
        "runtime_webhook_disabled": False,
        "provider_endpoint_restored": False,
        "provider_key_cooldown_restored": False,
        "all_sessions_revoked": False,
        "temporary_users_disabled": False,
    }
    sensitive: tuple[str, ...] = (config.admin_password, config.virtual_key)
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
        httpx.Client(
            base_url=config.console_origin,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as console,
    ):
        acceptance = RuntimeAcceptance(config, control, gateway, console)
        try:
            candidate = acceptance.run()
        finally:
            sensitive = acceptance.sensitive_values()
            try:
                cleanup["runtime_webhook_disabled"] = acceptance.disable_runtime_webhook()
                cleanup["provider_endpoint_restored"] = acceptance.restore_provider_endpoint()
                cleanup["provider_key_cooldown_restored"] = (
                    acceptance.restore_provider_key_cooldown()
                )
                cleanup.update(acceptance.logout_all())
                cleanup["temporary_users_disabled"] = acceptance.disable_temporary_users()
                if candidate is not None:
                    privacy = acceptance.verify_persisted_privacy()
            finally:
                acceptance.clear_sensitive_memory()
    _require(candidate is not None, "runtime.execute", "candidate_missing")
    _require(
        cleanup.get("runtime_webhook_disabled") is True,
        "runtime.cleanup",
        "webhook_endpoint",
    )
    _require(
        cleanup.get("provider_endpoint_restored") is True,
        "runtime.cleanup",
        "provider_endpoint",
    )
    _require(
        cleanup.get("provider_key_cooldown_restored") is True,
        "runtime.cleanup",
        "provider_key_cooldown",
    )
    _require(cleanup.get("all_sessions_revoked") is True, "runtime.cleanup", "session")
    _require(cleanup.get("temporary_users_disabled") is True, "runtime.cleanup", "users")
    _require(privacy is not None, "privacy.scan", "missing")
    candidate["checks"]["cleanup"] = cleanup
    candidate["checks"]["privacy"] = privacy
    assert_safe_payload(candidate, sensitive_values=sensitive)
    publish_evidence(config.artifact_directory, candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    """命令行入口仅输出固定成功摘要或脱敏失败分类。"""

    try:
        config = parse_config(argv)
        execute_runtime(config)
    except AcceptanceFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "prd056_runtime_acceptance_failed stage=runtime.unhandled code=unexpected",
            file=sys.stderr,
        )
        return 1
    print('{"prd_id":"PRD-056","status":"passed","evidence":"runtime-acceptance.json"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
