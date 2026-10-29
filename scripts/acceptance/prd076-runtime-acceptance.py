"""PRD-076 Response Cache 的真实多进程 Runtime 验收器。

验收器使用两个独立空 SQLite：迁移库只验证 0069 fresh/repeat/down/re-up，业务库
启动真实 Gateway、Control、Console、回环 Provider 与 Webhook。最终只发布 exact
白名单 JSON 和单行 SHA-256；私有正文、凭据、内部摘要和数据库路径均不得进入证据。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
ROOT = Path(__file__).resolve().parents[2]
for _source_root in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(_source_root))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.db.security import (  # noqa: E402
    encrypt_provider_key,
    hash_password,
    hash_secret,
    secret_prefix,
)
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402


def _load_prd075_helpers():
    """加载已冻结的通用 owner/HTTP/隐私 helper，导入阶段不启动任何资源。"""

    name = "_metahub_prd075_runtime_helpers_for_prd076"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = ROOT / "scripts" / "acceptance" / "prd075-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        raise RuntimeError("runtime helper unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
_BASE = _load_prd075_helpers()
AcceptanceFailure = _BASE.AcceptanceFailure
SafeArgumentParser = _BASE.SafeArgumentParser
ManagedProcess = _BASE.ManagedProcess
OwnedProcesses = _BASE.OwnedProcesses
require = _BASE.require
utc_now = _BASE.utc_now
workspace_path = _BASE.workspace_path
sensitive_encodings = _BASE.sensitive_encodings
verify_private_storage = _BASE.verify_private_storage


# 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 8000
CONTROL_PORT = 8001
CONSOLE_PORT = 5173
PROVIDER_PORT = 18855
WEBHOOK_PORT = 18856
FIXED_PORTS = (
    GATEWAY_PORT,
    CONTROL_PORT,
    CONSOLE_PORT,
    PROVIDER_PORT,
    WEBHOOK_PORT,
)
WORK_ROOT = ROOT / ".tmp" / "prd076-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd076-runtime"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = ROOT / ".tools" / "node-v22.23.2-win-arm64" / "node.exe"
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
MIGRATION_UP_VERSIONS = tuple(f"{version:04d}" for version in range(1, 70))
SOURCE_PROVENANCE_NAMES = frozenset(
    {
        "runtime_acceptance",
        "runtime_helper",
        "migration_set",
        "migration_0069_up",
        "migration_0069_down",
    }
)
SAFE_FACT_KEYS = frozenset(
    {
        "services",
        "migration_roundtrip",
        "access_control",
        "cache_flow",
        "isolation",
        "billing",
        "invalidation",
        "recovery",
        "database",
        "privacy_canaries",
    }
)
EVIDENCE_TOP_LEVEL_KEYS = frozenset(
    {
        *SAFE_FACT_KEYS,
        "schema_version",
        "prd",
        "generated_at",
        "all_108_prds_complete",
        "source_provenance",
        "privacy",
        "cleanup",
    }
)
MAX_STUB_BODY_BYTES = 128 * 1024
WEBHOOK_TIMEOUT_SECONDS = 30

# PRD-075 的通用启动 helper 在自身模块全局读取这些值；只修改独立加载实例。
_BASE.LOOPBACK = LOOPBACK
_BASE.GATEWAY_PORT = GATEWAY_PORT
_BASE.CONTROL_PORT = CONTROL_PORT
_BASE.CONSOLE_PORT = CONSOLE_PORT
_BASE.FIXED_PORTS = FIXED_PORTS


def safe_run_id(value: str | None) -> str:
    """限制运行 ID 为有界 ASCII 标识，禁止路径注入与历史覆盖。"""

    candidate = value or time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime())
    compact = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and compact.isascii() and compact.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


def create_run_directories(run_id: str) -> tuple[Path, Path]:
    """独占创建仓库内运行树和制品目标，禁止符号链接与覆盖。"""

    try:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        require(not WORK_ROOT.is_symlink(), "config.work_root", "symlink")
        require(not ARTIFACT_ROOT.is_symlink(), "config.artifact_root", "symlink")
        work = workspace_path(WORK_ROOT / run_id, WORK_ROOT, stage="config.work")
        artifact = workspace_path(
            ARTIFACT_ROOT / run_id,
            ARTIFACT_ROOT,
            stage="config.artifact",
        )
        require(not work.exists() and not artifact.exists(), "config.run_directory", "exists")
        work.mkdir()
        for name in ("logs", "temp", "usage-spool"):
            (work / name).mkdir()
        return work, artifact
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("config.run_directory", "filesystem") from None


def migration_database_path(work: Path) -> Path:
    """返回独立迁移往返库路径，不允许越过本次运行目录。"""

    return workspace_path(
        Path(work) / "migration-roundtrip.sqlite3",
        work,
        stage="migration_database.path",
    )


def business_database_path(work: Path) -> Path:
    """返回产生 PRD-076 事实的业务库路径，与迁移往返库严格分离。"""

    return workspace_path(
        Path(work) / "business-runtime.sqlite3",
        work,
        stage="business_database.path",
    )


def database_url(path: Path) -> str:
    """把仓库内 SQLite 文件转换为共享连接层 URL。"""

    return f"sqlite:///{path.as_posix()}"


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """集中持有一次性凭据与互不复用的隐私 canary。"""

    admin_password: str = field(repr=False)
    finance_password: str = field(repr=False)
    viewer_password: str = field(repr=False)
    second_tenant_password: str = field(repr=False)
    provider_key: str = field(repr=False)
    virtual_key: str = field(repr=False)
    second_virtual_key: str = field(repr=False)
    second_tenant_virtual_key: str = field(repr=False)
    auth_secret: str = field(repr=False)
    provider_encryption_secret: str = field(repr=False)
    file_secret: str = field(repr=False)
    batch_secret: str = field(repr=False)
    prompt_template_secret: str = field(repr=False)
    cache_affinity_secret: str = field(repr=False)
    cache_hmac_secret: str = field(repr=False)
    cache_encryption_secret: str = field(repr=False)
    cache_control_secret: str = field(repr=False)
    failure_spool_secret: str = field(repr=False)
    prompt_canary: str = field(repr=False)
    response_canary: str = field(repr=False)
    data_class_canary: str = field(repr=False)
    second_data_class_canary: str = field(repr=False)
    tag_canary: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        """为每一类敏感材料生成独立随机值，禁止复用同一 marker。"""

        def random_value(prefix: str, size: int = 32) -> str:
            """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 random_value 对应步骤。"""
            # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
            return f"{prefix}{secrets.token_urlsafe(size)}"

        return cls(
            admin_password=random_value("Prd076-Admin-Aa1!", 28),
            finance_password=random_value("Prd076-Finance-Aa1!", 28),
            viewer_password=random_value("Prd076-Viewer-Aa1!", 28),
            second_tenant_password=random_value("Prd076-Tenant2-Aa1!", 28),
            provider_key=random_value("sk-prd076-provider-", 32),
            virtual_key=random_value("mhv_prd076_primary_", 32),
            second_virtual_key=random_value("mhv_prd076_secondary_", 32),
            second_tenant_virtual_key=random_value("mhv_prd076_tenant2_", 32),
            auth_secret=random_value("auth-prd076-", 48),
            provider_encryption_secret=random_value("provider-prd076-", 48),
            file_secret=random_value("file-prd076-", 48),
            batch_secret=random_value("batch-prd076-", 48),
            prompt_template_secret=random_value("template-prd076-", 48),
            cache_affinity_secret=random_value("affinity-prd076-", 48),
            cache_hmac_secret=random_value("response-cache-hmac-prd076-", 48),
            cache_encryption_secret=random_value("response-cache-aes-prd076-", 48),
            cache_control_secret=random_value("response-cache-control-prd076-", 48),
            failure_spool_secret=random_value("response-cache-spool-prd076-", 48),
            prompt_canary=random_value("PRD076_PRIVATE_PROMPT_", 24),
            response_canary=random_value("PRD076_PRIVATE_RESPONSE_", 24),
            data_class_canary=f"private-{secrets.token_hex(12)}",
            second_data_class_canary=f"restricted-{secrets.token_hex(12)}",
            tag_canary=f"tag-{secrets.token_hex(12)}",
        )

    def storage_private_values(self, *runtime_values: str) -> tuple[str, ...]:
        """返回数据库和日志均不得明文保存的凭据、正文与 Session。"""

        return (
            self.admin_password,
            self.finance_password,
            self.viewer_password,
            self.second_tenant_password,
            self.provider_key,
            self.virtual_key,
            self.second_virtual_key,
            self.second_tenant_virtual_key,
            self.auth_secret,
            self.provider_encryption_secret,
            self.file_secret,
            self.batch_secret,
            self.prompt_template_secret,
            self.cache_affinity_secret,
            self.cache_hmac_secret,
            self.cache_encryption_secret,
            self.cache_control_secret,
            self.failure_spool_secret,
            self.prompt_canary,
            self.response_canary,
            *runtime_values,
        )

    def non_database_private_values(
        self,
        database: Path,
        *runtime_values: str,
    ) -> tuple[str, ...]:
        """日志和非数据库 artifact 还不得保存真实分级、tag 与数据库路径。"""

        return (
            *self.storage_private_values(*runtime_values),
            self.data_class_canary,
            self.second_data_class_canary,
            self.tag_canary,
            str(database),
            database.as_posix(),
            database_url(database),
        )


@dataclass(frozen=True, slots=True)
class MigrationManifestEntry:
    """冻结单个迁移源文件与规范执行文本的 SHA-256。"""

    version: str
    name: str
    filename: str
    source_sha256: str
    execution_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenMigrationSnapshot:
    """绑定完整迁移目录、69 个 up 文本与唯一 0069 down 文本。"""

    directory: Path
    migration_set_sha256: str
    up: tuple[MigrationManifestEntry, ...]
    up_sql: tuple[str, ...] = field(repr=False)
    down_0069: MigrationManifestEntry
    down_0069_sql: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeRollbackResult:
    """只返回实际 down 版本与内存执行文本摘要。"""

    rolled_back: str
    execution_sha256: str


@dataclass(slots=True)
class FrozenUpMigrationFile:
    """向 shared loader 暴露一个只读内存 up 文件并记录实际消费。"""

    entry: MigrationManifestEntry
    sql: str = field(repr=False)
    consumed_execution_sha256: str | None = None
    read_count: int = 0

    @property
    def name(self) -> str:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 name 对应步骤。"""
        return self.entry.filename

    @property
    def stem(self) -> str:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 stem 对应步骤。"""
        return self.entry.filename[: -len(".sql")]

    def __lt__(self, other: FrozenUpMigrationFile) -> bool:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 __lt__ 对应步骤。"""
        return self.name < other.name

    def read_text(self, *, encoding: str) -> str:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 read_text 对应步骤。"""
        require(encoding == "utf-8", "database.migrate", "up_encoding")
        require(self.read_count == 0, "database.migrate", "up_read_count")
        self.read_count += 1
        self.consumed_execution_sha256 = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()
        return self.sql


@dataclass(slots=True)
class FrozenUpMigrationDirectory:
    """让 shared apply 只消费冻结内存清单，禁止重新读取快照文件。"""

    snapshot: FrozenMigrationSnapshot
    files: tuple[FrozenUpMigrationFile, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 __post_init__ 对应步骤。"""
        require(
            len(self.snapshot.up) == len(self.snapshot.up_sql),
            "database.migrate",
            "up_manifest_shape",
        )
        self.files = tuple(
            FrozenUpMigrationFile(entry, sql)
            for entry, sql in zip(self.snapshot.up, self.snapshot.up_sql, strict=True)
        )

    def exists(self) -> bool:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 exists 对应步骤。"""
        return True

    def glob(self, pattern: str) -> tuple[FrozenUpMigrationFile, ...]:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 glob 对应步骤。"""
        require(pattern == "*.sql", "database.migrate", "up_glob_pattern")
        return self.files

    def verify_consumed(self, *, stage: str) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 verify_consumed 对应步骤。"""
        require(
            tuple((item.read_count, item.consumed_execution_sha256) for item in self.files)
            == tuple((1, entry.execution_sha256) for entry in self.snapshot.up),
            stage,
            "up_consumption_contract",
        )


@dataclass(slots=True)
class FrozenDownMigrationDirectory:
    """让 shared rollback 只消费唯一冻结 0069 down 文本。

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 FrozenDownMigrationDirectory 对应步骤。"""

    snapshot: FrozenMigrationSnapshot
    requested_filename: str | None = None
    consumed_execution_sha256: str | None = None
    read_count: int = 0

    def __truediv__(self, filename: str) -> FrozenDownMigrationDirectory:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 __truediv__ 对应步骤。"""
        expected = f"{self.snapshot.down_0069.version}_{self.snapshot.down_0069.name}.down.sql"
        require(filename == expected, "database.migrate_down", "down_filename")
        self.requested_filename = filename
        return self

    def exists(self) -> bool:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 exists 对应步骤。"""
        return self.requested_filename is not None

    def read_text(self, *, encoding: str) -> str:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 read_text 对应步骤。"""
        require(encoding == "utf-8", "database.migrate_down", "down_encoding")
        require(self.read_count == 0, "database.migrate_down", "down_read_count")
        self.read_count += 1
        sql = self.snapshot.down_0069_sql
        self.consumed_execution_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return sql


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    """保存业务验收所需安全 owner ID，路径和数据库 URL 不进入 repr。"""

    database_url: str = field(repr=False)
    database: Path = field(repr=False)
    tenant_id: str
    admin_user_id: str
    finance_user_id: str
    viewer_user_id: str
    project_id: str
    environment_id: str
    model_id: str
    second_model_id: str
    provider_id: str
    model_alias_id: str
    second_model_alias_id: str
    model_alias: str
    second_model_alias: str
    virtual_key_id: str
    second_virtual_key_id: str
    second_tenant_id: str
    second_tenant_user_id: str
    second_tenant_provider_id: str
    second_tenant_model_id: str
    second_tenant_model_alias_id: str
    second_tenant_virtual_key_id: str
    second_tenant_slug: str
    second_tenant_email: str


