"""以可复现的真实进程验证 PRD-085 迁移、业务链路、隐私与清理门禁。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# 仓库根目录限定依赖加载、运行工具、临时目录和来源证明的可信边界。
ROOT = Path(__file__).resolve().parents[2]
for source in (
    ROOT / "shared-sdk" / "python",
    ROOT / "gateway-data-plane" / "src",
    ROOT / "control-plane-api" / "src",
):
    sys.path.insert(0, str(source))

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError  # noqa: E402
from metahub_shared.runtime.webhooks import verify_webhook_signature  # noqa: E402

# 固定运行时、目录、迁移、事件、证据模式和来源清单共同定义正式验收契约。
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
NODE = ROOT / ".tools" / "node-v22.23.2-win-arm64" / "node.exe"
WORK_ROOT = ROOT / ".tmp" / "prd085-runtime-live"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd085-runtime"
EXPECTED_MIGRATION_COUNT = 78
EXPECTED_MIGRATION_VERSION = "0078"
PREVIOUS_MIGRATION_VERSION = "0077"
EVENT_TYPES = (
    "tenant.registered",
    "provider.created",
    "virtual_key.created",
    "gateway.first_call_succeeded",
    "budget.configured",
    "teammate.invited",
    "request_logs.viewed",
)
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "url",
        "endpoint",
        "endpoint_url",
        "email",
        "authorization",
        "token",
        "plain_value",
        "provider_key",
        "virtual_key",
        "prompt",
        "response",
        "content",
        "payload",
        "secret",
        "database_url",
        "request_id",
        "trace_id",
    }
)
EVIDENCE_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "prd_id",
        "status",
        "generated_at",
        "all_108_prds_complete",
        "migration",
        "services",
        "workflow",
        "funnel",
        "traceability",
        "database",
        "privacy",
        "cleanup",
        "source_provenance",
    }
)
SERVICE_FACTS = {
    "control_http": True,
    "gateway_http": True,
    "console_http": True,
    "console_static_asset": True,
    "openai_compatible_stub": True,
    "signed_webhook_receiver": True,
}
WORKFLOW_FACTS = {
    "config_import_dual_mapping": True,
    "gateway_first_success": True,
    "budget_configured": True,
    "teammate_invited": True,
    "request_logs_viewed": True,
    "authorized_funnel": True,
    "anonymous_denied": True,
    "unauthorized_role_denied": True,
    "outbox_retry_recovered": True,
    "live_down_blocked": True,
}
CLEANUP_FACTS = {
    "owned_processes_stopped": True,
    "owned_threads_stopped": True,
    "ports_released": True,
    "work_tree_removed": True,
    "sqlite_sidecars_remaining": 0,
}
SOURCE_FILES = {
    "acceptance": Path(__file__).resolve(),
    "contract": ROOT / "scripts" / "acceptance" / "tests" / "test_prd085_runtime_acceptance.py",
    "migration_up": ROOT / "infra" / "migrations" / "0078_product_analytics_activation_funnel.sql",
    "migration_down": ROOT
    / "infra"
    / "migrations"
    / "0078_product_analytics_activation_funnel.down.sql",
    "control_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "product_analytics"
    / "routes.py",
    "control_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "product_analytics"
    / "service.py",
    "control_outbox": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "product_analytics"
    / "outbox.py",
    "gateway": ROOT
    / "gateway-data-plane"
    / "src"
    / "metahub_gateway"
    / "api"
    / "openai_compatible.py",
    "console": ROOT
    / "console-web"
    / "src"
    / "features"
    / "product-analytics"
    / "renderProductAnalytics.js",
}


class AcceptanceFailure(RuntimeError):
    """以固定阶段和公开错误码表示验收失败，不携带任何上游私密内容。"""

    def __init__(self, stage: str, code: str) -> None:
        """保存可公开诊断的阶段与代码，并构造稳定且无敏感值的消息。"""
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


def require(condition: bool, stage: str, code: str) -> None:
    """在验收条件不成立时抛出固定失败，统一收敛所有门禁错误。"""
    if not condition:
        raise AcceptanceFailure(stage, code)


def utc_now() -> str:
    """生成不含微秒的 UTC ISO 时间，确保正式证据格式稳定可比较。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_run_id(value: str) -> str:
    """限制运行标识字符和长度，防止路径穿越及无界产物文件名。"""
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,79}", value) is not None,
        "artifact.run_id",
        "invalid",
    )
    return value


def source_provenance() -> dict[str, str]:
    """计算验收、迁移及业务关键来源文件的完整 SHA-256 摘要。"""
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in SOURCE_FILES.items()
    }


def empty_evidence_payload() -> dict[str, Any]:
    """创建字段精确且明确未完成的证据骨架，避免部分结果被误判成功。"""
    return {
        "schema_version": "1.0",
        "prd_id": "PRD-085",
        "status": "incomplete",
        "generated_at": None,
        "all_108_prds_complete": False,
        "migration": {},
        "services": {},
        "workflow": {},
        "funnel": {},
        "traceability": {},
        "database": {},
        "privacy": {},
        "cleanup": {},
        "source_provenance": {},
    }


