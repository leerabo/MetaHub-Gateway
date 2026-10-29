"""PRD-052 三级预算的聚焦真实运行验收。

脚本只连接调用方已经启动的回环 Control API 与 Gateway，不负责启动服务或执行迁移。
运行数据库必须是同一批次 artifacts/prd052/<batch>/runtime.db；管理员登录后，
脚本先用 Session 哈希证明 Control 绑定该库，再以本轮 Project/Application/Virtual Key
归属写入一条受控 Usage，由 0041 触发器生成正式 Usage Charge。最终证据只保留安全 ID、
HTTP 状态和布尔结论，不保存口令、Session Token、Virtual Key 明文或模型输入输出。
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlencode, urlsplit

import httpx


# 网络目标只允许显式端口的 HTTP 回环 origin，认证材料不得被参数误送到外部主机。
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
BATCH_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")

# 证据字段和值同时执行隐私扫描；Virtual Key 的内部 ID 可以保留，但任何明文槽位都禁止。
FORBIDDEN_EVIDENCE_KEY_PATTERN = re.compile(
    r"password|access_token|authorization|plain_value|prompt|response|secret",
    re.IGNORECASE,
)
FORBIDDEN_EVIDENCE_TEXT_PATTERN = re.compile(
    r"Bearer\s+|mhv_[A-Za-z0-9_-]{8,}",
    re.IGNORECASE,
)

# 受控 Usage 高于 Project 额度，而 Tenant 与 Key 额度保持充足，确保 Gateway 主因稳定为 Project。
POSTED_USAGE_CNY = "1.000000"
PROJECT_BUDGET_BEFORE_PATCH_CNY = "2.000000"
PROJECT_BUDGET_AFTER_PATCH_CNY = "0.500000"
WIDE_BUDGET_CNY = "100.000000"
DEFAULT_MODEL_ALIAS = "default-chat"
# 验收器直接注入的 Usage 没有真实模型费率，只能使用 0041 定义的 legacy amount-only
# 证据组合；source 与 schema version 必须成对固定，否则数据库 owner trigger 会拒绝写入。
FORMAL_USAGE_PRICE_SNAPSHOT_SOURCE = "finops_legacy_backfill"
FORMAL_USAGE_PRICE_SCHEMA_VERSION = "legacy-unversioned-v1"

# 运行前只检查 PRD-052 所需的最小 schema 面，脚本绝不自行创建表或执行迁移。
REQUIRED_RUNTIME_TABLES = frozenset(
    {
        "applications",
        "budget_rules",
        "budget_scope_events",
        "environments",
        "finops_ledger_entries",
        "gateway_request_claims",
        "price_snapshots",
        "projects",
        "request_logs",
        "routing_attempts",
        "schema_migrations",
        "sessions",
        "usage_ledger",
        "virtual_keys",
    }
)
REQUIRED_RUNTIME_TRIGGERS = frozenset(
    {
        "trg_price_snapshots_owner_insert_prd051",
        "trg_usage_ledger_finops_owner_insert_prd051",
        "trg_usage_ledger_finops_amount_insert_prd051",
        "trg_usage_ledger_finops_projection_insert_prd051",
    }
)


class AcceptanceFailure(RuntimeError):
    """只携带脚本内部固定 stage/code 的脱敏失败，禁止底层正文和路径进入终端。"""

    def __init__(self, stage: str, code: str) -> None:
        """构造公开失败；两个值都必须由脚本常量调用点提供。"""

        super().__init__(f"prd052_runtime_acceptance_failed stage={stage} code={code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeConfig:
    """描述聚焦验收配置，敏感管理员口令只驻留当前 Python 进程。"""

    # 两个 origin 均为无路径、无凭据的回环地址，且必须彼此不同。
    control_origin: str
    gateway_origin: str
    # 证据和数据库严格共享一个 PRD-052 批次目录，禁止写入开发库。
    artifact_directory: Path
    database_path: Path
    # 租户入口和管理员身份来自环境变量，tenant_id 仍只能由登录响应恢复。
    tenant_slug: str
    admin_email: str
    admin_password: str
    # 单请求超时同时作为 SQLite busy timeout 上限，防止验收无限等待。
    timeout_seconds: float


@dataclass
class SessionCredential:
    """保存本轮短生命周期 Session 的内存态，最终只公开注销布尔结论。"""

    # role、tenant_id、user_id、session_id 是可核验的安全身份 ID。
    role: str
    tenant_id: str
    user_id: str
    session_id: str
    # access_value 与 password 是敏感材料，必须在 finally 中清空且绝不进入证据。
    access_value: str
    password: str


def _fail(stage: str, code: str) -> None:
    """抛出固定脱敏验收错误，不拼接数据库异常、HTTP 正文或用户输入。"""

    raise AcceptanceFailure(stage, code)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行布尔门禁；失败时只暴露调用点声明的固定阶段与错误码。"""

    if not condition:
        _fail(stage, code)


def _safe_identifier(value: object, stage: str, code: str) -> str:
    """读取服务端安全 ID，并拒绝空值、超长值和可能承载正文的字符。"""

    _require(isinstance(value, str), stage, code)
    _require(SAFE_IDENTIFIER_PATTERN.fullmatch(value) is not None, stage, code)
    return value


def normalize_loopback_origin(value: str, *, stage: str) -> str:
    """规范化一个回环 HTTP origin，拒绝外部主机、凭据、路径、查询和 fragment。"""

    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    # URL 或端口解析错误不得回显原始参数，以免凭据型错误输入进入日志。
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
    """从脚本物理位置返回 MetaHub 工作区根，避免依赖调用方当前目录。"""

    root = Path(__file__).resolve().parents[2]
    _require(root.name.lower() == "metahub", "config.workspace", "unexpected_root")
    return root


def resolve_artifact_directory(
    value: str | Path,
    *,
    workspace_root: Path | None = None,
) -> Path:
    """把证据目录限制在 artifacts/prd052 下恰好一个批次层级。"""

    root = (workspace_root or _workspace_root()).resolve()
    artifact_root = (root / "artifacts" / "prd052").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(artifact_root)
    # 越界路径只映射固定错误，不把宿主机目录结构写入失败输出。
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
    """把绝对 Windows/Posix 路径编码为无查询参数的 canonical SQLite URL。"""

    resolved = path.resolve()
    _require(resolved.is_absolute(), "config.database_url", "path_not_absolute")
    return f"sqlite:///{resolved.as_posix()}"


def resolve_runtime_database(value: str, *, artifact_directory: Path) -> Path:
    """解析 SQLite URL，并强制目标等于当前批次的 runtime.db。"""

    try:
        parsed = urlsplit(str(value))
    # URL 类型错误统一返回固定配置错误，不能回显可能包含的宿主机路径。
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
    candidate_source = Path(raw_path)
    _require(candidate_source.is_absolute(), "config.database_url", "path_not_absolute")
    candidate = candidate_source.resolve()
    expected = (artifact_directory / "runtime.db").resolve()
    _require(candidate == expected, "config.database_url", "not_batch_runtime")
    _require(not candidate_source.is_symlink(), "config.database_url", "database_symlink")
    return candidate