class FixedThreadingHTTPServer(ThreadingHTTPServer):
    """禁用端口复用，并让请求线程随验收 Stub 一起退出。"""

    allow_reuse_address = False
    daemon_threads = True


def read_stub_json(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    """严格、有界读取一个 JSON object，禁止重复 Content-Length。"""

    lengths = handler.headers.get_all("Content-Length", failobj=[])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        raise ValueError("length")
    size = int(lengths[0])
    if not 0 < size <= MAX_STUB_BODY_BYTES:
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        raise ValueError("length")
    body = handler.rfile.read(size)
    if len(body) != size:
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        raise ValueError("length")
    value = json.loads(body.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        raise ValueError("shape")
    return value


def send_json(handler: BaseHTTPRequestHandler, status: int, value: object) -> None:
    """发送固定 JSON 响应，不记录正文或请求 Header。"""

    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    if status != 204:
        handler.wfile.write(encoded)


class ProviderStub:
    """本地 OpenAI-compatible Provider，只保存调用与认证聚合计数。"""

    def __init__(self, *, key: str, answer: str) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 __init__ 对应步骤。"""
        self.key = key
        self.answer = answer
        self._lock = threading.Lock()
        self.calls = 0
        self.invalid = 0
        self.auth_mismatches = 0
        self.delay_seconds = 0.0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        """独占启动回环 Provider，返回仅供数据库配置的 base URL。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 Handler 对应步骤。"""
            def do_POST(self) -> None:  # noqa: N802
                """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 do_POST 对应步骤。"""
                try:
                    payload = read_stub_json(self)
                    messages = payload.get("messages")
                    auth_ok = self.headers.get("Authorization") == f"Bearer {owner.key}"
                    valid = (
                        self.path == "/v1/chat/completions"
                        and payload.get("model") == "prd076-runtime-upstream"
                        and payload.get("stream", False) is False
                        and isinstance(messages, list)
                        and bool(messages)
                        and all(isinstance(item, dict) for item in messages)
                        and auth_ok
                    )
                    del payload, messages
                # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    send_json(self, 400, {"error": {"message": "invalid request"}})
                    return
                if not valid:
                    with owner._lock:
                        owner.invalid += 1
                        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                        if not auth_ok:
                            owner.auth_mismatches += 1
                    send_json(self, 400, {"error": {"message": "invalid request"}})
                    return
                with owner._lock:
                    owner.calls += 1
                    delay = owner.delay_seconds
                if delay > 0:
                    time.sleep(delay)
                send_json(
                    self,
                    200,
                    {
                        "id": "chatcmpl_prd076_runtime",
                        "object": "chat.completion",
                        "model": "prd076-runtime-upstream",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": owner.answer},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 7,
                            "total_tokens": 12,
                        },
                    },
                )

            def log_message(self, _format: str, *_args: object) -> None:
                """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 log_message 对应步骤。"""
                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK, PROVIDER_PORT), Handler)
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        except OSError:
            # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
            raise AcceptanceFailure("provider", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd076-provider",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{PROVIDER_PORT}/v1"

    def stop(self) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 stop 对应步骤。"""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def set_delay(self, seconds: float) -> None:
        """Set a bounded response delay used only to create an overlapping miss window.

        运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 set_delay 对应步骤。"""

        require(0 <= seconds <= 5, "provider.delay", "invalid")
        with self._lock:
            self.delay_seconds = seconds

    def snapshot(self) -> dict[str, int]:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 snapshot 对应步骤。"""
        with self._lock:
            return {
                "calls": self.calls,
                "invalid": self.invalid,
                "auth_mismatches": self.auth_mismatches,
            }


def contains_forbidden_webhook_field(value: object) -> bool:
    """拒绝 Webhook 暴露正文、凭据、selector 或内部缓存材料。"""

    forbidden = {
        "prompt",
        "response",
        "content",
        "authorization",
        "provider_key",
        "access_token",
        "token",
        "secret",
        "endpoint",
        "base_url",
        "selector",
        "tags",
        "tag",
        "data_class",
        "cache_key",
        "prompt_hash",
        "digest",
        "nonce",
        "ciphertext",
        "source_request_id",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or contains_forbidden_webhook_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(contains_forbidden_webhook_field(child) for child in value)
    return False


class WebhookStub:
    """验证 cache.hit/cache.invalidated 签名，只保留事件类型聚合计数。"""

    def __init__(self) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 __init__ 对应步骤。"""
        self._lock = threading.Lock()
        self._secret: str | None = None
        self.hits = 0
        self.invalidations = 0
        self.invalid = 0
        self.signature_failures = 0
        self.forbidden_hits = 0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def set_secret(self, value: str) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 set_secret 对应步骤。"""
        require(len(value) > 32, "webhook.secret", "invalid")
        self._secret = value

    def start(self) -> str:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 start 对应步骤。"""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 Handler 对应步骤。"""
            def do_POST(self) -> None:  # noqa: N802
                """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 do_POST 对应步骤。"""
                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    if len(lengths) != 1 or not lengths[0].isdecimal():
                        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                        raise ValueError("length")
                    size = int(lengths[0])
                    if not 0 < size <= MAX_STUB_BODY_BYTES:
                        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                        raise ValueError("length")
                    body = self.rfile.read(size)
                    if len(body) != size:
                        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                        raise ValueError("length")
                    payload = json.loads(body.decode("utf-8", errors="strict"))
                    event_type = payload.get("event_type") if isinstance(payload, dict) else None
                    forbidden = contains_forbidden_webhook_field(payload)
                    signature_ok = bool(owner._secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(owner._secret),
                        body,
                    )
                    header_ok = self.headers.get("X-MetaHub-Event") == event_type
                    del body, payload
                # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    send_json(self, 400, {"ok": False})
                    return
                valid_type = event_type in {"cache.hit", "cache.invalidated"}
                valid = valid_type and signature_ok and header_ok and not forbidden
                with owner._lock:
                    if event_type == "cache.hit":
                        owner.hits += 1
                    elif event_type == "cache.invalidated":
                        owner.invalidations += 1
                    else:
                        owner.invalid += 1
                    if not signature_ok:
                        owner.signature_failures += 1
                    if forbidden:
                        owner.forbidden_hits += 1
                send_json(self, 204 if valid else 400, {})

            def log_message(self, _format: str, *_args: object) -> None:
                """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 log_message 对应步骤。"""
                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK, WEBHOOK_PORT), Handler)
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        except OSError:
            raise AcceptanceFailure("webhook", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd076-webhook",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{WEBHOOK_PORT}/events"

    def stop(self) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 stop 对应步骤。"""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 snapshot 对应步骤。"""
        with self._lock:
            return {
                "hits": self.hits,
                "invalidations": self.invalidations,
                "invalid": self.invalid,
                "signature_failures": self.signature_failures,
                "forbidden_hits": self.forbidden_hits,
            }


def assert_ports_available() -> None:
    """在启动前独占探测固定端口，不枚举或终止任何未知进程。"""

    sockets: list[socket.socket] = []
    try:
        for port in FIXED_PORTS:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((LOOPBACK, port))
            # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
            except OSError:
                candidate.close()
                raise AcceptanceFailure("config.ports", "fixed_port_unavailable") from None
            sockets.append(candidate)
    finally:
        for candidate in sockets:
            candidate.close()


def _source_bytes(source: Path, boundary: Path) -> bytes:
    """在仓库边界读取普通文件，底层路径异常只映射为固定坐标。"""

    try:
        require(
            source.is_file() and not source.is_symlink(),
            "evidence.provenance",
            "source_missing",
        )
        verified = workspace_path(source, boundary, stage="evidence.provenance")
        return verified.read_bytes()
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def _migration_files(migrations_dir: Path) -> tuple[Path, ...]:
    """列出 shared loader 可见的完整 SQL 集，并拒绝 reparse 成员。"""

    try:
        require(
            migrations_dir.is_dir() and not migrations_dir.is_symlink(),
            "evidence.provenance",
            "migration_directory",
        )
        verified = workspace_path(migrations_dir, ROOT, stage="evidence.provenance")
        files = tuple(
            sorted(
                (path for path in verified.iterdir() if path.suffix.casefold() == ".sql"),
                key=lambda path: path.name,
            )
        )
        require(files, "evidence.provenance", "migration_set_empty")
        require(
            all(path.is_file() and not path.is_symlink() for path in files),
            "evidence.provenance",
            "migration_member",
        )
        return files
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def source_provenance_files(migrations_dir: Path) -> dict[str, Path]:
    """返回固定摘要名到源码的映射；路径本身不进入证据。"""

    return {
        "runtime_acceptance": Path(__file__),
        "runtime_helper": ROOT
        / "scripts"
        / "acceptance"
        / "prd075-runtime-acceptance.py",
        "migration_0069_up": migrations_dir
        / "0069_response_cache_optional_capability.sql",
        "migration_0069_down": migrations_dir
        / "0069_response_cache_optional_capability.down.sql",
    }


def _extend_migration_set_digest(digest: Any, filename: str, content: bytes) -> None:
    """以无歧义 filename/length/content 帧计算迁移全集摘要。"""

    try:
        name = filename.encode("utf-8")
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except UnicodeError:
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None
    digest.update(len(name).to_bytes(4, "big"))
    digest.update(name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def capture_source_provenance(migrations_dir: Path | None = None) -> dict[str, str]:
    """冻结当前验收器、通用 helper、完整迁移集与 0069 双向 SQL。"""

    actual_migrations_dir = migrations_dir or MIGRATIONS_DIR
    migration_set = hashlib.sha256()
    for source in _migration_files(actual_migrations_dir):
        content = _source_bytes(source, actual_migrations_dir)
        _extend_migration_set_digest(migration_set, source.name, content)
    provenance = {"migration_set": migration_set.hexdigest()}
    for name, source in source_provenance_files(actual_migrations_dir).items():
        boundary = (
            ROOT
            if name in {"runtime_acceptance", "runtime_helper"}
            else actual_migrations_dir
        )
        provenance[name] = hashlib.sha256(_source_bytes(source, boundary)).hexdigest()
    return provenance


def _decode_migration_sql(content: bytes) -> str:
    """复刻 Path.read_text 的 UTF-8 与 universal-newline 执行语义。

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 _decode_migration_sql 对应步骤。"""

    try:
        decoded = content.decode("utf-8")
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except UnicodeError:
        raise AcceptanceFailure("evidence.provenance", "migration_unicode") from None
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _manifest_entry(source: Path, content: bytes) -> tuple[MigrationManifestEntry, str]:
    """从同一次 bytes 读取生成源摘要和执行文本摘要。"""

    require(
        source.name.endswith(".sql") and not source.name.endswith(".down.sql"),
        "evidence.provenance",
        "up_filename",
    )
    stem = source.stem
    require("_" in stem, "evidence.provenance", "up_filename")
    version, name = stem.split("_", 1)
    sql = _decode_migration_sql(content)
    return (
        MigrationManifestEntry(
            version=version,
            name=name,
            filename=source.name,
            source_sha256=hashlib.sha256(content).hexdigest(),
            execution_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        ),
        sql,
    )


def validate_source_provenance(provenance: dict[str, str]) -> None:
    """要求 provenance 只有固定名称与小写 SHA-256。

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 validate_source_provenance 对应步骤。"""

    require(
        set(provenance) == SOURCE_PROVENANCE_NAMES,
        "evidence.provenance",
        "shape",
    )
    require(
        all(
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
            for value in provenance.values()
        ),
        "evidence.provenance",
        "digest",
    )


def create_migration_snapshot(
    work: Path,
    expected_provenance: dict[str, str],
) -> FrozenMigrationSnapshot:
    """复制完整 SQL 集并冻结 69 个 up 与 0069 down 的内存文本。"""

    validate_source_provenance(expected_provenance)
    snapshot = workspace_path(
        Path(work) / "migration-snapshot",
        work,
        stage="evidence.provenance",
    )
    require(not snapshot.exists(), "evidence.provenance", "snapshot_exists")
    try:
        snapshot.mkdir()
        for source in _migration_files(MIGRATIONS_DIR):
            target = workspace_path(
                snapshot / source.name,
                snapshot,
                stage="evidence.provenance",
            )
            target.write_bytes(_source_bytes(source, MIGRATIONS_DIR))
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "snapshot_write_failed") from None
    require(
        capture_source_provenance(snapshot) == expected_provenance,
        "evidence.provenance",
        "snapshot_mismatch",
    )
    migration_set = hashlib.sha256()
    up_entries: list[MigrationManifestEntry] = []
    up_sql: list[str] = []
    down_entry: MigrationManifestEntry | None = None
    down_sql: str | None = None
    for source in _migration_files(snapshot):
        content = _source_bytes(source, snapshot)
        _extend_migration_set_digest(migration_set, source.name, content)
        if source.name.endswith(".down.sql"):
            if source.name == "0069_response_cache_optional_capability.down.sql":
                down_sql = _decode_migration_sql(content)
                down_entry = MigrationManifestEntry(
                    version="0069",
                    name="response_cache_optional_capability",
                    filename=source.name,
                    source_sha256=hashlib.sha256(content).hexdigest(),
                    execution_sha256=hashlib.sha256(down_sql.encode("utf-8")).hexdigest(),
                )
            continue
        entry, sql = _manifest_entry(source, content)
        up_entries.append(entry)
        up_sql.append(sql)
    require(
        migration_set.hexdigest() == expected_provenance["migration_set"],
        "evidence.provenance",
        "manifest_set_mismatch",
    )
    require(
        tuple(entry.version for entry in up_entries) == MIGRATION_UP_VERSIONS,
        "evidence.provenance",
        "up_version_contract",
    )
    require(
        down_entry is not None and down_sql is not None,
        "evidence.provenance",
        "migration_0069_down_missing",
    )
    if down_entry is None or down_sql is None:
        raise AcceptanceFailure("evidence.provenance", "migration_0069_down_missing")
    require(
        up_entries[-1].source_sha256 == expected_provenance["migration_0069_up"]
        and down_entry.source_sha256 == expected_provenance["migration_0069_down"],
        "evidence.provenance",
        "migration_0069_digest",
    )
    require(
        capture_source_provenance(snapshot) == expected_provenance,
        "evidence.provenance",
        "snapshot_mismatch",
    )
    return FrozenMigrationSnapshot(
        directory=snapshot,
        migration_set_sha256=expected_provenance["migration_set"],
        up=tuple(up_entries),
        up_sql=tuple(up_sql),
        down_0069=down_entry,
        down_0069_sql=down_sql,
    )


def synthetic_snapshot(directory: Path) -> FrozenMigrationSnapshot:
    """为无 I/O 生命周期单测提供最小冻结对象。"""

    entry = MigrationManifestEntry(
        version="0069",
        name="response_cache_optional_capability",
        filename="0069_response_cache_optional_capability.sql",
        source_sha256="0" * 64,
        execution_sha256="0" * 64,
    )
    down = MigrationManifestEntry(
        version="0069",
        name="response_cache_optional_capability",
        filename="0069_response_cache_optional_capability.down.sql",
        source_sha256="0" * 64,
        execution_sha256="0" * 64,
    )
    return FrozenMigrationSnapshot(directory, "0" * 64, (entry,), ("",), down, "")


def migration_health(
    url: str,
    *,
    expected_count: int,
    expected_head: str,
    stage: str,
) -> dict[str, object]:
    """返回不含数据库位置的迁移、integrity 与 FK 有界摘要。"""

    try:
        connection = connect_database(url)
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "connect_failed") from None
    try:
        migration = connection.execute(
            "SELECT COUNT(*) AS count,MAX(version) AS latest "
            "FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        summary = {
            "migration_count": int(migration["count"]),
            "latest_migration": str(migration["latest"]),
            "integrity_check": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "health_query_failed") from None
    finally:
        try:
            connection.close()
        # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
        except sqlite3.Error:
            raise AcceptanceFailure(stage, "close_failed") from None
    require(
        summary
        == {
            "migration_count": expected_count,
            "latest_migration": expected_head,
            "integrity_check": "ok",
            "foreign_key_violations": 0,
        },
        stage,
        "contract",
    )
    return summary


def verify_snapshot_directory(snapshot: FrozenMigrationSnapshot, *, stage: str) -> None:
    """在每次执行前后证明快照目录未发生持久漂移。"""

    try:
        current = capture_source_provenance(snapshot.directory)
    except AcceptanceFailure:
        raise AcceptanceFailure(stage, "snapshot_unavailable") from None
    require(
        current["migration_set"] == snapshot.migration_set_sha256
        and current["migration_0069_up"] == snapshot.up[-1].source_sha256
        and current["migration_0069_down"] == snapshot.down_0069.source_sha256,
        stage,
        "snapshot_changed",
    )


def verify_migration_execution_checksums(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
    down_0069: bool = False,
) -> None:
    """以 schema_migrations 回证 shared 实际消费的全部冻结文本。"""

    try:
        connection = connect_database(database_url_)
        rows = connection.execute(
            "SELECT version,name,checksum,status FROM schema_migrations ORDER BY version"
        ).fetchall()
        actual = tuple(
            (str(row["version"]), str(row["name"]), str(row["checksum"]), str(row["status"]))
            for row in rows
        )
        expected = tuple(
            (
                entry.version,
                entry.name,
                entry.execution_sha256,
                "rolled_back" if down_0069 and entry.version == "0069" else "applied",
            )
            for entry in snapshot.up
        )
        require(actual == expected, stage, "execution_checksum_contract")
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "checksum_query_failed") from None
    finally:
        if "connection" in locals():
            try:
                connection.close()
            # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
            except sqlite3.Error:
                raise AcceptanceFailure(stage, "close_failed") from None


def apply_runtime_migrations(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
):
    """让 shared apply 只消费冻结内存 up，并双重回证 checksum。"""

    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_before")
    frozen = FrozenUpMigrationDirectory(snapshot)
    try:
        result = apply_migrations(database_url_, migrations_dir=frozen)  # type: ignore[arg-type]
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError):
        raise AcceptanceFailure(stage, "failed") from None
    frozen.verify_consumed(stage=f"{stage}.consumed")
    verify_migration_execution_checksums(database_url_, snapshot, stage=f"{stage}.consumed")
    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_after")
    return result


def rollback_runtime_migration(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
) -> RuntimeRollbackResult:
    """让 shared rollback 只消费冻结内存 0069 down 文本。

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 rollback_runtime_migration 对应步骤。"""

    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_before")
    verify_migration_execution_checksums(database_url_, snapshot, stage=f"{stage}.head")
    frozen = FrozenDownMigrationDirectory(snapshot)
    try:
        result = rollback_last_migration(database_url_, migrations_dir=frozen)  # type: ignore[arg-type]
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        raise AcceptanceFailure(stage, "failed") from None
    consumed = frozen.consumed_execution_sha256
    require(
        result.rolled_back == "0069"
        and frozen.read_count == 1
        and consumed == snapshot.down_0069.execution_sha256,
        stage,
        "down_consumption_contract",
    )
    verify_migration_execution_checksums(
        database_url_,
        snapshot,
        stage=f"{stage}.consumed",
        down_0069=True,
    )
    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_after")
    return RuntimeRollbackResult("0069", str(consumed))


def prepare_migration_database(
    work: Path,
    snapshot: FrozenMigrationSnapshot,
) -> dict[str, object]:
    """在独立空库真实执行 fresh/repeat/0069 down/re-up。

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 prepare_migration_database 对应步骤。"""

    database = migration_database_path(work)
    require(not database.exists(), "migration_database", "not_empty")
    url = database_url(database)
    first = apply_runtime_migrations(url, snapshot, stage="migration_database.fresh")
    require(tuple(first.applied) == MIGRATION_UP_VERSIONS, "migration_database.fresh", "contract")
    fresh = migration_health(
        url,
        expected_count=69,
        expected_head="0069",
        stage="migration_database.fresh_health",
    )
    repeat = apply_runtime_migrations(url, snapshot, stage="migration_database.repeat")
    require(
        repeat.applied == [] and tuple(repeat.skipped) == MIGRATION_UP_VERSIONS,
        "migration_database.repeat",
        "not_idempotent",
    )
    repeated = migration_health(
        url,
        expected_count=69,
        expected_head="0069",
        stage="migration_database.repeat_health",
    )
    rolled = rollback_runtime_migration(url, snapshot, stage="migration_database.down")
    require(rolled.rolled_back == "0069", "migration_database.down", "contract")
    down = migration_health(
        url,
        expected_count=68,
        expected_head="0068",
        stage="migration_database.down_health",
    )
    reapplied = apply_runtime_migrations(url, snapshot, stage="migration_database.reup")
    require(reapplied.applied == ["0069"], "migration_database.reup", "contract")
    reup = migration_health(
        url,
        expected_count=69,
        expected_head="0069",
        stage="migration_database.reup_health",
    )
    return {
        "fresh_up": fresh,
        "repeat_noop": {**repeated, "applied_count": 0, "skipped_count": 69},
        "down": {**down, "rolled_back": "0069"},
        "re_up": {**reup, "applied": ["0069"]},
    }


@contextmanager
def temporary_environment(values: dict[str, str]) -> Iterator[None]:
    """临时注入 seed/runner 环境，并在成功或失败后精确恢复调用方状态。"""

    previous = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def prepare_business_database(
    work: Path,
    secrets_: RuntimeSecrets,
    provider_url: str,
    snapshot: FrozenMigrationSnapshot,
) -> RuntimeGraph:
    """从另一个空库迁移到 0069，并创建真实双 Key、双 model 与三角色 owner 图。"""

    database = business_database_path(work)
    require(not database.exists(), "business_database", "not_empty")
    url = database_url(database)
    first = apply_runtime_migrations(url, snapshot, stage="business_database.fresh")
    require(tuple(first.applied) == MIGRATION_UP_VERSIONS, "business_database.fresh", "contract")
    migration_health(
        url,
        expected_count=69,
        expected_head="0069",
        stage="business_database.health",
    )
    seed_values = {
        "METAHUB_SEED_ADMIN_PASSWORD": secrets_.admin_password,
        "METAHUB_SEED_PROVIDER_API_KEY": secrets_.provider_key,
        "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_url,
    }
    second_seed_path = workspace_path(
        work / "second-tenant-seed.json",
        work,
        stage="business_database.seed_path",
    )
    second_seed_payload = {
        "tenant": {
            "slug": "prd076-runtime-second",
            "name": "PRD076 Runtime Second Tenant",
            "service_mode": "internal_only",
        },
        "admin": {
            "email": "admin-prd076-second@local.invalid",
            "display_name": "PRD076 second tenant admin",
            "password": "environment-override-required",
        },
        "provider": {
            "provider_code": "local_openai",
            "name": "PRD076 second tenant provider",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key_placeholder": "environment-override-required",
        },
        "model": {
            "model_alias": "default-chat",
            "upstream_model_name": "prd076-runtime-upstream",
            "provider_model_id": "prd076-runtime-upstream",
            "display_name": "PRD076 second tenant model",
            "context_window": 8192,
            "input_price_per_1k": 0.10,
            "output_price_per_1k": 0.30,
            "currency": "CNY",
            "price_schema_version": "prd076-runtime-v1",
        },
        "virtual_key": {
            "name": "PRD076 second tenant key",
            "plain_value": "environment-override-required",
        },
    }
    try:
        second_seed_path.write_text(
            json.dumps(second_seed_payload, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        with temporary_environment(seed_values):
            seed = apply_seed_data(url)
        second_seed_values = {
            **seed_values,
            "METAHUB_SEED_ADMIN_PASSWORD": secrets_.second_tenant_password,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.second_tenant_virtual_key,
        }
        with temporary_environment(second_seed_values):
            second_seed = apply_seed_data(url, seed_file=second_seed_path)
        second_seed_path.unlink()
        connection = connect_database(url)
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure("business_database.seed", "failed") from None
    stage = "owner_load"
    try:
        owner = connection.execute(
            """
            SELECT user.user_id,environment.environment_id,environment.code,
                   provider.provider_id,model.model_id,virtual_key.virtual_key_id
            FROM users user
            JOIN environments environment ON environment.tenant_id=user.tenant_id
            JOIN providers provider ON provider.tenant_id=user.tenant_id
            JOIN models model ON model.tenant_id=user.tenant_id
              AND model.provider_id=provider.provider_id
            JOIN virtual_keys virtual_key ON virtual_key.tenant_id=user.tenant_id
            WHERE user.tenant_id=? AND user.role='admin' AND user.status='active'
              AND environment.code='dev' AND provider.provider_code='local_openai'
              AND model.model_alias='default-chat'
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        require(owner is not None, "business_database.seed", "owner_missing")
        second_owner = connection.execute(
            """
            SELECT user.user_id,environment.environment_id,environment.code,
                   provider.provider_id,model.model_id,virtual_key.virtual_key_id
            FROM users user
            JOIN environments environment ON environment.tenant_id=user.tenant_id
            JOIN providers provider ON provider.tenant_id=user.tenant_id
            JOIN models model ON model.tenant_id=user.tenant_id
              AND model.provider_id=provider.provider_id
            JOIN virtual_keys virtual_key ON virtual_key.tenant_id=user.tenant_id
            WHERE user.tenant_id=? AND user.role='admin' AND user.status='active'
              AND environment.code='dev' AND provider.provider_code='local_openai'
              AND model.model_alias='default-chat'
            LIMIT 1
            """,
            (second_seed.tenant_id,),
        ).fetchone()
        require(second_owner is not None, "business_database.seed", "second_owner_missing")
        now = utc_now()
        graph = RuntimeGraph(
            database_url=url,
            database=database,
            tenant_id=seed.tenant_id,
            admin_user_id=str(owner["user_id"]),
            finance_user_id="user_prd076_runtime_finance",
            viewer_user_id="user_prd076_runtime_viewer",
            project_id="project_prd076_runtime",
            environment_id=str(owner["environment_id"]),
            model_id=str(owner["model_id"]),
            second_model_id="model_prd076_runtime_second",
            provider_id=str(owner["provider_id"]),
            model_alias_id="alias_prd076_runtime_primary",
            second_model_alias_id="alias_prd076_runtime_second",
            model_alias="prd076-runtime-cache",
            second_model_alias="prd076-runtime-model-isolation",
            virtual_key_id=str(owner["virtual_key_id"]),
            second_virtual_key_id="vkey_prd076_runtime_second",
            second_tenant_id=second_seed.tenant_id,
            second_tenant_user_id=str(second_owner["user_id"]),
            second_tenant_provider_id=str(second_owner["provider_id"]),
            second_tenant_model_id=str(second_owner["model_id"]),
            second_tenant_model_alias_id="alias_prd076_runtime_tenant2",
            second_tenant_virtual_key_id=str(second_owner["virtual_key_id"]),
            second_tenant_slug="prd076-runtime-second",
            second_tenant_email="admin-prd076-second@local.invalid",
        )
        stage = "provider"
        connection.execute(
            """
            UPDATE providers
            SET base_url=?,health_status='healthy',health_updated_at=?,updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (provider_url, now, now, graph.tenant_id, graph.provider_id),
        )
        connection.execute(
            """
            UPDATE models
            SET upstream_model_name='prd076-runtime-upstream',
                provider_model_id='prd076-runtime-upstream',
                input_price_per_1k=0.10,output_price_per_1k=0.30,
                currency='CNY',price_schema_version='prd076-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, graph.tenant_id, graph.model_id),
        )
        encrypted = encrypt_provider_key(
            secrets_.provider_key,
            encryption_secret=secrets_.provider_encryption_secret,
        )
        credential_fingerprint = hashlib.sha256(
            secrets_.provider_key.encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            UPDATE provider_credentials
            SET encrypted_key=?,key_fingerprint=?,name='runtime-provider',key_prefix=?,
                weight=100,probe_health_status='unknown',updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (
                encrypted,
                credential_fingerprint,
                secret_prefix(secrets_.provider_key),
                now,
                graph.tenant_id,
                graph.provider_id,
            ),
        )
        connection.execute(
            """
            UPDATE providers
            SET base_url=?,health_status='healthy',health_updated_at=?,updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (
                provider_url,
                now,
                now,
                graph.second_tenant_id,
                graph.second_tenant_provider_id,
            ),
        )
        connection.execute(
            """
            UPDATE models
            SET upstream_model_name='prd076-runtime-upstream',
                provider_model_id='prd076-runtime-upstream',
                input_price_per_1k=0.10,output_price_per_1k=0.30,
                currency='CNY',price_schema_version='prd076-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, graph.second_tenant_id, graph.second_tenant_model_id),
        )
        connection.execute(
            """
            UPDATE provider_credentials
            SET encrypted_key=?,key_fingerprint=?,name='runtime-provider',key_prefix=?,
                weight=100,probe_health_status='unknown',updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (
                encrypted,
                credential_fingerprint,
                secret_prefix(secrets_.provider_key),
                now,
                graph.second_tenant_id,
                graph.second_tenant_provider_id,
            ),
        )
        stage = "project"
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,description,owner_user_id,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'prd076-runtime','PRD076 runtime',NULL,?,'active',?,?,?,?)
            """,
            (
                graph.project_id,
                graph.tenant_id,
                graph.admin_user_id,
                graph.admin_user_id,
                graph.admin_user_id,
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (graph.project_id, now, graph.tenant_id, graph.virtual_key_id),
        )
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id,tenant_id,project_id,name,key_prefix,key_hash,status,
              environment,environment_id,created_at,updated_at
            ) VALUES(?,?,?,'PRD076 runtime second',?,?,'active','dev',?,?,?)
            """,
            (
                graph.second_virtual_key_id,
                graph.tenant_id,
                graph.project_id,
                secret_prefix(secrets_.second_virtual_key),
                hash_secret(secrets_.second_virtual_key),
                graph.environment_id,
                now,
                now,
            ),
        )
        stage = "model"
        connection.execute(
            """
            INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,context_window,
              input_price_per_1k,output_price_per_1k,currency,price_schema_version,
              status,created_by,created_at,updated_at
            ) VALUES(?,?,?,'prd076-runtime-second','prd076-runtime-upstream',
                     'prd076-runtime-upstream','PRD076 runtime second',
                     '{"chat":true,"stream":true}',8192,0.10,0.30,'CNY',
                     'prd076-runtime-v1','active',?,?,?)
            """,
            (
                graph.second_model_id,
                graph.tenant_id,
                graph.provider_id,
                graph.admin_user_id,
                now,
                now,
            ),
        )
        stage = "aliases"
        connection.executemany(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,
              fallback_model_id,status,description,created_by,created_at,updated_at,
              environment_id
            ) VALUES(?,?,?,?,?,NULL,'active','PRD076 runtime alias',?,?,?,?)
            """,
            (
                (
                    graph.model_alias_id,
                    graph.tenant_id,
                    graph.model_alias,
                    str(owner["code"]),
                    graph.model_id,
                    graph.admin_user_id,
                    now,
                    now,
                    graph.environment_id,
                ),
                (
                    graph.second_model_alias_id,
                    graph.tenant_id,
                    graph.second_model_alias,
                    str(owner["code"]),
                    graph.second_model_id,
                    graph.admin_user_id,
                    now,
                    now,
                    graph.environment_id,
                ),
                (
                    graph.second_tenant_model_alias_id,
                    graph.second_tenant_id,
                    graph.model_alias,
                    str(second_owner["code"]),
                    graph.second_tenant_model_id,
                    graph.second_tenant_user_id,
                    now,
                    now,
                    str(second_owner["environment_id"]),
                ),
            ),
        )
        stage = "users"
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    graph.finance_user_id,
                    graph.tenant_id,
                    "finance-prd076@local.invalid",
                    "Runtime finance",
                    hash_password(secrets_.finance_password),
                    "finance_admin",
                    now,
                    now,
                ),
                (
                    graph.viewer_user_id,
                    graph.tenant_id,
                    "viewer-prd076@local.invalid",
                    "Runtime viewer",
                    hash_password(secrets_.viewer_password),
                    "viewer",
                    now,
                    now,
                ),
            ),
        )
        connection.commit()
        catalog = connection.execute(
            """
            SELECT MIN(catalog_revision),MAX(catalog_revision),
                   SUM(code IN(
                     'response_cache.read','response_cache.write','response_cache.invalidate'
                   ))
            FROM permissions WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        require(tuple(catalog) == (12, 12, 3), "business_database.rbac", "catalog_contract")
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "business_database.integrity",
            "failed",
        )
        require(
            connection.execute("PRAGMA foreign_key_check").fetchall() == [],
            "business_database.foreign_keys",
            "violations",
        )
        return graph
    except AcceptanceFailure:
        connection.rollback()
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except sqlite3.Error:
        connection.rollback()
        raise AcceptanceFailure("business_database.prepare", f"sql_failed_{stage}") from None
    finally:
        connection.close()


def service_environment(
    work: Path,
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
) -> dict[str, str]:
    """构造最小子进程环境，并把所有可写位置限定在本次运行树。"""

    allowed_system = {
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
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in allowed_system
    }
    source_roots = (
        ROOT / "shared-sdk" / "python",
        ROOT / "gateway-data-plane" / "src",
        ROOT / "control-plane-api" / "src",
    )
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(item) for item in source_roots),
            "TEMP": str(work / "temp"),
            "TMP": str(work / "temp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "ERROR",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": secrets_.auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets_.provider_encryption_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": secrets_.file_secret,
            "METAHUB_BATCH_ENCRYPTION_SECRET": secrets_.batch_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": secrets_.prompt_template_secret,
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": secrets_.cache_affinity_secret,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "usage-spool"),
            "METAHUB_GATEWAY_API_BASE_URL": f"http://{LOOPBACK}:{GATEWAY_PORT}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK}:{CONTROL_PORT}",
            "METAHUB_ALLOWED_ORIGINS": (
                f"http://localhost:{CONSOLE_PORT},http://{LOOPBACK}:{CONSOLE_PORT}"
            ),
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_COST_ANOMALY_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "false",
            "METAHUB_BATCH_WORKER_ENABLED": "false",
            "METAHUB_RESPONSE_CACHE_ENABLED": "true",
            "METAHUB_RESPONSE_CACHE_HMAC_VERSION": "1",
            "METAHUB_RESPONSE_CACHE_HMAC_SECRET": secrets_.cache_hmac_secret,
            "METAHUB_RESPONSE_CACHE_ENCRYPTION_VERSION": "1",
            "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET": secrets_.cache_encryption_secret,
            "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_VERSION": "1",
            "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET": secrets_.cache_control_secret,
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_VERSION": "1",
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET": secrets_.failure_spool_secret,
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT": str(
                work / "response-cache-failure-spool"
            ),
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_POLL_INTERVAL_SECONDS": "1",
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_CLAIM_LEASE_SECONDS": "60",
            "METAHUB_CONSOLE_PORT": str(CONSOLE_PORT),
        }
    )
    return environment


start_process = _BASE.start_process
start_service = _BASE.start_service
start_console = _BASE.start_console
stop_owned_process = _BASE.stop_owned_process
wait_health = _BASE.wait_health
request_headers = _BASE.request_headers
login = _BASE.login


def stop_runtime_owners(processes: OwnedProcesses, *services: object) -> None:
    """尝试关闭全部精确 owner；一个失败不得阻断其它资源释放。"""

    failed = False
    try:
        processes.close()
    except Exception:
        failed = True
    for service in services:
        try:
            service.stop()  # type: ignore[attr-defined]
        except Exception:
            failed = True
    if failed:
        raise AcceptanceFailure("cleanup.owners", "failed")


def create_webhook(
    client: httpx.Client,
    token: str,
    url: str,
    stub: WebhookStub,
) -> str:
    """通过真实 Control 创建只订阅 PRD-076 事件的本地 endpoint。"""

    response = client.post(
        "/webhooks",
        headers=request_headers(token, "prd076-runtime-webhook-create"),
        json={
            "name": "PRD076 runtime receiver",
            "endpoint_url": url,
            "event_types": ["cache.hit", "cache.invalidated"],
            "status": "active",
            "timeout_ms": 2000,
        },
    )
    require(response.status_code == 200, "http.webhook_create", "status")
    payload = response.json()
    signing_secret = payload.get("signing_secret") if isinstance(payload, dict) else None
    require(isinstance(signing_secret, str), "http.webhook_create", "secret_missing")
    stub.set_secret(signing_secret)
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    return signing_secret


def policy_payload(
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
    *,
    second: bool,
    expected_version: int,
    charge_cache_hits: bool,
    second_data_class: bool = False,
    second_tenant: bool = False,
    ttl_seconds: int = 300,
) -> dict[str, object]:
    """构造完整 CAS 策略；私有分级和 tag 只存在于请求内存与业务库。"""

    return {
        "model_alias_id": (
            graph.second_tenant_model_alias_id
            if second_tenant
            else graph.second_model_alias_id
            if second
            else graph.model_alias_id
        ),
        "status": "enabled",
        "data_class": (
            secrets_.second_data_class_canary
            if second_data_class
            else secrets_.data_class_canary
        ),
        "single_data_class_confirmed": True,
        "ttl_seconds": ttl_seconds,
        "charge_cache_hits": charge_cache_hits,
        "tags": [secrets_.tag_canary],
        "expected_version": expected_version,
    }


def patch_policy(
    client: httpx.Client,
    token: str,
    payload: dict[str, object],
    *,
    sequence: int,
) -> dict[str, object]:
    """执行真实策略 CAS，并只返回当前调用栈内的响应 object。"""

    response = client.patch(
        "/cache-policies",
        headers=request_headers(
            token,
            f"prd076-runtime-policy-{sequence}",
            idempotency_key=f"prd076-runtime-policy-intent-{sequence:04d}",
        ),
        json=payload,
    )
    require(response.status_code == 200, "http.cache_policy", f"status_{sequence}")
    require(
        response.headers.get("cache-control") == "no-store, private",
        "http.cache_policy",
        "cache_control",
    )
    result = response.json()
    require(isinstance(result, dict), "http.cache_policy", "shape")
    return result


def invalidate_cache(
    client: httpx.Client,
    token: str,
    *,
    sequence: int,
    tag: str | None,
) -> dict[str, object]:
    """通过真实 Control 执行 tag all-match 或全租户失效。"""

    response = client.post(
        "/cache/invalidate",
        headers=request_headers(
            token,
            f"prd076-runtime-invalidate-{sequence}",
            idempotency_key=f"prd076-runtime-invalidation-{sequence:04d}",
        ),
        json={
            "scope": "tags" if tag is not None else "all",
            "match_mode": "all",
            "tags": [f"tag:{tag}"] if tag is not None else [],
            "reason_code": "manual_finops" if tag is not None else "manual_other",
        },
    )
    require(response.status_code == 200, "http.cache_invalidation", f"status_{sequence}")
    result = response.json()
    require(isinstance(result, dict), "http.cache_invalidation", "shape")
    return result


def provider_call_count(provider: ProviderStub) -> int:
    """读取不含正文和 Key 的单一 Provider 调用计数。"""

    return int(provider.snapshot()["calls"])


def invoke_gateway(
    graph: RuntimeGraph,
    virtual_key: str,
    *,
    request_id: str,
    model_alias: str,
    prompt: str,
    expected_status: int = 200,
    tenant_id: str | None = None,
) -> dict[str, object]:
    """调用真实 OpenAI-compatible Gateway；正文只保留在当前调用栈。"""

    headers = {
        "Authorization": f"Bearer {virtual_key}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace-{request_id}",
        "X-MetaHub-Contract-Tenant-ID": tenant_id or graph.tenant_id,
    }
    try:
        response = httpx.post(
            f"http://{LOOPBACK}:{GATEWAY_PORT}/v1/chat/completions",
            headers=headers,
            json={
                "model": model_alias,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except httpx.HTTPError:
        raise AcceptanceFailure("http.gateway", "transport") from None
    require(response.status_code == expected_status, "http.gateway", f"status_{request_id}")
    try:
        payload = response.json()
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise AcceptanceFailure("http.gateway", "json") from None
    require(isinstance(payload, dict), "http.gateway", "shape")
    if expected_status != 200:
        # 负例同样必须走统一错误合同；只检查固定 code/链路形态，不把错误正文写入证据。
        require(
            isinstance(payload.get("code"), str)
            and payload.get("request_id") == request_id
            and response.headers.get("cache-control") == "no-store",
            "http.gateway",
            "error_contract",
        )
    return payload


def response_cache_fact(
    graph: RuntimeGraph,
    request_id: str,
    *,
    tenant_id: str | None = None,
) -> sqlite3.Row:
    """读取单个请求的 PRD-076 安全状态；调用方不得序列化 owner ID。"""

    connection = connect_database(graph.database_url)
    try:
        row = connection.execute(
            """
            SELECT status,hit,charge_cache_hits,billed_cost_status,billed_cost_cny_micros,
                   saved_cost_status,saved_cost_cny_micros
            FROM response_cache_request_facts
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id or graph.tenant_id, request_id),
        ).fetchone()
        require(row is not None, "database.cache_fact", "missing")
        return row
    finally:
        connection.close()