def _expected_migration(count: int, version: str) -> dict[str, object]:
    """构造指定数量和版本对应的完整性正常、无外键违规迁移事实。"""
    return {
        "count": count,
        "version": version,
        "integrity": "ok",
        "foreign_key_violations": 0,
    }


def _validate_evidence(evidence: dict[str, Any]) -> None:
    """逐区验证正式证据的精确模式、追踪下限、隐私结果和来源摘要。"""
    require(set(evidence) == EVIDENCE_TOP_LEVEL_KEYS, "evidence", "top_level")
    require(evidence.get("prd_id") == "PRD-085", "evidence", "prd_id")
    require(evidence.get("status") == "passed", "evidence", "status")
    require(evidence.get("all_108_prds_complete") is False, "evidence", "global_status")
    migration = evidence.get("migration", {})
    require(
        migration.get("fresh") == _expected_migration(78, "0078")
        and migration.get("repeat") == _expected_migration(78, "0078")
        and migration.get("down") == _expected_migration(77, "0077")
        and migration.get("reup") == _expected_migration(78, "0078")
        and migration.get("checksum_verified") is True,
        "evidence",
        "migration",
    )
    require(evidence.get("services") == SERVICE_FACTS, "evidence", "services")
    require(evidence.get("workflow") == WORKFLOW_FACTS, "evidence", "workflow")
    funnel = evidence.get("funnel", {})
    require(
        funnel.get("stage_count") == 7
        and funnel.get("all_stages_reached") is True
        and funnel.get("activated") is True,
        "evidence",
        "funnel",
    )
    trace = evidence.get("traceability", {})
    require(
        trace.get("successful_request_rows", 0) >= 1
        and trace.get("failed_request_rows", 0) >= 1
        and trace.get("usage_rows", 0) >= 1
        and trace.get("positive_cost") is True
        and trace.get("product_event_rows", 0) >= 7
        and trace.get("source_outbox_sent_rows", 0) >= 6
        and trace.get("webhook_success_rows", 0) >= 6,
        "evidence",
        "traceability",
    )
    database = evidence.get("database", {})
    require(
        database.get("integrity") == "ok"
        and database.get("foreign_key_violations") == 0
        and database.get("latest_migration") == "0078"
        and database.get("nonterminal_outbox_rows") == 0,
        "evidence",
        "database",
    )
    privacy = evidence.get("privacy", {})
    require(
        privacy.get("service_log_canary_hits") == 0
        and privacy.get("database_secret_canary_hits") == 0
        and privacy.get("formal_evidence_canary_hits") == 0
        and privacy.get("forbidden_key_matches") == 0,
        "evidence",
        "privacy",
    )
    require(evidence.get("cleanup") == CLEANUP_FACTS, "evidence", "cleanup")
    provenance = evidence.get("source_provenance", {})
    require(
        set(provenance) == set(SOURCE_FILES)
        and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in provenance.values()),
        "evidence",
        "provenance",
    )


def assert_safe_evidence(evidence: dict[str, Any], sensitive_values: tuple[str, ...]) -> str:
    """拒绝禁止字段和私密值后，返回稳定排序且仅含 ASCII 的证据文本。"""

    def walk(value: object) -> None:
        """递归检查字典键和列表成员，不读取或改写业务字段的具体语义。"""
        if isinstance(value, dict):
            keys = {str(key).casefold() for key in value}
            require(not keys.intersection(FORBIDDEN_EVIDENCE_KEYS), "evidence", "forbidden_key")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(evidence)
    serialized = json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    for value in sensitive_values:
        require(not value or value not in serialized, "evidence", "sensitive_canary")
    return serialized


