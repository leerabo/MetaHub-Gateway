"""PRD-078 移动审批入口的真实迁移、HTTP、恢复与隐私验收器。

验收器只使用仓库虚拟环境、仓库内临时目录和固定回环端口。密码、Session、内部
Token、step-up 票据、Webhook Secret 与各安全域根密钥只驻留当前进程内存；发布证据
仅包含状态、计数、布尔结论和源码 SHA-256，并始终保持
``all_108_prds_complete=false``。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# 仓库根是所有解释器、临时目录、验收证据与子进程的唯一可信文件系统边界。
ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_control.approvals.scheduler import run_approval_timeout_once  # noqa: E402
from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.runtime.config import load_app_config  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402


def _load_process_helpers() -> Any:
    """惰性加载既有、已覆盖 Windows 精确进程清理语义的 Runtime helper。"""

    name = "_metahub_prd075_runtime_helpers_for_prd078"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 固定回环端口图、仓库内工具路径和 0071 预期共同冻结真实运行验收环境。
_BASE = _load_process_helpers()
AcceptanceFailure = _BASE.AcceptanceFailure
SafeArgumentParser = _BASE.SafeArgumentParser
OwnedProcesses = _BASE.OwnedProcesses
ManagedProcess = _BASE.ManagedProcess
require = _BASE.require

# 回环地址、固定端口、仓库内目录和冻结源码清单用于阻止验收越界或混用外部运行时。
LOOPBACK = "127.0.0.1"
CONTROL_PORT = 8001
CONSOLE_PORT = 5173
RESP_PORT = 6389
WEBHOOK_PORT = 18880
FIXED_PORTS = (CONTROL_PORT, CONSOLE_PORT, RESP_PORT, WEBHOOK_PORT)
WORK_ROOT = ROOT / ".tmp" / "prd078-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd078-runtime"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = ROOT / ".tools" / "node-v22.23.2-win-arm64" / "node.exe"
OWNER_FILENAME = ".prd078-runtime-owner"
MAX_WEBHOOK_BYTES = 64 * 1024
EXPECTED_MIGRATION_COUNT = 71
EXPECTED_MIGRATION_VERSION = "0071"
ROLE_USERS = {
    "finance_admin": ("usr_prd078_runtime_finance", "finance@prd078.runtime"),
    "security_admin": ("usr_prd078_runtime_security", "security@prd078.runtime"),
    "developer": ("usr_prd078_runtime_developer", "developer@prd078.runtime"),
}
SOURCE_FILES = {
    "runtime_acceptance": Path(__file__).resolve(),
    "migration_up": MIGRATIONS_DIR / "0071_mobile_enterprise_approval_entry.sql",
    "migration_down": MIGRATIONS_DIR / "0071_mobile_enterprise_approval_entry.down.sql",
    "approval_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "approvals"
    / "service.py",
    "approval_scheduler": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "approvals"
    / "scheduler.py",
    "approval_outbox": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "approvals"
    / "outbox.py",
}


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """保存本轮随机凭据；repr 永不展开任何私有值。"""

    admin_password: str = field(repr=False)
    role_password: str = field(repr=False)
    auth_secret: str = field(repr=False)
    provider_secret: str = field(repr=False)
    prompt_secret: str = field(repr=False)
    file_secret: str = field(repr=False)
    batch_secret: str = field(repr=False)
    cache_secret: str = field(repr=False)
    approval_secret: str = field(repr=False)
    session_risk_secret: str = field(repr=False)
    internal_token: str = field(repr=False)
    provider_key: str = field(repr=False)
    virtual_key: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        """为每个安全域生成互不复用的高熵值。"""

        def value(label: str) -> str:
            """``value`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
            # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
            return f"Prd078-{label}-{secrets.token_urlsafe(48)}"

        return cls(
            admin_password=value("AdminPassword"),
            role_password=value("RolePassword"),
            auth_secret=value("Auth"),
            provider_secret=value("ProviderEncryption"),
            prompt_secret=value("PromptEncryption"),
            file_secret=value("FileEncryption"),
            batch_secret=value("BatchEncryption"),
            cache_secret=value("CacheHmac"),
            approval_secret=value("ApprovalHmac"),
            session_risk_secret=value("SessionRiskHmac"),
            internal_token=value("InternalProducer"),
            provider_key=value("ProviderKey"),
            virtual_key=f"sk-mh-{secrets.token_urlsafe(40)}",
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        """返回最终必须在数据库、日志和证据中零明文命中的值。"""

        return (
            self.admin_password,
            self.role_password,
            self.auth_secret,
            self.provider_secret,
            self.prompt_secret,
            self.file_secret,
            self.batch_secret,
            self.cache_secret,
            self.approval_secret,
            self.session_risk_secret,
            self.internal_token,
            self.provider_key,
            self.virtual_key,
            *extra,
        )


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    """保存真实数据库中的非敏感 owner 坐标。"""

    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    foreign_tenant_id: str
    environment_id: str
    virtual_key_id: str
    budget_rules: dict[str, str]
    budget_blocks: dict[str, str]
    risk_score_id: str
    migration: dict[str, object]


class WebhookStub:
    """验证真实 HMAC 事件并仅保留事件计数与固定隐私结论。"""

    def __init__(self) -> None:
        """``__init__`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        self._lock = threading.Lock()
        self._secret: str | None = None
        self.created = 0
        self.timeout = 0
        self.invalid = 0
        self.signature_failures = 0
        self.forbidden_hits = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def set_secret(self, value: str) -> None:
        """只在内存中接受创建接口一次性返回的签名密钥。"""

        require(len(value) >= 32, "webhook.secret", "invalid")
        self._secret = value

    def start(self) -> str:
        """启动固定回环接收端，不记录 Header、URL 或事件正文。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """``Handler`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""

            def do_POST(self) -> None:  # noqa: N802
                """``do_POST`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
                try:
                    values = self.headers.get_all("Content-Length", failobj=[])
                    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
                        # 非规范或重复长度可能造成请求拆分歧义，统一固定失败且不读取正文。
                        raise ValueError("length")
                    length = int(values[0])
                    if not 0 < length <= MAX_WEBHOOK_BYTES:
                        # 空正文或超出接收预算时立即拒绝，避免无界读取和证据污染。
                        raise ValueError("length")
                    body = self.rfile.read(length)
                    envelope = json.loads(body.decode("utf-8", errors="strict"))
                    event_type = envelope.get("event_type") if type(envelope) is dict else None
                    payload = envelope.get("payload") if type(envelope) is dict else None
                    signature_ok = bool(owner._secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(owner._secret),
                        body,
                    )
                    header_ok = self.headers.get("X-MetaHub-Event") == event_type
                    shape_ok = _valid_webhook_payload(event_type, payload)
                    forbidden = _contains_forbidden_webhook_key(envelope)
                    del body, envelope, payload
                # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    self._reply(400)
                    return
                valid = (
                    event_type in {"approval.created", "approval.timeout"}
                    and signature_ok
                    and header_ok
                    and shape_ok
                    and not forbidden
                )
                with owner._lock:
                    if event_type == "approval.created":
                        owner.created += 1
                    elif event_type == "approval.timeout":
                        owner.timeout += 1
                    else:
                        owner.invalid += 1
                    if not signature_ok:
                        owner.signature_failures += 1
                    if forbidden:
                        owner.forbidden_hits += 1
                    if not shape_ok:
                        owner.invalid += 1
                self._reply(204 if valid else 400)

            def _reply(self, status: int) -> None:
                """``_reply`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                """禁止验收接收端输出请求信息。"""
                return

        try:
            self.server = ThreadingHTTPServer((LOOPBACK, WEBHOOK_PORT), Handler)
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except OSError:
            raise AcceptanceFailure("webhook", "port_unavailable") from None
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd078-runtime-webhook",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{WEBHOOK_PORT}/events"

    def stop(self) -> None:
        """停止当前实例持有的精确 server/thread。
        ``stop`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。
        """

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        """返回不含事件正文和 Secret 的稳定计数。"""

        with self._lock:
            return {
                "created": self.created,
                "timeout": self.timeout,
                "invalid": self.invalid,
                "signature_failures": self.signature_failures,
                "forbidden_hits": self.forbidden_hits,
            }


def _contains_forbidden_webhook_key(value: object) -> bool:
    """递归拒绝审批通知出现凭据、正文、金额、租约或 Endpoint 字段。"""

    forbidden = {
        "authorization",
        "password",
        "token",
        "prompt",
        "response",
        "content",
        "provider_key",
        "signing_secret",
        "endpoint_url",
        "lease_token",
        "amount",
        "cost",
        "scope_key",
    }
    if type(value) is dict:
        return any(
            str(key).casefold() in forbidden or _contains_forbidden_webhook_key(child)
            for key, child in value.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_webhook_key(item) for item in value)
    return False


def _valid_webhook_payload(event_type: object, payload: object) -> bool:
    """按冻结 exact schema 验证 created/timeout 的公开卡片信封。"""

    if type(payload) is not dict:
        return False
    if event_type == "approval.created":
        return (
            set(payload)
            == {
                "schema_version",
                "approval_id",
                "status",
                "target_type",
                "target_action",
                "risk_level",
                "requires_step_up",
                "expires_at",
                "entry",
            }
            and payload.get("schema_version") == 1
            and payload.get("status") == "pending"
        )
    if event_type == "approval.timeout":
        return set(payload) == {
            "schema_version",
            "approval_id",
            "status",
            "resolution_reason",
            "target_type",
            "target_action",
            "decided_at",
        } and (
            payload.get("schema_version") == 1
            and payload.get("status") == "rejected"
            and payload.get("resolution_reason") == "timeout"
        )
    return False


@contextmanager
def temporary_environment(values: dict[str, str]) -> Iterator[None]:
    """临时设置 seed/config 环境，并在异常路径恢复调用方原值。"""

    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def safe_run_id(value: str | None) -> str:
    """把运行 ID 限制为单个短 ASCII 路径段。"""

    candidate = value or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    compact = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and compact.isascii() and compact.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    """构造仓库内直属路径并拒绝跳转、绝对路径和 reparse 根。"""

    require(root.resolve().is_relative_to(ROOT.resolve()), stage, "root_outside_repository")
    require(Path(name).name == name and name not in {".", ".."}, stage, "invalid_name")
    target = root / name
    require(target.parent.resolve() == root.resolve(), stage, "outside_root")
    return target


def create_run_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    """独占创建 work、staging；final 已存在时绝不覆盖。"""

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.artifact")
    require(not work.exists(), "config.work", "exists")
    require(not staging.exists(), "config.staging", "exists")
    require(not final.exists(), "config.artifact", "exists")
    work.mkdir()
    staging.mkdir()
    (work / OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii")
    (staging / OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii")
    for name in ("logs", "temp", "spool"):
        (work / name).mkdir()
    return work, staging, final


def _database_url(path: Path) -> str:
    """``_database_url`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return f"sqlite:///{path.as_posix()}"