def wait_until_cache_entry_expires(
    graph: RuntimeGraph,
    source_request_id: str,
    *,
    timeout: float = 70.0,
) -> None:
    """Wait for the persisted entry to cross its SQLite expiration boundary.

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 wait_until_cache_entry_expires 对应步骤。"""

    deadline = time.monotonic() + timeout
    observed_remaining: int | None = None
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            row = connection.execute(
                """
                SELECT unixepoch(expires_at)-unixepoch('now') AS remaining
                FROM response_cache_entries
                WHERE tenant_id=? AND source_request_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (graph.tenant_id, source_request_id),
            ).fetchone()
        finally:
            connection.close()
        require(row is not None, "database.ttl", "entry_missing")
        remaining = int(row["remaining"])
        if observed_remaining is None:
            observed_remaining = remaining
            require(0 < remaining <= 61, "database.ttl", "boundary_invalid")
        if remaining <= 0:
            return
        time.sleep(min(0.25, max(0.05, float(remaining))))
    raise AcceptanceFailure("database.ttl", "expiry_timeout")


def exercise_overlapping_misses(
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
    provider: ProviderStub,
    *,
    prompt: str,
) -> dict[str, int | bool]:
    """Issue two synchronized misses and verify one fill owner plus one non-owner.

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 exercise_overlapping_misses 对应步骤。"""

    request_ids = (
        "prd076-runtime-concurrent-a",
        "prd076-runtime-concurrent-b",
    )
    barrier = threading.Barrier(3)
    result_lock = threading.Lock()
    completed: dict[str, bool] = {}
    failures = 0

    def worker(request_id: str) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 worker 对应步骤。"""
        nonlocal failures
        try:
            barrier.wait(timeout=5)
            payload = invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=request_id,
                model_alias=graph.model_alias,
                prompt=prompt,
            )
            _verify_gateway_answer(payload, secrets_.response_canary)
            with result_lock:
                completed[request_id] = True
        except Exception:
            with result_lock:
                failures += 1

    baseline = provider_call_count(provider)
    threads = tuple(
        threading.Thread(
            target=worker,
            args=(request_id,),
            name=f"prd076-overlap-{index}",
            daemon=True,
        )
        for index, request_id in enumerate(request_ids, start=1)
    )
    provider.set_delay(0.75)
    try:
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=25)
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except threading.BrokenBarrierError:
        raise AcceptanceFailure("http.concurrent", "barrier_failed") from None
    finally:
        provider.set_delay(0)
    require(
        failures == 0
        and len(completed) == 2
        and all(not thread.is_alive() for thread in threads),
        "http.concurrent",
        "request_failed",
    )
    statuses = tuple(
        str(response_cache_fact(graph, request_id)["status"])
        for request_id in request_ids
    )
    fill_owner_count = statuses.count("fill_stored")
    non_owner_count = statuses.count("miss_non_owner")
    require(
        fill_owner_count == 1
        and non_owner_count == 1
        and provider_call_count(provider) - baseline == 2,
        "database.concurrent",
        "ownership_contract",
    )
    return {
        "concurrent_single_fill_owner": True,
        "concurrent_fill_owner_count": fill_owner_count,
        "concurrent_non_owner_count": non_owner_count,
    }


