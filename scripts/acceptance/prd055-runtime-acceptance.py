"""PRD-055 Trace 查询与 SavedFilter 真实运行验收器。

脚本只连接调用方已经启动的本机三服务，并把 10 万行性能夹具、第二租户和验收证据
限制在 artifacts/prd055/<batch>/runtime.db。它不启动服务、不执行迁移、不访问外网，
也不写系统临时目录；管理员密码、Session Token 和一次性账号只驻留当前进程内存。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from metahub_shared.db.security import hash_password


# 所有网络目标、标识和证据批次使用显式白名单，防止验收器越出本机与项目目录。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")
QUERY_EVENT_PATTERN = re.compile(r"^lqe_[0-9a-f]{32}$")
QUERY_WEBHOOK_EVENT_PATTERN = re.compile(r"^lqevt_[0-9a-f]{32}$")
SAVED_FILTER_PATTERN = re.compile(r"^sf_[0-9a-f]{32}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_FILENAME = "0045_trace_query_enhancement.sql"

# 0045 的核心表、索引与触发器必须同时存在，只有迁移版本号不能证明可运行。
REQUIRED_TABLES = frozenset({"saved_filters", "request_log_query_events"})
REQUIRED_INDEXES = frozenset(
    {
        "idx_saved_filters_owner_name_prd055",
        "idx_saved_filters_owner_time_prd055",
        "idx_request_log_query_events_actor_time_prd055",
        "idx_request_log_query_events_pending_prd055",
        "idx_request_logs_trace_time_prd055",
        "idx_request_logs_virtual_key_time_prd055",
        "idx_request_logs_provider_time_prd055",
        "idx_request_logs_status_time_prd055",
        "idx_request_logs_latency_time_prd055",
        "idx_request_logs_tokens_time_prd055",
        "idx_request_logs_cost_time_prd055",
        "idx_request_logs_terminal_stage_time_prd055",
    }
)
REQUIRED_TRIGGERS = frozenset(
    {
        "trg_saved_filters_owner_insert_prd055",
        "trg_saved_filters_state_insert_prd055",
        "trg_saved_filters_payload_insert_prd055",
        "trg_saved_filters_conflict_insert_prd055",
        "trg_saved_filters_rowid_insert_prd055",
        "trg_saved_filters_state_update_prd055",
        "trg_saved_filters_payload_update_prd055",
        "trg_saved_filters_immutable_delete_prd055",
        "trg_request_log_query_events_owner_insert_prd055",
        "trg_request_log_query_events_state_insert_prd055",
        "trg_request_log_query_events_keys_insert_prd055",
        "trg_request_log_query_events_conflict_insert_prd055",
        "trg_request_log_query_events_rowid_insert_prd055",
        "trg_request_log_query_events_state_update_prd055",
        "trg_request_log_query_events_immutable_delete_prd055",
    }
)

# 性能门槛来自 PRD-055 两轮设计自审，样本包含预热后 20 次真实 HTTP 查询。
PERFORMANCE_FIXTURE_ROWS = 100_000
PERFORMANCE_SAMPLE_COUNT = 20
PERFORMANCE_P95_LIMIT_MS = 500
PERFORMANCE_SINGLE_LIMIT_MS = 1_500

# 公开 DTO 和最终证据不得包含凭据、请求正文、Provider 正文或远端投递细节。
FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "actor_session_id",
        "actor_user_id",
        "credential_id",
        "encrypted_secret",
        "endpoint_url",
        "error_summary",
        "key_hash",
        "key_plaintext",
        "key_secret",
        "key_value",
        "messages",
        "password",
        "password_hash",
        "payload",
        "payload_json",
        "prompt",
        "provider_key",
        "raw_usage",
        "response",
        "response_body",
        "secret",
        "session_id",
        "session_token",
        "token",
        "token_hash",
    }
)
FORBIDDEN_VALUE_PATTERN = re.compile(
    r"Bearer\s+[^\s\"']{8,}|\bmhv_[A-Za-z0-9_-]{16,}\b|\bsk[-_][A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)


class AcceptanceFailure(RuntimeError):
    """只携带固定 stage/code 的验收异常，禁止传播 HTTP、SQLite 或正文细节。"""

    def __init__(self, stage: str, code: str) -> None:
        """保存脱敏失败分类；调用方不得把外部输入或原始异常拼入字段。"""

        super().__init__(f"prd055_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """真实运行验收配置；数据库与证据必须属于同一个 PRD-055 批次。"""

    control_origin: str
    gateway_origin: str
    console_origin: str
    artifact_directory: Path
    database_path: Path
    tenant_slug: str
    admin_email: str
    admin_password: str
    timeout_seconds: float


@dataclass
class SessionCredential:
    """保存短生命周期身份；结束时主动清空 Token、密码和一次性邮箱引用。"""

    session_key: str
    role: str
    tenant_slug: str
    tenant_id: str
    user_id: str
    session_id: str
    access_value: str
    email: str
    password: str


@dataclass(frozen=True)
class QueryAuditExpectation:
    """保存一次成功查询应落库的最小审计事实，供运行验收逐字段交叉核对。"""

    query_event_id: str
    tenant_id: str
    actor_user_id: str
    actor_session_id: str
    operation_trace_id: str
    filter_fingerprint: str
    filter_keys: tuple[str, ...]
    saved_filter_id: str | None
    sort_by: str
    sort_direction: str
    query_limit: int
    query_offset: int
    matched_count: int
    returned_count: int
    has_more: bool


def _fail(stage: str, code: str) -> None:
    """以固定错误分类终止验收，不把不可信值带入终端或证据。"""

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；失败时只使用调用点声明的稳定分类。"""

    # 所有条件失败都折叠为固定分类，避免原始响应或数据库值穿透。
    if not condition:
        _fail(stage, code)


def _safe_identifier(value: object, stage: str, code: str) -> str:
    """读取公开不透明 ID，并拒绝空值、超长值和正文型字符。"""

    _require(isinstance(value, str), stage, code)
    _require(SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None, stage, code)
    return value


def normalize_loopback_origin(value: str, *, stage: str) -> str:
    """规范化显式端口 HTTP 回环 origin，拒绝凭据、路径、查询和 fragment。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 解析错误可能含调用方输入，只允许固定配置分类进入终端。
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
    """从脚本物理位置恢复唯一 MetaHub 根，避免当前目录改变证据边界。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def _path_contains_link(path: Path, *, boundary: Path) -> bool:
    """检查批次边界内任一路径节点是否为符号链接或 Windows junction。"""

    current = path
    while True:
        junction_check = getattr(current, "is_junction", None)
        # 既检查文件链接也检查目录 junction，避免 resolve 后链接身份被抹去。
        if current.is_symlink() or (
            callable(junction_check) and bool(junction_check())
        ):
            return True
        if current == boundary:
            return False
        # 调用方会先验证 containment；此保护只阻止异常路径导致无限向上遍历。
        if current.parent == current:
            return False
        current = current.parent


