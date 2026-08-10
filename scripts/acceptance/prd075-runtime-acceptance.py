"""PRD-075 在线模型实验的真实多进程运行验收器。

验收器只在仓库内创建隔离 SQLite、临时日志和脱敏证据，启动自身持有的 Control、Gateway、
Console、两个回环 Provider 与 Webhook。Provider 请求正文只在处理函数局部内存中存在，
不得保留到计数器、日志或最终证据；总体状态始终保持 ``all_108_prds_complete=false``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# 直接执行时只加入仓库源码根，禁止依赖调用方意外提供的外部 PYTHONPATH。
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
    secret_fingerprint,
    secret_prefix,
)
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402

# 所有监听地址固定为本机回环；默认服务端口让实际 Console 能连接真实 Control。
LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 8000
CONTROL_PORT = 8001
CONSOLE_PORT = 5173
PROVIDER_A_PORT = 18852
PROVIDER_B_PORT = 18853
WEBHOOK_PORT = 18854
FIXED_PORTS = (
    GATEWAY_PORT,
    CONTROL_PORT,
    CONSOLE_PORT,
    PROVIDER_A_PORT,
    PROVIDER_B_PORT,
    WEBHOOK_PORT,
)

# 临时运行树与可提交证据树都在仓库边界内，旧运行永不覆盖。
WORK_ROOT = ROOT / ".tmp" / "prd075-runtime"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd075-runtime"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = ROOT / ".tools" / "node-v22.23.2-win-arm64" / "node.exe"
MIGRATION_UP_VERSIONS = tuple(f"{version:04d}" for version in range(1, 69))
MIGRATIONS_DIR = ROOT / "infra" / "migrations"
SOURCE_PROVENANCE_NAMES = frozenset(
    {
        "runtime_acceptance",
        "migration_set",
        "migration_0068_up",
        "migration_0068_down",
    }
)
MAX_STUB_BODY_BYTES = 128 * 1024
# 生产 outbox 租约冻结为 90 秒；停机恢复验收必须覆盖完整租约并留出调度与 HTTP 投递余量。
WEBHOOK_RECOVERY_TIMEOUT_SECONDS = 120


class AcceptanceFailure(RuntimeError):
    """只携带稳定阶段和错误坐标，绝不把响应正文或凭据拼入异常。"""

    def __init__(self, stage: str, code: str) -> None:
        """只把代码内固定 stage/code 保存到异常，禁止附带运行时对象。"""

        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


class SafeArgumentParser(argparse.ArgumentParser):
    """拒绝 argparse 默认回显原参数，避免未知参数携带路径或凭据时进入 stderr。"""

    def error(self, _message: str) -> None:
        """把所有参数语法错误收敛为固定坐标，不输出 usage、参数值或解析细节。"""

        raise AcceptanceFailure("config.arguments", "invalid")


def require(condition: bool, stage: str, code: str) -> None:
    """把验收断言收敛为不含敏感上下文的稳定失败坐标。"""

    if not condition:
        # 失败信息只能使用代码内固定文本，避免 HTTP 或数据库值进入控制台输出。
        raise AcceptanceFailure(stage, code)


def utc_now() -> str:
    """生成秒级规范 UTC 时间，供安全证据和固定数据库事实使用。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def workspace_path(value: str | Path, boundary: str | Path, *, stage: str) -> Path:
    """解析仓库内路径并同时验证指定子边界，防止清理或写证据越界。"""

    try:
        candidate = Path(value).resolve()
        allowed = Path(boundary).resolve()
        candidate.relative_to(allowed)
        allowed.relative_to(ROOT.resolve())
    # 两层 relative_to 任一失败都说明目标不能被当前验收器读写或清理。
    except ValueError:
        # 路径越界时不回显调用方字符串，避免把主机目录信息带入验收证据。
        raise AcceptanceFailure(stage, "path_outside_workspace") from None
    # Windows reparse、ACL 或卷状态异常同样只允许形成固定坐标，不能回显宿主路径。
    except (OSError, RuntimeError):
        raise AcceptanceFailure(stage, "path_unavailable") from None
    return candidate


def safe_run_id(value: str | None) -> str:
    """限制运行 ID 为有界 ASCII 标识，避免路径注入和覆盖既有证据。"""

    candidate = value or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    compact = candidate.replace("-", "").replace("_", "")
    require(
        3 <= len(candidate) <= 80 and compact.isascii() and compact.isalnum(),
        "config.run_id",
        "invalid",
    )
    return candidate


def create_run_directories(run_id: str) -> tuple[Path, Path]:
    """独占创建本次临时树和证据树，禁止覆盖或复用上一轮运行结果。"""

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
        for name in ("logs", "temp", "spool"):
            (work / name).mkdir()
        return work, artifact
    except AcceptanceFailure:
        raise
    # 创建、解析或检查目录的宿主异常统一隐藏真实路径。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("config.run_directory", "filesystem") from None


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """集中持有仅限本进程内存和子进程环境的随机凭据与私有正文。"""

    admin_password: str = field(repr=False)
    viewer_password: str = field(repr=False)
    isolated_password: str = field(repr=False)
    provider_a_key: str = field(repr=False)
    provider_b_key: str = field(repr=False)
    virtual_key: str = field(repr=False)
    auth_secret: str = field(repr=False)
    provider_encryption_secret: str = field(repr=False)
    file_secret: str = field(repr=False)
    batch_secret: str = field(repr=False)
    prompt_secret: str = field(repr=False)
    cache_secret: str = field(repr=False)
    request_text: str = field(repr=False)
    provider_a_answer: str = field(repr=False)
    provider_b_answer: str = field(repr=False)

    @classmethod
    def create(cls) -> RuntimeSecrets:
        """为每次验收生成互不复用的高熵值，避免固定 fixture 掩盖明文泄露。"""

        marker = secrets.token_urlsafe(18)
        return cls(
            admin_password=f"Prd075-Admin-Aa1!{secrets.token_urlsafe(28)}",
            viewer_password=f"Prd075-Viewer-Aa1!{secrets.token_urlsafe(28)}",
            isolated_password=f"Prd075-Isolated-Aa1!{secrets.token_urlsafe(28)}",
            provider_a_key=f"sk-prd075-a-{secrets.token_urlsafe(32)}",
            provider_b_key=f"sk-prd075-b-{secrets.token_urlsafe(32)}",
            virtual_key=f"mhv_prd075_{secrets.token_urlsafe(32)}",
            auth_secret=f"auth-prd075-{secrets.token_urlsafe(48)}",
            provider_encryption_secret=f"provider-prd075-{secrets.token_urlsafe(48)}",
            file_secret=f"file-prd075-{secrets.token_urlsafe(48)}",
            batch_secret=f"batch-prd075-{secrets.token_urlsafe(48)}",
            prompt_secret=f"prompt-prd075-{secrets.token_urlsafe(48)}",
            cache_secret=f"cache-prd075-{secrets.token_urlsafe(48)}",
            request_text=f"PRD075_PRIVATE_REQUEST_{marker}",
            provider_a_answer=f"PRD075_PRIVATE_A_{secrets.token_urlsafe(20)}",
            provider_b_answer=f"PRD075_PRIVATE_B_{secrets.token_urlsafe(20)}",
        )

    def private_values(self, *session_values: str) -> tuple[str, ...]:
        """返回隐私扫描必须零命中的完整明文集合，不包含可公开本地对象 ID。"""

        return (
            self.admin_password,
            self.viewer_password,
            self.isolated_password,
            self.provider_a_key,
            self.provider_b_key,
            self.virtual_key,
            self.auth_secret,
            self.provider_encryption_secret,
            self.file_secret,
            self.batch_secret,
            self.prompt_secret,
            self.cache_secret,
            self.request_text,
            self.provider_a_answer,
            self.provider_b_answer,
            *session_values,
        )


@dataclass(frozen=True, slots=True)
class MigrationManifestEntry:
    """冻结单个迁移的文件摘要与迁移器实际使用的规范文本 checksum。"""

    version: str
    name: str
    filename: str
    source_sha256: str
    execution_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenMigrationSnapshot:
    """绑定完整 SQL 目录、68 个 up 文本和只从内存执行的 0068 down 文本。"""

    directory: Path
    migration_set_sha256: str
    up: tuple[MigrationManifestEntry, ...]
    up_sql: tuple[str, ...] = field(repr=False)
    down_0068: MigrationManifestEntry
    down_0068_sql: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeRollbackResult:
    """返回验收器实际执行的 down 版本及文本 checksum，不携带 SQL 或路径。"""

    rolled_back: str
    execution_sha256: str


@dataclass(slots=True)
class FrozenUpMigrationFile:
    """向 shared loader 暴露单个冻结 up 文件，并记录它实际取得的文本 checksum。"""

    entry: MigrationManifestEntry
    sql: str = field(repr=False)
    consumed_execution_sha256: str | None = None
    read_count: int = 0

    @property
    def name(self) -> str:
        """返回冻结文件名，供 shared loader 排除 down 文件和生成错误坐标。"""

        return self.entry.filename

    @property
    def stem(self) -> str:
        """返回不含 `.sql` 的冻结文件名，完全复刻 Path.stem 解析合同。"""

        return self.entry.filename[: -len(".sql")]

    def __lt__(self, other: FrozenUpMigrationFile) -> bool:
        """按文件名提供稳定排序，使 shared loader 保持 0001 到 0068 顺序。"""

        return self.name < other.name

    def read_text(self, *, encoding: str) -> str:
        """把冻结 up SQL 原样交给 shared，并绑定这一次实际消费。"""

        require(encoding == "utf-8", "database.migrate", "up_encoding")
        require(self.read_count == 0, "database.migrate", "up_read_count")
        self.read_count += 1
        self.consumed_execution_sha256 = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()
        return self.sql