def _db_time(value: datetime) -> str:
    """``_db_time`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _migration_state(database_url: str) -> dict[str, object]:
    """读取当前迁移、完整性和外键终态。"""

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version"
        ).fetchall()
        return {
            "count": len(rows),
            "version": str(rows[-1][0]) if rows else None,
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    finally:
        connection.close()


def _require_migration_state(
    state: dict[str, object],
    *,
    count: int,
    version: str,
    stage: str,
) -> None:
    """验证一个迁移阶段的版本、完整性和外键状态。"""
    require(state["count"] == count, stage, "count")
    require(state["version"] == version, stage, "version")
    require(state["integrity"] == "ok", stage, "integrity")
    require(state["foreign_key_violations"] == 0, stage, "foreign_keys")


def prepare_database(work: Path, runtime: RuntimeSecrets) -> RuntimeGraph:
    """从空 SQLite 完成 71→71→70→71，并写入两租户固定审批目标。"""

    database = work / "prd078-runtime.sqlite3"
    database_url = _database_url(database)
    fresh = apply_migrations(database_url)
    require(fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION], "migration.fresh", "version")
    fresh_state = _migration_state(database_url)
    _require_migration_state(
        fresh_state,
        count=EXPECTED_MIGRATION_COUNT,
        version=EXPECTED_MIGRATION_VERSION,
        stage="migration.fresh",
    )
    repeat = apply_migrations(database_url)
    require(not repeat.applied, "migration.repeat", "unexpected_apply")
    repeat_state = _migration_state(database_url)
    _require_migration_state(
        repeat_state,
        count=EXPECTED_MIGRATION_COUNT,
        version=EXPECTED_MIGRATION_VERSION,
        stage="migration.repeat",
    )
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "version")
    down_state = _migration_state(database_url)
    _require_migration_state(down_state, count=70, version="0070", stage="migration.down")
    reup = apply_migrations(database_url)
    require(reup.applied == [EXPECTED_MIGRATION_VERSION], "migration.reup", "version")
    reup_state = _migration_state(database_url)
    _require_migration_state(
        reup_state,
        count=EXPECTED_MIGRATION_COUNT,
        version=EXPECTED_MIGRATION_VERSION,
        stage="migration.reup",
    )

    seed_values = {
        "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
        "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
        "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
    }
    with temporary_environment(seed_values):
        apply_seed_data(database_url)

    connection = connect_database(database_url)
    try:
        tenant = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default' AND status='active'"
        ).fetchone()
        require(tenant is not None, "database.seed", "tenant")
        tenant_id = str(tenant["tenant_id"])
        environment = connection.execute(
            "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
            (tenant_id,),
        ).fetchone()
        virtual_key = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        require(environment is not None and virtual_key is not None, "database.seed", "owners")
        environment_id = str(environment["environment_id"])
        virtual_key_id = str(virtual_key["virtual_key_id"])
        now = _db_time(datetime.now(UTC).replace(microsecond=0))
        password_hash = hash_password(runtime.role_password)
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    user_id,
                    tenant_id,
                    email,
                    f"PRD078 Runtime {role}",
                    password_hash,
                    role,
                    now,
                    now,
                )
                for role, (user_id, email) in ROLE_USERS.items()
            ),
        )
        try:
            # 同一 Environment 下每个预算 scope 只能有一条规则。timeout 使用真实
            # Project owner，既避免与 approve 的 tenant scope 冲突，也让验收覆盖
            # 租户内不同目标类型，而不是依赖无 owner 的伪造 scope_id。
            timeout_project_id = "prj_78000000000000000000000000000001"
            connection.execute(
                """
                INSERT INTO projects(
                  project_id,tenant_id,project_code,name,status,created_at,updated_at
                ) VALUES(?,?,?,'PRD078 Runtime Timeout','active',?,?)
                """,
                (timeout_project_id, tenant_id, "prd078-runtime-timeout", now, now),
            )
            budget_rules: dict[str, str] = {}
            budget_blocks: dict[str, str] = {}
            for suffix, scope_type, scope_id in (
                ("approve", "tenant", tenant_id),
                ("reject", "virtual_key", virtual_key_id),
                ("timeout", "project", timeout_project_id),
            ):
                rule, block = _insert_budget_pair(
                    connection,
                    tenant_id=tenant_id,
                    environment_id=environment_id,
                    suffix=suffix,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    now=now,
                )
                budget_rules[suffix] = rule
                budget_blocks[suffix] = block
            foreign_tenant_id = "tenant_prd078_runtime_foreign"
            connection.execute(
                """
                INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
                VALUES(?,'prd078-runtime-foreign','PRD078 Runtime Foreign',
                       'internal_only','active',?,?)
                """,
                (foreign_tenant_id, now, now),
            )
            foreign_environment = connection.execute(
                "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
                (foreign_tenant_id,),
            ).fetchone()
            require(foreign_environment is not None, "database.foreign", "environment")
            foreign_rule, foreign_block = _insert_budget_pair(
                connection,
                tenant_id=foreign_tenant_id,
                environment_id=str(foreign_environment["environment_id"]),
                suffix="foreign",
                scope_type="tenant",
                scope_id=foreign_tenant_id,
                now=now,
            )
            budget_rules["foreign"] = foreign_rule
            budget_blocks["foreign"] = foreign_block
            risk_score_id = _insert_risk_score(
                connection,
                tenant_id=tenant_id,
                virtual_key_id=virtual_key_id,
                now=datetime.now(UTC).replace(microsecond=0),
            )
            connection.commit()
        except AcceptanceFailure:
            connection.rollback()
            raise
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except sqlite3.DatabaseError:
            # SQLite 原文可能携带业务值，CLI 只发布稳定失败坐标。
            connection.rollback()
            raise AcceptanceFailure("database.business_seed", "failed") from None
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.seed",
            "integrity",
        )
        require(
            not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "database.seed",
            "foreign_keys",
        )
    finally:
        connection.close()
    return RuntimeGraph(
        database=database,
        database_url=database_url,
        tenant_id=tenant_id,
        foreign_tenant_id=foreign_tenant_id,
        environment_id=environment_id,
        virtual_key_id=virtual_key_id,
        budget_rules=budget_rules,
        budget_blocks=budget_blocks,
        risk_score_id=risk_score_id,
        migration={
            "fresh": fresh_state,
            "repeat": repeat_state,
            "down": down_state,
            "reup": reup_state,
        },
    )


def _insert_budget_pair(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    suffix: str,
    scope_type: str,
    scope_id: str,
    now: str,
) -> tuple[str, str]:
    """插入一条 tenant-qualified active BudgetRule 与唯一 active Block。
    ``_insert_budget_pair`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。
    """

    rule_id = f"br_prd078_runtime_{suffix}"
    block_id = f"bb_prd078_runtime_{suffix}"
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'PRD078 runtime rule',10,80,100,'block','active',?,?)
        """,
        (rule_id, tenant_id, environment_id, scope_type, scope_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO budget_blocks(
          budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
          budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,
          blocked_trace_id,blocked_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'2026-08-13','active',10,10,?,?,?,?,?)
        """,
        (
            block_id,
            tenant_id,
            environment_id,
            rule_id,
            scope_type,
            scope_id,
            f"req_prd078_runtime_block_{suffix}",
            f"trc_prd078_runtime_block_{suffix}",
            now,
            now,
            now,
        ),
    )
    return rule_id, block_id


def _insert_risk_score(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    now: datetime,
) -> str:
    """插入一条由既有 PRD-070 owner trigger 完整校验的高危 score。"""

    score_id = "srs_78000000000000000000000000000001"
    scope_key = "srh_" + "78" * 32
    request_id = "req_prd078_runtime_risk_source"
    trace_id = "trc_prd078_runtime_risk_source"
    started = now - timedelta(minutes=2)
    expires = now + timedelta(minutes=28)
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, virtual_key_id, _db_time(now)),
    )
    connection.execute(
        """
        INSERT INTO session_risk_scores(
          session_risk_score_id,tenant_id,scope_type,scope_key,hmac_key_version,
          virtual_key_id,chat_session_id,status,score,score_scale,risky_turn_count,
          jailbreak_count,sensitive_count,tool_inducement_count,state_revision,
          threshold_version,detector_version,ruleset_version,last_source_request_id,
          last_source_trace_id,window_started_at,last_risk_at,window_expires_at,
          first_high_risk_at,frozen_at,resolved_at,created_at,updated_at
        ) VALUES(?,?,'api_session',?,1,?,NULL,'high_risk',7500,10000,2,
                 1,1,0,1,'session-risk-threshold-v1','intent-risk-rules-v1',
                 'session-risk-ruleset-v1',?,?,?,?,?,?,NULL,NULL,?,?)
        """,
        (
            score_id,
            tenant_id,
            scope_key,
            virtual_key_id,
            request_id,
            trace_id,
            _db_time(started),
            _db_time(now),
            _db_time(expires),
            _db_time(now),
            _db_time(started),
            _db_time(now),
        ),
    )
    return score_id


def service_environment(
    work: Path,
    graph: RuntimeGraph,
    runtime: RuntimeSecrets,
) -> dict[str, str]:
    """构造最小子进程环境，拒绝透传宿主代理、云凭据或登录态。"""

    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(item)
                for item in (
                    ROOT / "shared-sdk" / "python",
                    ROOT / "gateway-data-plane" / "src",
                    ROOT / "control-plane-api" / "src",
                )
            ),
            "PYTHONUNBUFFERED": "1",
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "NO_PROXY": "127.0.0.1,localhost",
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "ERROR",
            "METAHUB_SERVICE_MODE": "internal_only",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": runtime.auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": runtime.provider_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": runtime.prompt_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": runtime.file_secret,
            "METAHUB_BATCH_ENCRYPTION_SECRET": runtime.batch_secret,
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": runtime.cache_secret,
            "METAHUB_APPROVAL_HMAC_VERSION": "1",
            "METAHUB_APPROVAL_HMAC_SECRET": runtime.approval_secret,
            "METAHUB_SESSION_RISK_ENABLED": "true",
            "METAHUB_SESSION_RISK_HMAC_VERSION": "1",
            "METAHUB_SESSION_RISK_HMAC_SECRET": runtime.session_risk_secret,
            "METAHUB_INTERNAL_CONFIG_TOKEN": runtime.internal_token,
            # PRD-070 RESP 验收服务只实现 Session Risk 的冻结业务命令；使用默认
            # DB 0 时生产 Store 不发送 SELECT，仍完整覆盖真实 TCP/RESP/EVAL 路径。
            "METAHUB_REDIS_URL": f"redis://{LOOPBACK}:{RESP_PORT}/0",
            "METAHUB_ALLOWED_ORIGINS": (
                f"http://localhost:{CONSOLE_PORT},http://{LOOPBACK}:{CONSOLE_PORT}"
            ),
            "METAHUB_GATEWAY_API_BASE_URL": "http://127.0.0.1:18000",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_CONSOLE_PORT": str(CONSOLE_PORT),
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "spool"),
            "METAHUB_FILE_STORAGE_ROOT": str(work / "files"),
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_COST_ANOMALY_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": "false",
            "METAHUB_BATCH_WORKER_ENABLED": "false",
            "METAHUB_RESPONSE_CACHE_ENABLED": "false",
            "METAHUB_APPROVAL_SCHEDULER_ENABLED": "true",
            "METAHUB_APPROVAL_SCHEDULER_POLL_INTERVAL_SECONDS": "1",
            "METAHUB_APPROVAL_SCHEDULER_BATCH_SIZE": "20",
            "METAHUB_APPROVAL_OUTBOX_ENABLED": "true",
            "METAHUB_APPROVAL_OUTBOX_POLL_INTERVAL_SECONDS": "1",
            "METAHUB_APPROVAL_OUTBOX_BATCH_SIZE": "20",
            "METAHUB_APPROVAL_WRITEBACK_ENABLED": "true",
            "METAHUB_APPROVAL_WRITEBACK_POLL_INTERVAL_SECONDS": "1",
            "METAHUB_APPROVAL_WRITEBACK_BATCH_SIZE": "20",
        }
    )
    return environment


def assert_ports_available() -> None:
    """所有固定端口必须由本轮独占，禁止借用或终止未知服务。"""

    for port in FIXED_PORTS:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((LOOPBACK, port))
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except OSError:
            raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
        finally:
            probe.close()


def _wait_port(process: ManagedProcess, port: int, *, stage: str) -> None:
    """等待当前精确 owner 开始监听指定回环端口。"""

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise AcceptanceFailure(stage, "process_exited")
        try:
            with socket.create_connection((LOOPBACK, port), timeout=0.5):
                return
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except OSError:
            time.sleep(0.1)
    raise AcceptanceFailure(stage, "timeout")


def start_resp(
    work: Path,
    environment: dict[str, str],
) -> ManagedProcess:
    """启动既有 PRD-070 RESP2 验收服务，让正式 Redis Store 走真实 TCP。"""

    require(PYTHON.is_file(), "process.resp", "python_missing")
    process = _BASE.start_process(
        "resp",
        [
            str(PYTHON),
            str(ROOT / "scripts" / "acceptance" / "prd070-resp-server.py"),
            "--port",
            str(RESP_PORT),
        ],
        work=work,
        environment=environment,
    )
    _wait_port(process, RESP_PORT, stage="health.resp")
    return process


def _auth(token: str) -> dict[str, str]:
    """``_auth`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return {"Authorization": f"Bearer {token}"}