def _migration_checksum(*, workspace_root: Path | None = None) -> str:
    """按正式迁移器规则计算 0045 SQL 的 UTF-8 SHA-256 摘要。"""

    root = (workspace_root or _workspace_root()).resolve()
    source = root / "infra" / "migrations" / MIGRATION_FILENAME
    try:
        sql = source.read_text(encoding="utf-8")
    # 迁移文件缺失或不可读只能输出固定分类，禁止把主机路径写入验收日志。
    except OSError:
        _fail("database.readiness", "migration_source_unavailable")
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def resolve_artifact_directory(
    value: str | Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """把证据目录限制为 artifacts/prd055 下恰好一个安全批次层级。"""

    root = (workspace_root or _workspace_root()).resolve()
    artifact_root = root / "artifacts" / "prd055"
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    # abspath 只规整点段而不跟随链接，以便在 resolve 前保留链接节点证据。
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(artifact_root)
    # 越出项目证据根时只返回固定失败，不能回显主机绝对路径。
    except ValueError:
        _fail("config.artifact_directory", "outside_workspace")
    _require(len(relative.parts) == 1, "config.artifact_directory", "batch_depth")
    _require(
        BATCH_NAME_PATTERN.fullmatch(relative.name) is not None,
        "config.artifact_directory",
        "batch_name",
    )
    _require(
        not _path_contains_link(candidate, boundary=artifact_root),
        "config.artifact_directory",
        "symlink",
    )
    resolved = candidate.resolve()
    try:
        resolved_relative = resolved.relative_to(artifact_root.resolve())
    # 即使链接节点在检查后被替换，解析后的批次也不得逃逸证据根目录。
    except ValueError:
        _fail("config.artifact_directory", "outside_workspace")
    _require(len(resolved_relative.parts) == 1, "config.artifact_directory", "batch_depth")
    return resolved


def resolve_runtime_database(value: str, *, artifact_directory: Path) -> Path:
    """只接受当前批次内名为 runtime.db 的非链接 SQLite 文件路径。"""

    try:
        parsed = urlsplit(str(value))
    # 数据库 URL 异常不得泄露原始路径或调用参数。
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
    candidate = Path(os.path.abspath(source))
    expected = Path(os.path.abspath(artifact_directory / "runtime.db"))
    _require(candidate == expected, "config.database_url", "not_batch_runtime")
    _require(
        not _path_contains_link(candidate, boundary=artifact_directory),
        "config.database_url",
        "database_symlink",
    )
    resolved = candidate.resolve()
    _require(resolved == expected.resolve(), "config.database_url", "not_batch_runtime")
    return resolved


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeConfig:
    """解析回环地址与批次路径，并在返回前从环境删除管理员密码。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(description="PRD-055 Trace 查询与 SavedFilter 真实运行验收")
    parser.add_argument("--control-url", default=env.get("METAHUB_PRD055_CONTROL_URL", "http://localhost:8001"))
    parser.add_argument("--gateway-url", default=env.get("METAHUB_PRD055_GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument("--console-url", default=env.get("METAHUB_PRD055_CONSOLE_URL", "http://localhost:5173"))
    parser.add_argument("--artifact-dir", default=env.get("METAHUB_PRD055_ARTIFACT_DIR", ""))
    parser.add_argument("--database-url", default=env.get("METAHUB_PRD055_DATABASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    primary_password = env.pop("METAHUB_PRD055_ADMIN_PASSWORD", "")
    fallback_password = env.pop("METAHUB_ADMIN_PASSWORD", "")
    # 两个环境变量都必须无条件移除；短路求值会让 fallback 明文残留在进程环境。
    password = primary_password or fallback_password
    _require(bool(password), "config.credentials", "admin_password_missing")
    _require(bool(args.artifact_dir), "config.artifact_directory", "missing")
    _require(bool(args.database_url), "config.database_url", "missing")
    _require(1.0 <= args.timeout <= 120.0, "config.timeout", "out_of_range")
    artifact_directory = resolve_artifact_directory(args.artifact_dir)
    database_path = resolve_runtime_database(args.database_url, artifact_directory=artifact_directory)
    origins = {
        normalize_loopback_origin(args.control_url, stage="config.control_origin"),
        normalize_loopback_origin(args.gateway_url, stage="config.gateway_origin"),
        normalize_loopback_origin(args.console_url, stage="config.console_origin"),
    }
    _require(len(origins) == 3, "config.origins", "must_be_distinct")
    tenant_slug = env.get("METAHUB_PRD055_TENANT_SLUG") or env.get("METAHUB_TENANT_SLUG", "default")
    admin_email = env.get("METAHUB_PRD055_ADMIN_EMAIL") or env.get("METAHUB_ADMIN_EMAIL", "admin@metahub.local")
    _require(bool(tenant_slug.strip()), "config.credentials", "tenant_slug_missing")
    _require("@" in admin_email, "config.credentials", "admin_email_invalid")
    return RuntimeConfig(
        control_origin=normalize_loopback_origin(args.control_url, stage="config.control_origin"),
        gateway_origin=normalize_loopback_origin(args.gateway_url, stage="config.gateway_origin"),
        console_origin=normalize_loopback_origin(args.console_url, stage="config.console_origin"),
        artifact_directory=artifact_directory,
        database_path=database_path,
        tenant_slug=tenant_slug.strip(),
        admin_email=admin_email.strip().lower(),
        admin_password=password,
        timeout_seconds=args.timeout,
    )


def _request_identity(action: str, suffix: str) -> tuple[str, str]:
    """为一个真实调用生成互不复用的 operation request_id 与 trace_id。"""

    request_id = f"req_prd055_accept_{action}_{suffix}"
    trace_id = f"trc_prd055_accept_{action}_{suffix}"
    _safe_identifier(request_id, "request.identity", "request_id_invalid")
    _safe_identifier(trace_id, "request.identity", "trace_id_invalid")
    _require(len(request_id) <= 128 and len(trace_id) <= 128, "request.identity", "too_long")
    return request_id, trace_id


def _wire_filters(payload: Mapping[str, Any]) -> str:
    """生成无填充 Base64URL v1 JSON，运行验收不把筛选值写入证据。"""

    raw = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """读取 JSON object；代理页或异常正文解析失败时只返回固定错误。"""

    try:
        payload = response.json()
    # 响应正文可能含内部信息，解析失败只能折叠为固定分类。
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _fail(stage, "response_not_json")
    _require(isinstance(payload, dict), stage, "response_not_object")
    return payload


def _safe_http_call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    stage: str,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> httpx.Response:
    """只向预绑定回环 origin 发送相对路径，并折叠所有传输异常。"""

    _require(path.startswith("/") and not path.startswith("//"), stage, "path_invalid")
    _require("\r" not in path and "\n" not in path, stage, "path_invalid")
    try:
        return client.request(method, path, headers=dict(headers or {}), json=body)
    # 网络异常可能携带 URL、Header 或正文，不能原样进入终端证据。
    except httpx.HTTPError:
        _fail(stage, "transport_failed")


def _normalize_key(value: str) -> str:
    """统一 camelCase、连字符和空白为下划线小写，供隐私字段门禁复用。"""

    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").lower()


def assert_safe_payload(value: Any, *, sensitive_values: Iterable[str] = ()) -> None:
    """递归拒绝敏感字段、凭据形态、非 JSON 值和本轮已知明文。"""

    def walk(node: Any) -> None:
        """遍历一个 JSON 节点，字典键和值分别执行最小披露约束。"""

        if isinstance(node, Mapping):
            for key, child in node.items():
                _require(isinstance(key, str), "privacy.scan", "non_string_key")
                _require(_normalize_key(key) not in FORBIDDEN_KEYS, "privacy.scan", "forbidden_key")
                walk(child)
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        # RequestLog 的公开成本 DTO 是 JSON number；只接受有限浮点数，继续拒绝
        # NaN/Infinity，避免 Python 宽松编码产生非标准 JSON 证据。
        if isinstance(node, float):
            _require(math.isfinite(node), "privacy.scan", "non_json_value")
            return
        _require(node is None or isinstance(node, (str, int, bool)), "privacy.scan", "non_json_value")

    walk(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    _require(FORBIDDEN_VALUE_PATTERN.search(encoded) is None, "privacy.scan", "credential_pattern")
    for sensitive in sensitive_values:
        if sensitive:
            _require(sensitive not in encoded, "privacy.scan", "known_sensitive_value")


def _nearest_rank_p95(samples: list[int]) -> int:
    """按 nearest-rank 计算整数毫秒 P95，避免平均数掩盖慢请求。"""

    _require(bool(samples), "performance.samples", "empty")
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def verify_response_contract(
    response: httpx.Response,
    *,
    stage: str,
    expected_status: int,
    request_id: str,
    trace_id: str,
    expected_code: str | None = None,
    require_private_cache: bool = False,
) -> dict[str, Any]:
    """核对状态、统一链路、领域错误与 RequestLog 防缓存响应头。"""

    _require(response.status_code == expected_status, stage, "status_mismatch")
    _require(response.headers.get("x-request-id") == request_id, stage, "request_header")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_header")
    if expected_status == 204:
        _require(response.content == b"", stage, "response_not_empty")
        payload: dict[str, Any] = {}
    else:
        payload = _json_object(response, stage)
        _require(payload.get("request_id") == request_id, stage, "request_body")
        _require(payload.get("trace_id") == trace_id, stage, "trace_body")
    if expected_code is not None:
        _require(payload.get("code") == expected_code, stage, "error_code")
    if require_private_cache:
        _require(response.headers.get("cache-control") == "no-store, private", stage, "cache_control")
        _require(response.headers.get("pragma") == "no-cache", stage, "pragma")
        vary = {item.strip().lower() for item in response.headers.get("vary", "").split(",")}
        _require("authorization" in vary, stage, "vary")
    return payload


def _public_http_evidence(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    include_code: bool,
) -> dict[str, Any]:
    """只保留状态与链路布尔值，原始响应、URL 和身份不得进入证据。"""

    evidence: dict[str, Any] = {
        "status": response.status_code,
        "request_id_present": bool(payload.get("request_id") or response.headers.get("x-request-id")),
        "trace_id_present": bool(payload.get("trace_id") or response.headers.get("x-trace-id")),
    }
    if include_code:
        evidence["code"] = str(payload.get("code", ""))
    return evidence


def _db_time(value: datetime) -> str:
    """把 UTC 时间固定为微秒精度 Z 文本，供批次夹具稳定排序。"""

    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RuntimeAcceptance:
    """通过真实 Control API、三服务和隔离运行库验收 PRD-055。"""

    def __init__(
        self,
        config: RuntimeConfig,
        control_client: httpx.Client,
        gateway_client: httpx.Client,
        console_client: httpx.Client,
    ) -> None:
        """保存回环客户端和随机链路后缀；认证材料只留在实例内存。"""

        self.config = config
        self.control = control_client
        self.gateway = gateway_client
        self.console = console_client
        self.suffix = secrets.token_hex(6)
        self.fixture_marker = hashlib.sha256(
            config.artifact_directory.name.encode("utf-8")
        ).hexdigest()[:10]
        self.sessions: dict[str, SessionCredential] = {}
        self.resource_ids: dict[str, str] = {}
        self.http_evidence: dict[str, dict[str, Any]] = {}
        self.known_sensitive: list[str] = [config.admin_password]
        self.secondary_identity: dict[str, str] = {}
        self.operation_trace_ids: dict[str, str] = {}
        self.query_audit_expectations: dict[str, QueryAuditExpectation] = {}

    def _connect_runtime(self) -> sqlite3.Connection:
        """连接已存在批次运行库，禁止 SQLite 隐式创建其他文件。"""

        expected = (self.config.artifact_directory / "runtime.db").resolve()
        _require(
            self.config.database_path.resolve() == expected,
            "database.connect",
            "not_batch_runtime",
        )
        _require(self.config.database_path.is_file(), "database.connect", "runtime_missing")
        _require(
            not _path_contains_link(
                self.config.database_path,
                boundary=self.config.artifact_directory,
            ),
            "database.connect",
            "database_symlink",
        )
        try:
            connection = sqlite3.connect(
                self.config.database_path,
                timeout=self.config.timeout_seconds,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=20000")
            return connection
        # 打开失败可能暴露数据库路径，因此统一转换为固定错误。
        except sqlite3.Error:
            _fail("database.connect", "open_failed")

    def verify_runtime_database_ready(self) -> dict[str, Any]:
        """确认 0045、关键对象、校验和、完整性和外键已由调用方准备完成。"""

        connection = self._connect_runtime()
        try:
            migration = connection.execute(
                "SELECT status, checksum FROM schema_migrations WHERE version='0045'"
            ).fetchone()
            _require(
                migration is not None and migration["status"] == "applied",
                "database.readiness",
                "migration_0045_missing",
            )
            stored_checksum = migration["checksum"]
            expected_checksum = _migration_checksum()
            _require(
                isinstance(stored_checksum, str)
                and FINGERPRINT_PATTERN.fullmatch(stored_checksum) is not None
                and hmac.compare_digest(stored_checksum, expected_checksum),
                "database.readiness",
                "migration_checksum",
            )
            objects = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            ).fetchall()
            by_type = {
                kind: {str(row["name"]) for row in objects if row["type"] == kind}
                for kind in ("table", "index", "trigger")
            }
            _require(REQUIRED_TABLES <= by_type["table"], "database.readiness", "table_missing")
            _require(REQUIRED_INDEXES <= by_type["index"], "database.readiness", "index_missing")
            _require(REQUIRED_TRIGGERS <= by_type["trigger"], "database.readiness", "trigger_missing")
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
        # 缺表、锁和损坏不得把 SQL 或文件路径带入证据。
        except sqlite3.Error:
            _fail("database.readiness", "query_failed")
        finally:
            connection.close()
        return {
            "database_in_batch": True,
            "migration_0045_applied": True,
            "migration_checksum_verified": True,
            "required_tables_present": True,
            "required_indexes_present": True,
            "required_triggers_present": True,
            "integrity_verified": True,
        }

    def verify_services(self) -> dict[str, bool]:
        """验证调用方已启动的 Gateway、Control 和 Console 均可由回环地址访问。"""

        gateway = _safe_http_call(self.gateway, "GET", "/health", stage="services.gateway")
        control = _safe_http_call(self.control, "GET", "/health", stage="services.control")
        console = _safe_http_call(self.console, "GET", "/", stage="services.console")
        _require(gateway.status_code == 200, "services.gateway", "health_status")
        _require(control.status_code == 200, "services.control", "health_status")
        _require(console.status_code == 200, "services.console", "root_status")
        _require(
            "text/html" in console.headers.get("content-type", "").lower(),
            "services.console",
            "content_type",
        )
        return {
            "gateway_http_200": True,
            "control_http_200": True,
            "console_http_200": True,
        }

    def _headers(
        self,
        action: str,
        *,
        session_key: str | None,
    ) -> tuple[dict[str, str], str, str]:
        """构造真实请求的认证与链路头，Token 不进入返回证据。"""

        request_id, trace_id = _request_identity(action, self.suffix)
        headers = {
            "Accept": "application/json",
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
        }
        # 只有显式指定的已登录身份才附加认证头，匿名权限用例必须保持无 Token。
        if session_key is not None:
            credential = self.sessions.get(session_key)
            _require(credential is not None, "auth.headers", "session_missing")
            _require(bool(credential.access_value), "auth.headers", "session_empty")
            headers["Authorization"] = f"Bearer {credential.access_value}"
        return headers, request_id, trace_id

    def _control_call(
        self,
        *,
        action: str,
        session_key: str | None,
        method: str,
        path: str,
        expected_status: int,
        body: Mapping[str, Any] | None = None,
        expected_code: str | None = None,
        private_cache: bool = False,
    ) -> tuple[httpx.Response, dict[str, Any], str]:
        """调用真实 Control API 并立即验证统一响应合同。"""

        headers, request_id, trace_id = self._headers(action, session_key=session_key)
        if body is not None:
            headers["Content-Type"] = "application/json"
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
            require_private_cache=private_cache,
        )
        self.http_evidence[action] = _public_http_evidence(
            response,
            payload,
            include_code=expected_code is not None,
        )
        # 只在内存保存 operation 的 trace 映射，最终证据不输出具体链路值。
        self.operation_trace_ids[request_id] = trace_id
        return response, payload, request_id


    def _login(
        self,
        *,
        session_key: str,
        tenant_slug: str,
        email: str,
        password: str,
        expected_role: str,
        expected_user_id: str | None = None,
    ) -> dict[str, Any]:
        """通过公开登录建立 Session，并核对角色、租户与同库 Token 哈希绑定。"""

        action = f"login_{session_key}"
        headers, request_id, trace_id = self._headers(action, session_key=None)
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/login",
            stage=f"auth.{action}",
            headers={**headers, "Content-Type": "application/json"},
            body={"tenant_slug": tenant_slug, "email": email, "password": password},
        )
        provisional = _json_object(response, f"auth.{action}")
        access = provisional.get("access_token")
        user = provisional.get("user")
        if isinstance(access, str) and access and isinstance(user, dict):
            self.sessions[session_key] = SessionCredential(
                session_key=session_key,
                role=expected_role,
                tenant_slug=tenant_slug,
                tenant_id=str(user.get("tenant_id", "")),
                user_id=str(user.get("user_id", "")),
                session_id=str(provisional.get("session_id", "")),
                access_value=access,
                email=email,
                password=password,
            )
            self.known_sensitive.extend((email, password, access))
        payload = verify_response_contract(
            response,
            stage=f"auth.{action}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        user = payload.get("user")
        _require(
            isinstance(user, dict) and user.get("role") == expected_role,
            f"auth.{action}",
            "role",
        )
        tenant_id = _safe_identifier(user.get("tenant_id"), f"auth.{action}", "tenant_id")
        user_id = _safe_identifier(user.get("user_id"), f"auth.{action}", "user_id")
        session_id = _safe_identifier(payload.get("session_id"), f"auth.{action}", "session_id")
        access = payload.get("access_token")
        _require(isinstance(access, str) and bool(access), f"auth.{action}", "access_missing")
        if expected_user_id is not None:
            _require(user_id == expected_user_id, f"auth.{action}", "user_id_mismatch")
        self.sessions[session_key] = SessionCredential(
            session_key,
            expected_role,
            tenant_slug,
            tenant_id,
            user_id,
            session_id,
            access,
            email,
            password,
        )
        _require(
            self.verify_session_database_binding(session_key),
            f"auth.{action}",
            "database_binding",
        )
        evidence = _public_http_evidence(response, payload, include_code=False)
        self.http_evidence[action] = evidence
        return evidence

    def verify_session_database_binding(self, session_key: str) -> bool:
        """用 Session 行与不可逆 Token 哈希证明服务绑定当前 runtime.db。"""

        credential = self.sessions.get(session_key)
        _require(credential is not None, "database.session_binding", "session_missing")
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
        # Session 绑定查询失败不能回显 Token 哈希或数据库位置。
        except sqlite3.Error:
            _fail("database.session_binding", "query_failed")
        finally:
            connection.close()
        _require(row is not None, "database.session_binding", "session_not_found")
        return bool(
            row["tenant_id"] == credential.tenant_id
            and row["user_id"] == credential.user_id
            and row["role"] == credential.role
            and row["status"] == "active"
            and hmac.compare_digest(str(row["token_hash"]), expected_hash)
        )

    def create_role_users(self) -> dict[str, bool]:
        """经管理员 API 创建 developer/viewer，并登录形成真实权限攻击身份。"""

        for role in ("developer", "viewer"):
            password = f"Prd055!{secrets.token_urlsafe(18)}"
            email = f"prd055-{role}-{self.suffix}@metahub.local"
            _, payload, _ = self._control_call(
                action=f"create_{role}",
                session_key="admin",
                method="POST",
                path="/users",
                expected_status=201,
                body={
                    "email": email,
                    "display_name": f"PRD055 runtime {role}",
                    "password": password,
                    "role": role,
                    "status": "active",
                },
            )
            user = payload.get("user")
            _require(
                isinstance(user, dict) and user.get("role") == role,
                f"users.{role}",
                "role",
            )
            user_id = _safe_identifier(user.get("user_id"), f"users.{role}", "user_id")
            self._login(
                session_key=role,
                tenant_slug=self.config.tenant_slug,
                email=email,
                password=password,
                expected_role=role,
                expected_user_id=user_id,
            )
        return {
            "developer_session_bound": True,
            "viewer_session_bound": True,
        }

    def _performance_claim_rows(
        self,
        tenant_id: str,
        anchor: datetime,
    ) -> Iterable[tuple[object, ...]]:
        """逐行生成 10 万个不可变 claim，避免在验收进程中构造巨型列表。"""

        prefix = f"req_p55_{self.fixture_marker}_"
        for index in range(PERFORMANCE_FIXTURE_ROWS):
            created_at = _db_time(anchor - timedelta(milliseconds=index))
            yield (
                tenant_id,
                f"{prefix}{index:06d}",
                f"trc_p55_{self.fixture_marker}_{index // 10:05d}",
                created_at,
            )

    def _performance_log_rows(
        self,
        tenant_id: str,
        anchor: datetime,
    ) -> Iterable[tuple[object, ...]]:
        """生成可区分 Provider、状态、阶段、延迟、Token 与成本的日志夹具。"""

        prefix = f"req_p55_{self.fixture_marker}_"
        for index in range(PERFORMANCE_FIXTURE_ROWS):
            created_at = _db_time(anchor - timedelta(milliseconds=index))
            failed = index % 4 == 0
            provider = "deepseek" if index % 2 == 0 else "qwen"
            yield (
                f"{prefix}{index:06d}",
                tenant_id,
                f"trc_p55_{self.fixture_marker}_{index // 10:05d}",
                "failed" if failed else "success",
                "gateway.upstream_error" if failed else None,
                index % 2000,
                "enterprise-chat",
                "deepseek-chat" if provider == "deepseek" else "qwen-plus",
                provider,
                10 + index % 5000,
                f"0.{index % 100000 + 1:06d}",
                created_at,
                created_at,
            )


    def _verify_performance_fixture(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        fixture_prefix: str,
    ) -> dict[str, int]:
        """核对 10 万行日志与 claim 一一对应，并证明目标筛选样本数量准确。"""

        prefix_length = len(fixture_prefix)
        log_facts = connection.execute(
            """
            SELECT
              COUNT(*) AS row_count,
              COUNT(DISTINCT request_id) AS distinct_count,
              SUM(CASE
                WHEN request_model='enterprise-chat'
                 AND total_tokens BETWEEN 10 AND 5009
                 AND currency='CNY'
                 AND price_schema_version='prd055-v1'
                THEN 1 ELSE 0 END
              ) AS contract_count,
              SUM(CASE
                WHEN status='failed'
                 AND provider_code='deepseek'
                 AND error_code='gateway.upstream_error'
                 AND latency_ms BETWEEN 0 AND 1999
                THEN 1 ELSE 0 END
              ) AS target_count
            FROM request_logs
            WHERE tenant_id=? AND substr(request_id, 1, ?)=?
            """,
            (tenant_id, prefix_length, fixture_prefix),
        ).fetchone()
        claim_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM gateway_request_claims
            WHERE tenant_id=? AND substr(request_id, 1, ?)=?
            """,
            (tenant_id, prefix_length, fixture_prefix),
        ).fetchone()
        joined_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM request_logs rl
            JOIN gateway_request_claims grc
              ON grc.tenant_id=rl.tenant_id
             AND grc.request_id=rl.request_id
             AND grc.trace_id=rl.trace_id
            WHERE rl.tenant_id=? AND substr(rl.request_id, 1, ?)=?
            """,
            (tenant_id, prefix_length, fixture_prefix),
        ).fetchone()
        expected_target_count = (PERFORMANCE_FIXTURE_ROWS + 3) // 4
        _require(log_facts is not None, "fixtures.performance", "logs_missing")
        _require(claim_count is not None, "fixtures.performance", "claims_missing")
        _require(joined_count is not None, "fixtures.performance", "join_missing")
        # 三个计数必须精确一致，禁止孤儿 claim、历史同前缀行或缺失日志伪造负载规模。
        _require(
            int(log_facts["row_count"]) == PERFORMANCE_FIXTURE_ROWS
            and int(log_facts["distinct_count"]) == PERFORMANCE_FIXTURE_ROWS
            and int(log_facts["contract_count"] or 0) == PERFORMANCE_FIXTURE_ROWS
            and int(claim_count["row_count"]) == PERFORMANCE_FIXTURE_ROWS
            and int(joined_count["row_count"]) == PERFORMANCE_FIXTURE_ROWS,
            "fixtures.performance",
            "row_contract",
        )
        _require(
            int(log_facts["target_count"] or 0) == expected_target_count,
            "fixtures.performance",
            "target_distribution",
        )
        return {
            "row_count": PERFORMANCE_FIXTURE_ROWS,
            "target_count": expected_target_count,
        }

    def prepare_fixtures(self) -> dict[str, Any]:
        """在隔离运行库创建第二租户、同 ID 哨兵和 10 万行性能夹具。"""

        connection = self._connect_runtime()
        secondary_password = f"Prd055TenantB!{secrets.token_urlsafe(18)}"
        secondary_email = f"prd055-tenant-b-{self.suffix}@metahub.local"
        secondary_slug = f"prd055-{self.fixture_marker}"
        secondary_tenant_id = f"tenant_prd055_{self.fixture_marker}"
        secondary_user_id = f"user_prd055_{self.suffix}"
        shared_request_id = f"req_prd055_shared_{self.fixture_marker}"
        now = datetime.now(UTC).replace(microsecond=0)
        timestamp = _db_time(now)
        try:
            default_tenant = connection.execute(
                "SELECT tenant_id FROM tenants WHERE slug=? AND status='active'",
                (self.config.tenant_slug,),
            ).fetchone()
            _require(default_tenant is not None, "fixtures.default_tenant", "missing")
            default_tenant_id = str(default_tenant["tenant_id"])
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id, slug, name, service_mode, status, created_at, updated_at
                ) VALUES(?, ?, 'PRD055 Tenant B', 'internal_only', 'active', ?, ?)
                ON CONFLICT(tenant_id) DO NOTHING
                """,
                (secondary_tenant_id, secondary_slug, timestamp, timestamp),
            )
            secondary_tenant = connection.execute(
                """
                SELECT slug, service_mode, status
                FROM tenants WHERE tenant_id=?
                """,
                (secondary_tenant_id,),
            ).fetchone()
            # 确定性 ID 若碰到非本批次租户必须失败关闭，不能改写对方 slug 或服务模式。
            _require(
                secondary_tenant is not None
                and secondary_tenant["slug"] == secondary_slug
                and secondary_tenant["service_mode"] == "internal_only"
                and secondary_tenant["status"] == "active",
                "fixtures.secondary_tenant",
                "identity_conflict",
            )
            existing_secondary_user = connection.execute(
                """
                SELECT user_id FROM users
                WHERE user_id=? OR (tenant_id=? AND email=?)
                """,
                (secondary_user_id, secondary_tenant_id, secondary_email),
            ).fetchone()
            # 随机管理员身份仍需在写事务中查重，任何已有 ID/邮箱都不能被验收器覆盖或提权。
            _require(
                existing_secondary_user is None,
                "fixtures.secondary_user",
                "identity_conflict",
            )
            connection.execute(
                """
                INSERT INTO users(
                  user_id, tenant_id, email, display_name, password_hash, role, status,
                  created_at, updated_at, failed_login_count, locked_until
                ) VALUES(?, ?, ?, 'PRD055 Tenant B Admin', ?, 'admin', 'active', ?, ?, 0, NULL)
                """,
                (
                    secondary_user_id,
                    secondary_tenant_id,
                    secondary_email,
                    hash_password(secondary_password),
                    timestamp,
                    timestamp,
                ),
            )
            secondary_user = connection.execute(
                """
                SELECT tenant_id, email, role, status
                FROM users WHERE user_id=?
                """,
                (secondary_user_id,),
            ).fetchone()
            # admin 夹具只能绑定刚核验的第二租户；全局 user_id 冲突不得触发跨租户提权。
            _require(
                secondary_user is not None
                and secondary_user["tenant_id"] == secondary_tenant_id
                and secondary_user["email"] == secondary_email
                and secondary_user["role"] == "admin"
                and secondary_user["status"] == "active",
                "fixtures.secondary_user",
                "identity_conflict",
            )
            fixture_prefix = f"req_p55_{self.fixture_marker}_"
            existing = connection.execute(
                """
                SELECT COUNT(*) AS count FROM request_logs
                WHERE tenant_id=? AND substr(request_id, 1, ?)=?
                """,
                (default_tenant_id, len(fixture_prefix), fixture_prefix),
            ).fetchone()
            existing_count = int(existing["count"] if existing else 0)
            _require(
                existing_count in {0, PERFORMANCE_FIXTURE_ROWS},
                "fixtures.performance",
                "partial_existing",
            )
            if existing_count == 0:
                connection.executemany(
                    """
                    INSERT INTO gateway_request_claims(
                      tenant_id, request_id, trace_id, virtual_key_id,
                      snapshot_version, policy_snapshot_status, claimed_at
                    ) VALUES(?, ?, ?, NULL, NULL, 'disabled', ?)
                    """,
                    self._performance_claim_rows(default_tenant_id, now),
                )
                connection.executemany(
                    """
                    INSERT INTO request_logs(
                      request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
                      endpoint, status, error_code, latency_ms, request_model, resolved_model,
                      provider_code, input_tokens, output_tokens, total_tokens,
                      estimated_cost_amount, currency, price_schema_version,
                      snapshot_version, policy_snapshot_status, created_at, updated_at
                    ) VALUES(
                      ?, ?, ?, NULL, NULL, NULL, '/v1/chat/completions', ?, ?, ?, ?, ?, ?,
                      4, 6, ?, ?, 'CNY', 'prd055-v1', NULL, 'disabled', ?, ?
                    )
                    """,
                    self._performance_log_rows(default_tenant_id, now),
                )
            fixture_facts = self._verify_performance_fixture(
                connection,
                tenant_id=default_tenant_id,
                fixture_prefix=fixture_prefix,
            )
            for tenant_id, provider_code in (
                (default_tenant_id, "default-provider"),
                (secondary_tenant_id, "tenant-b-provider"),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO gateway_request_claims(
                      tenant_id, request_id, trace_id, virtual_key_id,
                      snapshot_version, policy_snapshot_status, claimed_at
                    ) VALUES(?, ?, ?, NULL, NULL, 'disabled', ?)
                    """,
                    (
                        tenant_id,
                        shared_request_id,
                        f"trc_shared_{self.fixture_marker}",
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO request_logs(
                      request_id, tenant_id, trace_id, endpoint, request_model,
                      resolved_model, provider_code, status, error_code, latency_ms,
                      input_tokens, output_tokens, total_tokens, estimated_cost_amount,
                      currency, price_schema_version, policy_snapshot_status,
                      created_at, updated_at
                    ) VALUES(
                      ?, ?, ?, '/v1/chat/completions', 'shared-model', 'shared-model',
                      ?, 'failed', 'gateway.upstream_error', 777, 4, 6, 10,
                      '0.000010', 'CNY', 'prd055-v1', 'disabled', ?, ?
                    )
                    """,
                    (
                        shared_request_id,
                        tenant_id,
                        f"trc_shared_{self.fixture_marker}",
                        provider_code,
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute("ANALYZE request_logs")
            connection.commit()
        # 夹具写入错误不得把 SQL、随机密码或数据库路径输出。
        except sqlite3.Error:
            connection.rollback()
            _fail("fixtures.database", "write_failed")
        finally:
            connection.close()

        self.secondary_identity = {
            "tenant_slug": secondary_slug,
            "email": secondary_email,
            "password": secondary_password,
            "user_id": secondary_user_id,
        }
        self.known_sensitive.extend((secondary_email, secondary_password))
        self.resource_ids.update(
            {
                "default_tenant_id": default_tenant_id,
                "secondary_tenant_id": secondary_tenant_id,
                "shared_request_id": shared_request_id,
            }
        )
        return {
            "performance_rows": fixture_facts["row_count"],
            "performance_target_rows": fixture_facts["target_count"],
            "performance_fixture_verified": True,
            "tenant_count": 2,
            "same_request_id_across_tenants": True,
            "fixture_in_batch_database": True,
        }


    def login_secondary_tenant(self) -> dict[str, Any]:
        """登录第二租户管理员，形成同 request_id 的真实横向隔离攻击身份。"""

        _require(bool(self.secondary_identity), "auth.secondary", "identity_missing")
        return self._login(
            session_key="tenant_b_admin",
            tenant_slug=self.secondary_identity["tenant_slug"],
            email=self.secondary_identity["email"],
            password=self.secondary_identity["password"],
            expected_role="admin",
            expected_user_id=self.secondary_identity["user_id"],
        )

    def _validate_list_payload(
        self,
        payload: Mapping[str, Any],
        *,
        expected_tenant_id: str,
        expected_provider: str | None = None,
    ) -> list[Mapping[str, Any]]:
        """校验分页 DTO、租户归属、QueryEvent 身份与敏感字段最小披露。"""

        logs = payload.get("request_logs")
        _require(isinstance(logs, list), "query.response", "logs_not_list")
        _require(type(payload.get("total")) is int, "query.response", "total_type")
        _require(type(payload.get("limit")) is int, "query.response", "limit_type")
        _require(type(payload.get("offset")) is int, "query.response", "offset_type")
        _require(type(payload.get("has_more")) is bool, "query.response", "has_more_type")
        event_id = payload.get("query_event_id")
        _require(
            isinstance(event_id, str) and QUERY_EVENT_PATTERN.fullmatch(event_id) is not None,
            "query.response",
            "query_event_id",
        )
        for item in logs:
            _require(isinstance(item, Mapping), "query.response", "log_not_object")
            _require(item.get("tenant_id") == expected_tenant_id, "query.response", "tenant_mismatch")
            # 指定 Provider 的用例必须逐行核对，防止分页结果混入其他上游。
            if expected_provider is not None:
                _require(item.get("provider_code") == expected_provider, "query.response", "provider_mismatch")
        assert_safe_payload(payload, sensitive_values=self.known_sensitive)
        return logs

    def _remember_query_audit(
        self,
        operation_request_id: str,
        *,
        session_key: str,
        filters: Mapping[str, Any],
        payload: Mapping[str, Any],
        saved_filter_id: str | None,
        sort_by: str,
        sort_direction: str,
        query_limit: int,
        query_offset: int,
    ) -> None:
        """冻结一次 HTTP 成功查询的预期事实，稍后与数据库 QueryEvent 一一比对。"""

        credential = self.sessions.get(session_key)
        trace_id = self.operation_trace_ids.get(operation_request_id)
        logs = payload.get("request_logs")
        event_id = payload.get("query_event_id")
        _require(credential is not None, "query_event.expectation", "session_missing")
        _require(
            isinstance(trace_id, str) and bool(trace_id),
            "query_event.expectation",
            "trace_missing",
        )
        _require(isinstance(logs, list), "query_event.expectation", "logs_missing")
        _require(
            isinstance(event_id, str) and QUERY_EVENT_PATTERN.fullmatch(event_id) is not None,
            "query_event.expectation",
            "event_id",
        )
        _require(filters.get("v") == 1, "query_event.expectation", "filter_version")
        canonical = json.dumps(
            dict(filters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expectation = QueryAuditExpectation(
            query_event_id=event_id,
            tenant_id=credential.tenant_id,
            actor_user_id=credential.user_id,
            actor_session_id=credential.session_id,
            operation_trace_id=trace_id,
            filter_fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            filter_keys=tuple(sorted(key for key in filters if key != "v")),
            saved_filter_id=saved_filter_id,
            sort_by=sort_by,
            sort_direction=sort_direction,
            query_limit=query_limit,
            query_offset=query_offset,
            matched_count=int(payload["total"]),
            returned_count=len(logs),
            has_more=bool(payload["has_more"]),
        )
        # 同一 operation 只能登记一次，否则后写预期可能掩盖重复 request_id。
        _require(
            operation_request_id not in self.query_audit_expectations,
            "query_event.expectation",
            "operation_duplicate",
        )
        self.query_audit_expectations[operation_request_id] = expectation
        self.successful_query_request_ids.append(operation_request_id)

    def verify_permissions_and_query_modes(self) -> dict[str, Any]:
        """验证三种查询来源、异常、viewer/匿名权限与第二租户同形隔离。"""

        self.successful_query_request_ids: list[str] = []
        self._control_call(
            action="anonymous_malformed",
            session_key=None,
            method="GET",
            path="/request-logs?filters=not_base64%25",
            expected_status=401,
            expected_code="auth.token_missing",
        )
        self._control_call(
            action="viewer_malformed",
            session_key="viewer",
            method="GET",
            path="/request-logs?filters=not_base64%25",
            expected_status=403,
            expected_code="auth.permission_denied",
        )

        shared_request_id = self.resource_ids["shared_request_id"]
        shared_filters = {"v": 1, "request_id": shared_request_id}
        shared_wire = _wire_filters(shared_filters)
        _, default_payload, default_operation = self._control_call(
            action="default_shared_query",
            session_key="admin",
            method="GET",
            path=f"/request-logs?filters={shared_wire}&limit=20&offset=0",
            expected_status=200,
            private_cache=True,
        )
        default_logs = self._validate_list_payload(
            default_payload,
            expected_tenant_id=self.resource_ids["default_tenant_id"],
            expected_provider="default-provider",
        )
        _require(
            len(default_logs) == 1 and default_logs[0].get("request_id") == shared_request_id,
            "query.default_shared",
            "result_mismatch",
        )
        self._remember_query_audit(
            default_operation,
            session_key="admin",
            filters=shared_filters,
            payload=default_payload,
            saved_filter_id=None,
            sort_by="created_at",
            sort_direction="desc",
            query_limit=20,
            query_offset=0,
        )

        _, secondary_payload, secondary_operation = self._control_call(
            action="secondary_shared_query",
            session_key="tenant_b_admin",
            method="GET",
            path=f"/request-logs?filters={shared_wire}&limit=20&offset=0",
            expected_status=200,
            private_cache=True,
        )
        secondary_logs = self._validate_list_payload(
            secondary_payload,
            expected_tenant_id=self.resource_ids["secondary_tenant_id"],
            expected_provider="tenant-b-provider",
        )
        _require(
            len(secondary_logs) == 1 and secondary_logs[0].get("request_id") == shared_request_id,
            "query.secondary_shared",
            "result_mismatch",
        )
        self._remember_query_audit(
            secondary_operation,
            session_key="tenant_b_admin",
            filters=shared_filters,
            payload=secondary_payload,
            saved_filter_id=None,
            sort_by="created_at",
            sort_direction="desc",
            query_limit=20,
            query_offset=0,
        )

        advanced_filters = {
            "v": 1,
            "status": "failed",
            "provider_code": "deepseek",
            "terminal_stage": "provider",
            "latency_ms_min": 0,
            "latency_ms_max": 1999,
        }
        advanced_wire = _wire_filters(advanced_filters)
        _, advanced_payload, advanced_operation = self._control_call(
            action="developer_advanced_query",
            session_key="developer",
            method="GET",
            path=(
                f"/request-logs?filters={advanced_wire}"
                "&sort_by=latency_ms&sort_direction=asc&limit=50&offset=50"
            ),
            expected_status=200,
            private_cache=True,
        )
        advanced_logs = self._validate_list_payload(
            advanced_payload,
            expected_tenant_id=self.resource_ids["default_tenant_id"],
            expected_provider="deepseek",
        )
        _require(len(advanced_logs) == 50, "query.advanced", "page_size")
        _require(
            all(item.get("status") == "failed" and item.get("terminal_stage") == "provider" for item in advanced_logs),
            "query.advanced",
            "filter_accuracy",
        )
        latencies = [int(item["latency_ms"]) for item in advanced_logs]
        _require(latencies == sorted(latencies), "query.advanced", "sort_accuracy")
        self._remember_query_audit(
            advanced_operation,
            session_key="developer",
            filters=advanced_filters,
            payload=advanced_payload,
            saved_filter_id=None,
            sort_by="latency_ms",
            sort_direction="asc",
            query_limit=50,
            query_offset=50,
        )

        _, legacy_payload, legacy_operation = self._control_call(
            action="legacy_query",
            session_key="admin",
            method="GET",
            path="/request-logs?model=enterprise-chat&limit=5&offset=0",
            expected_status=200,
            private_cache=True,
        )
        legacy_logs = self._validate_list_payload(
            legacy_payload,
            expected_tenant_id=self.resource_ids["default_tenant_id"],
        )
        _require(
            len(legacy_logs) == 5
            and all(
                item.get("request_model") == "enterprise-chat"
                or item.get("resolved_model") == "enterprise-chat"
                for item in legacy_logs
            ),
            "query.legacy",
            "model_semantics",
        )
        self._remember_query_audit(
            legacy_operation,
            session_key="admin",
            filters={"v": 1, "model": "enterprise-chat"},
            payload=legacy_payload,
            saved_filter_id=None,
            sort_by="created_at",
            sort_direction="desc",
            query_limit=5,
            query_offset=0,
        )

        self._control_call(
            action="mixed_query_rejected",
            session_key="admin",
            method="GET",
            path=f"/request-logs?filters={advanced_wire}&status=failed",
            expected_status=422,
            expected_code="request_log.query_invalid",
        )
        self._control_call(
            action="sort_rejected",
            session_key="admin",
            method="GET",
            path=f"/request-logs?filters={advanced_wire}&sort_by=tenant_id",
            expected_status=422,
            expected_code="request_log.sort_invalid",
        )
        self._control_call(
            action="pagination_rejected",
            session_key="admin",
            method="GET",
            path=f"/request-logs?filters={advanced_wire}&limit=101",
            expected_status=422,
            expected_code="request_log.pagination_invalid",
        )
        return {
            "anonymous_before_parse": True,
            "viewer_before_parse": True,
            "inline_v1_accurate": True,
            "legacy_compatible": True,
            "mixed_sources_rejected": True,
            "sort_whitelist_enforced": True,
            "pagination_bounded": True,
            "cross_tenant_same_id_isolated": True,
        }

    def verify_saved_filter_lifecycle(self) -> dict[str, Any]:
        """验证个人 SavedFilter CRUD、owner 同形 404、乐观锁与软删除。"""

        body = {
            "name": f"PRD055 Provider failures {self.fixture_marker}",
            "filters": {
                "v": 1,
                "status": "failed",
                "provider_code": "deepseek",
                "terminal_stage": "provider",
            },
            "sort_by": "latency_ms",
            "sort_direction": "asc",
        }
        _, created_payload, _ = self._control_call(
            action="saved_filter_create",
            session_key="admin",
            method="POST",
            path="/request-logs/saved-filters",
            expected_status=201,
            body=body,
            private_cache=True,
        )
        saved = created_payload.get("saved_filter")
        _require(isinstance(saved, Mapping), "saved_filter.create", "object_missing")
        saved_filter_id = saved.get("saved_filter_id")
        _require(
            isinstance(saved_filter_id, str)
            and SAVED_FILTER_PATTERN.fullmatch(saved_filter_id) is not None,
            "saved_filter.create",
            "id_invalid",
        )
        _require(saved.get("version") == 1, "saved_filter.create", "version")
        _require("tenant_id" not in saved and "owner_user_id" not in saved, "saved_filter.create", "owner_leak")

        _, listed_payload, _ = self._control_call(
            action="saved_filter_list",
            session_key="admin",
            method="GET",
            path="/request-logs/saved-filters",
            expected_status=200,
            private_cache=True,
        )
        listed = listed_payload.get("saved_filters")
        _require(
            isinstance(listed, list)
            and sum(item.get("saved_filter_id") == saved_filter_id for item in listed if isinstance(item, Mapping)) == 1,
            "saved_filter.list",
            "created_missing",
        )

        _, applied_payload, operation_id = self._control_call(
            action="saved_filter_apply",
            session_key="admin",
            method="GET",
            path=f"/request-logs?saved_filter_id={saved_filter_id}&limit=10&offset=0",
            expected_status=200,
            private_cache=True,
        )
        applied_logs = self._validate_list_payload(
            applied_payload,
            expected_tenant_id=self.resource_ids["default_tenant_id"],
            expected_provider="deepseek",
        )
        _require(
            bool(applied_logs) and all(item.get("status") == "failed" for item in applied_logs),
            "saved_filter.apply",
            "filter_accuracy",
        )
        self._remember_query_audit(
            operation_id,
            session_key="admin",
            filters=body["filters"],
            payload=applied_payload,
            saved_filter_id=saved_filter_id,
            sort_by="latency_ms",
            sort_direction="asc",
            query_limit=10,
            query_offset=0,
        )

        for session_key, action in (
            ("developer", "saved_filter_other_owner"),
            ("tenant_b_admin", "saved_filter_cross_tenant"),
        ):
            self._control_call(
                action=action,
                session_key=session_key,
                method="GET",
                path=f"/request-logs?saved_filter_id={saved_filter_id}",
                expected_status=404,
                expected_code="saved_filter.not_found",
            )

        developer_body = {
            **body,
            "name": f"PRD055 Developer private {self.fixture_marker}",
        }
        _, developer_created_payload, _ = self._control_call(
            action="saved_filter_developer_create",
            session_key="developer",
            method="POST",
            path="/request-logs/saved-filters",
            expected_status=201,
            body=developer_body,
            private_cache=True,
        )
        developer_saved = developer_created_payload.get("saved_filter")
        _require(
            isinstance(developer_saved, Mapping),
            "saved_filter.developer_create",
            "object_missing",
        )
        developer_saved_id = developer_saved.get("saved_filter_id")
        _require(
            isinstance(developer_saved_id, str)
            and SAVED_FILTER_PATTERN.fullmatch(developer_saved_id) is not None,
            "saved_filter.developer_create",
            "id_invalid",
        )
        # admin 只有管理权限而没有他人个人筛选所有权，必须与普通跨 owner 访问同样返回 404。
        self._control_call(
            action="saved_filter_admin_override",
            session_key="admin",
            method="GET",
            path=f"/request-logs?saved_filter_id={developer_saved_id}",
            expected_status=404,
            expected_code="saved_filter.not_found",
        )
        self._control_call(
            action="saved_filter_developer_delete",
            session_key="developer",
            method="DELETE",
            path=f"/request-logs/saved-filters/{developer_saved_id}?version=1",
            expected_status=204,
            private_cache=True,
        )

        _, patched_payload, _ = self._control_call(
            action="saved_filter_update",
            session_key="admin",
            method="PATCH",
            path=f"/request-logs/saved-filters/{saved_filter_id}",
            expected_status=200,
            body={"version": 1, "name": f"PRD055 renamed {self.fixture_marker}"},
            private_cache=True,
        )
        patched = patched_payload.get("saved_filter")
        _require(isinstance(patched, Mapping) and patched.get("version") == 2, "saved_filter.update", "version")
        self._control_call(
            action="saved_filter_stale",
            session_key="admin",
            method="PATCH",
            path=f"/request-logs/saved-filters/{saved_filter_id}",
            expected_status=409,
            body={"version": 1, "name": "stale"},
            expected_code="saved_filter.version_conflict",
        )
        self._control_call(
            action="saved_filter_delete",
            session_key="admin",
            method="DELETE",
            path=f"/request-logs/saved-filters/{saved_filter_id}?version=2",
            expected_status=204,
            private_cache=True,
        )
        self._control_call(
            action="saved_filter_deleted_apply",
            session_key="admin",
            method="GET",
            path=f"/request-logs?saved_filter_id={saved_filter_id}",
            expected_status=404,
            expected_code="saved_filter.not_found",
        )
        self.resource_ids["deleted_saved_filter_id"] = saved_filter_id
        return {
            "create_list_apply_update_delete": True,
            "owner_isolation_same_tenant": True,
            "admin_cannot_override_owner": True,
            "tenant_isolation": True,
            "optimistic_lock": True,
            "soft_delete": True,
            "identity_fields_not_exposed": True,
        }


    def verify_query_plans(self) -> dict[str, Any]:
        """用 EXPLAIN QUERY PLAN 证明 tenant-first 常用筛选命中新索引。"""

        connection = self._connect_runtime()
        tenant_id = self.resource_ids["default_tenant_id"]
        shared_trace = f"trc_shared_{self.fixture_marker}"
        cases = {
            "provider": (
                """
                EXPLAIN QUERY PLAN
                SELECT request_id FROM request_logs
                WHERE tenant_id=? AND provider_code=?
                ORDER BY created_at DESC, request_id DESC LIMIT 21
                """,
                (tenant_id, "deepseek"),
                "idx_request_logs_provider_time_prd055",
            ),
            "trace": (
                """
                EXPLAIN QUERY PLAN
                SELECT request_id FROM request_logs
                WHERE tenant_id=? AND trace_id=?
                ORDER BY created_at DESC, request_id DESC LIMIT 21
                """,
                (tenant_id, shared_trace),
                "idx_request_logs_trace_time_prd055",
            ),
            "latency_sort": (
                """
                EXPLAIN QUERY PLAN
                SELECT request_id FROM request_logs
                WHERE tenant_id=?
                ORDER BY latency_ms ASC, created_at ASC, request_id ASC LIMIT 21
                """,
                (tenant_id,),
                "idx_request_logs_latency_time_prd055",
            ),
        }
        used: list[str] = []
        try:
            for name, (statement, parameters, expected_index) in cases.items():
                details = " ".join(
                    str(row["detail"]) for row in connection.execute(statement, parameters).fetchall()
                )
                _require(expected_index in details, f"query_plan.{name}", "index_not_used")
                _require("SCAN request_logs" not in details, f"query_plan.{name}", "full_scan")
                used.append(expected_index)
        # 查询计划读取失败只输出固定分类，不能把 SQL 或计划正文写入证据。
        except sqlite3.Error:
            _fail("query_plan.database", "explain_failed")
        finally:
            connection.close()
        return {
            "explain_query_plan_verified": True,
            "tenant_first_indexes_used": sorted(used),
            "full_table_scan_rejected": True,
        }

    def verify_performance(self) -> dict[str, Any]:
        """在十万行真实库上测量预热后 20 次 API 查询的 P95 与单次上限。"""

        performance_filters = {
            "v": 1,
            "status": "failed",
            "provider_code": "deepseek",
            "terminal_stage": "provider",
            "latency_ms_min": 0,
            "latency_ms_max": 1999,
        }
        filters = _wire_filters(performance_filters)
        _, warmup, warmup_operation = self._control_call(
            action="performance_warmup",
            session_key="developer",
            method="GET",
            path=(
                f"/request-logs?filters={filters}"
                "&sort_by=latency_ms&sort_direction=asc&limit=20&offset=0"
            ),
            expected_status=200,
            private_cache=True,
        )
        self._validate_list_payload(
            warmup,
            expected_tenant_id=self.resource_ids["default_tenant_id"],
            expected_provider="deepseek",
        )
        self._remember_query_audit(
            warmup_operation,
            session_key="developer",
            filters=performance_filters,
            payload=warmup,
            saved_filter_id=None,
            sort_by="latency_ms",
            sort_direction="asc",
            query_limit=20,
            query_offset=0,
        )

        samples: list[int] = []
        for index in range(PERFORMANCE_SAMPLE_COUNT):
            offset = (index % 5) * 20
            started = perf_counter()
            _, payload, operation_id = self._control_call(
                action=f"performance_{index:02d}",
                session_key="developer",
                method="GET",
                path=(
                    f"/request-logs?filters={filters}"
                    f"&sort_by=latency_ms&sort_direction=asc&limit=20&offset={offset}"
                ),
                expected_status=200,
                private_cache=True,
            )
            elapsed_ms = max(0, round((perf_counter() - started) * 1000))
            logs = self._validate_list_payload(
                payload,
                expected_tenant_id=self.resource_ids["default_tenant_id"],
                expected_provider="deepseek",
            )
            _require(len(logs) == 20, "performance.response", "page_size")
            _require(int(payload.get("total", 0)) >= 25_000, "performance.response", "dataset_too_small")
            samples.append(elapsed_ms)
            self._remember_query_audit(
                operation_id,
                session_key="developer",
                filters=performance_filters,
                payload=payload,
                saved_filter_id=None,
                sort_by="latency_ms",
                sort_direction="asc",
                query_limit=20,
                query_offset=offset,
            )
        p95 = _nearest_rank_p95(samples)
        maximum = max(samples)
        _require(p95 <= PERFORMANCE_P95_LIMIT_MS, "performance.threshold", "p95_exceeded")
        _require(
            maximum <= PERFORMANCE_SINGLE_LIMIT_MS,
            "performance.threshold",
            "single_exceeded",
        )
        return {
            "fixture_rows": PERFORMANCE_FIXTURE_ROWS,
            "sample_count": len(samples),
            "warmup_excluded": True,
            "p95_ms": p95,
            "max_ms": maximum,
            "p95_limit_ms": PERFORMANCE_P95_LIMIT_MS,
            "single_limit_ms": PERFORMANCE_SINGLE_LIMIT_MS,
            "thresholds_passed": True,
        }

    def verify_query_events_and_outbox(self) -> dict[str, Any]:
        """核对每次成功查询的不可变事实、隐私字段与真实后台投递终态。"""

        operation_ids = tuple(self.query_audit_expectations)
        _require(bool(operation_ids), "query_event.audit", "operations_missing")
        _require(
            len(self.successful_query_request_ids) == len(operation_ids)
            and set(self.successful_query_request_ids) == set(operation_ids),
            "query_event.audit",
            "expectation_count",
        )
        placeholders = ",".join("?" for _ in operation_ids)
        connection = self._connect_runtime()
        try:
            rows = connection.execute(
                f"""
                SELECT query_event_id, event_id, tenant_id, actor_user_id, actor_session_id,
                       filter_version, filter_fingerprint, filter_keys_json, saved_filter_id,
                       sort_by, sort_direction, query_limit, query_offset, matched_count,
                       returned_count, has_more, query_duration_ms, operation_request_id,
                       operation_trace_id, event_dispatch_status, event_dispatch_attempts
                FROM request_log_query_events
                WHERE operation_request_id IN ({placeholders})
                """,
                operation_ids,
            ).fetchall()
            _require(len(rows) == len(operation_ids), "query_event.audit", "event_count")
            rows_by_operation: dict[str, sqlite3.Row] = {}
            for row in rows:
                operation_id = str(row["operation_request_id"])
                expectation = self.query_audit_expectations.get(operation_id)
                _require(expectation is not None, "query_event.audit", "operation_unknown")
                _require(
                    operation_id not in rows_by_operation,
                    "query_event.audit",
                    "operation_duplicate",
                )
                rows_by_operation[operation_id] = row
                filter_keys = json.loads(str(row["filter_keys_json"]))
                _require(
                    isinstance(filter_keys, list)
                    and tuple(filter_keys) == expectation.filter_keys,
                    "query_event.audit",
                    "filter_keys",
                )
                _require(
                    str(row["query_event_id"]) == expectation.query_event_id
                    and QUERY_EVENT_PATTERN.fullmatch(str(row["query_event_id"])) is not None
                    and QUERY_WEBHOOK_EVENT_PATTERN.fullmatch(str(row["event_id"])) is not None,
                    "query_event.audit",
                    "event_identity",
                )
                # actor 三元组与 operation trace 必须来自发起该查询的真实 Session，不能只验证格式。
                _require(
                    row["tenant_id"] == expectation.tenant_id
                    and row["actor_user_id"] == expectation.actor_user_id
                    and row["actor_session_id"] == expectation.actor_session_id
                    and row["operation_trace_id"] == expectation.operation_trace_id,
                    "query_event.audit",
                    "actor_binding",
                )
                fingerprint = str(row["filter_fingerprint"])
                _require(
                    FINGERPRINT_PATTERN.fullmatch(fingerprint) is not None
                    and hmac.compare_digest(
                        fingerprint,
                        expectation.filter_fingerprint,
                    ),
                    "query_event.audit",
                    "fingerprint",
                )
                _require(row["filter_version"] == 1, "query_event.audit", "filter_version")
                _require(
                    row["saved_filter_id"] == expectation.saved_filter_id
                    and row["sort_by"] == expectation.sort_by
                    and row["sort_direction"] == expectation.sort_direction,
                    "query_event.audit",
                    "query_contract",
                )
                _require(
                    row["query_limit"] == expectation.query_limit
                    and row["query_offset"] == expectation.query_offset
                    and row["matched_count"] == expectation.matched_count
                    and row["returned_count"] == expectation.returned_count
                    and row["has_more"] == int(expectation.has_more),
                    "query_event.audit",
                    "result_contract",
                )
                _require(row["query_duration_ms"] >= 0, "query_event.audit", "duration")
                _require(
                    row["event_dispatch_status"] in {"pending", "processing", "dispatched"},
                    "query_event.audit",
                    "dispatch_state",
                )
            _require(
                set(rows_by_operation) == set(operation_ids),
                "query_event.audit",
                "event_coverage",
            )
        # 审计查询失败不能暴露事件 ID、actor、Session 或数据库位置。
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            _fail("query_event.audit", "query_failed")
        finally:
            connection.close()

        deadline = perf_counter() + 15.0
        states: list[sqlite3.Row] = []
        while perf_counter() < deadline:
            connection = self._connect_runtime()
            try:
                states = connection.execute(
                    f"""
                    SELECT operation_request_id, event_dispatch_status, event_dispatch_attempts
                    FROM request_log_query_events
                    WHERE operation_request_id IN ({placeholders})
                    """,
                    operation_ids,
                ).fetchall()
            # 后台状态读取错误统一失败，不泄露 source outbox 内部列值。
            except sqlite3.Error:
                _fail("query_event.outbox", "query_failed")
            finally:
                connection.close()
            if len(states) == len(operation_ids) and all(
                row["event_dispatch_status"] == "dispatched" for row in states
            ):
                break
            time.sleep(0.25)
        _require(len(states) == len(operation_ids), "query_event.outbox", "event_count")
        _require(
            all(row["event_dispatch_status"] == "dispatched" for row in states),
            "query_event.outbox",
            "not_dispatched",
        )
        # 每条事件都必须真正被 worker claim 过；只检查 MAX 会掩盖零次投递的事件。
        _require(
            all(1 <= int(row["event_dispatch_attempts"]) <= 10 for row in states),
            "query_event.outbox",
            "attempts",
        )

        first_operation = operation_ids[0]
        self.verify_database_immutability(
            rows_by_operation[first_operation]["query_event_id"]
        )
        return {
            "successful_query_event_count": len(operation_ids),
            "filter_values_absent": True,
            "fingerprints_verified": True,
            "owner_session_bound": True,
            "source_outbox_dispatched": True,
            "attempts_bounded": True,
            "immutable_facts_verified": True,
        }

    def verify_database_immutability(self, query_event_id: str) -> None:
        """直接攻击 QueryEvent 和已删除 SavedFilter，证明数据库不可变门禁生效。"""

        connection = self._connect_runtime()
        event_blocked = False
        filter_blocked = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE request_log_query_events SET matched_count=matched_count+1 WHERE query_event_id=?",
                    (query_event_id,),
                )
            # 不可变事件必须由 0045 触发器拒绝，不能只依赖服务层没有更新入口。
            except sqlite3.IntegrityError:
                event_blocked = True
            connection.rollback()

            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM saved_filters WHERE saved_filter_id=?",
                    (self.resource_ids["deleted_saved_filter_id"],),
                )
            # 软删除后的个人筛选仍是审计事实，物理删除必须被数据库拒绝。
            except sqlite3.IntegrityError:
                filter_blocked = True
            connection.rollback()
        # 攻击验证异常不得回显触发器名称、SQL 或对象 ID。
        except sqlite3.Error:
            connection.rollback()
            _fail("database.immutability", "attack_failed")
        finally:
            connection.close()
        _require(event_blocked, "database.immutability", "event_update_allowed")
        _require(filter_blocked, "database.immutability", "filter_delete_allowed")


    def verify_final_database(self) -> dict[str, Any]:
        """在全部 API 与攻击用例后复查行数、完整性、外键和 WAL checkpoint。"""

        connection = self._connect_runtime()
        try:
            default_count = connection.execute(
                "SELECT COUNT(*) AS count FROM request_logs WHERE tenant_id=?",
                (self.resource_ids["default_tenant_id"],),
            ).fetchone()
            secondary_count = connection.execute(
                "SELECT COUNT(*) AS count FROM request_logs WHERE tenant_id=?",
                (self.resource_ids["secondary_tenant_id"],),
            ).fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            _require(
                default_count is not None and int(default_count["count"]) >= PERFORMANCE_FIXTURE_ROWS,
                "database.final",
                "default_rows",
            )
            _require(
                secondary_count is not None and int(secondary_count["count"]) >= 1,
                "database.final",
                "secondary_rows",
            )
            _require(
                len(integrity) == 1 and str(integrity[0][0]).lower() == "ok",
                "database.final",
                "integrity",
            )
            _require(not foreign_keys, "database.final", "foreign_keys")
            _require(checkpoint is not None and len(checkpoint) == 3, "database.final", "checkpoint")
            busy, log_frames, checkpointed_frames = (int(value) for value in checkpoint)
            # PASSIVE checkpoint 不得残留未刷盘帧；-1/-1 表示当前库未启用 WAL，同样没有待刷帧。
            _require(
                busy == 0
                and (
                    (log_frames == -1 and checkpointed_frames == -1)
                    or (
                        log_frames >= 0
                        and checkpointed_frames == log_frames
                    )
                ),
                "database.final",
                "checkpoint_incomplete",
            )
        # 最终数据库检查只返回固定分类，不能把路径、SQL 或 checkpoint 细节写入证据。
        except sqlite3.Error:
            _fail("database.final", "query_failed")
        finally:
            connection.close()
        return {
            "default_rows_at_least_100000": True,
            "secondary_tenant_rows_present": True,
            "integrity_check": "ok",
            "foreign_key_check": "ok",
            "wal_checkpoint_executed": True,
            "wal_checkpoint_complete": True,
        }

    def run(self) -> dict[str, Any]:
        """按迁移、服务、夹具、身份、API、性能和事件顺序执行聚焦验收。"""

        database = self.verify_runtime_database_ready()
        services = self.verify_services()
        fixtures = self.prepare_fixtures()
        admin_login = self._login(
            session_key="admin",
            tenant_slug=self.config.tenant_slug,
            email=self.config.admin_email,
            password=self.config.admin_password,
            expected_role="admin",
        )
        roles = self.create_role_users()
        secondary_login = self.login_secondary_tenant()
        queries = self.verify_permissions_and_query_modes()
        saved_filters = self.verify_saved_filter_lifecycle()
        query_plans = self.verify_query_plans()
        performance = self.verify_performance()
        events = self.verify_query_events_and_outbox()
        final_database = self.verify_final_database()
        return {
            "schema_version": 1,
            "prd_id": "PRD-055",
            "status": "passed",
            "generated_resources": {
                "shared_request_id": self.resource_ids["shared_request_id"],
                "performance_rows": PERFORMANCE_FIXTURE_ROWS,
            },
            "checks": {
                "database": database,
                "services": services,
                "fixtures": fixtures,
                "admin_login": admin_login,
                "secondary_login": secondary_login,
                "roles": roles,
                "queries": queries,
                "saved_filters": saved_filters,
                "query_plans": query_plans,
                "performance": performance,
                "events": events,
                "final_database": final_database,
            },
            "http": dict(self.http_evidence),
        }

    def _logout_one(self, session_key: str) -> bool:
        """注销一个已登记 Session，并用同库状态证明服务端完成撤销。"""

        credential = self.sessions.get(session_key)
        if credential is None or not credential.access_value:
            return False
        headers, request_id, trace_id = self._headers(
            f"logout_{session_key}",
            session_key=session_key,
        )
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/logout",
            stage=f"auth.logout_{session_key}",
            headers=headers,
        )
        verify_response_contract(
            response,
            stage=f"auth.logout_{session_key}",
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
        # 注销后的数据库核验失败不能回显 Session ID 或数据库路径。
        except sqlite3.Error:
            _fail(f"auth.logout_{session_key}", "query_failed")
        finally:
            connection.close()
        return row is not None and row["status"] == "revoked"

    def logout_all(self) -> dict[str, bool]:
        """按低权限到管理员顺序撤销本轮所有 Session，兼容中途失败清理。"""

        results: dict[str, bool] = {}
        session_order = tuple(
            key
            for key in ("viewer", "developer", "tenant_b_admin", "admin")
            if key in self.sessions
        )
        for session_key in session_order:
            try:
                results[f"{session_key}_session_revoked"] = self._logout_one(session_key)
            # 单个 Session 注销失败不能阻止后续身份撤销；最终统一以 cleanup 失败关闭验收。
            except Exception:
                results[f"{session_key}_session_revoked"] = False
        expected = {f"{key}_session_revoked" for key in session_order}
        results["session_revoked"] = bool(expected) and all(
            results.get(key) is True for key in expected
        )
        return results

    def sensitive_values(self) -> tuple[str, ...]:
        """返回内存中已知明文，供最终证据做等值泄露扫描。"""

        return tuple(value for value in self.known_sensitive if value)

    def clear_sensitive_memory(self) -> None:
        """清空验收器持有的密码、Token、邮箱和第二租户身份引用。"""

        for credential in self.sessions.values():
            credential.access_value = ""
            credential.password = ""
            credential.email = ""
        self.secondary_identity.clear()
        self.known_sensitive.clear()


def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """以独占创建方式发布 Runtime 证据，禁止同批次重跑覆盖事实。"""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "runtime-acceptance.json"
    encoded = (
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    try:
        with target.open("xb") as handle:
            handle.write(encoded)
    # 已存在或写入失败都必须失败关闭，防止覆盖权威验收证据。
    except FileExistsError:
        _fail("evidence.publish", "already_exists")
    except OSError:
        _fail("evidence.publish", "write_failed")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """执行真实验收、强制注销全部 Session、隐私扫描并发布证据。"""

    acceptance: RuntimeAcceptance | None = None
    candidate: dict[str, Any] | None = None
    cleanup: dict[str, bool] = {"session_revoked": False}
    sensitive: tuple[str, ...] = (config.admin_password,)
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
            try:
                sensitive = acceptance.sensitive_values()
                cleanup = acceptance.logout_all()
            # 即使注销实现出现意外异常，也必须先清除验收器持有的明文引用再向上失败。
            finally:
                acceptance.clear_sensitive_memory()
    _require(candidate is not None, "runtime.execute", "candidate_missing")
    _require(
        cleanup.get("admin_session_revoked") is True
        and cleanup.get("developer_session_revoked") is True
        and cleanup.get("viewer_session_revoked") is True
        and cleanup.get("tenant_b_admin_session_revoked") is True
        and cleanup.get("session_revoked") is True,
        "runtime.cleanup",
        "session_not_revoked",
    )
    candidate["checks"]["cleanup"] = cleanup
    assert_safe_payload(candidate, sensitive_values=sensitive)
    publish_evidence(config.artifact_directory, candidate)
    return candidate


def main(argv: list[str] | None = None) -> int:
    """命令行入口只输出固定成功摘要或脱敏失败分类。"""

    try:
        config = parse_config(argv)
        execute_runtime(config)
    except AcceptanceFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print(
            "prd055_runtime_acceptance_failed stage=runtime.unhandled code=unexpected",
            file=sys.stderr,
        )
        return 1
    print('{"prd_id":"PRD-055","status":"passed","evidence":"runtime-acceptance.json"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