@dataclass(slots=True)
class FrozenUpMigrationDirectory:
    """为 shared apply 提供完整的只读内存 up 集合，禁止再次读取可变路径。"""

    snapshot: FrozenMigrationSnapshot
    files: tuple[FrozenUpMigrationFile, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """要求 entry 与 SQL 一一对齐后创建本次调用独占的消费计数器。"""

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
        """冻结内存目录在本次 shared 调用生命周期内始终存在。"""

        return True

    def glob(self, pattern: str) -> tuple[FrozenUpMigrationFile, ...]:
        """只接受 shared loader 的精确 SQL 枚举合同并返回完整冻结文件集合。"""

        require(pattern == "*.sql", "database.migrate", "up_glob_pattern")
        return self.files

    def verify_consumed(self, *, stage: str) -> None:
        """要求 shared 对 68 个冻结文本各读取一次且实际 checksum 全部一致。"""

        require(
            tuple(
                (item.read_count, item.consumed_execution_sha256)
                for item in self.files
            )
            == tuple((1, entry.execution_sha256) for entry in self.snapshot.up),
            stage,
            "up_consumption_contract",
        )


@dataclass(slots=True)
class FrozenDownMigrationDirectory:
    """向 shared rollback 暴露唯一内存 down 文件，并记录实际返回文本的 checksum。"""

    snapshot: FrozenMigrationSnapshot
    requested_filename: str | None = None
    consumed_execution_sha256: str | None = None
    read_count: int = 0

    def __truediv__(self, filename: str) -> FrozenDownMigrationDirectory:
        """只接受 shared 根据数据库 head 计算出的精确 0068 down 文件名。"""

        expected = (
            f"{self.snapshot.down_0068.version}_{self.snapshot.down_0068.name}.down.sql"
        )
        require(filename == expected, "database.migrate_down", "down_filename")
        self.requested_filename = filename
        return self

    def exists(self) -> bool:
        """内存文件仅在 shared 已请求精确名称后存在。"""

        return self.requested_filename is not None

    def read_text(self, *, encoding: str) -> str:
        """把冻结 SQL 原样交给 shared，并绑定这一次实际消费。"""

        require(encoding == "utf-8", "database.migrate_down", "down_encoding")
        require(self.read_count == 0, "database.migrate_down", "down_read_count")
        sql = self.snapshot.down_0068_sql
        self.read_count += 1
        self.consumed_execution_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        return sql


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    """保存验收所需的安全本地 owner ID，不携带任何凭据或客户正文。"""

    database_url: str = field(repr=False)
    database: Path = field(repr=False)
    migration_roundtrip: dict[str, object] = field(repr=False)
    tenant_id: str
    admin_user_id: str
    project_id: str
    environment_id: str
    model_alias_id: str
    model_alias: str
    model_a_id: str
    model_b_id: str
    provider_a_id: str
    provider_b_id: str


@dataclass(slots=True)
class ManagedProcess:
    """绑定验收器实际创建的 Popen 与日志句柄，清理时不枚举其它进程。"""

    name: str
    process: subprocess.Popen[bytes]
    stdout_file: Any = field(repr=False)
    stderr_file: Any = field(repr=False)


class OwnedProcesses:
    """只终止当前验收器持有的精确进程句柄，避免影响用户已有服务。"""

    def __init__(self) -> None:
        """初始化空 owner 集合，后续只接收本实例启动的精确进程句柄。"""

        self.items: list[ManagedProcess] = []

    def add(self, item: ManagedProcess) -> ManagedProcess:
        """登记新进程并返回同一 owner，便于调用方继续等待健康状态。"""

        self.items.append(item)
        return item

    def stop(self, item: ManagedProcess) -> None:
        """停止指定 owner 并从待清理集合移除，支持 Control 重启场景。"""

        stop_owned_process(item)
        if item in self.items:
            self.items.remove(item)

    def close(self) -> None:
        """按逆启动顺序尝试停止全部遗留进程，最后再报告固定清理失败。"""

        first_failure: AcceptanceFailure | None = None
        while self.items:
            item = self.items.pop()
            try:
                stop_owned_process(item)
            # 单个 owner 的底层异常不得阻断其它 owner；异常内容也不能进入最终错误文本。
            except Exception:
                if first_failure is None:
                    first_failure = AcceptanceFailure("cleanup.owners", "failed")
        if first_failure is not None:
            raise first_failure


class FixedThreadingHTTPServer(ThreadingHTTPServer):
    """禁用端口复用并让请求线程随回环 Stub 一起退出。"""

    allow_reuse_address = False
    daemon_threads = True


def read_stub_json(handler: BaseHTTPRequestHandler) -> dict[str, object]:
    """有界读取 Stub 请求并只返回当前调用栈内的 JSON object。"""

    lengths = handler.headers.get_all("Content-Length", failobj=[])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
        # 重复或非规范长度可能制造请求走私，Stub 必须与正式边界一样拒绝。
        raise ValueError("length")
    size = int(lengths[0])
    if not 0 < size <= MAX_STUB_BODY_BYTES:
        # Stub 不接受无界正文，避免验收器本身成为内存放大入口。
        raise ValueError("length")
    body = handler.rfile.read(size)
    if len(body) != size:
        # 截断正文不能进入 Provider 计数，防止把本地传输错误冒充真实调用。
        raise ValueError("length")
    value = json.loads(body.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        # Provider 合同只接受单个 JSON object，其它顶层类型统一拒绝。
        raise ValueError("shape")
    return value


def send_json(handler: BaseHTTPRequestHandler, status: int, value: object) -> None:
    """向回环调用方发送固定 JSON，不记录 payload 或请求 Header。"""

    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


class ProviderStub:
    """独立回环 Provider，只保存调用计数、认证布尔和失败模式。"""

    def __init__(self, *, port: int, model: str, key: str, answer: str, label: str) -> None:
        """绑定单个 arm 的端口、模型和内存凭据，计数器不保存请求内容。"""

        self.port = port
        self.model = model
        self.key = key
        self.answer = answer
        self.label = label
        self.fail_enabled = False
        self._lock = threading.Lock()
        self.calls = 0
        self.failures = 0
        self.invalid = 0
        self.auth_mismatches = 0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        """启动固定端口 OpenAI-compatible Stub，并返回仅供数据库使用的回环 base URL。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """每个请求只在局部变量中解析正文，离开方法前不保留客户内容。"""

            def do_POST(self) -> None:  # noqa: N802
                """校验模型和认证后返回固定用量，失败模式只影响当前 Provider。"""

                try:
                    payload = read_stub_json(self)
                    messages = payload.get("messages")
                    valid_messages = (
                        isinstance(messages, list)
                        and bool(messages)
                        and all(isinstance(item, dict) for item in messages)
                    )
                    auth_ok = self.headers.get("Authorization") == f"Bearer {owner.key}"
                    valid = (
                        self.path == "/v1/chat/completions"
                        and payload.get("model") == owner.model
                        and payload.get("stream", False) is False
                        and valid_messages
                        and auth_ok
                    )
                    del payload, messages
                # 解析失败统一计为无效本地请求，不记录引发失败的正文或 Header。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    send_json(self, 400, {"error": {"message": "invalid request"}})
                    return
                if not valid:
                    with owner._lock:
                        owner.invalid += 1
                        # 认证不匹配只增加布尔计数，绝不能记录收到或期望的 Key。
                        if not auth_ok:
                            owner.auth_mismatches += 1
                    send_json(self, 400, {"error": {"message": "invalid request"}})
                    return
                with owner._lock:
                    owner.calls += 1
                    fail = owner.fail_enabled
                    if fail:
                        owner.failures += 1
                if fail:
                    # 可信 HTTP 503 证明 Provider 已暴露，但不应被改派到另一实验臂。
                    send_json(
                        self,
                        503,
                        {"error": {"message": "synthetic failure", "type": "server_error"}},
                    )
                    return
                send_json(
                    self,
                    200,
                    {
                        "id": f"chatcmpl_prd075_{owner.label}",
                        "object": "chat.completion",
                        "model": owner.model,
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
                """关闭标准库访问日志，避免 Header、路径或正文被意外打印。"""

                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK, self.port), Handler)
        # Stub 无权占用或终止其它进程的监听端口，绑定失败必须立即退出。
        except OSError:
            # 端口已占用时停止验收，不能复用未知用户进程冒充本次 Provider。
            raise AcceptanceFailure(f"provider.{self.label}", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"prd075-provider-{self.label}",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{self.port}/v1"

    def stop(self) -> None:
        """停止该 Stub 的精确 server/thread owner，不影响其它本机监听。"""

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        """返回不含 Key、模型正文和响应正文的安全计数投影。"""

        with self._lock:
            return {
                "calls": self.calls,
                "failures": self.failures,
                "invalid": self.invalid,
                "auth_mismatches": self.auth_mismatches,
            }


class WebhookStub:
    """验证 experiment.completed 签名与隐私，只保留计数而不保存 payload。"""

    def __init__(self) -> None:
        """初始化签名内存槽和安全计数器，不创建任何 payload 收集容器。"""

        self._lock = threading.Lock()
        self._secret: str | None = None
        self.completed = 0
        self.invalid = 0
        self.signature_failures = 0
        self.forbidden_hits = 0
        self.server: FixedThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def set_secret(self, value: str) -> None:
        """只在内存中保存 Control 首次返回的签名密钥，供回环接收端验证。"""

        require(len(value) > 32, "webhook.secret", "invalid")
        self._secret = value

    def start(self) -> str:
        """启动固定回环接收端，并返回创建 Webhook endpoint 所需 URL。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """验证一次投递后立即丢弃 JSON 与原始字节。"""

            def do_POST(self) -> None:  # noqa: N802
                """检查签名、事件类型和禁止字段，不记录任何业务 payload。"""

                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    if len(lengths) != 1 or not lengths[0].isdecimal():
                        # 非规范长度无法证明读取边界，必须在解析 JSON 前拒绝。
                        raise ValueError("length")
                    size = int(lengths[0])
                    if not 0 < size <= MAX_STUB_BODY_BYTES:
                        # 零长度或超限投递不能进入事件与签名成功计数。
                        raise ValueError("length")
                    body = self.rfile.read(size)
                    payload = json.loads(body.decode("utf-8", errors="strict"))
                    event_type = payload.get("event_type") if isinstance(payload, dict) else None
                    forbidden = contains_forbidden_field(payload)
                    signature_ok = bool(owner._secret) and verify_webhook_signature(
                        self.headers.get("X-MetaHub-Signature", ""),
                        str(owner._secret),
                        body,
                    )
                    event_header_ok = self.headers.get("X-MetaHub-Event") == event_type
                    del body, payload
                # 解析异常只能形成固定失败计数，禁止把原始 body 写入日志或响应。
                except (UnicodeError, ValueError, json.JSONDecodeError):
                    with owner._lock:
                        owner.invalid += 1
                    send_json(self, 400, {"ok": False})
                    return
                valid = (
                    event_type == "experiment.completed"
                    and signature_ok
                    and event_header_ok
                    and not forbidden
                )
                with owner._lock:
                    if event_type == "experiment.completed":
                        owner.completed += 1
                    else:
                        owner.invalid += 1
                    if not signature_ok:
                        owner.signature_failures += 1
                    if forbidden:
                        owner.forbidden_hits += 1
                send_json(self, 204 if valid else 400, {})

            def log_message(self, _format: str, *_args: object) -> None:
                """关闭访问日志，Webhook URL、签名和 payload 均不能被打印。"""

                return

        try:
            self.server = FixedThreadingHTTPServer((LOOPBACK, WEBHOOK_PORT), Handler)
        # 接收端必须由本验收器独占，绑定失败不能借用未知服务形成假阳性。
        except OSError:
            # 固定端口不可用时拒绝复用未知接收端，防止把外部投递当验收证据。
            raise AcceptanceFailure("webhook", "port_unavailable") from None
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="prd075-webhook",
            daemon=True,
        )
        self.thread.start()
        return f"http://{LOOPBACK}:{WEBHOOK_PORT}/events"

    def stop(self) -> None:
        """停止本验收器创建的回环接收端和请求线程。"""

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def snapshot(self) -> dict[str, int]:
        """投影签名、事件和隐私验证计数，不返回 signing secret。"""

        with self._lock:
            return {
                "completed": self.completed,
                "invalid": self.invalid,
                "signature_failures": self.signature_failures,
                "forbidden_hits": self.forbidden_hits,
            }


def contains_forbidden_field(value: object) -> bool:
    """递归拒绝 Webhook 出现凭据、正文、Endpoint、bucket 或内部租约字段。"""

    forbidden = {
        "prompt",
        "response",
        "content",
        "authorization",
        "provider_key",
        "access_token",
        "endpoint",
        "base_url",
        "bucket",
        "lease_token",
        "sampling_key_fingerprint",
        "route_config_fingerprint",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower() in forbidden or contains_forbidden_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_forbidden_field(item) for item in value)
    return False


def assert_ports_available() -> None:
    """在启动前独占探测所有固定端口，避免杀进程或复用用户已有服务。"""

    sockets: list[socket.socket] = []
    try:
        for port in FIXED_PORTS:
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                candidate.bind((LOOPBACK, port))
            # 探测只负责证明端口空闲，不枚举或停止当前监听者。
            except OSError:
                candidate.close()
                # 任何固定端口被占用都必须停止，由用户决定何时重新运行。
                raise AcceptanceFailure("config.ports", "fixed_port_unavailable") from None
            sockets.append(candidate)
    finally:
        for candidate in sockets:
            candidate.close()


def database_url(path: Path) -> str:
    """把仓库内绝对 SQLite 路径编码为项目连接层使用的 URL。"""

    return f"sqlite:///{path.as_posix()}"


def migration_health(
    url: str,
    *,
    expected_count: int,
    expected_head: str,
    stage: str,
) -> dict[str, object]:
    """读取一个迁移阶段的有界健康摘要，不携带数据库路径、SQL 或业务事实。"""

    try:
        connection = connect_database(url)
    # 连接失败只映射固定证据阶段，不回显数据库 URL、驱动文本或本机路径。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "connect_failed") from None
    try:
        migration = connection.execute(
            """
            SELECT COUNT(*) AS count,MAX(version) AS latest
            FROM schema_migrations WHERE status='applied'
            """
        ).fetchone()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    # 迁移证据只允许暴露固定阶段码，不能把 SQLite 文本或本机路径写入终端和 JSON。
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "health_query_failed") from None
    finally:
        try:
            connection.close()
        # 关闭连接失败也不能把 SQLite 文件位置带入终端。
        except sqlite3.Error:
            raise AcceptanceFailure(stage, "close_failed") from None
    summary = {
        "migration_count": int(migration["count"]),
        "latest_migration": str(migration["latest"]),
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
    }
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
    """复核快照目录未漂移；实际消费证明仍以数据库 checksum 和内存 down 文本为准。"""

    try:
        current = capture_source_provenance(snapshot.directory)
    except AcceptanceFailure:
        raise AcceptanceFailure(stage, "snapshot_unavailable") from None
    require(
        current["migration_set"] == snapshot.migration_set_sha256
        and current["migration_0068_up"] == snapshot.up[-1].source_sha256
        and current["migration_0068_down"] == snapshot.down_0068.source_sha256,
        stage,
        "snapshot_changed",
    )