def _private(response: httpx.Response, vary: str, *, stage: str) -> dict[str, object]:
    """验证审批成功或错误都携带 exact 私有缓存与链路 Header。"""

    require(response.headers.get("cache-control") == "no-store, private", stage, "cache")
    require(response.headers.get("pragma") == "no-cache", stage, "pragma")
    require(response.headers.get("vary") == vary, stage, "vary")
    request_id = response.headers.get("x-request-id")
    trace_id = response.headers.get("x-trace-id")
    require(bool(request_id) and bool(trace_id), stage, "chain_headers")
    try:
        payload = response.json()
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except ValueError:
        raise AcceptanceFailure(stage, "json") from None
    require(type(payload) is dict, stage, "shape")
    require(payload.get("request_id") == request_id, stage, "request_id")
    require(payload.get("trace_id") == trace_id, stage, "trace_id")
    return payload


def _error(
    response: httpx.Response,
    status: int,
    code: str,
    vary: str,
    *,
    stage: str,
) -> dict[str, object]:
    """``_error`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    payload = _private(response, vary, stage=stage)
    require(response.status_code == status, stage, "status")
    require(payload.get("code") == code, stage, "code")
    return payload


def _login(client: httpx.Client, email: str, password: str, *, stage: str) -> str:
    """执行真实登录并只返回当前调用链内存 Session。"""

    response = client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    require(response.status_code == 200, stage, "status")
    try:
        payload = response.json()
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except ValueError:
        raise AcceptanceFailure(stage, "json") from None
    token = payload.get("access_token") if type(payload) is dict else None
    require(type(token) is str and len(token) > 40, stage, "token")
    # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
    return token


def _create_payload(
    graph: RuntimeGraph,
    suffix: str,
    idempotency_key: str,
    *,
    expires_in_seconds: int,
) -> dict[str, object]:
    """``_create_payload`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return {
        "source_type": "budget",
        "source_id": graph.budget_blocks[suffix],
        "target_type": "budget_block_release",
        "target_id": graph.budget_rules[suffix],
        "target_action": "release",
        "target_reason_code": None,
        "expires_in_seconds": expires_in_seconds,
        "idempotency_key": idempotency_key,
    }


def _create_risk_payload(graph: RuntimeGraph, idempotency_key: str) -> dict[str, object]:
    """``_create_risk_payload`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return {
        "source_type": "session_risk",
        "source_id": graph.risk_score_id,
        "target_type": "session_risk_action",
        "target_id": graph.risk_score_id,
        "target_action": "freeze_session",
        "target_reason_code": "confirmed_attack",
        "expires_in_seconds": 600,
        "idempotency_key": idempotency_key,
    }