def wait_outbox_settled(graph: RuntimeGraph, *, timeout: float = 30.0) -> dict[str, int]:
    """等待当前 source outbox 全部离开 pending/leased，并返回有界状态计数。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = connect_database(graph.database_url)
        try:
            rows = connection.execute(
                """
                SELECT status,COUNT(*) AS count
                FROM response_cache_event_outbox
                WHERE tenant_id=? GROUP BY status
                """,
                (graph.tenant_id,),
            ).fetchall()
        finally:
            connection.close()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        if counts.get("pending", 0) == 0 and counts.get("leased", 0) == 0:
            return counts
        time.sleep(0.2)
    raise AcceptanceFailure("recovery.outbox", "settle_timeout")


def wait_for_recovered_hit(
    graph: RuntimeGraph,
    webhook: WebhookStub,
    *,
    baseline_hits: int,
) -> dict[str, int]:
    """等待停机期间创建的 cache.hit source 被重启 Control 投递。"""

    deadline = time.monotonic() + WEBHOOK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            counts = wait_outbox_settled(graph, timeout=2.5)
        except AcceptanceFailure as exc:
            if (exc.stage, exc.code) != ("recovery.outbox", "settle_timeout"):
                raise
            time.sleep(0.2)
            continue
        snapshot = webhook.snapshot()
        if snapshot["hits"] > baseline_hits and counts.get("pending", 0) == 0:
            return counts
        time.sleep(0.2)
    raise AcceptanceFailure("recovery.outbox", "dispatch_timeout")


def verify_spools_empty(work: Path) -> dict[str, int]:
    """证明 Usage 与 Response Cache failure spool 的所有状态目录均为空。"""

    result: dict[str, int] = {}
    roots = {
        "failure_pending": work / "response-cache-failure-spool" / "pending",
        "failure_processing": work / "response-cache-failure-spool" / "processing",
        "failure_quarantine": work / "response-cache-failure-spool" / "quarantine",
        "usage_pending": work / "usage-spool" / "pending",
        "usage_processing": work / "usage-spool" / "processing",
        "usage_quarantine": work / "usage-spool" / "quarantine",
    }
    try:
        for name, directory in roots.items():
            result[name] = sum(1 for _item in directory.iterdir()) if directory.is_dir() else 0
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except OSError:
        raise AcceptanceFailure("cleanup.spool", "unavailable") from None
    require(all(value == 0 for value in result.values()), "cleanup.spool", "not_empty")
    return result


def verify_committed_hit_failure_spool_recovery(
    work: Path,
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
    *,
    request_id: str,
) -> dict[str, int | bool]:
    """Replay a real completed settlement after the Gateway owner has stopped.

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 verify_committed_hit_failure_spool_recovery 对应步骤。"""

    from types import SimpleNamespace

    from metahub_gateway.response_cache.failure_spool import (
        ResponseCacheFailureRecord,
        ResponseCacheFailureSpool,
        _replay_response_cache_failure_batch,
    )
    from metahub_gateway.response_cache.models import ResponseCacheSettlementIds
    from metahub_shared.response_cache import VersionedResponseCacheSecret

    connection = connect_database(graph.database_url)
    try:
        row = connection.execute(
            """
            SELECT fact.trace_id,fact.cache_policy_id,fact.cache_policy_version,
                   hit.response_cache_hit_id,hit.usage_ledger_id,
                   hit.finops_ledger_entry_id,
                   hit.current_reference_price_snapshot_id,
                   hit.billed_price_snapshot_id,charge.event_id AS finops_event_id,
                   event.response_cache_event_id
            FROM response_cache_request_facts fact
            JOIN response_cache_hits hit
              ON hit.tenant_id=fact.tenant_id
             AND hit.response_cache_hit_id=fact.response_cache_hit_id
            JOIN finops_ledger_entries charge
              ON charge.tenant_id=hit.tenant_id
             AND charge.finops_ledger_entry_id=hit.finops_ledger_entry_id
            JOIN response_cache_events event
              ON event.tenant_id=hit.tenant_id
             AND event.event_type='cache.hit'
             AND event.object_id=hit.response_cache_hit_id
            WHERE fact.tenant_id=? AND fact.request_id=? AND fact.status='hit'
            """,
            (graph.tenant_id, request_id),
        ).fetchone()
    finally:
        connection.close()
    require(row is not None, "recovery.failure_spool", "committed_hit_missing")
    ids = ResponseCacheSettlementIds(
        cache_hit_id=str(row["response_cache_hit_id"]),
        usage_ledger_id=str(row["usage_ledger_id"]),
        finops_ledger_entry_id=str(row["finops_ledger_entry_id"]),
        finops_event_id=str(row["finops_event_id"]),
        reference_price_snapshot_id=str(row["current_reference_price_snapshot_id"]),
        billed_price_snapshot_id=str(row["billed_price_snapshot_id"]),
        response_cache_event_id=str(row["response_cache_event_id"]),
    )
    config = SimpleNamespace(
        database_url=graph.database_url,
        response_cache_failure_spool_root=str(work / "response-cache-failure-spool"),
        response_cache_failure_spool_keyring=(
            VersionedResponseCacheSecret(1, secrets_.failure_spool_secret),
        ),
        response_cache_failure_spool_claim_lease_seconds=60,
        response_cache_failure_spool_poll_interval_seconds=1,
    )
    spool = ResponseCacheFailureSpool(config)
    spool.prepare()

    def counts() -> dict[str, int]:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 counts 对应步骤。"""
        return {
            "pending": sum(1 for _item in spool.pending.iterdir()),
            "processing": sum(1 for _item in spool.processing.iterdir()),
            "quarantine": sum(1 for _item in spool.quarantine.iterdir()),
        }

    require(all(value == 0 for value in counts().values()), "recovery.failure_spool", "not_empty")
    record = ResponseCacheFailureRecord(
        tenant_id=graph.tenant_id,
        request_id=request_id,
        trace_id=str(row["trace_id"]),
        cache_policy_id=str(row["cache_policy_id"]),
        cache_policy_version=int(row["cache_policy_version"]),
        ids=ids,
    )
    created = spool.store(record)
    stored_counts = counts()
    require(
        created
        and stored_counts == {"pending": 1, "processing": 0, "quarantine": 0},
        "recovery.failure_spool",
        "store_contract",
    )
    summary = _replay_response_cache_failure_batch(config)
    final_counts = counts()
    require(
        summary.available
        and summary.claimed == 1
        and summary.completed == 1
        and summary.recovered_failed == 0
        and summary.deferred == 0
        and summary.quarantined == 0
        and final_counts == {"pending": 0, "processing": 0, "quarantine": 0},
        "recovery.failure_spool",
        "replay_contract",
    )
    return {
        "failure_spool_record_created": True,
        "commit_unknown_recovered": True,
        "failure_spool_pending": final_counts["pending"],
        "failure_spool_processing": final_counts["processing"],
        "failure_spool_quarantine": final_counts["quarantine"],
    }