def verify_migration_execution_checksums(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
    down_0068: bool = False,
) -> None:
    """用 schema_migrations 回证 shared 迁移器实际加载并执行的每个 up 文本。"""

    try:
        connection = connect_database(database_url_)
    # checksum 回证连接失败只暴露当前固定阶段。
    except (DbError, OSError, UnicodeError):
        raise AcceptanceFailure(stage, "connect_failed") from None
    try:
        rows = connection.execute(
            """
            SELECT version,name,checksum,status
            FROM schema_migrations ORDER BY version
            """
        ).fetchall()
        actual = tuple(
            (
                str(row["version"]),
                str(row["name"]),
                str(row["checksum"]),
                str(row["status"]),
            )
            for row in rows
        )
        expected = tuple(
            (
                entry.version,
                entry.name,
                entry.execution_sha256,
                "rolled_back" if down_0068 and entry.version == "0068" else "applied",
            )
            for entry in snapshot.up
        )
        require(actual == expected, stage, "execution_checksum_contract")
    except AcceptanceFailure:
        raise
    # 回证查询的驱动、文件系统和类型异常均不得附带数据库路径。
    except (sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure(stage, "checksum_query_failed") from None
    finally:
        try:
            connection.close()
        # 回证连接必须关闭，关闭异常仍只返回固定坐标。
        except sqlite3.Error:
            raise AcceptanceFailure(stage, "close_failed") from None


def apply_runtime_migrations(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
):
    """让 shared 执行冻结内存 up 文本，并以消费计数和数据库 checksum 双重回证。"""

    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_before")
    frozen_directory = FrozenUpMigrationDirectory(snapshot)
    try:
        result = apply_migrations(
            database_url_,
            migrations_dir=frozen_directory,  # type: ignore[arg-type]
        )
    except AcceptanceFailure:
        raise
    # shared loader 的编码、路径和迁移错误统一收敛，禁止打印原始异常。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError):
        raise AcceptanceFailure(stage, "failed") from None
    # 内存目录记录 shared 的每次 read_text；数据库 checksum 再独立回证实际执行终态。
    frozen_directory.verify_consumed(stage=f"{stage}.consumed")
    verify_migration_execution_checksums(database_url_, snapshot, stage=f"{stage}.consumed")
    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_after")
    return result


def rollback_runtime_migration(
    database_url_: str,
    snapshot: FrozenMigrationSnapshot,
    *,
    stage: str,
) -> RuntimeRollbackResult:
    """只执行冻结清单持有的 0068 down 内存文本，并返回该实际文本 checksum。"""

    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_before")
    verify_migration_execution_checksums(
        database_url_,
        snapshot,
        stage=f"{stage}.head",
    )
    frozen_directory = FrozenDownMigrationDirectory(snapshot)
    try:
        result = rollback_last_migration(
            database_url_,
            migrations_dir=frozen_directory,  # type: ignore[arg-type]
        )
    except AcceptanceFailure:
        raise
    # shared rollback 的所有依赖异常必须隐藏 SQL、数据库和宿主路径。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        raise AcceptanceFailure(stage, "failed") from None
    consumed_checksum = frozen_directory.consumed_execution_sha256
    require(
        result.rolled_back == "0068"
        and frozen_directory.read_count == 1
        and consumed_checksum == snapshot.down_0068.execution_sha256,
        stage,
        "down_consumption_contract",
    )
    verify_migration_execution_checksums(
        database_url_,
        snapshot,
        stage=f"{stage}.consumed",
        down_0068=True,
    )
    verify_snapshot_directory(snapshot, stage=f"{stage}.snapshot_after")
    return RuntimeRollbackResult(
        rolled_back="0068",
        execution_sha256=str(consumed_checksum),
    )


def prepare_database(
    work: Path,
    secrets_: RuntimeSecrets,
    provider_a_url: str,
    provider_b_url: str,
    migration_snapshot: FrozenMigrationSnapshot,
) -> RuntimeGraph:
    """从空库迁移到 0068，两次应用后创建双 Provider、双租户和 RBAC 验收图。"""

    database = workspace_path(work / "prd075-runtime.sqlite3", work, stage="database.path")
    url = database_url(database)
    # Seed 只在本进程内读取随机值；随后凭据会升级为可解密 AES-GCM 密文。
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = secrets_.admin_password
    os.environ["METAHUB_SEED_PROVIDER_API_KEY"] = secrets_.provider_a_key
    os.environ["METAHUB_SEED_VIRTUAL_KEY"] = secrets_.virtual_key
    os.environ["METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL"] = provider_a_url
    first = apply_runtime_migrations(
        url,
        migration_snapshot,
        stage="database.migrate",
    )
    require(
        tuple(first.applied) == MIGRATION_UP_VERSIONS,
        "database.migrate",
        "contract",
    )
    fresh_health = migration_health(
        url,
        expected_count=68,
        expected_head="0068",
        stage="database.migrate_fresh_health",
    )
    second = apply_runtime_migrations(
        url,
        migration_snapshot,
        stage="database.migrate_repeat",
    )
    require(
        second.applied == [] and tuple(second.skipped) == MIGRATION_UP_VERSIONS,
        "database.migrate_repeat",
        "not_idempotent",
    )
    repeat_health = migration_health(
        url,
        expected_count=68,
        expected_head="0068",
        stage="database.migrate_repeat_health",
    )
    rolled_back = rollback_runtime_migration(
        url,
        migration_snapshot,
        stage="database.migrate_down",
    )
    require(
        rolled_back.rolled_back == "0068"
        and rolled_back.execution_sha256
        == migration_snapshot.down_0068.execution_sha256,
        "database.migrate_down",
        "contract",
    )
    down_health = migration_health(
        url,
        expected_count=67,
        expected_head="0067",
        stage="database.migrate_down_health",
    )
    reapplied = apply_runtime_migrations(
        url,
        migration_snapshot,
        stage="database.migrate_reup",
    )
    require(
        reapplied.applied == ["0068"],
        "database.migrate_reup",
        "contract",
    )
    reup_health = migration_health(
        url,
        expected_count=68,
        expected_head="0068",
        stage="database.migrate_reup_health",
    )
    migration_roundtrip = {
        "fresh_up": fresh_health,
        "repeat_noop": {**repeat_health, "applied_count": 0, "skipped_count": 68},
        "down": {**down_health, "rolled_back": "0068"},
        "re_up": {**reup_health, "applied": ["0068"]},
    }
    try:
        seed = apply_seed_data(url)
        connection = connect_database(url)
    # Seed 文件、编码或连接异常不得绕过统一验收错误坐标。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, TypeError):
        raise AcceptanceFailure("database.seed", "failed") from None
    database_stage = "owner_load"
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
        require(owner is not None, "database.seed", "owner_missing")
        now = utc_now()
        graph = RuntimeGraph(
            database_url=url,
            database=database,
            migration_roundtrip=migration_roundtrip,
            tenant_id=seed.tenant_id,
            admin_user_id=str(owner["user_id"]),
            project_id="project_prd075_runtime",
            environment_id=str(owner["environment_id"]),
            model_alias_id="alias_prd075_runtime",
            model_alias="prd075-runtime-ab",
            model_a_id=str(owner["model_id"]),
            model_b_id="model_prd075_runtime_b",
            provider_a_id=str(owner["provider_id"]),
            provider_b_id="provider_prd075_runtime_b",
        )
        # 默认 Provider 与 Model A 被固定到第一个回环端点和非零 CNY 价格。
        database_stage = "provider_a"
        connection.execute(
            """
            UPDATE providers
            SET base_url=?,health_status='healthy',health_updated_at=?,updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (provider_a_url, now, now, graph.tenant_id, graph.provider_a_id),
        )
        connection.execute(
            """
            UPDATE models
            SET upstream_model_name='prd075-runtime-a',provider_model_id='prd075-runtime-a',
                input_price_per_1k=0.10,output_price_per_1k=0.30,
                currency='CNY',price_schema_version='prd075-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, graph.tenant_id, graph.model_a_id),
        )
        encrypted_a = encrypt_provider_key(
            secrets_.provider_a_key,
            encryption_secret=secrets_.provider_encryption_secret,
        )
        database_stage = "credential_a"
        connection.execute(
            """
            UPDATE provider_credentials
            SET encrypted_key=?,key_fingerprint=?,name='runtime-a',key_prefix=?,weight=100,
                probe_health_status='unknown',updated_at=?
            WHERE tenant_id=? AND provider_id=?
            """,
            (
                encrypted_a,
                secret_fingerprint(secrets_.provider_a_key),
                secret_prefix(secrets_.provider_a_key),
                now,
                graph.tenant_id,
                graph.provider_a_id,
            ),
        )
        # Provider B 使用独立 owner、独立 Key 和独立回环 Endpoint，不能共享 A 臂凭据。
        database_stage = "provider_b"
        connection.execute(
            """
            INSERT INTO providers(
              provider_id,tenant_id,provider_code,name,base_url,status,provider_type,auth_type,
              description,health_status,created_by,health_updated_at,created_at,updated_at
            ) VALUES(?,?,'local_openai_b','PRD075 runtime B',?,'active','openai_compatible',
                     'bearer','isolated runtime provider','healthy',?,?,?,?)
            """,
            (
                graph.provider_b_id,
                graph.tenant_id,
                provider_b_url,
                graph.admin_user_id,
                now,
                now,
                now,
            ),
        )
        encrypted_b = encrypt_provider_key(
            secrets_.provider_b_key,
            encryption_secret=secrets_.provider_encryption_secret,
        )
        database_stage = "credential_b"
        connection.execute(
            """
            INSERT INTO provider_credentials(
              provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,
              name,key_prefix,weight,status,created_by,created_at,updated_at
            ) VALUES('pcred_prd075_runtime_b',?,?,?,?, 'runtime-b',?,100,'active',?,?,?)
            """,
            (
                graph.tenant_id,
                graph.provider_b_id,
                secret_fingerprint(secrets_.provider_b_key),
                encrypted_b,
                secret_prefix(secrets_.provider_b_key),
                graph.admin_user_id,
                now,
                now,
            ),
        )
        database_stage = "model_b"
        connection.execute(
            """
            INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,context_window,
              input_price_per_1k,output_price_per_1k,currency,price_schema_version,
              status,created_by,created_at,updated_at
            ) VALUES(?,?,?,'prd075-runtime-b','prd075-runtime-b','prd075-runtime-b',
                     'PRD075 runtime B','{"chat":true,"stream":true}',8192,
                     0.20,0.40,'CNY','prd075-runtime-v1','active',?,?,?)
            """,
            (
                graph.model_b_id,
                graph.tenant_id,
                graph.provider_b_id,
                graph.admin_user_id,
                now,
                now,
            ),
        )
        database_stage = "project"
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,description,owner_user_id,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'prd075-runtime','PRD075 runtime',NULL,?,'active',?,?,?,?)
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
        database_stage = "alias"
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,default_model_id,fallback_model_id,
              status,description,created_by,created_at,updated_at,environment_id
            ) VALUES(?,?,?,?,?,NULL,'active','PRD075 runtime exact alias',?,?,?,?)
            """,
            (
                graph.model_alias_id,
                graph.tenant_id,
                graph.model_alias,
                str(owner["code"]),
                graph.model_a_id,
                graph.admin_user_id,
                now,
                now,
                graph.environment_id,
            ),
        )
        database_stage = "virtual_key"
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (graph.project_id, now, graph.tenant_id, str(owner["virtual_key_id"])),
        )
        # Viewer 用于证明前端入口之外仍由后端拒绝 experiment.read。
        database_stage = "viewer"
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd075_viewer',?,'viewer-prd075@local.invalid','Runtime viewer',
                     ?,'viewer','active',?,?)
            """,
            (graph.tenant_id, hash_password(secrets_.viewer_password), now, now),
        )
        # 第二租户由 migration initializer 获得完整 revision-11 目录，随后用于同形 404 验证。
        database_stage = "isolated_tenant"
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES(
              'tenant_prd075_isolated','prd075-isolated','PRD075 isolated',
              'internal_only','active',?,?
            )
            """,
            (now, now),
        )
        database_stage = "isolated_user"
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd075_isolated','tenant_prd075_isolated',
                     'admin-prd075@isolated.invalid','Runtime isolated',?,'admin','active',?,?)
            """,
            (hash_password(secrets_.isolated_password), now, now),
        )
        database_stage = "commit"
        connection.commit()
        catalog = connection.execute(
            """
            SELECT COUNT(*) AS permission_count,MIN(catalog_revision) AS minimum_revision,
                   MAX(catalog_revision) AS maximum_revision
            FROM permissions WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        require(
            tuple(catalog) == (66, 11, 11),
            "database.rbac",
            "catalog_contract",
        )
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "database.integrity",
            "failed",
        )
        require(
            connection.execute("PRAGMA foreign_key_check").fetchall() == [],
            "database.foreign_keys",
            "violations",
        )
        return graph
    # SQLite 或显式验收失败时回滚整张 owner 图，禁止留下可被后续运行误用的半成品。
    except AcceptanceFailure:
        connection.rollback()
        raise
    # SQL 组装或 trigger 拒绝统一转换为固定坐标，不能把表结构和运行值打印到正式输出。
    except sqlite3.Error:
        connection.rollback()
        raise AcceptanceFailure(
            "database.prepare",
            f"sql_failed_{database_stage}",
        ) from None
    finally:
        connection.close()


def service_environment(
    work: Path,
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
) -> dict[str, str]:
    """构造最小子进程环境，并把全部可写路径和秘密生命周期限制在本次运行。"""

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
            "METAHUB_ENV": "dev",
            "METAHUB_LOG_LEVEL": "ERROR",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_AUTH_TOKEN_SECRET": secrets_.auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": secrets_.provider_encryption_secret,
            "METAHUB_FILE_ENCRYPTION_SECRET": secrets_.file_secret,
            "METAHUB_BATCH_ENCRYPTION_SECRET": secrets_.batch_secret,
            "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": secrets_.prompt_secret,
            "METAHUB_CACHE_AFFINITY_HMAC_SECRET": secrets_.cache_secret,
            "METAHUB_SEED_VIRTUAL_KEY": secrets_.virtual_key,
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(work / "spool"),
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
            "METAHUB_EXPERIMENT_SCHEDULER_INTERVAL_SECONDS": "1",
            "METAHUB_EXPERIMENT_FINALIZER_LEASE_SECONDS": "30",
            "METAHUB_EXPERIMENT_EXECUTION_LEASE_SECONDS": "30",
            "METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS": "5",
            "METAHUB_EXPERIMENT_SCHEDULER_BATCH_LIMIT": "20",
            "METAHUB_EXPERIMENT_EXECUTION_MAX_SECONDS": "60",
            "METAHUB_EXPERIMENT_SETTLEMENT_GRACE_SECONDS": "60",
            "METAHUB_CONSOLE_PORT": str(CONSOLE_PORT),
        }
    )
    return environment


def start_process(
    name: str,
    command: list[str],
    *,
    work: Path,
    environment: dict[str, str],
) -> ManagedProcess:
    """启动一个仓库内可执行文件，并把 stdout/stderr 限定到本次临时目录。"""

    logs = workspace_path(work / "logs", work, stage=f"process.{name}.logs")
    stdout_path = workspace_path(logs / f"{name}.stdout.log", logs, stage=f"process.{name}.stdout")
    stderr_path = workspace_path(logs / f"{name}.stderr.log", logs, stage=f"process.{name}.stderr")
    stdout_file = stdout_path.open("xb")
    stderr_file = stderr_path.open("xb")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    # 子进程启动异常不回显 command/env，因为二者可能间接暴露部署配置。
    except OSError:
        stdout_file.close()
        stderr_file.close()
        # 启动失败只暴露稳定进程名，不打印可能含环境敏感值的完整命令或环境。
        raise AcceptanceFailure(f"process.{name}", "start_failed") from None
    return ManagedProcess(name, process, stdout_file, stderr_file)


def start_service(
    name: str,
    app: str,
    port: int,
    *,
    work: Path,
    environment: dict[str, str],
) -> ManagedProcess:
    """通过仓库虚拟环境启动指定 ASGI 应用，关闭访问日志避免记录 Header。"""

    require(PYTHON.is_file(), f"process.{name}", "python_missing")
    return start_process(
        name,
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            app,
            "--host",
            LOOPBACK,
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "error",
        ],
        work=work,
        environment=environment,
    )


def start_console(*, work: Path, environment: dict[str, str]) -> ManagedProcess:
    """使用仓库固定 Node 运行真实 Console 静态开发服务器。"""

    require(NODE.is_file(), "process.console", "node_missing")
    return start_process(
        "console",
        [str(NODE), str(ROOT / "console-web" / "scripts" / "dev-server.mjs")],
        work=work,
        environment=environment,
    )


def stop_owned_process(item: ManagedProcess) -> None:
    """有界停止精确 Popen；进程操作失败时仍分别关闭两个日志句柄。"""

    process_stopped = False
    stream_close_failed = False
    try:
        try:
            process_stopped = item.process.poll() is not None
        # poll 本身失败时仍继续尝试 terminate/kill，不能直接跳过日志句柄清理。
        except Exception:
            process_stopped = False
        if not process_stopped:
            try:
                item.process.terminate()
            # terminate 的竞态或宿主错误不结束清理；wait 可证明进程是否已经自行退出。
            except Exception:
                pass
            try:
                item.process.wait(timeout=10)
                process_stopped = True
            except subprocess.TimeoutExpired:
                process_stopped = False
            except Exception:
                process_stopped = False
        if not process_stopped:
            # 温和退出未被证明后，只对当前精确 owner 尝试强制终止并再次有界等待。
            try:
                item.process.kill()
            except Exception:
                pass
            try:
                item.process.wait(timeout=5)
                process_stopped = True
            except Exception:
                process_stopped = False
    finally:
        # 两个句柄必须独立尝试关闭；stdout 失败不能阻断 stderr，反之亦然。
        for stream in (item.stdout_file, item.stderr_file):
            try:
                if not stream.closed:
                    stream.close()
            except Exception:
                stream_close_failed = True
    if not process_stopped or stream_close_failed:
        raise AcceptanceFailure("cleanup.owners", "failed")


def stop_runtime_owners(
    processes: OwnedProcesses,
    webhook: WebhookStub,
    provider_b: ProviderStub,
    provider_a: ProviderStub,
) -> None:
    """尝试关闭本次运行的全部 owner，保留首个固定失败并在全部尝试后抛出。"""

    first_failure: AcceptanceFailure | None = None
    # 顺序与原有生命周期相同：先停子进程，再停接收端，最后逆序停两个 Provider。
    for owner, method_name in (
        (processes, "close"),
        (webhook, "stop"),
        (provider_b, "stop"),
        (provider_a, "stop"),
    ):
        try:
            getattr(owner, method_name)()
        # 不保留底层异常文本；即使首个 owner 失败，也必须继续释放后续端口和线程。
        except Exception:
            if first_failure is None:
                first_failure = AcceptanceFailure("cleanup.owners", "failed")
    if first_failure is not None:
        raise first_failure


def wait_health(url: str, process: ManagedProcess, *, stage: str) -> None:
    """等待精确服务 owner 健康，进程提前退出或 30 秒超时即失败。"""

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            # 只报告固定阶段，不读取可能包含敏感上下文的服务 stderr。
            raise AcceptanceFailure(stage, "process_exited")
        try:
            response = httpx.get(url, timeout=1, follow_redirects=False, trust_env=False)
            if response.status_code == 200:
                return
        # 健康探测失败属于预期重试状态，不输出 URL 之外的网络异常细节。
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "health_timeout")


def request_headers(
    token: str,
    request_id: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    """构造仅供当前 HTTP 调用使用的认证头，调用方不得打印返回字典。"""

    result = {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    if idempotency_key is not None:
        # 幂等键只存在于请求内存，不进入证据或日志投影。
        result["Idempotency-Key"] = idempotency_key
    return result


def login(client: httpx.Client, slug: str, email: str, password: str, request_id: str) -> str:
    """通过真实 Control 登录，并只返回内存 Session Token。"""

    response = client.post(
        "/auth/login",
        json={"tenant_slug": slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    require(response.status_code == 200, "http.login", "status")
    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    require(isinstance(token, str) and len(token) > 40, "http.login", "token_missing")
    # Session Token 只交给本次调用链内存，最终隐私扫描会验证其未落盘。
    return token


def create_webhook(client: httpx.Client, token: str, url: str, stub: WebhookStub) -> str:
    """创建只订阅 experiment.completed 的真实 endpoint，并把首次密钥交给 Stub。"""

    response = client.post(
        "/webhooks",
        headers=request_headers(token, "prd075-runtime-webhook-create"),
        json={
            "name": "PRD075 runtime receiver",
            "endpoint_url": url,
            "event_types": ["experiment.completed"],
            "status": "active",
            "timeout_ms": 2000,
        },
    )
    require(response.status_code == 200, "http.webhook_create", "status")
    payload = response.json()
    signing_secret = payload.get("signing_secret") if isinstance(payload, dict) else None
    require(isinstance(signing_secret, str), "http.webhook_create", "secret_missing")
    stub.set_secret(signing_secret)
    # 首次签名密钥只进入 Stub 内存和最终零命中扫描集合，不写入任何事实投影。
    return signing_secret


def create_experiment(
    client: httpx.Client,
    token: str,
    graph: RuntimeGraph,
    *,
    marker: int,
    target: int,
) -> str:
    """通过真实五权限后端创建不可变实验，并返回安全本地 Experiment ID。"""

    response = client.post(
        "/experiments",
        headers=request_headers(
            token,
            f"prd075-runtime-create-{marker}",
            idempotency_key=f"75000000-0000-4000-8000-{marker:012d}",
        ),
        json={
            "name": f"PRD075 runtime experiment {marker}",
            "project_id": graph.project_id,
            "environment_id": graph.environment_id,
            "model_alias_id": graph.model_alias_id,
            "model_a_id": graph.model_a_id,
            "model_b_id": graph.model_b_id,
            "target_sample_count": target,
            "traffic_sample_bps": 10_000,
            "model_a_allocation_bps": 5_000,
            "sampling_duration_seconds": 60,
            "feedback_collection_enabled": False,
            "feedback_window_seconds": 0,
        },
    )
    require(response.status_code == 201, "http.experiment_create", "status")
    payload = response.json()
    experiment = payload.get("experiment") if isinstance(payload, dict) else None
    experiment_id = experiment.get("experiment_id") if isinstance(experiment, dict) else None
    require(isinstance(experiment_id, str), "http.experiment_create", "id_missing")
    return experiment_id


def invoke_gateway(
    graph: RuntimeGraph,
    secrets_: RuntimeSecrets,
    request_id: str,
    *,
    expect_success: bool | None,
) -> int:
    """发送一个真实公开 Chat 请求，验证安全响应后立即丢弃正文。"""

    with httpx.Client(
        base_url=f"http://{LOOPBACK}:{GATEWAY_PORT}",
        timeout=httpx.Timeout(20, connect=3),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {secrets_.virtual_key}",
                "X-Request-ID": request_id,
            },
            json={
                "model": graph.model_alias,
                "messages": [{"role": "user", "content": secrets_.request_text}],
            },
        )
    require(response.headers.get("x-request-id") == request_id, "http.gateway", "request_id")
    actual_success = response.status_code == 200
    if expect_success is not None:
        require(actual_success is expect_success, "http.gateway", "unexpected_status_class")
    if actual_success:
        require(response.status_code == 200, "http.gateway", "success_status")
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices else None
        answer = message.get("content") if isinstance(message, dict) else None
        require(
            answer in {secrets_.provider_a_answer, secrets_.provider_b_answer},
            "http.gateway",
            "success_contract",
        )
        del payload, choices, message, answer
    else:
        require(response.status_code >= 400, "http.gateway", "failure_status")
        payload = response.json()
        require(
            isinstance(payload, dict)
            and payload.get("request_id") == request_id
            and isinstance(payload.get("code"), str),
            "http.gateway",
            "failure_contract",
        )
        del payload
    return response.status_code


def wait_final_result(
    client: httpx.Client,
    token: str,
    experiment_id: str,
    marker: int,
) -> dict[str, object]:
    """轮询后端权威结果直到冻结，禁止从数据库自行拼装公开统计。"""

    deadline = time.monotonic() + 45
    request_marker = f"prd075-runtime-results-{marker}"
    while time.monotonic() < deadline:
        response = client.get(
            f"/experiments/{experiment_id}/results",
            headers=request_headers(token, request_marker),
        )
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("is_final") is True:
                return payload
        elif response.status_code == 503:
            # 250ms 单并发统计门禁允许安全重试，但必须保持统一错误码和当前 request_id。
            payload = response.json()
            require(
                isinstance(payload, dict)
                and payload.get("code") == "experiment.statistics_unavailable"
                and payload.get("request_id") == request_marker,
                "http.results",
                "retry_contract",
            )
        else:
            # 权限、租户或路由错误不属于统计瞬时不可用，不能被轮询掩盖。
            raise AcceptanceFailure("http.results", "unexpected_status")
        time.sleep(0.25)
    raise AcceptanceFailure("http.results", "finalization_timeout")


def wait_webhooks(stub: WebhookStub, minimum: int) -> dict[str, int]:
    """等待可靠 outbox 达到目标事件数，并返回安全计数快照。"""

    deadline = time.monotonic() + WEBHOOK_RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        snapshot = stub.snapshot()
        if snapshot["completed"] >= minimum:
            return snapshot
        time.sleep(0.2)
    raise AcceptanceFailure("webhook.delivery", "timeout")


def database_summary(graph: RuntimeGraph) -> dict[str, object]:
    """从最终数据库读取有界计数、迁移版本和完整性，不投影请求级或敏感字段。"""

    connection = connect_database(graph.database_url)
    try:
        migration = connection.execute(
            "SELECT COUNT(*) AS count,MAX(version) AS latest FROM schema_migrations"
        ).fetchone()
        experiment = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   SUM(status='completed') AS completed,
                   SUM(assigned_count) AS assigned,
                   SUM(model_a_assigned_count) AS model_a,
                   SUM(model_b_assigned_count) AS model_b
            FROM model_experiments WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        outcome = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   SUM(gateway_status='success') AS succeeded,
                   SUM(gateway_status='failed') AS failed,
                   SUM(gateway_status='unresolved') AS unresolved
            FROM experiment_request_outcomes WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        settlement = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   SUM(status='recorded') AS recorded,
                   SUM(status='pending') AS pending,
                   SUM(status='unavailable') AS unavailable
            FROM experiment_usage_settlements WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        outbox_pending = connection.execute(
            """
            SELECT COUNT(*) FROM experiment_event_outbox
            WHERE tenant_id=? AND dispatch_status!='dispatched'
            """,
            (graph.tenant_id,),
        ).fetchone()[0]
        summary = {
            "migration_count": int(migration["count"]),
            "latest_migration": str(migration["latest"]),
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
            "permission_count": int(
                connection.execute(
                    "SELECT COUNT(*) FROM permissions WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "experiments": {key: int(experiment[key] or 0) for key in experiment.keys()},
            "outcomes": {key: int(outcome[key] or 0) for key in outcome.keys()},
            "settlements": {key: int(settlement[key] or 0) for key in settlement.keys()},
            "result_rows": int(
                connection.execute(
                    "SELECT COUNT(*) FROM experiment_results WHERE tenant_id=?",
                    (graph.tenant_id,),
                ).fetchone()[0]
            ),
            "outbox_pending": int(outbox_pending),
            "migration_roundtrip": graph.migration_roundtrip,
        }
        require(
            summary["migration_count"] == 68
            and summary["latest_migration"] == "0068"
            and summary["integrity_check"] == "ok"
            and summary["foreign_key_violations"] == 0,
            "database.summary",
            "migration_or_integrity",
        )
        return summary
    finally:
        connection.close()


def exercise_runtime(
    work: Path,
    graph: RuntimeGraph,
    environment: dict[str, str],
    secrets_: RuntimeSecrets,
    provider_a: ProviderStub,
    provider_b: ProviderStub,
    webhook: WebhookStub,
    webhook_url: str,
    processes: OwnedProcesses,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """贯通三轮实验、权限隔离、Control 停机恢复、Console 和最终冻结结果。"""

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
            "prd075-runtime-login-admin",
        )
        viewer_token = login(
            client,
            "default",
            "viewer-prd075@local.invalid",
            secrets_.viewer_password,
            "prd075-runtime-login-viewer",
        )
        isolated_token = login(
            client,
            "prd075-isolated",
            "admin-prd075@isolated.invalid",
            secrets_.isolated_password,
            "prd075-runtime-login-isolated",
        )
        webhook_secret = create_webhook(client, admin_token, webhook_url, webhook)
        navigation = client.get(
            "/console/navigation?active_item_id=experiments",
            headers=request_headers(admin_token, "prd075-runtime-navigation"),
        )
        require(navigation.status_code == 200, "http.navigation", "status")
        nav_payload = navigation.json()
        nav_items = (
            nav_payload.get("navigation_items")
            if isinstance(nav_payload, dict)
            else None
        )
        require(
            isinstance(nav_items, list)
            and any(
                item.get("id") == "experiments"
                for item in nav_items
                if isinstance(item, dict)
            ),
            "http.navigation",
            "experiment_missing",
        )

        first_id = create_experiment(client, admin_token, graph, marker=1, target=4)
        for sequence in range(1, 5):
            invoke_gateway(
                graph,
                secrets_,
                f"prd075-runtime-main-{sequence}",
                expect_success=True,
            )
        first = wait_final_result(client, admin_token, first_id, 1)
        first_projection = json.dumps(first.get("results"), sort_keys=True, separators=(",", ":"))
        # completed 快照仍受 250ms 统计门禁约束；只重试精确 503 后再证明冻结结果不漂移。
        repeated = wait_final_result(client, admin_token, first_id, 101)
        require(
            json.dumps(repeated.get("results"), sort_keys=True, separators=(",", ":"))
            == first_projection,
            "http.results_repeat",
            "frozen_drift",
        )

        provider_b.fail_enabled = True
        second_id = create_experiment(client, admin_token, graph, marker=2, target=2)
        second_statuses = [
            invoke_gateway(
                graph,
                secrets_,
                f"prd075-runtime-failure-{sequence}",
                expect_success=None,
            )
            for sequence in range(1, 3)
        ]
        provider_b.fail_enabled = False
        # HMAC 偏好顺序可能变化，但精确配额保证两次调用中只有一个成功和一个失败。
        if sum(status == 200 for status in second_statuses) != 1:
            raise AcceptanceFailure("http.failure_experiment", "status_distribution")
        second = wait_final_result(client, admin_token, second_id, 2)

        third_id = create_experiment(client, admin_token, graph, marker=3, target=2)
        processes.stop(control)
        invoke_gateway(graph, secrets_, "prd075-runtime-recovery-1", expect_success=True)
        invoke_gateway(graph, secrets_, "prd075-runtime-recovery-2", expect_success=True)
        # Control 已停止时 Gateway 只能提交事实，不能由未知进程完成或投递实验。
        completed_before_restart = webhook.snapshot()["completed"]
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
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as recovered_client:
            third = wait_final_result(recovered_client, admin_token, third_id, 3)
            cross_tenant = recovered_client.get(
                f"/experiments/{first_id}",
                headers=request_headers(isolated_token, "prd075-runtime-cross-tenant"),
            )
            require(
                cross_tenant.status_code == 404
                and cross_tenant.json().get("code") == "experiment.not_found"
                and cross_tenant.json().get("request_id") == "prd075-runtime-cross-tenant",
                "http.cross_tenant",
                "contract",
            )
            denied = recovered_client.get(
                "/experiments",
                headers=request_headers(viewer_token, "prd075-runtime-viewer-denied"),
            )
            require(
                denied.status_code == 403
                and denied.json().get("code") == "auth.permission_denied"
                and denied.json().get("request_id") == "prd075-runtime-viewer-denied",
                "http.viewer",
                "contract",
            )
        webhook_fact = wait_webhooks(webhook, 3)
        require(
            completed_before_restart < webhook_fact["completed"]
            and webhook_fact["completed"] == 3
            and webhook_fact["invalid"] == 0
            and webhook_fact["signature_failures"] == 0
            and webhook_fact["forbidden_hits"] == 0,
            "webhook.final",
            "contract",
        )

        console_index = httpx.get(
            f"http://{LOOPBACK}:{CONSOLE_PORT}/",
            timeout=3,
            follow_redirects=False,
            trust_env=False,
        )
        console_feature = httpx.get(
            f"http://{LOOPBACK}:{CONSOLE_PORT}/src/features/experiments/renderExperiments.js",
            timeout=3,
            follow_redirects=False,
            trust_env=False,
        )
        require(
            console_index.status_code == 200
            and 'id="app"' in console_index.text
            and console_feature.status_code == 200
            and "renderExperiments" in console_feature.text,
            "http.console",
            "feature_unavailable",
        )
        provider_a_fact = provider_a.snapshot()
        provider_b_fact = provider_b.snapshot()
        require(
            provider_a_fact == {
                "calls": 4,
                "failures": 0,
                "invalid": 0,
                "auth_mismatches": 0,
            }
            and provider_b_fact == {
                "calls": 4,
                "failures": 1,
                "invalid": 0,
                "auth_mismatches": 0,
            },
            "provider.final",
            "contract",
        )
        result_facts = {
            "first": {
                "is_final": first.get("is_final") is True,
                "assigned": sum(int(item["assigned_count"]) for item in first["results"]),
                "gateway_success": sum(
                    int(item["gateway_success_count"]) for item in first["results"]
                ),
                "frozen_repeat_equal": True,
            },
            "second": {
                "is_final": second.get("is_final") is True,
                "assigned": sum(int(item["assigned_count"]) for item in second["results"]),
                "gateway_failure": sum(
                    int(item["gateway_failure_count"]) for item in second["results"]
                ),
            },
            "third": {
                "is_final": third.get("is_final") is True,
                "assigned": sum(int(item["assigned_count"]) for item in third["results"]),
                "recovered_after_control_restart": True,
            },
        }
        return (
            {
                "services": {
                    "gateway_health": 200,
                    "control_initial_health": 200,
                    "control_recovery_health": 200,
                    "console_index": 200,
                    "console_feature": 200,
                },
                "access_control": {
                    "backend_navigation_contains_experiments": True,
                    "viewer_status": 403,
                    "cross_tenant_status": 404,
                    "request_id_preserved": True,
                },
                "results": result_facts,
                "recovery": {
                    "control_stopped_before_finalization": True,
                    "gateway_facts_committed_during_outage": True,
                    "restart_completed_and_delivered": True,
                },
                "provider_a": provider_a_fact,
                "provider_b": provider_b_fact,
                "webhook": webhook_fact,
                "database": database_summary(graph),
            },
            (admin_token, viewer_token, isolated_token, webhook_secret),
        )


def sensitive_encodings(value: str) -> tuple[bytes, ...]:
    """生成 UTF-8 与两种 UTF-16 明文表示，覆盖 Windows 文本文件常见编码。"""

    require(len(value) >= 12, "privacy.value", "too_short")
    return tuple(value.encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be"))


def verify_private_storage(work: Path, private_values: tuple[str, ...]) -> dict[str, int]:
    """扫描受控文件，要求私有明文和 SQLite sidecar 都在停机后归零。"""

    scanned = 0
    matches = 0
    sqlite_sidecars_remaining = 0
    try:
        for path in sorted(work.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.casefold().endswith(("-wal", "-shm", "-journal")):
                # sidecar 即使不含私有值也证明仍有连接或异常终止，不能作为静止终态放行。
                sqlite_sidecars_remaining += 1
            size = path.stat().st_size
            require(size <= 128 * 1024 * 1024, "privacy.scan", "file_too_large")
            content = path.read_bytes()
            scanned += 1
            for value in private_values:
                if any(encoded in content for encoded in sensitive_encodings(value)):
                    matches += 1
    except AcceptanceFailure:
        raise
    # 扫描期间的锁、ACL 或 reparse 异常统一隐藏具体文件路径。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("privacy.scan", "filesystem") from None
    require(matches == 0, "privacy.scan", "plaintext_found")
    require(
        sqlite_sidecars_remaining == 0,
        "cleanup.sqlite_sidecars",
        "remaining",
    )
    return {
        "files_scanned": scanned,
        "plaintext_matches": matches,
        "sqlite_sidecars_remaining": sqlite_sidecars_remaining,
    }


def _source_bytes(source: Path, boundary: Path) -> bytes:
    """在仓库边界内读取普通文件，任何文件系统故障只返回稳定验收坐标。"""

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
    # 源文件读取或路径解析失败不得回显仓库绝对位置。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def _migration_files(migrations_dir: Path) -> tuple[Path, ...]:
    """列出迁移加载器会读取的完整 SQL 集合，并拒绝目录或成员 reparse。"""

    try:
        require(
            migrations_dir.is_dir() and not migrations_dir.is_symlink(),
            "evidence.provenance",
            "migration_directory",
        )
        verified = workspace_path(migrations_dir, ROOT, stage="evidence.provenance")
        files = tuple(
            sorted(
                (
                    path
                    for path in verified.iterdir()
                    if path.suffix.casefold() == ".sql"
                ),
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
    # 枚举完整迁移目录失败只保留稳定 provenance 坐标。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None


def source_provenance_files(migrations_dir: Path) -> dict[str, Path]:
    """返回固定证据名到实际执行文件的映射，调用方不得序列化路径。"""

    return {
        "runtime_acceptance": workspace_path(
            Path(__file__),
            ROOT,
            stage="evidence.provenance",
        ),
        "migration_0068_up": migrations_dir
        / "0068_model_ab_testing_quality_evaluation.sql",
        "migration_0068_down": migrations_dir
        / "0068_model_ab_testing_quality_evaluation.down.sql",
    }


def _extend_migration_set_digest(
    digest: Any,
    filename: str,
    content: bytes,
) -> None:
    """以无歧义 filename/length/content 帧把同一次读取加入完整集合摘要。"""

    try:
        name = filename.encode("utf-8")
    # 非法宿主文件名不得作为原始 Unicode 异常进入终端。
    except UnicodeError:
        raise AcceptanceFailure("evidence.provenance", "source_unavailable") from None
    digest.update(len(name).to_bytes(4, "big"))
    digest.update(name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def capture_source_provenance(migrations_dir: Path | None = None) -> dict[str, str]:
    """冻结验收脚本、完整迁移集合与 0068 双向 SQL，不公开本机路径。"""

    actual_migrations_dir = migrations_dir or MIGRATIONS_DIR
    migration_set = hashlib.sha256()
    for source in _migration_files(actual_migrations_dir):
        content = _source_bytes(source, actual_migrations_dir)
        # 文件名、长度和内容都进入集合摘要，避免重命名或边界拼接碰撞。
        _extend_migration_set_digest(migration_set, source.name, content)
    provenance = {"migration_set": migration_set.hexdigest()}
    for name, source in source_provenance_files(actual_migrations_dir).items():
        boundary = ROOT if name == "runtime_acceptance" else actual_migrations_dir
        provenance[name] = hashlib.sha256(_source_bytes(source, boundary)).hexdigest()
    return provenance


def _decode_migration_sql(content: bytes) -> str:
    """按 Path.read_text 的 UTF-8/universal-newline 语义生成迁移器实际消费文本。"""

    try:
        decoded = content.decode("utf-8")
    # 非 UTF-8 迁移不能进入执行清单，也不能暴露原始字节位置。
    except UnicodeError:
        raise AcceptanceFailure("evidence.provenance", "migration_unicode") from None
    # TextIOWrapper(newline=None) 会把 CRLF 和裸 CR 都规范为 LF；checksum 必须完全复刻该语义。
    return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _manifest_entry(source: Path, content: bytes) -> tuple[MigrationManifestEntry, str]:
    """从同一次受控读取生成源 bytes 摘要、执行文本 checksum 与规范文本。"""

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


def create_migration_snapshot(
    work: Path,
    expected_provenance: dict[str, str],
) -> FrozenMigrationSnapshot:
    """复制完整 SQL 集并冻结逐版本 up checksum 与 0068 down 内存执行文本。"""

    validate_source_provenance(expected_provenance)
    snapshot = workspace_path(work / "migration-snapshot", work, stage="evidence.provenance")
    require(not snapshot.exists(), "evidence.provenance", "snapshot_exists")
    try:
        snapshot.mkdir()
        for source in _migration_files(MIGRATIONS_DIR):
            destination = workspace_path(
                snapshot / source.name,
                snapshot,
                stage="evidence.provenance",
            )
            destination.write_bytes(_source_bytes(source, MIGRATIONS_DIR))
    except AcceptanceFailure:
        raise
    # 快照创建或写入失败只返回固定错误码，不公开目标路径。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.provenance", "snapshot_write_failed") from None
    require(
        capture_source_provenance(snapshot) == expected_provenance,
        "evidence.provenance",
        "snapshot_mismatch",
    )
    all_files = _migration_files(snapshot)
    manifest_set = hashlib.sha256()
    up_entries: list[MigrationManifestEntry] = []
    up_sql: list[str] = []
    down_entry: MigrationManifestEntry | None = None
    down_sql: str | None = None
    for source in all_files:
        content = _source_bytes(source, snapshot)
        # 此摘要与内存执行文本来自同一次 bytes 读取，目录恢复无法伪造其绑定关系。
        _extend_migration_set_digest(manifest_set, source.name, content)
        if source.name.endswith(".down.sql"):
            if source.name == "0068_model_ab_testing_quality_evaluation.down.sql":
                down_sql = _decode_migration_sql(content)
                down_entry = MigrationManifestEntry(
                    version="0068",
                    name="model_ab_testing_quality_evaluation",
                    filename=source.name,
                    source_sha256=hashlib.sha256(content).hexdigest(),
                    execution_sha256=hashlib.sha256(down_sql.encode("utf-8")).hexdigest(),
                )
            continue
        entry, sql = _manifest_entry(source, content)
        up_entries.append(entry)
        up_sql.append(sql)
    require(
        manifest_set.hexdigest() == expected_provenance["migration_set"],
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
        "migration_0068_down_missing",
    )
    if down_entry is None or down_sql is None:
        # require 已固定失败坐标；显式分支同时让静态分析确认后续值非空。
        raise AcceptanceFailure("evidence.provenance", "migration_0068_down_missing")
    require(
        up_entries[-1].source_sha256 == expected_provenance["migration_0068_up"]
        and down_entry.source_sha256 == expected_provenance["migration_0068_down"],
        "evidence.provenance",
        "migration_0068_digest",
    )
    # 清单构造后再复核完整目录，防止生成逐版本 checksum 的窗口内发生持久漂移。
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
        down_0068=down_entry,
        down_0068_sql=down_sql,
    )


def validate_source_provenance(provenance: dict[str, str]) -> None:
    """要求证据只含固定源码摘要，拒绝路径、额外字段或非 SHA-256 值。"""

    require(
        set(provenance) == SOURCE_PROVENANCE_NAMES,
        "evidence.provenance",
        "shape",
    )
    require(
        all(
            type(digest) is str
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in provenance.values()
        ),
        "evidence.provenance",
        "digest",
    )


def assert_safe_evidence(value: object, private_values: tuple[str, ...]) -> None:
    """在写盘前检查证据 JSON 不含私有值和 secret-shaped 字段名。"""

    forbidden_keys = {
        "token",
        "access_token",
        "authorization",
        "provider_key",
        "signing_secret",
        "password",
        "prompt",
        "response",
        "endpoint",
        "base_url",
    }
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    require(
        all(private not in serialized for private in private_values),
        "evidence.privacy",
        "plaintext",
    )

    def visit(item: object) -> None:
        """递归检查证据字段名，只遍历内存对象且不修改业务值。"""

        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in forbidden_keys:
                    # 即使字段值被掩码，正式证据也不应携带凭据或正文形态的键。
                    raise AcceptanceFailure("evidence.privacy", "forbidden_key")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)


def write_evidence(
    artifact: Path,
    facts: dict[str, object],
    privacy: dict[str, int],
    source_provenance: dict[str, str],
    private_values: tuple[str, ...],
) -> Path:
    """原子写入脱敏 JSON 与 SHA-256 清单，证据明确保持总体目标未完成。"""

    validate_source_provenance(source_provenance)
    reserved_keys = {
        "schema_version",
        "prd",
        "generated_at",
        "all_108_prds_complete",
        "source_provenance",
        "privacy",
        "cleanup",
    }
    require(
        not reserved_keys.intersection(facts),
        "evidence.shape",
        "reserved_key",
    )
    evidence = {
        **facts,
        "schema_version": 1,
        "prd": "PRD-075",
        "generated_at": utc_now(),
        "all_108_prds_complete": False,
        "source_provenance": {
            "algorithm": "sha256",
            **source_provenance,
        },
        "privacy": privacy,
        "cleanup": {
            "only_owned_processes_stopped": True,
            "fixed_ports_released": True,
        },
    }
    assert_safe_evidence(evidence, private_values)
    encoded = (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    staging = workspace_path(
        artifact.parent / f".{artifact.name}.staging",
        artifact.parent,
        stage="evidence.path",
    )
    require(
        artifact.parent.is_dir()
        and not artifact.parent.is_symlink()
        and not artifact.exists()
        and not staging.exists(),
        "evidence.path",
        "publish_target",
    )
    try:
        staging.mkdir()
        staged_evidence = workspace_path(
            staging / "runtime-acceptance.json",
            staging,
            stage="evidence.path",
        )
        staged_manifest = workspace_path(
            staging / "SHA256SUMS.txt",
            staging,
            stage="evidence.path",
        )
        staged_evidence.write_bytes(encoded)
        persisted = staged_evidence.read_bytes()
        require(persisted == encoded, "evidence.publish", "json_mismatch")
        digest = hashlib.sha256(persisted).hexdigest()
        manifest = f"{digest}  runtime-acceptance.json\n"
        staged_manifest.write_text(
            manifest,
            encoding="ascii",
            newline="\n",
        )
        require(
            staged_manifest.read_text(encoding="ascii") == manifest,
            "evidence.publish",
            "manifest_mismatch",
        )
        # 新目录只有在 JSON 与 manifest 均落盘并复读一致后才一次性对读者可见。
        staging.replace(artifact)
    except AcceptanceFailure:
        raise
    # staging 写入或目录 rename 失败保持 final 不存在并隐藏宿主路径。
    except (OSError, RuntimeError):
        raise AcceptanceFailure("evidence.publish", "filesystem") from None
    return workspace_path(
        artifact / "runtime-acceptance.json",
        artifact,
        stage="evidence.path",
    )


def run(run_id: str | None = None) -> Path:
    """执行完整验收、关闭全部 owner、扫描隐私并发布最小证据。"""

    actual_run_id = safe_run_id(run_id)
    source_provenance = capture_source_provenance()
    assert_ports_available()
    work, artifact = create_run_directories(actual_run_id)
    migration_snapshot = create_migration_snapshot(work, source_provenance)
    secrets_ = RuntimeSecrets.create()
    provider_a = ProviderStub(
        port=PROVIDER_A_PORT,
        model="prd075-runtime-a",
        key=secrets_.provider_a_key,
        answer=secrets_.provider_a_answer,
        label="a",
    )
    provider_b = ProviderStub(
        port=PROVIDER_B_PORT,
        model="prd075-runtime-b",
        key=secrets_.provider_b_key,
        answer=secrets_.provider_b_answer,
        label="b",
    )
    webhook = WebhookStub()
    processes = OwnedProcesses()
    facts: dict[str, object] | None = None
    session_values: tuple[str, ...] = ()
    try:
        provider_a_url = provider_a.start()
        provider_b_url = provider_b.start()
        webhook_url = webhook.start()
        graph = prepare_database(
            work,
            secrets_,
            provider_a_url,
            provider_b_url,
            migration_snapshot,
        )
        environment = service_environment(work, graph, secrets_)
        facts, session_values = exercise_runtime(
            work,
            graph,
            environment,
            secrets_,
            provider_a,
            provider_b,
            webhook,
            webhook_url,
            processes,
        )
    finally:
        # 任一 owner 失败都不能阻断其它资源释放；统一 helper 会在全部尝试后抛固定错误。
        stop_runtime_owners(processes, webhook, provider_b, provider_a)
    require(facts is not None, "runtime", "facts_missing")
    # 进程与 Stub 关闭后再扫描，确保 WAL/SHM 和日志都处于稳定终态。
    private_values = secrets_.private_values(*session_values)
    privacy = verify_private_storage(work, private_values)
    assert_ports_available()
    require(
        capture_source_provenance() == source_provenance,
        "evidence.provenance",
        "source_changed",
    )
    require(
        capture_source_provenance(migration_snapshot.directory) == source_provenance,
        "evidence.provenance",
        "snapshot_changed",
    )
    return write_evidence(
        artifact,
        facts,
        privacy,
        source_provenance,
        private_values,
    )


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """解析唯一可选的安全运行 ID，不提供路径或网络地址覆盖参数。"""

    parser = SafeArgumentParser(description="Run PRD-075 runtime acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行验收并只输出脱敏证据路径；失败只输出稳定阶段和错误码。"""

    try:
        # 解析必须位于统一错误边界内；SafeArgumentParser 不会先回显未知参数或宿主路径。
        arguments = parse_arguments(argv)
        evidence = run(arguments.run_id)
    except AcceptanceFailure as exc:
        print(f"PRD-075 runtime acceptance failed: {exc.stage}:{exc.code}", file=sys.stderr)
        return 1
    # 依赖边界遗漏的文件系统、编码或数据库异常仍必须失败关闭，绝不输出 traceback/路径。
    except (DbError, sqlite3.Error, OSError, UnicodeError, ValueError, RuntimeError, TypeError):
        print("PRD-075 runtime acceptance failed: runtime:dependency_failure", file=sys.stderr)
        return 1
    # 最终兜底只保留固定坐标；KeyboardInterrupt/SystemExit 仍由进程控制方处理。
    except Exception:
        print("PRD-075 runtime acceptance failed: runtime:unexpected_failure", file=sys.stderr)
        return 1
    print(evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