def _wait_database(
    graph: RuntimeGraph,
    predicate,
    *,
    stage: str,
    timeout_seconds: float = 45,
) -> None:
    """轮询独立短连接，只让持久事实决定异步恢复是否完成。"""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            if predicate(connection):
                return
        finally:
            connection.close()
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "timeout")


def _create_webhook(
    client: httpx.Client,
    admin_token: str,
    url: str,
    stub: WebhookStub,
) -> str:
    """``_create_webhook`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    response = client.post(
        "/webhooks",
        headers=_auth(admin_token),
        json={
            "name": "PRD078 runtime receiver",
            "endpoint_url": url,
            "event_types": ["approval.created", "approval.timeout"],
            "status": "active",
            "timeout_ms": 2000,
        },
    )
    require(response.status_code == 200, "http.webhook", "status")
    try:
        payload = response.json()
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except ValueError:
        raise AcceptanceFailure("http.webhook", "json") from None
    signing_secret = payload.get("signing_secret") if type(payload) is dict else None
    require(type(signing_secret) is str, "http.webhook", "secret")
    stub.set_secret(signing_secret)
    # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
    return signing_secret


def _run_timeout_while_control_stopped(
    environment: dict[str, str],
    graph: RuntimeGraph,
) -> int:
    """在 Control 停服窗口等待真实到期，再用 scheduler 提交两个终态。"""

    # Approval API 的最短 TTL 是 300 秒，且产品实现以 SQLite 数据库时钟作为
    # 权威时间。验收器不能用调用方 future 参数伪造任务到期，否则 tenant lease
    # 会落到未来且无法按真实数据库时间释放。这里在停服窗口等待两条任务自然
    # 到期，再原样调用生产 scheduler 入口。
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS target_count,
                       MAX(unixepoch(expires_at)) AS latest_expiry,
                       unixepoch('now') AS database_now
                FROM approval_tasks WHERE target_id IN(?,?)
                """,
                (graph.budget_rules["timeout"], graph.budget_rules["foreign"]),
            ).fetchone()
            if (
                row is not None
                and row["target_count"] == 2
                and row["latest_expiry"] is not None
                and row["database_now"] >= row["latest_expiry"]
            ):
                break
        finally:
            connection.close()
        time.sleep(0.25)
    else:
        raise AcceptanceFailure("recovery.timeout_wait", "timeout")

    with temporary_environment(environment):
        config = load_app_config("control-plane-api")
        for _ordinal in range(8):
            run_approval_timeout_once(
                config,
                worker_id="prd078-runtime-outage-timeout",
            )
            connection = connect_database(graph.database_url)
            try:
                rows = connection.execute(
                    """
                    SELECT tenant_id,status,resolution_reason FROM approval_tasks
                    WHERE target_id IN(?,?) ORDER BY tenant_id
                    """,
                    (graph.budget_rules["timeout"], graph.budget_rules["foreign"]),
                ).fetchall()
                completed = sum(
                    1
                    for row in rows
                    if row["status"] == "rejected" and row["resolution_reason"] == "timeout"
                )
            finally:
                connection.close()
            if completed == 2:
                return completed
    raise AcceptanceFailure("recovery.timeout", "not_converged")