def _schema_fingerprint(database: Path) -> str:
    """计算 schema 与迁移状态的确定性摘要，不把数据库位置写入结果。"""

    connection = connect_database(database_url(database))
    try:
        schema_rows = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
        migration_rows = connection.execute(
            "SELECT version,name,checksum,status FROM schema_migrations ORDER BY version"
        ).fetchall()
        value = [tuple(row) for row in schema_rows], [tuple(row) for row in migration_rows]
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()
    finally:
        connection.close()


def verify_business_down_guard(
    database: Path,
    snapshot: FrozenMigrationSnapshot,
    work: Path,
) -> dict[str, object]:
    """通过受信 runner 证明业务事实在 DDL 前固定阻断 0069 down。"""

    verified_database = workspace_path(database, work, stage="down_guard.database")
    verify_snapshot_directory(snapshot, stage="down_guard.snapshot_before")
    before_sha = hashlib.sha256(verified_database.read_bytes()).hexdigest()
    before_schema = _schema_fingerprint(verified_database)
    temp = workspace_path(Path(work) / "temp", work, stage="down_guard.temp")
    temp.mkdir(exist_ok=True)
    runner_environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper()
        in {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "OS",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
        }
    }
    runner_environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(item)
                for item in (
                    ROOT / "shared-sdk" / "python",
                    ROOT / "gateway-data-plane" / "src",
                )
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temp),
            "TMP": str(temp),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url(verified_database),
            "METAHUB_RESPONSE_CACHE_ENABLED": "false",
            "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT": str(
                work / "response-cache-failure-spool"
            ),
        }
    )
    try:
        completed = subprocess.run(
            [
                str(PYTHON),
                str(ROOT / "scripts" / "maintenance" / "response-cache-down.py"),
                "--confirm-stopped",
            ],
            cwd=str(ROOT),
            env=runner_environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, subprocess.SubprocessError):
        raise AcceptanceFailure("down_guard.runner", "failed") from None
    try:
        error = json.loads(completed.stderr.decode("ascii", errors="strict"))
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (UnicodeError, json.JSONDecodeError):
        raise AcceptanceFailure("down_guard.runner", "output") from None
    require(
        completed.returncode == 3
        and completed.stdout == b""
        and error
        == {
            "code": "response_cache.maintenance_down_blocked",
            "operation": "down",
            "status": "failed",
        },
        "down_guard.runner",
        "contract",
    )
    after_sha = hashlib.sha256(verified_database.read_bytes()).hexdigest()
    after_schema = _schema_fingerprint(verified_database)
    health = migration_health(
        database_url(verified_database),
        expected_count=69,
        expected_head="0069",
        stage="down_guard.health",
    )
    verify_snapshot_directory(snapshot, stage="down_guard.snapshot_after")
    require(before_sha == after_sha, "down_guard.database", "changed")
    require(before_schema == after_schema, "down_guard.schema", "changed")
    return {
        "blocked": True,
        "error_code": "response_cache.maintenance_down_blocked",
        "schema_unchanged": True,
        "database_unchanged": True,
        "latest_migration": health["latest_migration"],
    }