def publish_evidence(
    serialized: str,
    *,
    artifact_root: Path = ARTIFACT_ROOT,
    run_id: str,
) -> tuple[Path, str]:
    """以待发布文件原子写入证据及字节哈希，并拒绝覆盖既有结果。"""
    safe_id = _safe_run_id(run_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    target = artifact_root / f"{safe_id}.json"
    manifest = artifact_root / f"{safe_id}.sha256"
    require(not target.exists() and not manifest.exists(), "artifact", "exists")
    pending = artifact_root / f".{safe_id}.pending.json"
    pending.write_bytes(serialized.encode("utf-8"))
    os.replace(pending, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    pending_manifest = artifact_root / f".{safe_id}.pending.sha256"
    pending_manifest.write_bytes(f"{digest}  {target.name}\n".encode("ascii"))
    os.replace(pending_manifest, manifest)
    return target, digest


def free_port() -> int:
    """请求操作系统分配一个当前可用回环端口，供本次自有桩服务使用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@dataclass
class OwnedProcess:
    """保存子进程及其专属标准输出文件句柄，支持确定性停止和关闭。"""

    name: str
    process: subprocess.Popen[bytes]
    stdout: Any
    stderr: Any


def start_process(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> OwnedProcess:
    """在净化环境和固定工作目录启动无交互子进程，并定向保存服务日志。"""
    stdout = (WORK_ROOT / "logs" / f"{name}.stdout.log").open("wb")
    stderr = (WORK_ROOT / "logs" / f"{name}.stderr.log").open("wb")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return OwnedProcess(name, process, stdout, stderr)


def stop_process(item: OwnedProcess) -> None:
    """先正常终止自有进程，超时后强制结束，并始终关闭日志句柄。"""
    if item.process.poll() is None:
        item.process.terminate()
        try:
            item.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait(timeout=5)
    item.stdout.close()
    item.stderr.close()


def wait_http(target: str, item: OwnedProcess, *, marker: str | None = None) -> None:
    """在限定时间内等待自有服务就绪，并在进程退出或超时后明确失败。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if item.process.poll() is not None:
            raise AcceptanceFailure(f"service.{item.name}", "process_exited")
        try:
            response = httpx.get(target, timeout=1, trust_env=False)
            if response.status_code == 200 and (marker is None or marker in response.text):
                return
        # 服务启动窗口中的连接错误可以短暂等待，但不会扩展总超时时间。
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AcceptanceFailure(f"service.{item.name}", "health_timeout")


class OpenAIStub:
    """提供仅绑定回环地址的 OpenAI 兼容上游桩，记录成功失败调用数量。"""

    def __init__(self, provider_key: str, failure_input: str, success_output: str) -> None:
        """保存仅在内存使用的上游凭据与输入输出探针，并初始化调用计数。"""
        self.provider_key = provider_key
        self.failure_input = failure_input
        self.success_output = success_output
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.success_calls = 0
        self.failure_calls = 0

    def start(self) -> tuple[str, int]:
        """启动回环上游桩，严格校验路由、授权头及聊天消息结构。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """处理单一聊天完成路由，并以固定响应模拟成功或上游失败。"""

            def do_POST(self) -> None:  # noqa: N802
                """校验上游授权和消息后返回固定用量，绝不写入访问日志。"""
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length))
                    messages = body.get("messages")
                    content = messages[-1].get("content")
                    valid = (
                        self.path == "/v1/chat/completions"
                        and self.headers.get("Authorization") == f"Bearer {owner.provider_key}"
                        and isinstance(content, str)
                    )
                # 任一授权或请求结构解析异常都按无效请求失败关闭，不回显正文。
                except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                    valid = False
                    content = None
                if not valid:
                    self._json(401, {"error": {"message": "invalid"}})
                elif content == owner.failure_input:
                    owner.failure_calls += 1
                    self._json(503, {"error": {"message": "fixed-upstream-failure"}})
                else:
                    owner.success_calls += 1
                    self._json(
                        200,
                        {
                            "id": "chatcmpl-prd085",
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": owner.success_output,
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 5,
                                "total_tokens": 12,
                            },
                        },
                    )

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                """发送最小 JSON 上游响应，仅包含验收所需固定字段和用量。"""
                wire = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

            def log_message(self, _format: str, *_args: object) -> None:
                """关闭上游桩默认访问日志，防止授权头和请求路径被持久化。"""
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}/v1", port

    def stop(self) -> None:
        """关闭上游桩服务器并等待其自有后台线程退出。"""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


class WebhookReceiver:
    """在内存接收签名回调，并用首轮固定失败验证 outbox 恢复能力。"""

    def __init__(self) -> None:
        """初始化接收器生命周期、尝试计数和内存回调记录。"""
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.attempts = 0
        self.received: list[tuple[str, bytes]] = []

    def start(self) -> tuple[str, int]:
        """启动回环回调接收器，首个请求返回失败，后续请求接受但不落盘。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """执行确定性的首次失败与后续成功，用于检验回调重试链路。"""

            def do_POST(self) -> None:  # noqa: N802
                """读取单次回调到内存，并按尝试次数返回固定公开状态。"""
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.attempts += 1
                if owner.attempts == 1:
                    self.send_response(503)
                    self.end_headers()
                    return
                owner.received.append((self.headers.get("X-MetaHub-Signature", ""), body))
                self.send_response(204)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                """关闭回调接收器访问日志，避免签名或目标信息进入日志。"""
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}/events", port

    def stop(self) -> None:
        """关闭回调服务器并等待其自有后台线程退出。"""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


def migration_state(database_url: str) -> dict[str, object]:
    """读取迁移数量、最新版本、完整性检查和外键违规数形成事实。"""
    with closing(connect_database(database_url)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count,MAX(version) AS version FROM schema_migrations "
            "WHERE status='applied'"
        ).fetchone()
        return {
            "count": int(row["count"]),
            "version": str(row["version"]),
            "integrity": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violations": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def prepare_database(admin_password: str) -> tuple[str, str, dict[str, Any]]:
    """从空库执行 0078 正反往返、校验迁移摘要并播种默认租户。"""
    database = WORK_ROOT / "runtime.sqlite3"
    database_url = f"sqlite:///{database.as_posix()}"
    fresh = apply_migrations(database_url)
    require(
        fresh.applied == [f"{version:04d}" for version in range(1, 79)],
        "migration.fresh",
        "result",
    )
    fresh_state = migration_state(database_url)
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 78, "migration.repeat", "result")
    repeat_state = migration_state(database_url)
    down = rollback_last_migration(database_url, expected_version="0078")
    require(down.rolled_back == "0078", "migration.down", "result")
    down_state = migration_state(database_url)
    reup = apply_migrations(database_url)
    require(reup.applied == ["0078"], "migration.reup", "result")
    reup_state = migration_state(database_url)
    up_path = SOURCE_FILES["migration_up"]
    expected_checksum = hashlib.sha256(up_path.read_text(encoding="utf-8").encode()).hexdigest()
    with closing(connect_database(database_url)) as connection:
        checksum = str(
            connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0078'"
            ).fetchone()[0]
        )
    require(checksum == expected_checksum, "migration.checksum", "mismatch")
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = admin_password
    try:
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return (
        database_url,
        seed.tenant_id,
        {
            "fresh": fresh_state,
            "repeat": repeat_state,
            "down": down_state,
            "reup": reup_state,
            "checksum_verified": True,
        },
    )