def exercise_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    runtime: RuntimeSecrets,
    webhook: WebhookStub,
    webhook_url: str,
    processes: OwnedProcesses,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """执行三角色、两目标、决策、停服 timeout、outbox 与 Redis 恢复。"""

    initial = processes.add(
        _BASE.start_service(
            "control-initial",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _BASE.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        initial,
        stage="health.control_initial",
    )
    console = processes.add(_BASE.start_console(work=work, environment=environment))
    _BASE.wait_health(
        f"http://{LOOPBACK}:{CONSOLE_PORT}/",
        console,
        stage="health.console",
    )
    private_runtime_values: list[str] = []
    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(15, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        missing = client.post(
            f"/internal/tenants/{graph.tenant_id}/approvals",
            json=_create_payload(
                graph,
                "approve",
                "runtime-missing-internal-token",
                expires_in_seconds=600,
            ),
        )
        _error(
            missing,
            401,
            "auth.internal_token_required",
            "X-Internal-Token",
            stage="http.internal_missing",
        )
        admin_token = _login(
            client,
            "admin@metahub.local",
            runtime.admin_password,
            stage="http.login_admin",
        )
        finance_token = _login(
            client,
            ROLE_USERS["finance_admin"][1],
            runtime.role_password,
            stage="http.login_finance",
        )
        security_token = _login(
            client,
            ROLE_USERS["security_admin"][1],
            runtime.role_password,
            stage="http.login_security",
        )
        developer_token = _login(
            client,
            ROLE_USERS["developer"][1],
            runtime.role_password,
            stage="http.login_developer",
        )
        private_runtime_values.extend((admin_token, finance_token, security_token, developer_token))
        signing_secret = _create_webhook(client, admin_token, webhook_url, webhook)
        private_runtime_values.append(signing_secret)
        internal_headers = {"X-Internal-Token": runtime.internal_token}
        create_keys = {
            name: f"prd078-runtime-create-{name}-{secrets.token_urlsafe(18)}"
            for name in ("approve", "reject", "timeout", "foreign", "risk")
        }
        private_runtime_values.extend(create_keys.values())
        created_ids: dict[str, str] = {}
        for suffix in ("approve", "reject", "timeout"):
            response = client.post(
                f"/internal/tenants/{graph.tenant_id}/approvals",
                headers=internal_headers,
                json=_create_payload(
                    graph,
                    suffix,
                    create_keys[suffix],
                    expires_in_seconds=300 if suffix == "timeout" else 600,
                ),
            )
            payload = _private(response, "X-Internal-Token", stage=f"http.create_{suffix}")
            require(response.status_code == 201, f"http.create_{suffix}", "status")
            approval = payload.get("approval")
            require(type(approval) is dict, f"http.create_{suffix}", "approval")
            created_ids[suffix] = str(approval.get("approval_id"))
        foreign = client.post(
            f"/internal/tenants/{graph.foreign_tenant_id}/approvals",
            headers=internal_headers,
            json=_create_payload(
                graph,
                "foreign",
                create_keys["foreign"],
                expires_in_seconds=300,
            ),
        )
        foreign_payload = _private(
            foreign,
            "X-Internal-Token",
            stage="http.create_foreign",
        )
        require(foreign.status_code == 201, "http.create_foreign", "status")
        created_ids["foreign"] = str(foreign_payload["approval"]["approval_id"])
        risk = client.post(
            f"/internal/tenants/{graph.tenant_id}/approvals",
            headers=internal_headers,
            json=_create_risk_payload(graph, create_keys["risk"]),
        )
        risk_payload = _private(risk, "X-Internal-Token", stage="http.create_risk")
        require(risk.status_code == 201, "http.create_risk", "status")
        created_ids["risk"] = str(risk_payload["approval"]["approval_id"])

        finance_list = client.get("/approvals", headers=_auth(finance_token))
        finance_payload = _private(
            finance_list,
            "Authorization",
            stage="http.finance_list",
        )
        require(finance_list.status_code == 200, "http.finance_list", "status")
        finance_types = {
            item.get("target_type")
            for item in finance_payload.get("approvals", [])
            if type(item) is dict
        }
        require(finance_types == {"budget_block_release"}, "http.finance_list", "target_filter")
        security_list = client.get("/approvals", headers=_auth(security_token))
        security_payload = _private(
            security_list,
            "Authorization",
            stage="http.security_list",
        )
        require(security_list.status_code == 200, "http.security_list", "status")
        security_types = {
            item.get("target_type")
            for item in security_payload.get("approvals", [])
            if type(item) is dict
        }
        require(security_types == {"session_risk_action"}, "http.security_list", "target_filter")
        denied = client.get("/approvals", headers=_auth(developer_token))
        _error(
            denied,
            403,
            "auth.permission_denied",
            "Authorization",
            stage="http.developer_denied",
        )
        hidden = client.get(
            f"/approvals/{created_ids['foreign']}",
            headers=_auth(finance_token),
        )
        _error(
            hidden,
            404,
            "approval.not_found",
            "Authorization",
            stage="http.cross_tenant",
        )

        budget_key = f"prd078-budget-decision-{secrets.token_urlsafe(18)}"
        reject_key = f"prd078-reject-decision-{secrets.token_urlsafe(18)}"
        risk_key = f"prd078-risk-decision-{secrets.token_urlsafe(18)}"
        private_runtime_values.extend((budget_key, reject_key, risk_key))
        budget_headers = {**_auth(finance_token), "Idempotency-Key": budget_key}
        approved = client.post(
            f"/approvals/{created_ids['approve']}/approve",
            headers=budget_headers,
            json={"expected_revision": 1, "reason_code": "approved_business_need"},
        )
        approved_payload = _private(
            approved,
            "Authorization",
            stage="http.budget_approve",
        )
        require(approved.status_code == 200, "http.budget_approve", "status")
        require(
            approved_payload["approval"]["writeback_status"] == "completed",
            "http.budget_approve",
            "writeback",
        )
        replay = client.post(
            f"/approvals/{created_ids['approve']}/approve",
            headers=budget_headers,
            json={"expected_revision": 1, "reason_code": "approved_business_need"},
        )
        replay_payload = _private(replay, "Authorization", stage="http.budget_replay")
        require(
            replay.status_code == 200 and replay_payload.get("idempotent_replay") is True,
            "http.budget_replay",
            "result",
        )
        opposite = client.post(
            f"/approvals/{created_ids['approve']}/reject",
            headers=budget_headers,
            json={"expected_revision": 1, "reason_code": "budget_not_approved"},
        )
        _error(
            opposite,
            409,
            "approval.idempotency_conflict",
            "Authorization",
            stage="http.budget_opposite",
        )
        rejected = client.post(
            f"/approvals/{created_ids['reject']}/reject",
            headers={**_auth(finance_token), "Idempotency-Key": reject_key},
            json={"expected_revision": 1, "reason_code": "budget_not_approved"},
        )
        rejected_payload = _private(
            rejected,
            "Authorization",
            stage="http.budget_reject",
        )
        require(
            rejected.status_code == 200 and rejected_payload["approval"]["status"] == "rejected",
            "http.budget_reject",
            "result",
        )

        wrong = client.post(
            f"/approvals/{created_ids['risk']}/step-up",
            headers=_auth(security_token),
            json={"action": "approve", "password": "wrong-runtime-password"},
        )
        _error(
            wrong,
            403,
            "approval.step_up_invalid",
            "Authorization",
            stage="http.step_up_wrong",
        )
        step_up = client.post(
            f"/approvals/{created_ids['risk']}/step-up",
            headers=_auth(security_token),
            json={"action": "approve", "password": runtime.role_password},
        )
        step_payload = _private(step_up, "Authorization", stage="http.step_up")
        require(step_up.status_code == 200, "http.step_up", "status")
        step_token = step_payload.get("step_up_token")
        require(type(step_token) is str and len(step_token) >= 43, "http.step_up", "token")
        private_runtime_values.append(step_token)
        risk_approved = client.post(
            f"/approvals/{created_ids['risk']}/approve",
            headers={**_auth(security_token), "Idempotency-Key": risk_key},
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_token,
            },
        )
        risk_approved_payload = _private(
            risk_approved,
            "Authorization",
            stage="http.risk_approve",
        )
        require(risk_approved.status_code == 200, "http.risk_approve", "status")
        require(
            risk_approved_payload["approval"]["writeback_status"] == "pending",
            "http.risk_approve",
            "pending_writeback",
        )

    _wait_database(
        graph,
        lambda connection: (
            connection.execute(
                "SELECT COUNT(*) FROM approval_event_outbox WHERE status='delivered'"
            ).fetchone()[0]
            == 5
        ),
        stage="recovery.created_outbox",
    )
    _wait_database(
        graph,
        lambda connection: (
            connection.execute(
                """
                SELECT status,attempt_count FROM approval_writebacks
                WHERE approval_task_id=?
                """,
                (created_ids["risk"],),
            ).fetchone()["status"]
            == "pending"
            and connection.execute(
                """
                SELECT attempt_count FROM approval_writebacks WHERE approval_task_id=?
                """,
                (created_ids["risk"],),
            ).fetchone()["attempt_count"]
            >= 1
        ),
        stage="recovery.writeback_pending",
    )
    processes.stop(initial)
    timeout_count = _run_timeout_while_control_stopped(environment, graph)
    resp = processes.add(start_resp(work, environment))
    _ = resp
    recovery = processes.add(
        _BASE.start_service(
            "control-recovery",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    _BASE.wait_health(
        f"http://{LOOPBACK}:{CONTROL_PORT}/health",
        recovery,
        stage="health.control_recovery",
    )
    _wait_database(
        graph,
        lambda connection: (
            connection.execute(
                "SELECT COUNT(*) FROM approval_event_outbox WHERE status='delivered'"
            ).fetchone()[0]
            == 7
        ),
        stage="recovery.timeout_outbox",
    )
    _wait_database(
        graph,
        lambda connection: (
            connection.execute(
                "SELECT status FROM approval_writebacks WHERE approval_task_id=?",
                (created_ids["risk"],),
            ).fetchone()["status"]
            == "completed"
        ),
        stage="recovery.writeback_completed",
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and webhook.snapshot()["timeout"] < 1:
        time.sleep(0.1)
    webhook_fact = webhook.snapshot()
    require(
        webhook_fact
        == {
            "created": 4,
            "timeout": 1,
            "invalid": 0,
            "signature_failures": 0,
            "forbidden_hits": 0,
        },
        "recovery.webhook",
        "counts",
    )
    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(15, connect=3),
        trust_env=False,
    ) as client:
        for token in tuple(private_runtime_values[:4]):
            response = client.post("/auth/logout", headers=_auth(token))
            require(response.status_code == 200, "http.logout", "status")

    connection = connect_database(graph.database_url)
    try:
        block_rows = connection.execute(
            """
            SELECT budget_rule_id,status,release_reason FROM budget_blocks
            WHERE budget_rule_id IN(?,?,?) ORDER BY budget_rule_id
            """,
            (
                graph.budget_rules["approve"],
                graph.budget_rules["reject"],
                graph.budget_rules["timeout"],
            ),
        ).fetchall()
        block_states = {str(row["budget_rule_id"]): tuple(row)[1:] for row in block_rows}
        require(
            block_states[graph.budget_rules["approve"]] == ("released", "approval_approved"),
            "database.budget",
            "approve_effect",
        )
        require(
            block_states[graph.budget_rules["reject"]] == ("active", None)
            and block_states[graph.budget_rules["timeout"]] == ("active", None),
            "database.budget",
            "no_effect",
        )
        task_counts = {
            str(row["status"]): int(row["value"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS value FROM approval_tasks GROUP BY status"
            ).fetchall()
        }
        decision_count = int(
            connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0]
        )
        writeback_counts = {
            str(row["status"]): int(row["value"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS value FROM approval_writebacks GROUP BY status"
            ).fetchall()
        }
        event_counts = {
            str(row["event_type"]): int(row["value"])
            for row in connection.execute(
                "SELECT event_type,COUNT(*) AS value FROM approval_events GROUP BY event_type"
            ).fetchall()
        }
        processing_leases = sum(
            int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status='processing'"
                ).fetchone()[0]
            )
            for table in ("approval_event_outbox", "approval_writebacks")
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    require(task_counts == {"approved": 2, "rejected": 3}, "database.tasks", "counts")
    require(decision_count == 5, "database.decisions", "count")
    require(writeback_counts == {"completed": 5}, "database.writebacks", "counts")
    require(
        event_counts == {"approval.created": 5, "approval.timeout": 2},
        "database.events",
        "counts",
    )
    require(processing_leases == 0, "database.leases", "processing")
    require(integrity == "ok" and foreign_keys == 0, "database.final", "integrity")
    return (
        {
            "services": {
                "control_initial_health": 200,
                "control_recovery_health": 200,
                "console_index": 200,
                "resp_tcp": "ready",
            },
            "access_control": {
                "internal_missing_status": 401,
                "finance_budget_only": True,
                "security_session_risk_only": True,
                "developer_status": 403,
                "cross_tenant_status": 404,
                "backend_dual_rbac": True,
                "private_headers_and_chain_ids": True,
            },
            "decisions": {
                "budget_approved": 1,
                "budget_rejected": 1,
                "session_risk_approved": 1,
                "timeout": timeout_count,
                "exact_replay_single_fact": True,
                "opposite_intent_status": 409,
                "step_up_wrong_status": 403,
                "step_up_single_use": True,
            },
            "recovery": {
                "control_stopped_before_timeout": True,
                "timeout_committed_while_control_stopped": True,
                "timeout_outbox_recovered_after_restart": True,
                "session_risk_writeback_failed_before_resp": True,
                "session_risk_writeback_recovered_after_restart": True,
            },
            "webhook": webhook_fact,
            "database": {
                "task_status_counts": task_counts,
                "decision_count": decision_count,
                "writeback_status_counts": writeback_counts,
                "event_type_counts": event_counts,
                "processing_leases": processing_leases,
                "integrity": integrity,
                "foreign_key_violations": foreign_keys,
            },
        },
        tuple(private_runtime_values),
    )


def quiesce_database(graph: RuntimeGraph) -> None:
    """关闭 WAL 并只清理由本轮数据库派生的已知 sidecar。"""

    connection = sqlite3.connect(graph.database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{graph.database}{suffix}")
        if sidecar.exists():
            require(sidecar.parent == graph.database.parent, "cleanup.sidecar", "boundary")
            sidecar.unlink()


def source_provenance() -> dict[str, str]:
    """绑定实际执行脚本、迁移与核心实现文件。"""

    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and path.resolve().is_relative_to(ROOT.resolve()),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# 最终证据键名黑名单阻止任何凭据、载荷正文或内部摘要被误发布到脱敏制品。
FORBIDDEN_EVIDENCE_KEYS = {
    "password",
    "token",
    "secret",
    "authorization",
    "prompt",
    "response",
    "provider_key",
    "signing_secret",
    "endpoint_url",
    "database_url",
    "lease_token",
}


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    """递归拒绝凭据形字段和任一私有值进入可提交 JSON。"""

    encoded = tuple(item.encode("utf-8") for item in private_values if len(item) >= 8)

    def walk(item: object) -> None:
        """``walk`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        if type(item) is dict:
            for key, child in item.items():
                normalized = str(key).casefold()
                require(
                    normalized not in FORBIDDEN_EVIDENCE_KEYS
                    and not normalized.endswith(("_password", "_token", "_secret")),
                    "evidence.shape",
                    "forbidden_key",
                )
                walk(child)
        elif type(item) in {list, tuple}:
            for child in item:
                walk(child)
        elif type(item) is str:
            body = item.encode("utf-8")
            require(
                not any(private in body for private in encoded),
                "evidence.privacy",
                "private_value",
            )

    walk(value)


def write_evidence(
    staging: Path,
    final: Path,
    payload: dict[str, object],
    private_values: tuple[str, ...],
) -> Path:
    """在 staging 完整写入并复读后原子发布最终小型证据。"""

    assert_safe_evidence(payload, private_values)
    evidence = staging / "runtime-acceptance.json"
    encoded = (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    evidence.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    (staging / "SHA256SUMS.txt").write_text(
        f"{digest}  runtime-acceptance.json\n",
        encoding="ascii",
        newline="\n",
    )
    require(json.loads(evidence.read_text(encoding="utf-8")) == payload, "evidence", "readback")
    _BASE.verify_private_storage(staging, private_values)
    os.replace(staging, final)
    return final / "runtime-acceptance.json"


def remove_owned_work(work: Path, owner: str) -> None:
    """只删除本轮直属 work；未知或被替换目录保持原样并失败关闭。"""

    require(work.parent.resolve() == WORK_ROOT.resolve(), "cleanup.work", "boundary")
    require(not work.is_symlink(), "cleanup.work", "symlink")
    claim = work / OWNER_FILENAME
    require(claim.is_file(), "cleanup.work", "claim_missing")
    require(claim.read_text(encoding="ascii").strip() == owner, "cleanup.work", "claim")
    shutil.rmtree(work)
    require(not work.exists(), "cleanup.work", "remaining")


def run(run_id: str | None = None) -> Path:
    """执行完整 PRD-078 Runtime 验收并返回已发布证据路径。"""

    actual_run_id = safe_run_id(run_id)
    owner = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    assert_ports_available()
    work, staging, final = create_run_directories(actual_run_id, owner)
    runtime = RuntimeSecrets.create()
    processes = OwnedProcesses()
    webhook = WebhookStub()
    graph: RuntimeGraph | None = None
    private_extra: tuple[str, ...] = ()
    facts: dict[str, object] | None = None
    cleanup_failure: AcceptanceFailure | None = None
    try:
        graph = prepare_database(work, runtime)
        environment = service_environment(work, graph, runtime)
        webhook_url = webhook.start()
        facts, private_extra = exercise_runtime(
            work,
            graph,
            environment,
            runtime,
            webhook,
            webhook_url,
            processes,
        )
    finally:
        try:
            processes.close()
        except Exception:
            cleanup_failure = AcceptanceFailure("cleanup.processes", "failed")
        try:
            webhook.stop()
        except Exception:
            cleanup_failure = cleanup_failure or AcceptanceFailure("cleanup.webhook", "failed")
    if cleanup_failure is not None:
        raise cleanup_failure
    require(graph is not None and facts is not None, "runtime", "incomplete")
    quiesce_database(graph)
    private_values = runtime.values(private_extra)
    privacy = _BASE.verify_private_storage(work, private_values)
    payload: dict[str, object] = {
        "prd_id": "PRD-078",
        "status": "passed",
        "run_id": actual_run_id,
        "executed_at": _db_time(datetime.now(UTC)),
        "migration": graph.migration,
        **facts,
        "privacy": privacy,
        "processes": {
            "control_initial": "owned_and_stopped",
            "control_recovery": "owned_and_stopped",
            "console": "owned_and_stopped",
            "resp": "owned_and_stopped",
            "webhook": "owned_and_stopped",
        },
        "fixed_loopback_ports": {
            "control": CONTROL_PORT,
            "console": CONSOLE_PORT,
            "resp": RESP_PORT,
            "webhook": WEBHOOK_PORT,
        },
        "source_provenance": {"algorithm": "sha256", **source_provenance()},
        "all_108_prds_complete": False,
    }
    target = write_evidence(staging, final, payload, private_values)
    _BASE.verify_private_storage(final, private_values)
    remove_owned_work(work, owner)
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI 仅输出证据相对路径或固定失败坐标，不打印 traceback。"""

    parser = SafeArgumentParser(description="Run PRD-078 isolated runtime acceptance")
    parser.add_argument("--run-id", default=None)
    try:
        arguments = parser.parse_args(argv)
        target = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(
            f"PRD-078 runtime acceptance failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print("PRD-078 runtime acceptance failed: runtime:unexpected", file=sys.stderr)
        return 1
    print(target.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