def verify_non_database_privacy(
    work: Path,
    private_values: tuple[str, ...],
) -> dict[str, int]:
    """扫描日志、spool 与临时文件，数据库中的可信 policy 配置除外。"""

    scanned = 0
    matches = 0
    try:
        for path in sorted(Path(work).rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.casefold().endswith((".sqlite3", "-wal", "-shm", "-journal")):
                continue
            require(path.stat().st_size <= 128 * 1024 * 1024, "privacy.scan", "file_too_large")
            content = path.read_bytes()
            scanned += 1
            for value in private_values:
                if any(encoded in content for encoded in sensitive_encodings(value)):
                    matches += 1
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None
    require(matches == 0, "privacy.non_database", "plaintext_found")
    return {"files_scanned": scanned, "plaintext_matches": matches}


def database_summary(graph: RuntimeGraph) -> dict[str, object]:
    """读取最终有界聚合，不返回 request、trace、owner、digest 或路径。"""

    connection = connect_database(graph.database_url)
    try:
        status_rows = connection.execute(
            """
            SELECT status,COUNT(*) AS count FROM response_cache_request_facts
            WHERE tenant_id=? GROUP BY status ORDER BY status
            """,
            (graph.tenant_id,),
        ).fetchall()
        outbox_rows = connection.execute(
            """
            SELECT status,COUNT(*) AS count FROM response_cache_event_outbox
            WHERE tenant_id=? GROUP BY status ORDER BY status
            """,
            (graph.tenant_id,),
        ).fetchall()
        summary = {
            "migration_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE status='applied'"
                ).fetchone()[0]
            ),
            "latest_migration": str(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations WHERE status='applied'"
                ).fetchone()[0]
            ),
            "integrity_check": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "rbac_revision": int(
                connection.execute(
                    "SELECT MIN(catalog_revision) FROM permissions WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "entries_ready": int(
                connection.execute(
                    "SELECT COUNT(*) FROM response_cache_entries WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "hits": int(
                connection.execute(
                    "SELECT COUNT(*) FROM response_cache_hits WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "invalidations": int(
                connection.execute(
                    "SELECT COUNT(*) FROM response_cache_invalidations WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "fact_status_counts": {
                str(row["status"]): int(row["count"]) for row in status_rows
            },
            "outbox_status_counts": {
                str(row["status"]): int(row["count"]) for row in outbox_rows
            },
        }
    finally:
        connection.close()
    require(
        summary["migration_count"] == 69
        and summary["latest_migration"] == "0069"
        and summary["integrity_check"] == "ok"
        and summary["foreign_key_violations"] == 0
        and summary["rbac_revision"] == 12,
        "database.summary",
        "contract",
    )
    return summary


def empty_safe_facts() -> dict[str, object]:
    """返回 exact 顶层白名单；调用方只能替换值，不能增删证据字段。"""

    return {
        "services": {},
        "migration_roundtrip": {},
        "access_control": {},
        "cache_flow": {},
        "isolation": {},
        "billing": {},
        "invalidation": {},
        "recovery": {},
        "database": {},
        "privacy_canaries": {},
    }


def validate_runtime_coverage(facts: dict[str, object]) -> None:
    """Reject publication unless every PRD-076 runtime proof is independently present.

    运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 validate_runtime_coverage 对应步骤。"""

    required: dict[str, dict[str, bool | int]] = {
        "isolation": {
            "tenant_mismatch_rejected": True,
            "virtual_key_miss": True,
            "same_model_data_class_miss": True,
            "resolved_model_miss": True,
            "prompt_miss": True,
            "second_tenant_legal_miss": True,
        },
        "cache_flow": {
            "miss_fill_hit": True,
            "hit_provider_delta": 0,
            "ttl_boundary_miss": True,
            "concurrent_single_fill_owner": True,
            "concurrent_fill_owner_count": 1,
            "concurrent_non_owner_count": 1,
        },
        "billing": {
            "charge_off_zero": True,
            "charge_on_positive": True,
            "budget_exhaustion_blocked_before_provider": True,
            "budget_block_provider_delta": 0,
        },
        "recovery": {
            "control_outage_source_pending": True,
            "control_restart_dispatched": True,
            "failure_spool_record_created": True,
            "commit_unknown_recovered": True,
            "failure_spool_pending": 0,
            "failure_spool_processing": 0,
            "failure_spool_quarantine": 0,
        },
    }
    complete = True
    for section_name, expected_fields in required.items():
        section = facts.get(section_name)
        if not isinstance(section, dict):
            complete = False
            continue
        for field_name, expected in expected_fields.items():
            actual = section.get(field_name)
            if type(expected) is bool:
                complete = complete and actual is expected
            else:
                complete = complete and type(actual) is int and actual == expected
    require(complete, "evidence.runtime_coverage", "incomplete")


def _verify_gateway_answer(payload: dict[str, object], expected: str) -> None:
    """只在调用栈内比较 Provider 正文，绝不把正文复制到异常或最终证据。"""

    try:
        choices = payload["choices"]
        message = choices[0]["message"]  # type: ignore[index]
        content = message["content"]  # type: ignore[index]
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (KeyError, IndexError, TypeError):
        raise AcceptanceFailure("http.gateway", "completion_shape") from None
    require(content == expected, "http.gateway", "completion_mismatch")


def _cache_log_projection(
    client: httpx.Client,
    token: str,
    request_id: str,
    *,
    sequence: int,
) -> dict[str, object]:
    """通过真实 Control 读取安全投影，只返回不含 owner/正文的金额状态。"""

    response = client.get(
        f"/request-logs/{quote(request_id, safe='')}",
        headers=request_headers(token, f"prd076-runtime-log-{sequence}"),
    )
    require(
        response.status_code == 200
        and response.headers.get("cache-control") == "no-store, private",
        "http.request_log",
        f"status_{sequence}",
    )
    body = response.json()
    detail = body.get("request_log") if isinstance(body, dict) else None
    projection = detail.get("response_cache") if isinstance(detail, dict) else None
    require(
        isinstance(projection, dict)
        and projection.get("status") == "hit"
        and projection.get("hit") is True,
        "http.request_log",
        f"projection_{sequence}",
    )
    allowed = {
        "status",
        "hit",
        "charge_cache_hits",
        "source_generation_cost_status",
        "current_reference_cost_status",
        "saved_cost_status",
        "billed_cost_status",
    }
    return {name: projection.get(name) for name in sorted(allowed)}


def _billing_summary(graph: RuntimeGraph, hit_request_ids: tuple[str, ...]) -> dict[str, int]:
    """证明每个 hit 恰有逻辑 Usage/FinOps，且所有预算预留都已结算为零。"""

    usage_rows = 0
    finops_rows = 0
    reservations = 0
    zero_reservations = 0
    connection = connect_database(graph.database_url)
    try:
        for request_id in hit_request_ids:
            usage = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM usage_ledger
                    WHERE tenant_id=? AND request_id=? AND usage_source='response_cache'
                    """,
                    (graph.tenant_id, request_id),
                ).fetchone()[0]
            )
            finops = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM finops_ledger_entries
                    WHERE tenant_id=? AND source_request_id=? AND entry_type='usage_charge'
                    """,
                    (graph.tenant_id, request_id),
                ).fetchone()[0]
            )
            budget = connection.execute(
                """
                SELECT outstanding_cost_cny FROM budget_reservations
                WHERE tenant_id=? AND request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchall()
            require(usage == 1 and finops == 1 and bool(budget), "database.billing", "owner")
            usage_rows += usage
            finops_rows += finops
            reservations += len(budget)
            zero_reservations += sum(
                1 for row in budget if Decimal(str(row["outstanding_cost_cny"])) == Decimal("0")
            )
    finally:
        connection.close()
    require(reservations == zero_reservations, "database.billing", "reservation_outstanding")
    return {
        "hit_requests": len(hit_request_ids),
        "logical_usage_rows": usage_rows,
        "finops_usage_charge_rows": finops_rows,
        "budget_reservations": reservations,
        "budget_reservations_zero": zero_reservations,
    }


def _pending_outbox_count(graph: RuntimeGraph) -> int:
    """读取 Control 停机窗口内尚未投递的 source owner 数。"""

    connection = connect_database(graph.database_url)
    try:
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM response_cache_event_outbox
                WHERE tenant_id=? AND status IN('pending','leased')
                """,
                (graph.tenant_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def exercise_response_cache_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    secrets_: RuntimeSecrets,
    provider: ProviderStub,
    webhook: WebhookStub,
    webhook_url: str,
    processes: OwnedProcesses,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """贯通真实三服务、缓存主链路、权限、财务、失效和 Control 恢复。"""

    gateway = processes.add(
        start_service(
            "gateway",
            "metahub_gateway.app:app",
            GATEWAY_PORT,
            work=work,
            environment=environment,
        )
    )
    wait_health(f"http://{LOOPBACK}:{GATEWAY_PORT}/health", gateway, stage="health.gateway")
    control = processes.add(
        start_service(
            "control-initial",
            "metahub_control.app:app",
            CONTROL_PORT,
            work=work,
            environment=environment,
        )
    )
    wait_health(f"http://{LOOPBACK}:{CONTROL_PORT}/health", control, stage="health.control")
    console = processes.add(start_console(work=work, environment=environment))
    wait_health(f"http://{LOOPBACK}:{CONSOLE_PORT}/", console, stage="health.console")

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        admin_token = login(
            client,
            "default",
            "admin@metahub.local",
            secrets_.admin_password,
            "prd076-runtime-login-admin",
        )
        finance_token = login(
            client,
            "default",
            "finance-prd076@local.invalid",
            secrets_.finance_password,
            "prd076-runtime-login-finance",
        )
        viewer_token = login(
            client,
            "default",
            "viewer-prd076@local.invalid",
            secrets_.viewer_password,
            "prd076-runtime-login-viewer",
        )
        second_admin_token = login(
            client,
            graph.second_tenant_slug,
            graph.second_tenant_email,
            secrets_.second_tenant_password,
            "prd076-runtime-login-second-tenant",
        )
        webhook_secret = create_webhook(client, admin_token, webhook_url, webhook)

        navigation = client.get(
            "/console/navigation?active_item_id=response-cache",
            headers=request_headers(admin_token, "prd076-runtime-navigation"),
        )
        nav_body = navigation.json()
        nav_items = nav_body.get("navigation_items") if isinstance(nav_body, dict) else None
        require(
            navigation.status_code == 200
            and isinstance(nav_items, list)
            and any(
                isinstance(item, dict) and item.get("id") == "response-cache"
                for item in nav_items
            ),
            "http.navigation",
            "response_cache_missing",
        )
        denied = client.get(
            "/cache-policies",
            headers=request_headers(viewer_token, "prd076-runtime-viewer-denied"),
        )
        denied_body = denied.json()
        require(
            denied.status_code == 403
            and denied_body.get("code") == "auth.permission_denied"
            and denied_body.get("request_id") == "prd076-runtime-viewer-denied",
            "http.viewer",
            "contract",
        )
        finance_read = client.get(
            "/cache-policies",
            headers=request_headers(finance_token, "prd076-runtime-finance-read"),
        )
        require(finance_read.status_code == 200, "http.finance", "read_denied")

        # 高额度 project 规则让普通 miss 和 cache hit 都实际经过预算 owner；终态再逐请求证明归零。
        budget = client.patch(
            "/budgets",
            headers=request_headers(finance_token, "prd076-runtime-budget-create"),
            json={
                "scope_type": "project",
                "scope_id": graph.project_id,
                "environment_id": graph.environment_id,
                "name": "PRD076 runtime budget",
                "daily_budget_cny": "1000",
                "warning_threshold_percent": 80,
                "exhausted_action": "block",
                "status": "active",
            },
        )
        require(budget.status_code == 200, "http.budget", "create_failed")

        free_policy = patch_policy(
            client,
            finance_token,
            policy_payload(
                graph,
                secrets_,
                second=False,
                expected_version=0,
                charge_cache_hits=False,
            ),
            sequence=1,
        )
        require(
            free_policy.get("cache_policy", {}).get("version") == 1,  # type: ignore[union-attr]
            "http.cache_policy",
            "free_version",
        )

        free_miss_id = "prd076-runtime-free-miss"
        free_hit_id = "prd076-runtime-free-hit"
        first = invoke_gateway(
            graph,
            secrets_.virtual_key,
            request_id=free_miss_id,
            model_alias=graph.model_alias,
            prompt=secrets_.prompt_canary,
        )
        _verify_gateway_answer(first, secrets_.response_canary)
        require(provider_call_count(provider) == 1, "provider.free_miss", "call_count")
        hit_provider_before = provider_call_count(provider)
        second = invoke_gateway(
            graph,
            secrets_.virtual_key,
            request_id=free_hit_id,
            model_alias=graph.model_alias,
            prompt=secrets_.prompt_canary,
        )
        _verify_gateway_answer(second, secrets_.response_canary)
        hit_provider_delta = provider_call_count(provider) - hit_provider_before
        require(hit_provider_delta == 0, "provider.free_hit", "unexpected_call")
        free_miss = response_cache_fact(graph, free_miss_id)
        free_hit = response_cache_fact(graph, free_hit_id)
        require(
            free_miss["status"] == "fill_stored"
            and free_hit["status"] == "hit"
            and int(free_hit["charge_cache_hits"]) == 0
            and free_hit["billed_cost_status"] == "known"
            and int(free_hit["billed_cost_cny_micros"]) == 0
            and free_hit["saved_cost_status"] == "known"
            and int(free_hit["saved_cost_cny_micros"]) > 0,
            "database.free_hit",
            "contract",
        )
        free_projection = _cache_log_projection(
            client,
            admin_token,
            free_hit_id,
            sequence=1,
        )

        charged_policy = patch_policy(
            client,
            admin_token,
            policy_payload(
                graph,
                secrets_,
                second=False,
                expected_version=1,
                charge_cache_hits=True,
            ),
            sequence=2,
        )
        require(
            charged_policy.get("cache_policy", {}).get("version") == 2,  # type: ignore[union-attr]
            "http.cache_policy",
            "charged_version",
        )
        charged_miss_id = "prd076-runtime-charged-miss"
        charged_hit_id = "prd076-runtime-charged-hit"
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=charged_miss_id,
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(provider_call_count(provider) == 2, "provider.charged_miss", "call_count")
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=charged_hit_id,
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(provider_call_count(provider) == 2, "provider.charged_hit", "unexpected_call")
        charged_hit = response_cache_fact(graph, charged_hit_id)
        require(
            charged_hit["status"] == "hit"
            and int(charged_hit["charge_cache_hits"]) == 1
            and charged_hit["billed_cost_status"] == "known"
            and int(charged_hit["billed_cost_cny_micros"]) > 0
            and charged_hit["saved_cost_status"] == "known"
            and int(charged_hit["saved_cost_cny_micros"]) == 0,
            "database.charged_hit",
            "contract",
        )
        charged_projection = _cache_log_projection(
            client,
            admin_token,
            charged_hit_id,
            sequence=2,
        )

        prompt_miss_id = "prd076-runtime-prompt-isolation"
        prompt_variant = f"{secrets_.prompt_canary}-prompt-isolation"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=prompt_miss_id,
                model_alias=graph.model_alias,
                prompt=prompt_variant,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, prompt_miss_id)["status"] == "fill_stored",
            "provider.prompt_isolation",
            "contract",
        )

        virtual_key_miss_id = "prd076-runtime-key-isolation"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.second_virtual_key,
                request_id=virtual_key_miss_id,
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, virtual_key_miss_id)["status"] == "fill_stored",
            "provider.key_isolation",
            "contract",
        )

        patch_policy(
            client,
            admin_token,
            policy_payload(
                graph,
                secrets_,
                second=True,
                expected_version=0,
                charge_cache_hits=False,
            ),
            sequence=3,
        )
        model_miss_id = "prd076-runtime-model-isolation"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=model_miss_id,
                model_alias=graph.second_model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, model_miss_id)["status"] == "fill_stored",
            "provider.model_isolation",
            "contract",
        )

        data_class_policy = patch_policy(
            client,
            admin_token,
            policy_payload(
                graph,
                secrets_,
                second=False,
                expected_version=2,
                charge_cache_hits=False,
                second_data_class=True,
            ),
            sequence=4,
        )
        require(
            data_class_policy.get("cache_policy", {}).get("version") == 3,  # type: ignore[union-attr]
            "http.cache_policy",
            "data_class_version",
        )
        data_class_miss_id = "prd076-runtime-data-class-isolation"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=data_class_miss_id,
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, data_class_miss_id)["status"] == "fill_stored",
            "provider.data_class_isolation",
            "contract",
        )

        patch_policy(
            client,
            second_admin_token,
            policy_payload(
                graph,
                secrets_,
                second=False,
                second_tenant=True,
                expected_version=0,
                charge_cache_hits=False,
            ),
            sequence=5,
        )
        second_tenant_miss_id = "prd076-runtime-second-tenant-miss"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.second_tenant_virtual_key,
                request_id=second_tenant_miss_id,
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
                tenant_id=graph.second_tenant_id,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(
                graph,
                second_tenant_miss_id,
                tenant_id=graph.second_tenant_id,
            )["status"]
            == "fill_stored",
            "provider.second_tenant_isolation",
            "contract",
        )

        tenant_provider_before = provider_call_count(provider)
        tenant_error = invoke_gateway(
            graph,
            secrets_.virtual_key,
            request_id="prd076-runtime-tenant-isolation",
            model_alias=graph.model_alias,
            prompt=secrets_.prompt_canary,
            expected_status=401,
            tenant_id="tenant_prd076_mismatch",
        )
        require(
            tenant_error.get("code") == "gateway.auth_invalid"
            and provider_call_count(provider) - tenant_provider_before == 0,
            "http.tenant_isolation",
            "contract",
        )

        tag_result = invalidate_cache(
            client,
            admin_token,
            sequence=1,
            tag=secrets_.tag_canary,
        )
        tag_fact = tag_result.get("invalidation")
        require(
            isinstance(tag_fact, dict)
            and int(tag_fact.get("tag_count", -1)) == 1
            and int(tag_fact.get("deleted_entry_count", -1)) >= 1,
            "http.cache_invalidation",
            "tag_contract",
        )
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id="prd076-runtime-after-tag",
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        tag_miss = response_cache_fact(graph, "prd076-runtime-after-tag")
        require(
            provider_call_count(provider) - provider_before == 1
            and tag_miss["status"] == "fill_stored",
            "provider.after_tag",
            "contract",
        )

        all_result = invalidate_cache(client, admin_token, sequence=2, tag=None)
        all_fact = all_result.get("invalidation")
        require(
            isinstance(all_fact, dict)
            and all_fact.get("scope") == "all"
            and int(all_fact.get("deleted_entry_count", -1)) >= 1,
            "http.cache_invalidation",
            "all_contract",
        )
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id="prd076-runtime-after-all",
                model_alias=graph.model_alias,
                prompt=secrets_.prompt_canary,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, "prd076-runtime-after-all")["status"]
            == "fill_stored",
            "provider.after_all",
            "contract",
        )

        concurrent = exercise_overlapping_misses(
            graph,
            secrets_,
            provider,
            prompt=f"{secrets_.prompt_canary}-concurrent",
        )

        ttl_policy = patch_policy(
            client,
            admin_token,
            policy_payload(
                graph,
                secrets_,
                second=False,
                expected_version=3,
                charge_cache_hits=False,
                second_data_class=True,
                ttl_seconds=60,
            ),
            sequence=6,
        )
        require(
            ttl_policy.get("cache_policy", {}).get("version") == 4,  # type: ignore[union-attr]
            "http.cache_policy",
            "ttl_version",
        )
        ttl_prompt = f"{secrets_.prompt_canary}-ttl-boundary"
        ttl_fill_id = "prd076-runtime-ttl-fill"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=ttl_fill_id,
                model_alias=graph.model_alias,
                prompt=ttl_prompt,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, ttl_fill_id)["status"] == "fill_stored",
            "provider.ttl_fill",
            "contract",
        )
        wait_until_cache_entry_expires(graph, ttl_fill_id)
        ttl_expired_id = "prd076-runtime-ttl-expired-miss"
        provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=ttl_expired_id,
                model_alias=graph.model_alias,
                prompt=ttl_prompt,
            ),
            secrets_.response_canary,
        )
        require(
            provider_call_count(provider) - provider_before == 1
            and response_cache_fact(graph, ttl_expired_id)["status"] == "fill_stored",
            "provider.ttl_expiry",
            "contract",
        )
        wait_outbox_settled(graph)

        baseline_hits = int(webhook.snapshot()["hits"])
        processes.stop(control)
        outage_hit_id = "prd076-runtime-outage-hit"
        outage_provider_before = provider_call_count(provider)
        _verify_gateway_answer(
            invoke_gateway(
                graph,
                secrets_.virtual_key,
                request_id=outage_hit_id,
                model_alias=graph.model_alias,
                prompt=ttl_prompt,
            ),
            secrets_.response_canary,
        )
        outage_pending = _pending_outbox_count(graph)
        require(
            provider_call_count(provider) - outage_provider_before == 0
            and outage_pending >= 1
            and response_cache_fact(graph, outage_hit_id)["status"] == "hit",
            "recovery.outage",
            "source_not_pending",
        )
        control = processes.add(
            start_service(
                "control-recovery",
                "metahub_control.app:app",
                CONTROL_PORT,
                work=work,
                environment=environment,
            )
        )
        wait_health(
            f"http://{LOOPBACK}:{CONTROL_PORT}/health",
            control,
            stage="health.control_recovery",
        )
        recovered_outbox = wait_for_recovered_hit(
            graph,
            webhook,
            baseline_hits=baseline_hits,
        )

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as recovered_client:
        outage_projection = _cache_log_projection(
            recovered_client,
            admin_token,
            outage_hit_id,
            sequence=3,
        )
        exhausted_budget = recovered_client.patch(
            "/budgets",
            headers=request_headers(admin_token, "prd076-runtime-budget-exhausted"),
            json={
                "scope_type": "virtual_key",
                "scope_id": graph.virtual_key_id,
                "environment_id": graph.environment_id,
                "name": "PRD076 runtime exhausted key budget",
                "daily_budget_cny": "0.000001",
                "warning_threshold_percent": 80,
                "exhausted_action": "block",
                "status": "active",
            },
        )
        exhausted_body = exhausted_budget.json()
        exhausted_rule = (
            exhausted_body.get("budget_rule") if isinstance(exhausted_body, dict) else None
        )
        exhausted_today = (
            exhausted_rule.get("today_status") if isinstance(exhausted_rule, dict) else None
        )
        require(
            exhausted_budget.status_code == 200
            and isinstance(exhausted_today, dict)
            and exhausted_today.get("status") == "exhausted"
            and exhausted_today.get("next_action") == "block",
            "http.budget",
            "exhausted_rule",
        )
        provider_before = provider_call_count(provider)
        budget_error = invoke_gateway(
            graph,
            secrets_.virtual_key,
            request_id="prd076-runtime-budget-blocked",
            model_alias=graph.model_alias,
            prompt=f"{secrets_.prompt_canary}-budget-blocked",
            expected_status=402,
        )
        budget_block_provider_delta = provider_call_count(provider) - provider_before
        require(
            budget_error.get("code") == "gateway.budget_exhausted"
            and budget_block_provider_delta == 0,
            "http.budget",
            "pre_provider_block",
        )
        connection = connect_database(graph.database_url)
        try:
            connection.execute(
                "UPDATE users SET role='viewer',updated_at=? WHERE tenant_id=? AND user_id=?",
                (utc_now(), graph.tenant_id, graph.finance_user_id),
            )
            connection.commit()
        finally:
            connection.close()
        revoked = recovered_client.patch(
            "/cache-policies",
            headers=request_headers(
                finance_token,
                "prd076-runtime-finance-revoked",
                idempotency_key="prd076-runtime-finance-revoked-intent",
            ),
            json=policy_payload(
                graph,
                secrets_,
                second=False,
                expected_version=4,
                charge_cache_hits=True,
            ),
        )
        revoked_body = revoked.json()
        require(
            revoked.status_code == 403
            and revoked_body.get("code") == "auth.permission_denied"
            and revoked_body.get("request_id") == "prd076-runtime-finance-revoked",
            "http.finance_revocation",
            "contract",
        )

    console_index = httpx.get(
        f"http://{LOOPBACK}:{CONSOLE_PORT}/",
        timeout=3,
        follow_redirects=False,
        trust_env=False,
    )
    console_feature = httpx.get(
        f"http://{LOOPBACK}:{CONSOLE_PORT}/src/features/cache-policies/renderResponseCache.js",
        timeout=3,
        follow_redirects=False,
        trust_env=False,
    )
    require(
        console_index.status_code == 200
        and 'id="app"' in console_index.text
        and console_feature.status_code == 200
        and "renderResponseCache" in console_feature.text,
        "http.console",
        "feature_unavailable",
    )
    provider_fact = provider.snapshot()
    webhook_fact = webhook.snapshot()
    require(
        provider_fact["calls"] >= 12
        and provider_fact["invalid"] == 0
        and provider_fact["auth_mismatches"] == 0
        and webhook_fact["hits"] >= 3
        and webhook_fact["invalidations"] >= 3
        and webhook_fact["invalid"] == 0
        and webhook_fact["signature_failures"] == 0
        and webhook_fact["forbidden_hits"] == 0,
        "runtime.stubs",
        "contract",
    )
    billing = _billing_summary(graph, (free_hit_id, charged_hit_id, outage_hit_id))
    facts = empty_safe_facts()
    facts.update(
        {
            "services": {
                "gateway_health": 200,
                "control_initial_health": 200,
                "control_recovery_health": 200,
                "console_index": 200,
                "console_feature": 200,
            },
            "access_control": {
                "backend_navigation_contains_response_cache": True,
                "finance_read_status": 200,
                "viewer_status": 403,
                "finance_revoked_status": 403,
                "request_id_preserved": True,
            },
            "cache_flow": {
                "miss_fill_hit": True,
                "hit_provider_delta": hit_provider_delta,
                "ttl_boundary_miss": True,
                **concurrent,
                "free_miss_status": str(free_miss["status"]),
                "free_hit_status": str(free_hit["status"]),
                "charged_hit_status": str(charged_hit["status"]),
                "provider_calls": int(provider_fact["calls"]),
                "free_projection": free_projection,
                "charged_projection": charged_projection,
                "outage_projection": outage_projection,
            },
            "isolation": {
                "tenant_mismatch_rejected": True,
                "virtual_key_miss": True,
                "same_model_data_class_miss": True,
                "resolved_model_miss": True,
                "prompt_miss": True,
                "second_tenant_legal_miss": True,
                "tenant_header_status": 401,
                "tenant_header_provider_calls": 0,
            },
            "billing": {
                **billing,
                "charge_off_zero": True,
                "charge_on_positive": True,
                "budget_exhaustion_blocked_before_provider": True,
                "budget_block_provider_delta": budget_block_provider_delta,
                "free_billed_micros": int(free_hit["billed_cost_cny_micros"]),
                "free_saved_micros": int(free_hit["saved_cost_cny_micros"]),
                "charged_billed_micros": int(charged_hit["billed_cost_cny_micros"]),
                "charged_saved_micros": int(charged_hit["saved_cost_cny_micros"]),
            },
            "invalidation": {
                "tag_count": int(tag_fact["tag_count"]),
                "tag_deleted": int(tag_fact["deleted_entry_count"]),
                "all_deleted": int(all_fact["deleted_entry_count"]),
                "webhook_invalidations": int(webhook_fact["invalidations"]),
            },
            "recovery": {
                "control_outage_source_pending": outage_pending >= 1,
                "control_restart_dispatched": True,
                "control_stopped_before_hit": True,
                "source_committed_during_outage": True,
                "restart_delivered": True,
                "outbox_pending": int(recovered_outbox.get("pending", 0)),
                "outbox_leased": int(recovered_outbox.get("leased", 0)),
            },
        }
    )
    return facts, (
        admin_token,
        finance_token,
        viewer_token,
        second_admin_token,
        webhook_secret,
    )