def parse_config(
    argv: list[str] | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> RuntimeConfig:
    """解析回环地址和批次路径，并只从环境变量读取管理员口令。"""

    env = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(description="PRD-052 三级预算真实运行验收")
    parser.add_argument(
        "--control-url",
        default=env.get("METAHUB_PRD052_CONTROL_URL", "http://localhost:8001"),
    )
    parser.add_argument(
        "--gateway-url",
        default=env.get("METAHUB_PRD052_GATEWAY_URL", "http://localhost:8000"),
    )
    parser.add_argument(
        "--artifact-dir",
        default=env.get("METAHUB_PRD052_ARTIFACT_DIR", ""),
    )
    parser.add_argument(
        "--database-url",
        default=env.get("METAHUB_PRD052_DATABASE_URL", ""),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    # 两个口令环境变量都要主动删除，避免专用值存在时 generic 值残留给后续子进程。
    specific_password = env.pop("METAHUB_PRD052_ADMIN_PASSWORD", "")
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
    control_origin = normalize_loopback_origin(
        args.control_url,
        stage="config.control_origin",
    )
    gateway_origin = normalize_loopback_origin(
        args.gateway_url,
        stage="config.gateway_origin",
    )
    # 分离 origin 可避免 Virtual Key 或 Session Token 因配置错误被发送到另一服务。
    _require(control_origin != gateway_origin, "config.origins", "must_be_distinct")

    tenant_slug = env.get("METAHUB_PRD052_TENANT_SLUG") or env.get(
        "METAHUB_TENANT_SLUG",
        "default",
    )
    admin_email = env.get("METAHUB_PRD052_ADMIN_EMAIL") or env.get(
        "METAHUB_ADMIN_EMAIL",
        "admin@metahub.local",
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
    """为一次真实 HTTP 操作生成无业务正文的 request_id 与 trace_id。"""

    request_id = f"req_prd052_accept_{action}_{suffix}"
    trace_id = f"trc_prd052_accept_{action}_{suffix}"
    _require(len(request_id) <= 128, "request.identity", "request_id_too_long")
    _require(len(trace_id) <= 128, "request.identity", "trace_id_too_long")
    _safe_identifier(request_id, "request.identity", "request_id_invalid")
    _safe_identifier(trace_id, "request.identity", "trace_id_invalid")
    return request_id, trace_id


def _json_object(response: httpx.Response, stage: str) -> dict[str, Any]:
    """读取 JSON object；失败时不回显正文、目标 URL 或代理错误页。"""

    try:
        payload = response.json()
    # 解码失败可能包含不可信代理正文，必须压缩成固定错误。
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
) -> dict[str, Any]:
    """核对 HTTP 状态、统一链路字段、错误码和预算 no-store 合同。"""

    _require(response.status_code == expected_status, stage, "status_mismatch")
    payload = _json_object(response, stage)
    _require(response.headers.get("x-request-id") == request_id, stage, "request_header")
    _require(response.headers.get("x-trace-id") == trace_id, stage, "trace_header")
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
    """调用固定相对路径，并把传输异常折叠为不含 URL 或认证值的安全错误。"""

    parsed = urlsplit(path)
    _require(
        path.startswith("/") and not parsed.scheme and not parsed.netloc and not parsed.fragment,
        stage,
        "path_not_relative",
    )
    try:
        if body is None:
            return client.request(method, path, headers=dict(headers))
        return client.request(method, path, headers=dict(headers), json=dict(body))
    # httpx 异常可能携带完整 URL，认证路径不允许把原异常文本冒泡到终端。
    except httpx.HTTPError:
        _fail(stage, "transport_error")


def _public_http_evidence(
    response: httpx.Response,
    payload: Mapping[str, Any],
    *,
    include_code: bool = False,
) -> dict[str, Any]:
    """把 HTTP 事实投影成状态和安全链路 ID，不复制任何原始请求或响应正文。"""

    evidence: dict[str, Any] = {
        "http_status": response.status_code,
        "request_id": payload["request_id"],
        "trace_id": payload["trace_id"],
    }
    if include_code:
        evidence["code"] = payload["code"]
    return evidence


class RuntimeAcceptance:
    """通过真实 Control/Gateway 与隔离运行库验证三级预算端到端合同。"""

    def __init__(
        self,
        config: RuntimeConfig,
        control_client: httpx.Client,
        gateway_client: httpx.Client,
    ) -> None:
        """保存客户端和本轮随机后缀；所有敏感值只存在当前实例内存。"""

        self.config = config
        self.control = control_client
        self.gateway = gateway_client
        self.suffix = secrets.token_hex(6)
        self.sessions: dict[str, SessionCredential] = {}
        self.resource_ids: dict[str, str] = {}
        self.budget_rule_ids: dict[str, str] = {}
        self.http_evidence: dict[str, dict[str, Any]] = {}
        self.known_sensitive: list[str] = [config.admin_password]
        self.virtual_key_value = ""
        self.gateway_input = f"prd052-private-runtime-input-{self.suffix}"
        self.known_sensitive.append(self.gateway_input)

    def _connect_runtime(self) -> sqlite3.Connection:
        """以读写模式连接已存在批次数据库，禁止 SQLite 隐式创建其他文件。"""

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
        # 打开失败不能回显 SQLite 物理路径或系统错误信息。
        except sqlite3.Error:
            _fail("database.connect", "open_failed")

    def verify_runtime_database_ready(self) -> dict[str, Any]:
        """确认 0042、所需表/触发器和 SQLite 完整性已由调用方提前准备。"""

        connection = self._connect_runtime()
        try:
            migration = connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0042'"
            ).fetchone()
            _require(
                migration is not None and migration["status"] == "applied",
                "database.readiness",
                "migration_0042_missing",
            )
            rows = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchall()
            tables = {str(row["name"]) for row in rows if row["type"] == "table"}
            triggers = {str(row["name"]) for row in rows if row["type"] == "trigger"}
            _require(
                REQUIRED_RUNTIME_TABLES <= tables,
                "database.readiness",
                "table_missing",
            )
            _require(
                REQUIRED_RUNTIME_TRIGGERS <= triggers,
                "database.readiness",
                "trigger_missing",
            )
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
        # schema 查询失败只说明运行库不满足验收前置条件，不能泄露 SQL 文本。
        except sqlite3.Error:
            _fail("database.readiness", "query_failed")
        finally:
            connection.close()
        return {
            "database_in_batch": True,
            "migration_0042_applied": True,
            "integrity_verified": True,
        }

    def _headers(
        self,
        action: str,
        *,
        role: str | None,
        include_access: bool = True,
    ) -> tuple[dict[str, str], str, str]:
        """构造一次请求链路头；Session 值只交给 httpx，不进入证据。"""

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
        role: str,
        method: str,
        path: str,
        expected_status: int,
        body: Mapping[str, Any] | None = None,
        expected_code: str | None = None,
        require_no_store: bool = False,
    ) -> tuple[httpx.Response, dict[str, Any]]:
        """调用单个 Control 接口并统一验证链路、错误码和预算缓存边界。"""

        headers, request_id, trace_id = self._headers(action, role=role)
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
        """创建短生命周期 Session，并校验角色、租户和可选用户 ID。"""

        action = f"login_{role}"
        headers, request_id, trace_id = self._headers(
            action,
            role=None,
            include_access=False,
        )
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
        # 登录成功即在服务端创建 Session，因此先从原始 JSON 登记可注销凭据；后续链路、
        # 角色、租户或 Session ID 校验即使失败，finally 仍能用有效 Token 主动撤销。
        provisional_payload = _json_object(response, f"auth.{action}")
        provisional_access = provisional_payload.get("access_token")
        if (
            isinstance(provisional_access, str)
            and bool(provisional_access)
        ):
            provisional_user = provisional_payload.get("user")
            provisional_tenant_id = (
                provisional_user.get("tenant_id") if isinstance(provisional_user, dict) else ""
            )
            provisional_user_id = (
                provisional_user.get("user_id") if isinstance(provisional_user, dict) else ""
            )
            provisional_session_id = provisional_payload.get("session_id")
            self.sessions[role] = SessionCredential(
                role=role,
                tenant_id=provisional_tenant_id if isinstance(provisional_tenant_id, str) else "",
                user_id=provisional_user_id if isinstance(provisional_user_id, str) else "",
                session_id=provisional_session_id if isinstance(provisional_session_id, str) else "",
                access_value=provisional_access,
                password=password,
            )
            self.known_sensitive.extend((password, provisional_access))
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
        tenant_id = _safe_identifier(
            user.get("tenant_id"),
            f"auth.{action}",
            "tenant_id_invalid",
        )
        user_id = _safe_identifier(
            user.get("user_id"),
            f"auth.{action}",
            "user_id_invalid",
        )
        if expected_user_id is not None:
            _require(user_id == expected_user_id, f"auth.{action}", "user_id_mismatch")
        # 首个 admin 登录建立权威租户，后续角色必须回到同一租户，避免测试账号串到其他组织。
        if self.resource_ids.get("tenant_id"):
            _require(
                tenant_id == self.resource_ids["tenant_id"],
                f"auth.{action}",
                "tenant_mismatch",
            )
        access_value = payload.get("access_token")
        _require(
            isinstance(access_value, str) and bool(access_value),
            f"auth.{action}",
            "access_missing",
        )
        session_id = _safe_identifier(
            payload.get("session_id"),
            f"auth.{action}",
            "session_id_invalid",
        )
        self.sessions[role] = SessionCredential(
            role=role,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            access_value=access_value,
            password=password,
        )
        self.resource_ids.setdefault("tenant_id", tenant_id)
        self.resource_ids[f"{role}_user_id"] = user_id
        evidence = _public_http_evidence(response, payload)
        self.http_evidence[action] = evidence
        return evidence

    def verify_session_database_binding(self, role: str) -> bool:
        """用 Session ID 与不可逆 Token 哈希证明 Control 实际绑定当前 runtime.db。"""

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
        # Session 绑定查询失败时不能继续向该文件写入伪造的运行证据。
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
        """经管理员公开 API 创建 developer/viewer，并登录形成真实权限攻击 Session。"""

        for role in ("developer", "viewer"):
            password = f"Prd052!{secrets.token_urlsafe(18)}"
            email = f"prd052-{role}-{self.suffix}@metahub.local"
            _, payload = self._control_call(
                action=f"create_{role}",
                role="admin",
                method="POST",
                path="/users",
                expected_status=201,
                body={
                    "email": email,
                    "display_name": f"PRD052 runtime {role}",
                    "password": password,
                    "role": role,
                    "status": "active",
                },
            )
            user = payload.get("user")
            _require(isinstance(user, dict), f"users.create_{role}", "user_missing")
            user_id = _safe_identifier(
                user.get("user_id"),
                f"users.create_{role}",
                "user_id_invalid",
            )
            _require(user.get("role") == role, f"users.create_{role}", "role_mismatch")
            self._login(
                role=role,
                email=email,
                password=password,
                expected_user_id=user_id,
            )
            _require(
                self.verify_session_database_binding(role),
                f"users.create_{role}",
                "database_binding",
            )
        # 两个角色只有同时通过公开创建、登录及同库哈希绑定，才可进入权限攻击验证。
        return {"developer_session_bound": True, "viewer_session_bound": True}

    def select_environment_and_model(self) -> dict[str, bool]:
        """从 active dev 复制安全模型引用，并经公开 API 创建本轮独立 test Environment。"""

        _, environments = self._control_call(
            action="source_environment_get",
            role="admin",
            method="GET",
            path="/environments?code=dev",
            expected_status=200,
        )
        items = environments.get("environments")
        _require(isinstance(items, list), "environment.source_get", "items_missing")
        active = [
            item
            for item in items
            if isinstance(item, dict) and item.get("code") == "dev" and item.get("status") == "active"
        ]
        _require(len(active) == 1, "environment.source_get", "active_dev_count")
        source_environment_id = _safe_identifier(
            active[0].get("environment_id"),
            "environment.source_get",
            "environment_id_invalid",
        )

        query = urlencode({"environment_id": source_environment_id, "status": "active"})
        _, aliases = self._control_call(
            action="source_alias_get",
            role="admin",
            method="GET",
            path=f"/model-aliases?{query}",
            expected_status=200,
        )
        alias_items = aliases.get("model_aliases")
        _require(isinstance(alias_items, list), "alias.source_get", "items_missing")
        matching = [
            item
            for item in alias_items
            if isinstance(item, dict)
            and item.get("default_internal_model_id") == DEFAULT_MODEL_ALIAS
            and item.get("status") == "active"
            and item.get("default_model_status") == "active"
            and item.get("default_provider_status") == "active"
        ]
        _require(len(matching) == 1, "alias.source_get", "default_chat_missing")
        default_model_id = _safe_identifier(
            matching[0].get("default_model_id"),
            "alias.source_get",
            "default_model_id_invalid",
        )

        # 开发库可能保留前序 PRD 的 dev 租户预算；独立 test Environment 使三条 POST
        # 都验证真实 create-only 行为，同时不删除或改写任何历史规则和 reservation。
        _, created_environment = self._control_call(
            action="environment_create",
            role="admin",
            method="POST",
            path="/environments",
            expected_status=201,
            body={
                "code": "test",
                "name": f"PRD052 runtime test {self.suffix}",
                "description": "PRD-052 isolated runtime acceptance",
            },
        )
        environment = created_environment.get("environment")
        _require(isinstance(environment, dict), "environment.create", "environment_missing")
        _require(
            environment.get("tenant_id") == self.resource_ids["tenant_id"],
            "environment.create",
            "tenant_id",
        )
        _require(environment.get("code") == "test", "environment.create", "code")
        _require(environment.get("status") == "active", "environment.create", "status")
        environment_id = _safe_identifier(
            environment.get("environment_id"),
            "environment.create",
            "environment_id_invalid",
        )
        self.resource_ids["environment_id"] = environment_id

        _, created_alias = self._control_call(
            action="alias_create",
            role="admin",
            method="POST",
            path="/model-aliases",
            expected_status=201,
            body={
                "alias": DEFAULT_MODEL_ALIAS,
                "environment_id": environment_id,
                "default_model_id": default_model_id,
                "description": "PRD-052 isolated runtime acceptance",
                "status": "active",
            },
        )
        alias = created_alias.get("model_alias")
        _require(isinstance(alias, dict), "alias.create", "alias_missing")
        _safe_identifier(alias.get("model_alias_id"), "alias.create", "model_alias_id_invalid")
        _require(alias.get("tenant_id") == self.resource_ids["tenant_id"], "alias.create", "tenant_id")
        _require(alias.get("environment_id") == environment_id, "alias.create", "environment_id")
        _require(alias.get("alias") == DEFAULT_MODEL_ALIAS, "alias.create", "alias")
        _require(alias.get("default_model_id") == default_model_id, "alias.create", "default_model_id")
        _require(alias.get("status") == "active", "alias.create", "status")
        _require(alias.get("default_model_status") == "active", "alias.create", "model_status")
        _require(
            alias.get("default_provider_status") == "active",
            "alias.create",
            "provider_status",
        )
        return {
            "environment_active": True,
            "environment_created_by_public_api": True,
            "default_model_route_active": True,
        }

    def create_scope_resources(self) -> dict[str, Any]:
        """创建本轮 Project/Application/Virtual Key，并核对完整可信归属链。"""

        tenant_id = self.resource_ids["tenant_id"]
        environment_id = self.resource_ids["environment_id"]
        _, project_payload = self._control_call(
            action="project_create",
            role="admin",
            method="POST",
            path="/projects",
            expected_status=201,
            body={
                "project_code": f"prd052-{self.suffix}",
                "name": f"PRD052 runtime project {self.suffix}",
                "description": "PRD-052 isolated runtime acceptance",
                "owner_user_id": self.resource_ids["admin_user_id"],
            },
        )
        project = project_payload.get("project")
        _require(isinstance(project, dict), "project.create", "project_missing")
        project_id = _safe_identifier(
            project.get("project_id"),
            "project.create",
            "project_id_invalid",
        )
        _require(project.get("tenant_id") == tenant_id, "project.create", "tenant_mismatch")
        self.resource_ids["project_id"] = project_id

        _, application_payload = self._control_call(
            action="application_create",
            role="admin",
            method="POST",
            path="/applications",
            expected_status=201,
            body={
                "project_id": project_id,
                "application_code": f"prd052-{self.suffix}",
                "name": f"PRD052 runtime application {self.suffix}",
                "description": "PRD-052 isolated runtime acceptance",
            },
        )
        application = application_payload.get("application")
        _require(isinstance(application, dict), "application.create", "application_missing")
        application_id = _safe_identifier(
            application.get("application_id"),
            "application.create",
            "application_id_invalid",
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
                "name": f"PRD052 runtime key {self.suffix}",
                "environment_id": environment_id,
                "project_id": project_id,
                "application_id": application_id,
                "description": "PRD-052 isolated runtime acceptance",
            },
        )
        key_info = key_payload.get("virtual_key")
        _require(isinstance(key_info, dict), "virtual_key.create", "key_missing")
        virtual_key_id = _safe_identifier(
            key_info.get("virtual_key_id"),
            "virtual_key.create",
            "virtual_key_id_invalid",
        )
        plain_value = key_payload.get("plain_value")
        _require(
            isinstance(plain_value, str) and plain_value.startswith("mhv_"),
            "virtual_key.create",
            "plain_value_missing",
        )
        _require(key_info.get("tenant_id") == tenant_id, "virtual_key.create", "tenant")
        _require(key_info.get("environment_id") == environment_id, "virtual_key.create", "environment")
        _require(key_info.get("project_id") == project_id, "virtual_key.create", "project")
        _require(key_info.get("application_id") == application_id, "virtual_key.create", "application")
        self.resource_ids["virtual_key_id"] = virtual_key_id
        self.virtual_key_value = plain_value
        self.known_sensitive.append(plain_value)
        return {"owner_chain_verified": True}

    def _scope_contract(self, scope_type: str) -> tuple[str, str | None, str | None]:
        """返回本轮权威 Scope ID 及其直接父级，禁止从响应反向推导归属。"""

        contracts: dict[str, tuple[str, str | None, str | None]] = {
            "tenant": (self.resource_ids["tenant_id"], None, None),
            "project": (
                self.resource_ids["project_id"],
                "tenant",
                self.resource_ids["tenant_id"],
            ),
            "application": (
                self.resource_ids["application_id"],
                "project",
                self.resource_ids["project_id"],
            ),
            "virtual_key": (
                self.resource_ids["virtual_key_id"],
                "application",
                self.resource_ids["application_id"],
            ),
        }
        _require(scope_type in contracts, "budget.scope_contract", "scope_type")
        return contracts[scope_type]

    def _verify_scope_projection(
        self,
        scope: object,
        *,
        scope_type: str,
        stage: str,
    ) -> bool:
        """把 Scope DTO 的租户、环境、资源和父级逐项绑定到本轮权威资源。"""

        _require(isinstance(scope, dict), stage, "budget_scope_missing")
        scope_id, parent_type, parent_id = self._scope_contract(scope_type)
        _require(scope.get("tenant_id") == self.resource_ids["tenant_id"], stage, "tenant_id")
        _require(
            scope.get("environment_id") == self.resource_ids["environment_id"],
            stage,
            "environment_id",
        )
        _require(scope.get("scope_type") == scope_type, stage, "scope_type")
        _require(scope.get("scope_id") == scope_id, stage, "scope_id")
        _require(scope.get("parent_scope_type") == parent_type, stage, "parent_scope_type")
        _require(scope.get("parent_scope_id") == parent_id, stage, "parent_scope_id")
        _safe_identifier(scope.get("budget_scope_id"), stage, "budget_scope_id")
        return True

    def _verify_budget_rule_projection(
        self,
        rule: object,
        *,
        scope_type: str,
        expected_budget_cny: str,
        stage: str,
    ) -> str:
        """验证 create/list 规则顶层字段及内嵌 Scope 均属于本轮租户资源。"""

        _require(isinstance(rule, dict), stage, "rule_missing")
        scope_id, _parent_type, _parent_id = self._scope_contract(scope_type)
        _require(rule.get("tenant_id") == self.resource_ids["tenant_id"], stage, "tenant_id")
        _require(
            rule.get("environment_id") == self.resource_ids["environment_id"],
            stage,
            "environment_id",
        )
        _require(rule.get("scope_type") == scope_type, stage, "scope_type")
        _require(rule.get("scope_id") == scope_id, stage, "scope_id")
        _require(rule.get("daily_budget_cny") == expected_budget_cny, stage, "budget_cny")
        self._verify_scope_projection(
            rule.get("budget_scope"),
            scope_type=scope_type,
            stage=stage,
        )
        return _safe_identifier(rule.get("budget_rule_id"), stage, "rule_id")

    def _budget_body(self, scope_type: str, *, amount: str) -> dict[str, Any]:
        """构造当前租户三层预算请求，scope_id 只取已核验资源 ID。"""

        scope_ids = {
            "tenant": self.resource_ids["tenant_id"],
            "project": self.resource_ids["project_id"],
            "virtual_key": self.resource_ids["virtual_key_id"],
        }
        _require(scope_type in scope_ids, "budget.body", "scope_type")
        return {
            "environment_id": self.resource_ids["environment_id"],
            "scope_type": scope_type,
            "scope_id": scope_ids[scope_type],
            "name": f"PRD052 runtime {scope_type} budget {self.suffix}",
            "daily_budget_cny": amount,
            "warning_threshold_percent": 80,
            "exhausted_action": "block",
            "status": "active",
        }

    def create_budget_rules(self) -> dict[str, Any]:
        """用 create-only POST 创建 tenant/project/key 规则，并验证重复 Project 返回 409。"""

        amounts = {
            "tenant": WIDE_BUDGET_CNY,
            "project": PROJECT_BUDGET_BEFORE_PATCH_CNY,
            "virtual_key": WIDE_BUDGET_CNY,
        }
        response_amounts = {
            "tenant": "100",
            "project": "2",
            "virtual_key": "100",
        }
        for scope_type in ("tenant", "project", "virtual_key"):
            _, payload = self._control_call(
                action=f"budget_post_{scope_type}",
                role="admin",
                method="POST",
                path="/budgets",
                expected_status=201,
                body=self._budget_body(scope_type, amount=amounts[scope_type]),
                require_no_store=True,
            )
            rule_id = self._verify_budget_rule_projection(
                payload.get("budget_rule"),
                scope_type=scope_type,
                expected_budget_cny=response_amounts[scope_type],
                stage=f"budget.post_{scope_type}",
            )
            self.budget_rule_ids[scope_type] = rule_id

        self._control_call(
            action="budget_post_conflict",
            role="admin",
            method="POST",
            path="/budgets",
            expected_status=409,
            body=self._budget_body("project", amount=PROJECT_BUDGET_BEFORE_PATCH_CNY),
            expected_code="budget.rule_conflict",
        )
        return {"create_only_conflict_verified": True}

    def verify_budget_summary_and_chain(self, *, exhausted: bool) -> dict[str, Any]:
        """严格绑定三级规则、四层父链、正式金额拆分和唯一 Project 耗尽结论。"""

        environment_id = self.resource_ids["environment_id"]
        _, listed = self._control_call(
            action="budget_list_after" if exhausted else "budget_list_before",
            role="admin",
            method="GET",
            path=f"/budgets?{urlencode({'environment_id': environment_id})}",
            expected_status=200,
            require_no_store=True,
        )
        rules = listed.get("budget_rules")
        summaries = listed.get("environment_summaries")
        _require(isinstance(rules, list), "budget.list", "rules_missing")
        _require(isinstance(summaries, list), "budget.list", "summaries_missing")

        expected_budgets = {
            "tenant": "100",
            "project": "0.5" if exhausted else "2",
            "virtual_key": "100",
        }
        owned_rules: dict[str, dict[str, Any]] = {}
        for scope_type in ("tenant", "project", "virtual_key"):
            matching_rules = [
                item
                for item in rules
                if isinstance(item, dict)
                and item.get("budget_rule_id") == self.budget_rule_ids[scope_type]
            ]
            _require(len(matching_rules) == 1, "budget.list", f"{scope_type}_rule_count")
            rule = matching_rules[0]
            returned_rule_id = self._verify_budget_rule_projection(
                rule,
                scope_type=scope_type,
                expected_budget_cny=expected_budgets[scope_type],
                stage=f"budget.list_{scope_type}",
            )
            _require(
                returned_rule_id == self.budget_rule_ids[scope_type],
                f"budget.list_{scope_type}",
                "rule_id",
            )
            owned_rules[scope_type] = rule

        matching_summaries = [
            item
            for item in summaries
            if isinstance(item, dict) and item.get("environment_id") == environment_id
        ]
        _require(len(matching_summaries) == 1, "budget.list", "environment_summary")
        scope_summaries = matching_summaries[0].get("scope_summaries")
        _require(isinstance(scope_summaries, list), "budget.list", "scope_summaries")
        summary_by_scope: dict[str, dict[str, Any]] = {}
        count_fields = (
            "rule_count",
            "ok_count",
            "warning_count",
            "exhausted_count",
            "block_count",
        )
        for item in scope_summaries:
            _require(isinstance(item, dict), "budget.list", "scope_summary_item")
            scope_type = item.get("scope_type")
            _require(
                scope_type in {"tenant", "project", "application", "virtual_key"},
                "budget.list",
                "summary_scope_type",
            )
            _require(scope_type not in summary_by_scope, "budget.list", "summary_scope_duplicate")
            for field in count_fields:
                value = item.get(field)
                _require(type(value) is int and value >= 0, "budget.list", f"summary_{field}")
            _require(
                item["rule_count"]
                == item["ok_count"] + item["warning_count"] + item["exhausted_count"],
                "budget.list",
                "summary_status_total",
            )
            _require(
                item["block_count"] <= item["rule_count"],
                "budget.list",
                "summary_block_total",
            )
            summary_by_scope[str(scope_type)] = item
        _require(
            set(summary_by_scope) == {"tenant", "project", "application", "virtual_key"},
            "budget.list",
            "summary_scope_set",
        )
        for scope_type in ("tenant", "project", "virtual_key"):
            _require(
                summary_by_scope[scope_type]["rule_count"] >= 1,
                "budget.list",
                f"{scope_type}_summary",
            )
        if exhausted:
            _require(
                summary_by_scope["project"]["exhausted_count"] >= 1
                and summary_by_scope["project"]["block_count"] >= 1,
                "budget.list",
                "project_summary_not_exhausted",
            )

        query = urlencode(
            {
                "virtual_key_id": self.resource_ids["virtual_key_id"],
                "environment_id": environment_id,
                "project_id": self.resource_ids["project_id"],
                "application_id": self.resource_ids["application_id"],
            }
        )
        _, status_payload = self._control_call(
            action="budget_status_after" if exhausted else "budget_status_before",
            role="admin",
            method="GET",
            path=f"/budget-status?{query}",
            expected_status=200,
            require_no_store=True,
        )
        statuses = status_payload.get("statuses")
        aggregate = status_payload.get("aggregate")
        _require(isinstance(statuses, list), "budget.status", "statuses_missing")
        _require(isinstance(aggregate, dict), "budget.status", "aggregate_missing")
        scope_chain = aggregate.get("scope_chain")
        _require(isinstance(scope_chain, list), "budget.status", "scope_chain_missing")
        expected_chain = ("tenant", "project", "application", "virtual_key")
        _require(len(scope_chain) == len(expected_chain), "budget.status", "scope_chain_length")
        chain_by_scope: dict[str, dict[str, Any]] = {}
        budget_scope_ids: set[str] = set()
        for priority, scope_type in enumerate(expected_chain):
            scope = scope_chain[priority]
            self._verify_scope_projection(
                scope,
                scope_type=scope_type,
                stage=f"budget.status_chain_{scope_type}",
            )
            _require(
                scope.get("priority") == priority,
                f"budget.status_chain_{scope_type}",
                "priority",
            )
            budget_scope_id = str(scope["budget_scope_id"])
            _require(
                budget_scope_id not in budget_scope_ids,
                f"budget.status_chain_{scope_type}",
                "budget_scope_duplicate",
            )
            budget_scope_ids.add(budget_scope_id)
            chain_by_scope[scope_type] = scope

        _require(len(statuses) == 3, "budget.status", "status_count")
        expected_split = {
            "ledger_spent_cny": "1" if exhausted else "0",
            "reserved_cny": "0",
            "effective_spend_cny": "1" if exhausted else "0",
        }
        for scope_type in ("tenant", "project", "virtual_key"):
            matching_statuses = [
                item
                for item in statuses
                if isinstance(item, dict) and item.get("scope_type") == scope_type
            ]
            _require(len(matching_statuses) == 1, "budget.status", f"{scope_type}_status_count")
            status = matching_statuses[0]
            expected_scope_id, _parent_type, _parent_id = self._scope_contract(scope_type)
            _require(
                status.get("budget_rule_id") == self.budget_rule_ids[scope_type],
                f"budget.status_{scope_type}",
                "budget_rule_id",
            )
            _require(status.get("environment_id") == environment_id, f"budget.status_{scope_type}", "environment_id")
            _require(status.get("environment") == "test", f"budget.status_{scope_type}", "environment")
            _require(status.get("scope_id") == expected_scope_id, f"budget.status_{scope_type}", "scope_id")
            self._verify_scope_projection(
                status.get("budget_scope"),
                scope_type=scope_type,
                stage=f"budget.status_{scope_type}",
            )
            _require(
                status.get("budget_scope") == chain_by_scope[scope_type],
                f"budget.status_{scope_type}",
                "budget_scope_chain_binding",
            )
            _require(
                status.get("budget_cny") == expected_budgets[scope_type],
                f"budget.status_{scope_type}",
                "budget_cny",
            )
            for field, expected_value in expected_split.items():
                _require(
                    status.get(field) == expected_value,
                    f"budget.status_{scope_type}",
                    field,
                )
            expected_status = "exhausted" if exhausted and scope_type == "project" else "ok"
            expected_action = "block" if exhausted and scope_type == "project" else "allow"
            _require(status.get("status") == expected_status, f"budget.status_{scope_type}", "status")
            _require(status.get("next_action") == expected_action, f"budget.status_{scope_type}", "next_action")
            _require(
                owned_rules[scope_type].get("budget_scope") == chain_by_scope[scope_type],
                f"budget.status_{scope_type}",
                "rule_scope_chain_binding",
            )

        _require(
            aggregate.get("aggregate_status") == ("exhausted" if exhausted else "ok"),
            "budget.status",
            "aggregate_status",
        )
        _require(
            aggregate.get("effective_action") == ("block" if exhausted else "allow"),
            "budget.status",
            "effective_action",
        )
        _require(
            aggregate.get("effective_remaining_cny") == ("0" if exhausted else "2"),
            "budget.status",
            "effective_remaining_cny",
        )
        _require(
            aggregate.get("limiting_rule_id") == self.budget_rule_ids["project"],
            "budget.status",
            "limiting_rule_id",
        )
        _require(
            aggregate.get("limiting_scope") == chain_by_scope["project"],
            "budget.status",
            "limiting_scope",
        )
        _require(
            aggregate.get("conflict_policy") == "all_applicable_caps_deny_wins_v1",
            "budget.status",
            "conflict_policy",
        )
        return {
            "three_level_summary_verified": True,
            "trusted_scope_chain_verified": True,
            "project_exhausted_verified": exhausted,
            "expected_split": expected_split,
            "expected_split_scope_types": ["tenant", "project", "virtual_key"],
        }

    def patch_project_budget(self) -> dict[str, Any]:
        """通过兼容 PATCH 把 Project 额度降到正式 Usage 以下，并确认规则 ID 不变。"""

        _, payload = self._control_call(
            action="budget_patch_project",
            role="admin",
            method="PATCH",
            path="/budgets",
            expected_status=200,
            body=self._budget_body("project", amount=PROJECT_BUDGET_AFTER_PATCH_CNY),
            require_no_store=True,
        )
        rule_id = self._verify_budget_rule_projection(
            payload.get("budget_rule"),
            scope_type="project",
            expected_budget_cny="0.5",
            stage="budget.patch_project",
        )
        _require(
            rule_id == self.budget_rule_ids["project"],
            "budget.patch_project",
            "rule_changed",
        )
        return {"patch_verified": True}

    def verify_write_permissions(self) -> dict[str, bool]:
        """对 developer/viewer 分别执行 POST 与 PATCH，证明后端四条写路径均返回 403。"""

        results: dict[str, bool] = {}
        for role in ("developer", "viewer"):
            self._control_call(
                action=f"{role}_budget_post",
                role=role,
                method="POST",
                path="/budgets",
                expected_status=403,
                body=self._budget_body("project", amount=PROJECT_BUDGET_AFTER_PATCH_CNY),
                expected_code="auth.permission_denied",
            )
            results[f"{role}_post_forbidden"] = True
            self._control_call(
                action=f"{role}_budget_patch",
                role=role,
                method="PATCH",
                path="/budgets",
                expected_status=403,
                body=self._budget_body("project", amount=PROJECT_BUDGET_AFTER_PATCH_CNY),
                expected_code="auth.permission_denied",
            )
            results[f"{role}_patch_forbidden"] = True
        return results

    def insert_formal_project_usage(self) -> dict[str, Any]:
        """在已绑定运行库中插入本轮可信 Usage，并验证 0041 自动生成 posted usage_charge。"""

        request_id, trace_id = _request_identity("formal_usage", self.suffix)
        usage_id = f"usg_prd052_accept_{self.suffix}"
        snapshot_id = f"prs_prd052_accept_{self.suffix}"
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        normalized_usage = json.dumps(
            {
                "input_tokens": 1,
                "output_tokens": 0,
                "total_tokens": 1,
                "cache_tokens": 0,
                "cost_amount": POSTED_USAGE_CNY,
                "cost_cny": POSTED_USAGE_CNY,
                "currency": "CNY",
                "posting_source": "direct",
                "price_snapshot_id": snapshot_id,
                "price_schema_version": FORMAL_USAGE_PRICE_SCHEMA_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
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
                )
                VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
                """,
                (
                    self.resource_ids["tenant_id"],
                    request_id,
                    trace_id,
                    self.resource_ids["virtual_key_id"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO request_logs(
                  request_id, tenant_id, trace_id, virtual_key_id, environment_id,
                  project_id, application_id, endpoint, request_model, status,
                  input_tokens, output_tokens, total_tokens, estimated_cost_amount,
                  currency, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, 'chat/completions', ?, 'success',
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
                    DEFAULT_MODEL_ALIAS,
                    POSTED_USAGE_CNY,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO price_snapshots(
                  price_snapshot_id, tenant_id, request_id, currency,
                  input_price_per_1k, output_price_per_1k, price_schema_version,
                  billing_dimensions, provider_raw_mapping, source,
                  created_at, updated_at
                )
                VALUES(?, ?, ?, 'CNY', 0, 0, ?, ?, '{}', ?, ?, ?)
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
                    now,
                    now,
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
                )
                VALUES(?, ?, ?, 1, 0, 1, 0, '{}', ?, ?, ?, 0, 'CNY', ?,
                       ?, ?, ?, ?, ?, 'chat/completions', ?, ?, ?)
                """,
                (
                    usage_id,
                    self.resource_ids["tenant_id"],
                    request_id,
                    normalized_usage,
                    POSTED_USAGE_CNY,
                    POSTED_USAGE_CNY,
                    snapshot_id,
                    FORMAL_USAGE_PRICE_SCHEMA_VERSION,
                    self.resource_ids["virtual_key_id"],
                    self.resource_ids["environment_id"],
                    self.resource_ids["project_id"],
                    self.resource_ids["application_id"],
                    DEFAULT_MODEL_ALIAS,
                    now,
                    now,
                ),
            )
            entry = connection.execute(
                """
                SELECT finops_ledger_entry_id, entry_type, direction, status,
                       normalized_cny_micros, virtual_key_id, environment_id,
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
                and entry["normalized_cny_micros"] == 1_000_000,
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
                entry["finops_ledger_entry_id"],
                "database.formal_usage",
                "entry_id",
            )
            connection.commit()
        # 任何 owner/trigger/写锁错误都先回滚，再返回固定失败码；禁止继续调用 Gateway。
        except AcceptanceFailure:
            connection.rollback()
            raise
        # SQLite 写入或触发器异常必须回滚，并收敛为固定错误，不能泄露 SQL 或数据库路径。
        except sqlite3.Error:
            connection.rollback()
            _fail("database.formal_usage", "transaction_failed")
        finally:
            connection.close()
        self.resource_ids["usage_ledger_id"] = usage_id
        self.resource_ids["finops_ledger_entry_id"] = entry_id
        return {"posted_usage_charge_verified": True, "full_scope_snapshot_verified": True}

    def verify_gateway_project_block(self) -> dict[str, Any]:
        """调用真实 Gateway，验证 Project 耗尽在 Provider 前返回统一 402。"""

        _require(bool(self.virtual_key_value), "gateway.block", "key_missing")
        headers, request_id, trace_id = self._headers(
            "gateway_project_block",
            role=None,
            include_access=False,
        )
        headers["Authorization"] = f"Bearer {self.virtual_key_value}"
        headers["Content-Type"] = "application/json"
        response = _safe_http_call(
            self.gateway,
            "POST",
            "/v1/chat/completions",
            stage="gateway.project_block",
            headers=headers,
            body={
                "model": DEFAULT_MODEL_ALIAS,
                "messages": [{"role": "user", "content": self.gateway_input}],
                "max_tokens": 1,
            },
        )
        payload = verify_response_contract(
            response,
            stage="gateway.project_block",
            expected_status=402,
            request_id=request_id,
            trace_id=trace_id,
            expected_code="gateway.budget_exhausted",
        )
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                """
                SELECT status, error_code, budget_rule_id, budget_scope_type,
                       budget_scope_id, budget_action
                FROM request_logs
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()
            _require(row is not None, "gateway.project_block", "request_log_missing")
            _require(
                row["status"] == "failed"
                and row["error_code"] == "gateway.budget_exhausted"
                and row["budget_rule_id"] == self.budget_rule_ids["project"]
                and row["budget_scope_type"] == "project"
                and row["budget_scope_id"] == self.resource_ids["project_id"]
                and row["budget_action"] == "block",
                "gateway.project_block",
                "request_log_contract",
            )
            attempt_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM routing_attempts
                WHERE tenant_id=? AND request_id=?
                """,
                (self.resource_ids["tenant_id"], request_id),
            ).fetchone()["count"]
            _require(attempt_count == 0, "gateway.project_block", "provider_attempted")
            scope_event = connection.execute(
                """
                SELECT budget_scope_event_id, event_id, scope_type, scope_id, request_id
                FROM budget_scope_events
                WHERE tenant_id=? AND budget_rule_id=?
                """,
                (self.resource_ids["tenant_id"], self.budget_rule_ids["project"]),
            ).fetchone()
            _require(scope_event is not None, "gateway.project_block", "scope_event_missing")
            _require(
                scope_event["scope_type"] == "project"
                and scope_event["scope_id"] == self.resource_ids["project_id"]
                and scope_event["request_id"] == request_id,
                "gateway.project_block",
                "scope_event_contract",
            )
            scope_event_id = _safe_identifier(
                scope_event["budget_scope_event_id"],
                "gateway.project_block",
                "scope_event_id",
            )
            event_id = _safe_identifier(
                scope_event["event_id"],
                "gateway.project_block",
                "event_id",
            )
        # Gateway 返回与同库事实必须同时成立，否则不能把单个 HTTP 状态标记为通过。
        except sqlite3.Error:
            _fail("gateway.project_block", "database_query_failed")
        finally:
            connection.close()
        evidence = _public_http_evidence(response, payload, include_code=True)
        evidence.update(
            {
                "budget_scope_event_id": scope_event_id,
                "event_id": event_id,
                "project_rule_governed": True,
                "provider_attempt_count_zero": True,
                "gateway_database_binding_verified": True,
            }
        )
        self.http_evidence["gateway_project_block"] = evidence
        return evidence

    def run(self) -> dict[str, Any]:
        """执行完整聚焦验收并返回尚未发布、尚未加入清理结论的安全证据。"""

        database_evidence = self.verify_runtime_database_ready()
        admin_login = self._login(
            role="admin",
            email=self.config.admin_email,
            password=self.config.admin_password,
        )
        _require(
            self.verify_session_database_binding("admin"),
            "database.session_binding",
            "admin_not_bound",
        )
        role_evidence = self.create_role_users()
        environment_evidence = self.select_environment_and_model()
        resource_checks = self.create_scope_resources()
        budget_create = self.create_budget_rules()
        before = self.verify_budget_summary_and_chain(exhausted=False)
        patch_evidence = self.patch_project_budget()
        permissions = self.verify_write_permissions()
        formal_usage = self.insert_formal_project_usage()
        after = self.verify_budget_summary_and_chain(exhausted=True)
        gateway = self.verify_gateway_project_block()

        evidence: dict[str, Any] = {
            "schema_version": 1,
            "prd_id": "PRD-052",
            "status": "passed",
            "generated_resources": {
                "tenant_id": self.resource_ids["tenant_id"],
                "environment_id": self.resource_ids["environment_id"],
                "project_id": self.resource_ids["project_id"],
                "application_id": self.resource_ids["application_id"],
                "virtual_key_id": self.resource_ids["virtual_key_id"],
                "budget_rule_ids": {
                    "tenant": self.budget_rule_ids["tenant"],
                    "project": self.budget_rule_ids["project"],
                    "virtual_key": self.budget_rule_ids["virtual_key"],
                },
                "usage_ledger_id": self.resource_ids["usage_ledger_id"],
                "finops_ledger_entry_id": self.resource_ids["finops_ledger_entry_id"],
            },
            "checks": {
                "runtime_binding": {
                    **database_evidence,
                    "control_session_bound": True,
                    "gateway_request_bound": gateway["gateway_database_binding_verified"],
                },
                "identities": {
                    "admin_user_id": self.resource_ids["admin_user_id"],
                    "developer_user_id": self.resource_ids["developer_user_id"],
                    "viewer_user_id": self.resource_ids["viewer_user_id"],
                    **role_evidence,
                },
                "resources": {**resource_checks, **environment_evidence},
                "budget_create": budget_create,
                "budget_status": {
                    "three_level_summary_verified": after["three_level_summary_verified"],
                    "trusted_scope_chain_verified": after["trusted_scope_chain_verified"],
                    "project_exhausted_verified": after["project_exhausted_verified"],
                    "expected_split": after["expected_split"],
                    "expected_split_scope_types": after["expected_split_scope_types"],
                    "initial_status_verified": before["project_exhausted_verified"] is False,
                    **patch_evidence,
                    **permissions,
                },
                "formal_usage": formal_usage,
                "gateway": gateway,
                "admin_login_request_id": admin_login["request_id"],
                "conflict_request_id": self.http_evidence["budget_post_conflict"]["request_id"],
            },
            "privacy": {
                "environment_only_credentials": True,
                "safe_projection_verified": True,
                "credential_material_absent": True,
                "model_content_absent": True,
            },
        }
        assert_safe_evidence(evidence, sensitive_values=self.sensitive_values())
        return evidence

    def _logout_one(self, role: str) -> bool:
        """注销一个已创建 Session，并核对 HTTP 与数据库 revoked 状态。"""

        credential = self.sessions[role]
        headers, request_id, trace_id = self._headers(f"logout_{role}", role=role)
        response = _safe_http_call(
            self.control,
            "POST",
            "/auth/logout",
            stage=f"auth.logout_{role}",
            headers=headers,
        )
        payload = verify_response_contract(
            response,
            stage=f"auth.logout_{role}",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )
        _require(payload.get("logged_out") is True, f"auth.logout_{role}", "not_revoked")
        connection = self._connect_runtime()
        try:
            row = connection.execute(
                "SELECT status FROM sessions WHERE session_id=? AND tenant_id=? AND user_id=?",
                (credential.session_id, credential.tenant_id, credential.user_id),
            ).fetchone()
        # 注销证据必须来自同一运行库，查询失败不能仅凭 HTTP 200 标记完成。
        except sqlite3.Error:
            _fail(f"auth.logout_{role}", "database_query_failed")
        finally:
            connection.close()
        _require(
            row is not None and row["status"] == "revoked",
            f"auth.logout_{role}",
            "database_status",
        )
        return True

    def logout_all(self) -> dict[str, bool]:
        """尽最大努力注销三类 Session；任一失败都阻止发布通过证据。"""

        results: dict[str, bool] = {}
        failed = False
        for role in ("viewer", "developer", "admin"):
            # 缺失凭据意味着该角色无法证明已注销；继续处理其他角色，但最终禁止发布通过证据。
            if role not in self.sessions or not self.sessions[role].access_value:
                results[f"{role}_session_revoked"] = False
                failed = True
                continue
            try:
                results[f"{role}_session_revoked"] = self._logout_one(role)
            # 单个注销失败后仍继续另外两类，避免异常路径遗留更多活动 Session。
            except Exception:
                results[f"{role}_session_revoked"] = False
                failed = True
        _require(not failed, "auth.logout_all", "session_cleanup_failed")
        # Browser 只消费聚合布尔值；角色级结果继续保留，便于停服审计定位具体清理对象。
        results["session_revoked"] = True
        return results

    def sensitive_values(self) -> tuple[str, ...]:
        """返回最终证据必须拒绝的全部已知口令、Token、Key 和模型输入。"""

        return tuple(value for value in self.known_sensitive if value)

    def clear_sensitive_memory(self) -> None:
        """清空实例持有的敏感引用，缩短异常退出后的误用窗口。"""

        for credential in self.sessions.values():
            credential.access_value = ""
            credential.password = ""
        self.virtual_key_value = ""
        self.gateway_input = ""
        self.known_sensitive.clear()


def assert_safe_evidence(value: Any, *, sensitive_values: tuple[str, ...]) -> None:
    """递归核对安全投影，拒绝敏感字段、已知明文、Bearer 和完整 Virtual Key。"""

    def walk(node: Any) -> None:
        """遍历 JSON 候选并限制为字符串键及标准 JSON 标量。"""

        if isinstance(node, dict):
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
    for sensitive in sensitive_values:
        if sensitive:
            _require(sensitive not in encoded, "evidence.privacy", "sensitive_value")


def publish_evidence(directory: Path, evidence: Mapping[str, Any]) -> Path:
    """以独占创建方式发布 JSON；同一批次重跑不得覆盖既有验收事实。"""

    _require(directory.is_dir(), "evidence.publish", "batch_directory_missing")
    target = directory / "runtime-evidence.json"
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
    # 已有证据必须保留，覆盖会混淆两次运行的资源和链路事实。
    except FileExistsError:
        _fail("evidence.publish", "output_exists")
    return target


def execute_runtime(config: RuntimeConfig) -> dict[str, Any]:
    """运行验收、无条件收口三类 Session，并仅在完整通过后发布安全证据。"""

    with (
        httpx.Client(
            base_url=config.control_origin,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as control_client,
        httpx.Client(
            base_url=config.gateway_origin,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as gateway_client,
    ):
        acceptance = RuntimeAcceptance(config, control_client, gateway_client)
        evidence: dict[str, Any] | None = None
        original_failure: BaseException | None = None
        cleanup_evidence: dict[str, bool] | None = None
        try:
            evidence = acceptance.run()
        except Exception as exc:
            original_failure = exc
        finally:
            sensitive_values = acceptance.sensitive_values()
            # 只要服务端可能创建过任一 Session，就必须在清空内存前尝试全部撤销。
            if acceptance.sessions:
                try:
                    cleanup_evidence = acceptance.logout_all()
                # 清理失败不能覆盖原始失败，但成功路径必须被清理失败阻断。
                except Exception as exc:
                    if original_failure is None:
                        original_failure = exc
            acceptance.clear_sensitive_memory()

        if original_failure is not None:
            if isinstance(original_failure, AcceptanceFailure):
                raise original_failure
            _fail("runtime.execute", "unexpected_failure")
        _require(evidence is not None, "runtime.execute", "evidence_missing")
        _require(cleanup_evidence is not None, "auth.logout_all", "evidence_missing")
        evidence["cleanup"] = cleanup_evidence
        assert_safe_evidence(evidence, sensitive_values=sensitive_values)
        publish_evidence(config.artifact_directory, evidence)
        return evidence


def main(argv: list[str] | None = None) -> int:
    """命令行入口：成功只打印安全 JSON，失败只打印固定 stage/code。"""

    try:
        evidence = execute_runtime(parse_config(argv))
    except AcceptanceFailure as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