def service_environment(
    database_url: str,
    *,
    control_port: int,
    gateway_port: int,
    console_port: int,
    stub_port: int,
    admin_password: str,
    auth_secret: str,
    encryption_secret: str,
) -> dict[str, str]:
    """构建最小服务环境，只启用产品漏斗所需任务并注入本次私密配置。"""
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                str(path)
                for path in (
                    ROOT / "shared-sdk" / "python",
                    ROOT / "gateway-data-plane" / "src",
                    ROOT / "control-plane-api" / "src",
                )
            ),
            "TEMP": str(WORK_ROOT / "temp"),
            "TMP": str(WORK_ROOT / "temp"),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": admin_password,
            "METAHUB_AUTH_TOKEN_SECRET": auth_secret,
            "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": encryption_secret,
            "METAHUB_CONTROL_API_BASE_URL": f"http://127.0.0.1:{control_port}",
            "METAHUB_GATEWAY_API_BASE_URL": f"http://127.0.0.1:{gateway_port}",
            "METAHUB_CONSOLE_PORT": str(console_port),
            "METAHUB_ALLOWED_ORIGINS": f"http://127.0.0.1:{console_port}",
            "METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS": str(stub_port),
            "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK": "true",
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_USAGE_EXPORT_ENABLED": "false",
            "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "false",
            "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": "false",
            "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "true",
            "METAHUB_PRODUCT_ANALYTICS_OUTBOX_ENABLED": "true",
            "METAHUB_PRODUCT_ANALYTICS_OUTBOX_POLL_INTERVAL_SECONDS": "1",
            "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
            "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
            "METAHUB_LOG_LEVEL": "INFO",
        }
    )
    return environment


def request_headers(credential: str | None, label: str) -> dict[str, str]:
    """为每个验收请求生成可追踪标识，并仅在需要时附加内存凭据。"""
    headers = {
        "X-Request-ID": f"req_prd085_runtime_{label}",
        "X-Trace-ID": f"trc_prd085_runtime_{label}",
    }
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def expect_json(response: httpx.Response, status: int, stage: str) -> dict[str, Any]:
    """验证 HTTP 状态及 JSON 对象结构，不把响应正文写入失败信息。"""
    require(response.status_code == status, stage, f"status_{response.status_code}")
    payload = response.json()
    require(isinstance(payload, dict), stage, "shape")
    return payload


def transient_control_call(operation: Callable[[], httpx.Response]) -> httpx.Response:
    """仅重试 SQLite writer 竞争公开映射出的 fail-closed RBAC 错误。"""

    for attempt in range(6):
        response = operation()
        if response.status_code != 503:
            return response
        try:
            code = response.json().get("code")
        # 非标准错误响应不具备可重试依据，原样返回并由调用方判定失败。
        except (AttributeError, ValueError):
            return response
        if code != "rbac.unavailable" or attempt == 5:
            return response
        time.sleep(1.5)
    # 循环边界理论上覆盖所有返回路径，此异常只保护未来修改不突破上限。
    raise AssertionError("bounded retry loop is exhaustive")