def execute_business_runtime(
    work: Path,
    snapshot: FrozenMigrationSnapshot,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """在独立业务库执行真实服务图，并在 owner 全停后完成静止终态检查。"""

    secrets_ = RuntimeSecrets.create()
    provider = ProviderStub(key=secrets_.provider_key, answer=secrets_.response_canary)
    webhook = WebhookStub()
    processes = OwnedProcesses()
    graph: RuntimeGraph | None = None
    facts: dict[str, object] | None = None
    session_values: tuple[str, ...] = ()
    try:
        provider_url = provider.start()
        webhook_url = webhook.start()
        graph = prepare_business_database(work, secrets_, provider_url, snapshot)
        environment = service_environment(work, graph, secrets_)
        facts, session_values = exercise_response_cache_runtime(
            work,
            graph,
            environment,
            secrets_,
            provider,
            webhook,
            webhook_url,
            processes,
        )
    finally:
        stop_runtime_owners(processes, webhook, provider)
    require(graph is not None and facts is not None, "runtime", "facts_missing")
    if graph is None or facts is None:
        raise AcceptanceFailure("runtime", "facts_missing")

    failure_spool = verify_committed_hit_failure_spool_recovery(
        work,
        graph,
        secrets_,
        request_id="prd076-runtime-outage-hit",
    )
    non_database = verify_non_database_privacy(
        work,
        secrets_.non_database_private_values(graph.database, *session_values),
    )
    spools = verify_spools_empty(work)
    down_guard = verify_business_down_guard(graph.database, snapshot, work)
    facts["database"] = database_summary(graph)
    recovery = facts.get("recovery")
    require(isinstance(recovery, dict), "runtime", "recovery_shape")
    recovery.update(
        {
            **failure_spool,
            "failure_spool_pending": int(spools["failure_pending"]),
            "failure_spool_processing": int(spools["failure_processing"]),
            "failure_spool_quarantine": int(spools["failure_quarantine"]),
            "usage_spool_pending": int(spools["usage_pending"]),
            "down_guard_blocked": bool(down_guard["blocked"]),
            "down_guard_schema_unchanged": bool(down_guard["schema_unchanged"]),
            "down_guard_database_unchanged": bool(down_guard["database_unchanged"]),
        }
    )
    facts["privacy_canaries"] = {
        "storage_categories": len(secrets_.storage_private_values(*session_values)),
        "non_database_categories": len(
            secrets_.non_database_private_values(graph.database, *session_values)
        ),
        "non_database_files_scanned": int(non_database["files_scanned"]),
        "non_database_plaintext_matches": int(non_database["plaintext_matches"]),
        "database_and_screenshot_flows_separated": True,
    }
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    return facts, secrets_.storage_private_values(*session_values)


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    """递归拒绝凭据/正文/路径形态字段，并在写盘前扫描全部私有值。"""

    forbidden_keys = {
        "token",
        "access_token",
        "authorization",
        "provider_key",
        "password",
        "secret",
        "signing_secret",
        "prompt",
        "response",
        "content",
        "endpoint",
        "base_url",
        "database_path",
        "database_url",
        "cache_key",
        "prompt_hash",
        "digest",
        "nonce",
        "ciphertext",
        "tags",
        "data_class",
    }

    def visit(item: object) -> None:
        """运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。 当前定义负责 visit 对应步骤。"""
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).casefold() in forbidden_keys:
                    raise AcceptanceFailure("evidence.privacy", "forbidden_key")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (TypeError, ValueError):
        raise AcceptanceFailure("evidence.shape", "not_json") from None
    require(
        all(private not in serialized for private in private_values),
        "evidence.privacy",
        "plaintext",
    )


def write_evidence(
    artifact: Path,
    facts: dict[str, object],
    privacy: dict[str, int],
    source_provenance: dict[str, str],
    private_values: tuple[str, ...],
) -> Path:
    """原子发布 exact 白名单 JSON 与单行 SHA-256，不覆盖既有批次。"""

    validate_source_provenance(source_provenance)
    require(set(facts) == SAFE_FACT_KEYS, "evidence.shape", "facts")
    evidence = {
        **facts,
        "schema_version": 1,
        "prd": "PRD-076",
        "generated_at": utc_now(),
        "all_108_prds_complete": False,
        "source_provenance": {"algorithm": "sha256", **source_provenance},
        "privacy": privacy,
        "cleanup": {
            "only_owned_processes_stopped": True,
            "fixed_ports_released": True,
            "sqlite_sidecars_remaining": int(privacy.get("sqlite_sidecars_remaining", -1)),
        },
    }
    require(set(evidence) == EVIDENCE_TOP_LEVEL_KEYS, "evidence.shape", "top_level")
    assert_safe_evidence(evidence, private_values)
    encoded = (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    artifact_parent = workspace_path(artifact.parent, ROOT, stage="evidence.path")
    target = workspace_path(artifact, artifact_parent, stage="evidence.path")
    staging = workspace_path(
        artifact_parent / f".{artifact.name}.staging",
        artifact_parent,
        stage="evidence.path",
    )
    require(
        artifact_parent.is_dir()
        and not artifact_parent.is_symlink()
        and not target.exists()
        and not staging.exists(),
        "evidence.path",
        "publish_target",
    )
    try:
        staging.mkdir()
        evidence_path = staging / "runtime-acceptance.json"
        manifest_path = staging / "SHA256SUMS.txt"
        evidence_path.write_bytes(encoded)
        persisted = evidence_path.read_bytes()
        require(persisted == encoded, "evidence.publish", "json_mismatch")
        manifest = f"{hashlib.sha256(persisted).hexdigest()}  runtime-acceptance.json\n"
        manifest_path.write_text(manifest, encoding="ascii", newline="\n")
        require(
            manifest_path.read_text(encoding="ascii") == manifest,
            "evidence.publish",
            "manifest_mismatch",
        )
        staging.replace(target)
    except AcceptanceFailure:
        raise
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.publish", "filesystem") from None
    return target / "runtime-acceptance.json"


def run(run_id: str | None = None) -> Path:
    """执行迁移往返、真实业务图、静止隐私检查和不可覆盖证据发布。"""

    actual_run_id = safe_run_id(run_id)
    source_provenance = capture_source_provenance()
    assert_ports_available()
    work, artifact = create_run_directories(actual_run_id)
    snapshot = create_migration_snapshot(work, source_provenance)
    migration = prepare_migration_database(work, snapshot)
    facts, private_values = execute_business_runtime(work, snapshot)
    require(set(facts) == SAFE_FACT_KEYS, "evidence.shape", "runtime_facts")
    facts["migration_roundtrip"] = migration
    # 全部 owner 已停止后再扫描 DB/WAL/日志；随后复核端口与 live/frozen 源码防止漂移发布。
    privacy = verify_private_storage(work, private_values)
    assert_ports_available()
    require(
        capture_source_provenance() == source_provenance,
        "evidence.provenance",
        "source_changed",
    )
    require(
        capture_source_provenance(snapshot.directory) == source_provenance,
        "evidence.provenance",
        "snapshot_changed",
    )
    validate_runtime_coverage(facts)
    return write_evidence(artifact, facts, privacy, source_provenance, private_values)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """只接受受限 run ID，不开放路径、端口或远程地址覆盖。"""

    parser = SafeArgumentParser(description="Run PRD-076 runtime acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """成功只输出证据路径；失败仅输出固定 stage/code，不回显参数。"""

    try:
        arguments = parse_arguments(argv)
        evidence = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(f"PRD-076 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    # 运行验收只记录固定阶段和脱敏计数，随机凭据与业务正文不得进入证据。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print("PRD-076 runtime acceptance failed: runtime:dependency_failure", file=sys.stderr)
        return 1
    except Exception:
        print("PRD-076 runtime acceptance failed: runtime:unexpected_failure", file=sys.stderr)
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