def import_manifest(stub_url: str) -> dict[str, Any]:
    """生成映射到本地上游桩的配置导入清单，不包含真实客户或提供商数据。"""
    return {
        "schema_version": "metahub.config/v1",
        "source_tenant_id": "prd085-runtime-source",
        "exported_at": "2026-08-17T00:00:00Z",
        "providers": [
            {
                "provider_code": "prd085_runtime_openai",
                "name": "PRD085 Runtime OpenAI",
                "provider_type": "openai_compatible",
                "base_url": stub_url,
                "auth_type": "bearer",
                "description": None,
                "maintenance_note": None,
                "status": "active",
            }
        ],
        "models": [
            {
                "internal_model_id": "prd085-runtime-model",
                "provider_code": "prd085_runtime_openai",
                "provider_model_id": "served-chat",
                "display_name": "PRD085 Runtime Model",
                "capabilities": {"chat": True, "stream": False, "embeddings": False},
                "context_window": 32768,
                "input_price_per_1k": 1,
                "output_price_per_1k": 2,
                "currency": "CNY",
                "price_schema_version": "prd085-runtime-v1",
                "description": None,
                "status": "active",
            }
        ],
        "aliases": [
            {
                "alias": "prd085-runtime-chat",
                "environment": "dev",
                "default_internal_model_id": "prd085-runtime-model",
                "fallback_internal_model_id": None,
                "fallback_conditions": [],
                "routing_mode": "fixed",
                "description": None,
                "status": "active",
            }
        ],
        "virtual_keys": [
            {
                "name": "PRD085 Runtime Key",
                "environment": "dev",
                "description": None,
                "project_code": None,
                "application_code": None,
                "status": "active",
                "rpm_limit": 60,
                "tpm_limit": 120000,
                "concurrency_limit": 8,
                "rate_limit_status": "disabled",
            }
        ],
    }


def wait_outbox(database_url: str, expected: int) -> None:
    """限定时间等待事件 outbox 与签名回调均达到预期成功数量。"""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with closing(connect_database(database_url)) as connection:
                sent = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM product_event_outbox WHERE status='sent'"
                    ).fetchone()[0]
                )
                deliveries = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM webhook_deliveries "
                        "WHERE event_type='product_event.recorded' AND status='success'"
                    ).fetchone()[0]
                )
            if sent >= expected and deliveries >= expected:
                return
        # 后台写入竞争期间允许短暂读取失败，最终仍受固定超时门禁约束。
        except sqlite3.Error:
            pass
        time.sleep(0.5)
    raise AcceptanceFailure("outbox", "settlement_timeout")


def scan_files(paths: list[Path], values: list[str]) -> int:
    """以字节方式统计服务日志和数据库中的私密探针命中数量。"""
    needles = [value.encode() for value in values if value]
    total = 0
    for path in paths:
        try:
            if path.is_file():
                data = path.read_bytes()
                total += sum(data.count(needle) for needle in needles)
        # 无法读取任一隐私扫描目标时必须失败，不能把缺失检查当作零命中。
        except OSError as error:
            raise AcceptanceFailure("privacy", "file_read_failed") from error
    return total


def release_check(ports: list[int]) -> bool:
    """重新绑定全部回环端口，确认验收自有服务已完整释放网络资源。"""
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
                candidate.bind(("127.0.0.1", port))
        # 任一端口仍被占用即表示清理未完成，返回失败交由证据门禁处理。
        except OSError:
            return False
    return True


def checkpoint_and_wait_for_sidecars(database_path: Path) -> None:
    """停服后合并 WAL，并等待 Windows 释放 sidecar 文件。"""

    connection = sqlite3.connect(database_path, timeout=5)
    try:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    require(tuple(checkpoint or ()) == (0, 0, 0), "cleanup", "sqlite_checkpoint")
    sidecars = (Path(str(database_path) + "-wal"), Path(str(database_path) + "-shm"))
    for _ in range(20):
        if not any(path.exists() for path in sidecars):
            return
        time.sleep(0.25)
    raise AcceptanceFailure("cleanup", "sqlite_sidecars")


def remove_tree(path: Path) -> None:
    """有限次重试删除本次固定工作树，适配 Windows 句柄释放延迟。"""
    for _ in range(20):
        try:
            if path.exists():
                shutil.rmtree(path)
            return
        # Windows 句柄可能延迟释放，只在固定次数内等待并最终明确失败。
        except OSError:
            time.sleep(0.25)
    raise AcceptanceFailure("cleanup", "work_tree_locked")


def main() -> int:
    """运行完整真实进程验收，汇总可追踪事实并在清理后发布安全证据。"""
    admin_password = "Prd085-Admin-Aa1!" + secrets.token_urlsafe(24)
    developer_password = "Prd085-Developer-Aa1!" + secrets.token_urlsafe(24)
    auth_secret = secrets.token_urlsafe(48)
    encryption_secret = secrets.token_urlsafe(48)
    provider_key = "prd085-provider-" + secrets.token_urlsafe(32)
    success_input = "prd085-success-input-" + secrets.token_urlsafe(18)
    failure_input = "prd085-failure-input-" + secrets.token_urlsafe(18)
    success_output = "prd085-success-output-" + secrets.token_urlsafe(18)
    invite_email = "prd085-" + secrets.token_hex(10) + "@example.invalid"
    invite_token = ""
    virtual_key = ""
    admin_token = ""
    developer_token = ""
    signing_secret = ""
    stub_url = ""
    receiver_url = ""
    processes: list[OwnedProcess] = []
    stub = OpenAIStub(provider_key, failure_input, success_output)
    receiver = WebhookReceiver()
    ports: list[int] = []
    evidence: dict[str, Any] | None = None
    database_url = ""
    try:
        require(PYTHON.is_file(), "preflight", "python_missing")
        require(NODE.is_file(), "preflight", "node_missing")
        remove_tree(WORK_ROOT)
        for directory in ("logs", "temp"):
            (WORK_ROOT / directory).mkdir(parents=True)
        stub_url, stub_port = stub.start()
        receiver_url, receiver_port = receiver.start()
        control_port, gateway_port, console_port = (free_port() for _ in range(3))
        ports = [stub_port, receiver_port, control_port, gateway_port, console_port]
        require(len(set(ports)) == 5, "preflight", "port_collision")
        database_url, tenant_id, migration = prepare_database(admin_password)
        environment = service_environment(
            database_url,
            control_port=control_port,
            gateway_port=gateway_port,
            console_port=console_port,
            stub_port=stub_port,
            admin_password=admin_password,
            auth_secret=auth_secret,
            encryption_secret=encryption_secret,
        )
        processes.append(
            start_process(
                "control",
                [
                    str(PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_control.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(control_port),
                    "--no-access-log",
                    "--no-proxy-headers",
                ],
                cwd=ROOT,
                environment=environment,
            )
        )
        control_base = f"http://127.0.0.1:{control_port}"
        wait_http(control_base + "/health", processes[0])
        processes.append(
            start_process(
                "gateway",
                [
                    str(PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_gateway.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(gateway_port),
                    "--no-access-log",
                    "--no-proxy-headers",
                ],
                cwd=ROOT,
                environment=environment,
            )
        )
        gateway_base = f"http://127.0.0.1:{gateway_port}"
        wait_http(gateway_base + "/health", processes[1])
        processes.append(
            start_process(
                "console",
                [str(NODE), str(ROOT / "console-web" / "scripts" / "dev-server.mjs")],
                cwd=ROOT / "console-web",
                environment=environment,
            )
        )
        console_base = f"http://127.0.0.1:{console_port}"
        wait_http(console_base + "/", processes[2], marker='id="app"')
        wait_http(
            console_base + "/src/features/product-analytics/renderProductAnalytics.js",
            processes[2],
            marker="renderProductAnalytics",
        )

        with (
            httpx.Client(base_url=control_base, timeout=30, trust_env=False) as control,
            httpx.Client(base_url=gateway_base, timeout=60, trust_env=False) as gateway,
        ):
            login = expect_json(
                control.post(
                    "/auth/login",
                    headers=request_headers(None, "admin_login"),
                    json={
                        "tenant_slug": "default",
                        "email": "admin@metahub.local",
                        "password": admin_password,
                    },
                ),
                200,
                "control.login",
            )
            admin_token = str(login.get("access_token", ""))
            require(len(admin_token) > 40, "control.login", "token_missing")
            webhook = expect_json(
                control.post(
                    "/webhooks",
                    headers=request_headers(admin_token, "webhook"),
                    json={
                        "name": "PRD085 runtime receiver",
                        "endpoint_url": receiver_url,
                        "event_types": ["product_event.recorded"],
                        "status": "active",
                        "timeout_ms": 3000,
                        "format": "json",
                    },
                ),
                200,
                "control.webhook",
            )
            signing_secret = str(webhook.get("signing_secret", ""))
            require(signing_secret.startswith("whsec_"), "control.webhook", "secret_missing")
            imported = expect_json(
                control.post(
                    "/config/import",
                    headers=request_headers(admin_token, "config_import"),
                    json=import_manifest(stub_url),
                ),
                201,
                "control.config_import",
            )
            generated = imported.get("generated_virtual_keys", [])
            require(
                isinstance(generated, list) and len(generated) == 1, "control.config_import", "key"
            )
            virtual_key = str(generated[0].get("plain_value", ""))
            require(virtual_key.startswith("mhv_dev_"), "control.config_import", "key")
            providers = expect_json(
                control.get("/providers", headers=request_headers(admin_token, "providers")),
                200,
                "control.providers",
            ).get("providers", [])
            provider_id = next(
                str(row["provider_id"])
                for row in providers
                if row.get("provider_code") == "prd085_runtime_openai"
            )
            key_response = control.post(
                f"/providers/{provider_id}/keys",
                headers=request_headers(admin_token, "provider_key"),
                json={"name": "PRD085 runtime upstream", "plain_value": provider_key},
            )
            expect_json(key_response, 201, "control.provider_key")
            require(provider_key not in key_response.text, "control.provider_key", "exposed")

            success_headers = request_headers(virtual_key, "gateway_success")
            success = gateway.post(
                "/v1/chat/completions",
                headers=success_headers,
                json={
                    "model": "prd085-runtime-chat",
                    "messages": [{"role": "user", "content": success_input}],
                },
            )
            require(success.status_code == 200, "gateway.success", f"status_{success.status_code}")
            require(success_output in success.text, "gateway.success", "wire")
            failure_headers = request_headers(virtual_key, "gateway_failure")
            failure = gateway.post(
                "/v1/chat/completions",
                headers=failure_headers,
                json={
                    "model": "prd085-runtime-chat",
                    "messages": [{"role": "user", "content": failure_input}],
                },
            )
            require(failure.status_code in {502, 503, 504}, "gateway.failure", "status")

            with closing(connect_database(database_url)) as connection:
                virtual_key_id = str(
                    connection.execute(
                        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? "
                        "AND name='PRD085 Runtime Key'",
                        (tenant_id,),
                    ).fetchone()[0]
                )
            expect_json(
                transient_control_call(
                    lambda: control.post(
                        "/budgets",
                        headers=request_headers(admin_token, "budget"),
                        json={
                            "scope_type": "virtual_key",
                            "scope_id": virtual_key_id,
                            "environment": "dev",
                            "name": "PRD085 runtime budget",
                            "daily_budget_cny": "1000",
                            "warning_threshold_percent": 80,
                            "exhausted_action": "alert_only",
                            "status": "active",
                        },
                    )
                ),
                201,
                "control.budget",
            )
            invited = expect_json(
                transient_control_call(
                    lambda: control.post(
                        "/invitations",
                        headers=request_headers(admin_token, "invite"),
                        json={
                            "email": invite_email,
                            "role": "developer",
                            "expires_in_hours": 72,
                        },
                    )
                ),
                201,
                "control.invite",
            )
            invite_token = str(invited.get("accept_token", ""))
            require(len(invite_token) == 43, "control.invite", "token_missing")
            expect_json(
                transient_control_call(
                    lambda: control.post(
                        f"/invitations/{invite_token}/accept",
                        headers=request_headers(None, "invite_accept"),
                        json={
                            "display_name": "PRD085 Runtime Developer",
                            "password": developer_password,
                        },
                    )
                ),
                201,
                "control.invite_accept",
            )
            developer_login = expect_json(
                transient_control_call(
                    lambda: control.post(
                        "/auth/login",
                        headers=request_headers(None, "developer_login"),
                        json={
                            "tenant_slug": "default",
                            "email": invite_email,
                            "password": developer_password,
                        },
                    )
                ),
                200,
                "control.developer_login",
            )
            developer_token = str(developer_login.get("access_token", ""))
            expect_json(
                transient_control_call(
                    lambda: control.get(
                        "/request-logs?limit=10",
                        headers=request_headers(admin_token, "request_logs"),
                    )
                ),
                200,
                "control.request_logs",
            )
            funnel = expect_json(
                transient_control_call(
                    lambda: control.get(
                        "/product-analytics/funnel",
                        headers=request_headers(admin_token, "funnel_admin"),
                    )
                ),
                200,
                "control.funnel",
            )
            stages = funnel.get("stages", [])
            require(
                [stage.get("event_type") for stage in stages] == list(EVENT_TYPES)
                and all(int(stage.get("event_count", 0)) >= 1 for stage in stages),
                "control.funnel",
                "stages",
            )
            anonymous = control.get(
                "/product-analytics/funnel", headers=request_headers(None, "funnel_anonymous")
            )
            denied = control.get(
                "/product-analytics/funnel",
                headers=request_headers(developer_token, "funnel_developer"),
            )
            require(
                anonymous.status_code == 401
                and anonymous.json().get("code") == "auth.token_missing"
                and anonymous.json().get("request_id"),
                "control.funnel_anonymous",
                "contract",
            )
            require(
                denied.status_code == 403
                and denied.json().get("code") == "auth.permission_denied"
                and denied.json().get("request_id"),
                "control.funnel_denied",
                "contract",
            )

        wait_outbox(database_url, 6)
        require(receiver.attempts > len(receiver.received), "outbox", "retry_not_observed")
        require(len(receiver.received) >= 6, "outbox", "receiver_count")
        for signature, body in receiver.received:
            require(
                verify_webhook_signature(signature, signing_secret, body), "outbox", "signature"
            )
            require(
                json.loads(body).get("event_type") == "product_event.recorded",
                "outbox",
                "event_type",
            )

        with closing(connect_database(database_url)) as connection:
            event_rows = connection.execute(
                "SELECT event_type,COUNT(*) AS count FROM product_events GROUP BY event_type"
            ).fetchall()
            event_counts = {str(row["event_type"]): int(row["count"]) for row in event_rows}
            require(set(event_counts) == set(EVENT_TYPES), "database.events", "types")
            logs = connection.execute(
                "SELECT status FROM request_logs WHERE request_id IN(?,?)",
                (
                    success_headers["X-Request-ID"],
                    failure_headers["X-Request-ID"],
                ),
            ).fetchall()
            usage = connection.execute(
                "SELECT COUNT(*) AS count,COALESCE(SUM(total_tokens),0) AS tokens,"
                "COALESCE(SUM(cost_cny),0) AS cost FROM usage_ledger WHERE request_id=?",
                (success_headers["X-Request-ID"],),
            ).fetchone()
            sent = int(
                connection.execute(
                    "SELECT COUNT(*) FROM product_event_outbox WHERE status='sent'"
                ).fetchone()[0]
            )
            nonterminal = int(
                connection.execute(
                    "SELECT COUNT(*) FROM product_event_outbox WHERE status!='sent'"
                ).fetchone()[0]
            )
            delivered = int(
                connection.execute(
                    "SELECT COUNT(*) FROM webhook_deliveries "
                    "WHERE event_type='product_event.recorded' AND status='success'"
                ).fetchone()[0]
            )
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        try:
            rollback_last_migration(database_url, expected_version="0078")
        # 活跃业务数据必须阻止降级，且只接受统一迁移失败公开错误码。
        except DbError as error:
            require(error.code == "db.migration_failed", "migration.live_down", "code")
        else:
            raise AcceptanceFailure("migration.live_down", "not_blocked")
        require(migration_state(database_url)["version"] == "0078", "migration.live_down", "state")

        succeeded = sum(1 for row in logs if row["status"] == "success")
        failed = sum(1 for row in logs if row["status"] == "failed")
        evidence = empty_evidence_payload()
        evidence.update(
            {
                "status": "passed",
                "generated_at": utc_now(),
                "migration": migration,
                "services": dict(SERVICE_FACTS),
                "workflow": dict(WORKFLOW_FACTS),
                "funnel": {
                    "stage_count": len(EVENT_TYPES),
                    "all_stages_reached": all(
                        event_counts.get(item, 0) >= 1 for item in EVENT_TYPES
                    ),
                    "activated": True,
                },
                "traceability": {
                    "successful_request_rows": succeeded,
                    "failed_request_rows": failed,
                    "usage_rows": int(usage["count"]),
                    "usage_total_tokens": int(usage["tokens"]),
                    "positive_cost": float(usage["cost"]) > 0,
                    "product_event_rows": sum(event_counts.values()),
                    "source_outbox_sent_rows": sent,
                    "webhook_success_rows": delivered,
                },
                "database": {
                    "integrity": integrity,
                    "foreign_key_violations": foreign_keys,
                    "latest_migration": "0078",
                    "nonterminal_outbox_rows": nonterminal,
                },
                "privacy": {},
                "cleanup": {},
                "source_provenance": source_provenance(),
            }
        )
    # 所有运行错误统一输出固定失败文本，避免令牌、请求或上游内容进入终端证据。
    except (AcceptanceFailure, DbError, httpx.HTTPError, OSError, sqlite3.Error, ValueError):
        print("PRD-085 runtime acceptance failed: runtime:fixed_failure", file=sys.stderr)
        return 1
    finally:
        for item in reversed(processes):
            stop_process(item)
        receiver.stop()
        stub.stop()

    require(evidence is not None, "evidence", "missing")
    sensitive = (
        admin_password,
        developer_password,
        auth_secret,
        encryption_secret,
        provider_key,
        virtual_key,
        admin_token,
        developer_token,
        invite_token,
        invite_email,
        success_input,
        failure_input,
        success_output,
        signing_secret,
        stub_url,
        receiver_url,
    )
    log_hits = scan_files(list((WORK_ROOT / "logs").glob("*.log")), list(sensitive))
    database_path = WORK_ROOT / "runtime.sqlite3"
    checkpoint_and_wait_for_sidecars(database_path)
    database_secret_hits = scan_files(
        [
            path
            for path in (
                database_path,
                Path(str(database_path) + "-wal"),
                Path(str(database_path) + "-shm"),
            )
            if path.exists()
        ],
        [
            provider_key,
            virtual_key,
            admin_token,
            developer_token,
            invite_token,
            success_input,
            failure_input,
            success_output,
        ],
    )
    require(log_hits == 0 and database_secret_hits == 0, "privacy", "sensitive_canary")
    require(release_check(ports), "cleanup", "ports_busy")
    remove_tree(WORK_ROOT)
    evidence["privacy"] = {
        "service_log_canary_hits": log_hits,
        "database_secret_canary_hits": database_secret_hits,
        "formal_evidence_canary_hits": 0,
        "forbidden_key_matches": 0,
    }
    evidence["cleanup"] = dict(CLEANUP_FACTS)
    _validate_evidence(evidence)
    serialized = assert_safe_evidence(evidence, sensitive)
    run_id = _safe_run_id(
        datetime.now(UTC).strftime("prd085-runtime-%Y%m%dT%H%M%SZ-") + secrets.token_hex(4)
    )
    target, digest = publish_evidence(serialized, run_id=run_id)
    require(hashlib.sha256(target.read_bytes()).hexdigest() == digest, "artifact", "digest")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
