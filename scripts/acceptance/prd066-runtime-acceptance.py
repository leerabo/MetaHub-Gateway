"""PRD-066 企业 Chat 的隔离真实运行验收器。

脚本在 ``D:/MetaHub/.artifacts/prd066-runtime/<run-id>`` 创建独占数据库、服务日志和
脱敏证据，使用空闲回环端口启动真实 Gateway、Control、Console 与 OpenAI-compatible
mock Provider。Prompt、Response、内部 Token、Provider Key 和登录密码只在当前进程内存
存活；最终 JSON 只保存固定状态、计数和 request_id，不保存业务正文或凭据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256


# 正式脚本位置反推仓库和证据根，调用方 cwd 不能把数据库或日志重定向到工作区之外。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = REPOSITORY_ROOT / ".artifacts" / "prd066-runtime"
PYTHON = Path(sys.executable).resolve()
# 运行期身份、凭据和正文金丝雀在模块加载时随机生成，只在当前验收进程及其子进程
# 内存存活；源码、命令行参数、环境证据和最终 JSON 都不保存实际值。
ADMIN_PASSWORD = f"Prd066-Admin-Aa1!{secrets.token_urlsafe(24)}"
DEVELOPER_PASSWORD = f"Prd066-Developer-Aa1!{secrets.token_urlsafe(24)}"
SECOND_PASSWORD = f"Prd066-Second-Aa1!{secrets.token_urlsafe(24)}"
PROVIDER_KEY = f"sk-{secrets.token_urlsafe(32)}"
VIRTUAL_KEY = f"mhv_{secrets.token_urlsafe(32)}"
INTERNAL_CHAT_TOKEN = secrets.token_urlsafe(48)
AUTH_TOKEN_SECRET = secrets.token_urlsafe(48)
PROMPT_CANARY = f"PRD066_RUNTIME_PROMPT_{secrets.randbelow(10**18):018d}"
ASSISTANT_CANARY = f"PRD066_RUNTIME_ASSISTANT_{secrets.token_urlsafe(24)}"
MESSAGE_REQUEST_ID = "req_prd066_runtime_message"
# 服务和 mock Provider 必须使用动态空闲回环端口，避免停止或复用用户现有开发进程。
LOOPBACK_HOST = "127.0.0.1"
# mock Provider 仅服务本地验收，仍应与正式 Chat 入口保持同一原始正文上限，避免异常客户端
# 通过伪造 Content-Length 让验收进程无界读取内存。
MOCK_PROVIDER_MAX_REQUEST_BYTES = 512 * 1024


class AcceptanceFailure(RuntimeError):
    """保存固定阶段与错误分类，禁止把响应正文、SQL 或凭据拼入异常。"""

    def __init__(self, stage: str, code: str) -> None:
        """构造可安全写入失败证据的稳定分类。"""

        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON 对象键值对转为字典，并拒绝任意嵌套层级的重复键。

    ``json.loads`` 默认会静默保留重复键的最后一个值，这会让 mock Provider 对 Gateway
    实际发送内容的判断产生歧义；通过 ``object_pairs_hook`` 在每层对象构造时失败关闭。
    """

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 重复键会让不同 JSON 实现得出不同业务值，因此不能静默采用后出现的值。
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RuntimeGraph:
    """保存隔离数据库中的稳定资源 ID，正文与任何明文密钥不进入该结构。"""

    database_url: str
    tenant_id: str
    project_id: str
    environment_id: str
    model_alias_id: str
    virtual_key_id: str
    developer_email: str
    second_tenant_slug: str
    second_email: str


@dataclass(slots=True)
class ManagedProcess:
    """记录验收器拥有的子进程及日志句柄，关闭时只操作本脚本创建的 PID。"""

    name: str
    process: subprocess.Popen[bytes]
    stdout_file: Any
    stderr_file: Any


class MockProvider:
    """在回环地址提供最小 OpenAI-compatible Chat 响应并只保留内存计数。"""

    def __init__(self) -> None:
        """初始化尚未监听的 Provider；请求正文不会写入属性或日志。"""

        self.call_count = 0
        self.invalid_count = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        """启动动态端口 HTTP Server，返回仅供服务配置使用的 `/v1` 基础 URL。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """校验固定路径、Bearer 和请求形状，再返回不落盘的规范 Usage。"""

            def do_POST(self) -> None:  # noqa: N802
                """处理非流式 Chat；任何非法请求只增加计数，不回显收到的正文。"""

                try:
                    lengths = self.headers.get_all("Content-Length", failobj=[])
                    length_text = lengths[0] if len(lengths) == 1 else ""
                    # 拒绝重复、非 ASCII、前导零、负数和超限长度；Transfer-Encoding 与
                    # Content-Length 并存会产生帧边界歧义，本地 mock 同样按失败关闭处理。
                    canonical_length = (
                        length_text.isascii()
                        and length_text.isdecimal()
                        and str(int(length_text)) == length_text
                    )
                    if (
                        not canonical_length
                        or self.headers.get("Transfer-Encoding") is not None
                    ):
                        # HTTP 帧边界不唯一时拒绝继续读取，避免验收结论建立在歧义请求上。
                        raise ValueError("invalid content length")
                    length = int(length_text)
                    # 空正文不是 Chat 请求，超限正文则可能耗尽验收进程内存，两者均直接拒绝。
                    if not 0 < length <= MOCK_PROVIDER_MAX_REQUEST_BYTES:
                        # 使用同一固定错误响应，不向调用方泄露具体命中的是哪类线协议约束。
                        raise ValueError("request body too large")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        # 客户端提前断开时不得对不完整 JSON 做宽松解析或生成成功响应。
                        raise ValueError("incomplete request body")
                    payload = json.loads(
                        raw.decode("utf-8"),
                        object_pairs_hook=_unique_json_object,
                    )
                # Provider 侧畸形输入不能把原始字节或 JSON 异常写入验收日志。
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    owner.invalid_count += 1
                    self._json(400, {"error": {"message": "invalid request"}})
                    return
                messages = payload.get("messages") if isinstance(payload, dict) else None
                last_message = messages[-1] if isinstance(messages, list) and messages else None
                valid = (
                    self.path == "/v1/chat/completions"
                    and self.headers.get("Authorization") == f"Bearer {PROVIDER_KEY}"
                    and isinstance(payload, dict)
                    and payload.get("stream") is False
                    and payload.get("model") == "local-chat-model"
                    and isinstance(last_message, dict)
                    and last_message.get("content") == PROMPT_CANARY
                )
                # 身份、模型或正文绑定任一不匹配都固定失败，不能放宽成“收到请求即成功”。
                if not valid:
                    owner.invalid_count += 1
                    self._json(400, {"error": {"message": "invalid request"}})
                    return
                owner.call_count += 1
                self._json(
                    200,
                    {
                        "id": "chatcmpl_prd066_runtime",
                        "object": "chat.completion",
                        "model": "local-chat-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": ASSISTANT_CANARY,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                            "prompt_tokens_details": {"cached_tokens": 0},
                        },
                    },
                )

            def _json(self, status: int, payload: dict[str, object]) -> None:
                """返回固定 JSON，不把请求 Header 或正文复制到响应。"""

                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                """禁用 BaseHTTPRequestHandler 的 URL/access 日志，防止未来正文参数旁路落盘。"""

                return

        self.server = ThreadingHTTPServer((LOOPBACK_HOST, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = int(self.server.server_address[1])
        return f"http://{LOOPBACK_HOST}:{port}/v1"

    def stop(self) -> None:
        """有界关闭 mock Provider，只等待本对象创建的后台线程。"""

        # 尚未启动属于正常清理路径，不能因为前序准备失败再掩盖原始固定阶段。
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；stage/code 必须是源码内固定字符串。"""

    if not condition:
        raise AcceptanceFailure(stage, code)


def _safe_run_id(value: str | None) -> str:
    """返回安全的单层 run-id，拒绝路径分隔符、空值和超长输入。"""

    candidate = value or datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    normalized = candidate.replace("-", "").replace("_", "")
    # ``str.isalnum`` 接受全量 Unicode；证据目录名固定为 ASCII，避免同形字符和跨工具
    # 规范化差异导致批次误认，同时继续允许自动生成格式中的连字符与下划线。
    allowed = normalized.isascii() and normalized.isalnum()
    _require(3 <= len(candidate) <= 80 and allowed, "config.run_id", "invalid")
    return candidate


def _create_run_directory(run_id: str) -> Path:
    """在固定证据根独占创建普通目录，历史批次存在时拒绝覆盖。"""

    artifacts = REPOSITORY_ROOT / ".artifacts"
    _require(artifacts.is_dir() and not artifacts.is_symlink(), "config.artifacts", "invalid")
    # 证据父目录是仓库内固定子目录；resolve 校验阻断 junction 或路径逃逸。
    if not ARTIFACT_ROOT.exists():
        ARTIFACT_ROOT.mkdir()
    _require(
        ARTIFACT_ROOT.is_dir()
        and not ARTIFACT_ROOT.is_symlink()
        and ARTIFACT_ROOT.resolve().parent == artifacts.resolve(),
        "config.artifact_root",
        "invalid",
    )
    target = ARTIFACT_ROOT / run_id
    _require(not target.exists(), "config.run_directory", "already_exists")
    target.mkdir()
    for name in ("logs", "spool", "temp"):
        (target / name).mkdir()
    return target


def _free_port() -> int:
    """由内核分配一个当前空闲回环端口，服务启动时仍通过健康轮询再次确认。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind((LOOPBACK_HOST, 0))
        return int(candidate.getsockname()[1])


def _database_url(path: Path) -> str:
    """把证据目录内 SQLite 文件转换为项目支持的 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(run_directory: Path, provider_base_url: str) -> RuntimeGraph:
    """创建 0058 隔离库、两租户、唯一 Project Key、别名、预算和 DLP 规则。"""

    database_url = _database_url(run_directory / "prd066-runtime.db")
    # Seed 的明文只存在当前进程环境；数据库 Provider 凭据仍是不可用占位，Gateway 使用受限 dev 环境 Key。
    seed_environment = {
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_SEED_PROVIDER_API_KEY": "seed-placeholder-never-used",
        "METAHUB_SEED_VIRTUAL_KEY": VIRTUAL_KEY,
        "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_base_url,
    }
    previous = {name: os.environ.get(name) for name in seed_environment}
    os.environ.update(seed_environment)
    try:
        migration = apply_migrations(database_url)
        seed = apply_seed_data(database_url)
    finally:
        # 不把验收凭据长期留在父进程环境；原值存在时逐字恢复。
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value
    connection = connect_database(database_url)
    try:
        migration_state = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        # MigrationResult 只记录 applied/skipped 文件；权威终态必须从隔离数据库本身读取。
        _require(
            migration_state is not None
            and int(migration_state[0]) == 58
            and migration_state[1] == "0058"
            and "0058" in {*migration.applied, *migration.skipped},
            "database.migrate",
            "not_0058",
        )
        tenant_id = str(seed.tenant_id)
        environment = connection.execute(
            """
            SELECT environment_id,code FROM environments
            WHERE tenant_id=? AND code='dev' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        model = connection.execute(
            """
            SELECT model_id FROM models
            WHERE tenant_id=? AND model_alias='default-chat' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        admin = connection.execute(
            """
            SELECT user_id FROM users
            WHERE tenant_id=? AND email='admin@metahub.local' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        virtual_key = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        _require(
            all(item is not None for item in (environment, model, admin, virtual_key)),
            "database.seed",
            "graph_missing",
        )
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        project_id = "prj_prd066_runtime"
        model_alias_id = "mal_prd066_runtime"
        developer_email = "prd066-runtime-developer@example.invalid"
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'developer','active',?,?)
            """,
            (
                "user_prd066_runtime_developer",
                tenant_id,
                developer_email,
                "PRD066 Runtime Developer",
                hash_password(DEVELOPER_PASSWORD),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO projects(
              project_id,tenant_id,project_code,name,description,owner_user_id,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?,?,?)
            """,
            (
                project_id,
                tenant_id,
                "prd066-runtime",
                "PRD066 Runtime",
                "",
                admin["user_id"],
                admin["user_id"],
                admin["user_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
              fallback_model_id,status,description,created_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,NULL,'active',?,?,?,?)
            """,
            (
                model_alias_id,
                tenant_id,
                "prd066-runtime-chat",
                environment["code"],
                environment["environment_id"],
                model["model_id"],
                "PRD066 runtime governed Chat",
                admin["user_id"],
                now,
                now,
            ),
        )
        # 唯一 active Project Key 是 Session 创建和 Gateway grant 消费共同复核的确定性边界。
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?,environment_id=?,updated_at=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (
                project_id,
                environment["environment_id"],
                now,
                tenant_id,
                virtual_key["virtual_key_id"],
            ),
        )
        # 非零 CNY 单价使 Usage、价格快照、预算结算和 Chat 六位成本展示都能被真实验证。
        connection.execute(
            """
            UPDATE models
            SET input_price_per_1k=0.1,output_price_per_1k=0.2,currency='CNY',
                price_schema_version='prd066-runtime-v1',updated_at=?
            WHERE tenant_id=? AND model_id=?
            """,
            (now, tenant_id, model["model_id"]),
        )
        connection.execute(
            """
            INSERT INTO budget_rules(
              budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
              daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
              exhausted_action,status,created_at,updated_at
            ) VALUES(
              'br_prd066_runtime',?,?, 'virtual_key',?,'PRD066 Runtime Budget',
              100,80,100,'block','active',?,?
            )
            """,
            (
                tenant_id,
                environment["environment_id"],
                virtual_key["virtual_key_id"],
                now,
                now,
            ),
        )
        pattern = "PRD066_RUNTIME_PROMPT_[0-9]+"
        pattern_hash = "sha256:" + hashlib.sha256(
            f"regex:{pattern}".encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO dlp_rules(
              dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,
              action,status,priority,created_at,updated_at
            ) VALUES(
              'dlp_prd066_runtime',?,'PRD066 Runtime DLP','regex',?,?,'log_only',
              'active',100,?,?
            )
            """,
            (tenant_id, pattern, pattern_hash, now, now),
        )
        second_tenant_id = "tenant_prd066_runtime_second"
        second_slug = "prd066-runtime-second"
        second_email = "prd066-runtime-second@example.invalid"
        # 0058 的 tenant 初始化 trigger 必须真实创建 revision 3 权限和六角色默认矩阵。
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (second_tenant_id, second_slug, "PRD066 Runtime Second", now, now),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'viewer','active',?,?)
            """,
            (
                "user_prd066_runtime_second",
                second_tenant_id,
                second_email,
                "PRD066 Runtime Second Viewer",
                hash_password(SECOND_PASSWORD),
                now,
                now,
            ),
        )
        connection.commit()
        return RuntimeGraph(
            database_url=database_url,
            tenant_id=tenant_id,
            project_id=project_id,
            environment_id=str(environment["environment_id"]),
            model_alias_id=model_alias_id,
            virtual_key_id=str(virtual_key["virtual_key_id"]),
            developer_email=developer_email,
            second_tenant_slug=second_slug,
            second_email=second_email,
        )
    # 准备失败必须回滚整个业务夹具，不能留下半绑定 Key 被后续批次误用。
    except (sqlite3.Error, AcceptanceFailure):
        connection.rollback()
        raise
    finally:
        connection.close()


def _service_environment(
    run_directory: Path,
    graph: RuntimeGraph,
    provider_base_url: str,
    gateway_port: int,
    control_port: int,
    console_port: int,
) -> dict[str, str]:
    """构造最小子进程环境，把数据库、spool、TEMP/TMP 和内部 URL 固定到本批次。"""

    allowed_system = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed_system
    }
    source_roots = (
        REPOSITORY_ROOT / "shared-sdk" / "python",
        REPOSITORY_ROOT / "gateway-data-plane" / "src",
        REPOSITORY_ROOT / "control-plane-api" / "src",
    )
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(item) for item in source_roots),
            "TEMP": str(run_directory / "temp"),
            "TMP": str(run_directory / "temp"),
            "METAHUB_ENV": "dev",
            "METAHUB_DATABASE_URL": graph.database_url,
            "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "METAHUB_AUTH_TOKEN_SECRET": AUTH_TOKEN_SECRET,
            "METAHUB_INTERNAL_CHAT_TOKEN": INTERNAL_CHAT_TOKEN,
            "METAHUB_GATEWAY_API_BASE_URL": f"http://{LOOPBACK_HOST}:{gateway_port}",
            "METAHUB_CONTROL_API_BASE_URL": f"http://{LOOPBACK_HOST}:{control_port}",
            "METAHUB_ALLOWED_ORIGINS": f"http://localhost:{console_port}",
            "METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL": provider_base_url,
            "METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY": PROVIDER_KEY,
            "METAHUB_SEED_VIRTUAL_KEY": VIRTUAL_KEY,
            "METAHUB_PROVIDER_HEALTH_ENABLED": "false",
            "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
            "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
            "METAHUB_FINOPS_USAGE_SPOOL_ROOT": str(run_directory / "spool"),
            "METAHUB_LOG_LEVEL": "INFO",
            "METAHUB_CONSOLE_PORT": str(console_port),
        }
    )
    return environment


def _start_process(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    run_directory: Path,
) -> ManagedProcess:
    """启动一个受控子进程并把 stdout/stderr 固定到当前批次日志。"""

    stdout_file = (run_directory / "logs" / f"{name}.stdout.log").open("wb")
    stderr_file = (run_directory / "logs" / f"{name}.stderr.log").open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    # 启动异常可能包含系统路径，不向证据传播，只保留固定服务名分类。
    except OSError:
        stdout_file.close()
        stderr_file.close()
        raise AcceptanceFailure(f"service.{name}", "start_failed") from None
    return ManagedProcess(name, process, stdout_file, stderr_file)


def _stop_process(item: ManagedProcess) -> None:
    """有界终止验收器拥有的子进程并关闭日志句柄。"""

    # 已正常退出时只关闭文件；仍运行时先发送普通终止，超时才杀死该精确 PID。
    if item.process.poll() is None:
        item.process.terminate()
        try:
            item.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait(timeout=5)
    item.stdout_file.close()
    item.stderr_file.close()


def _wait_http(url: str, process: ManagedProcess, stage: str) -> int:
    """轮询单个本地健康端点，子进程提前退出或超时均失败关闭。"""

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        # 子进程退出后继续轮询没有意义，也不能错误连接到同端口的后来进程。
        if process.process.poll() is not None:
            raise AcceptanceFailure(stage, "process_exited")
        try:
            response = httpx.get(url, timeout=1.0, follow_redirects=False)
            if response.status_code == 200:
                return response.status_code
        # 启动窗口内连接拒绝是预期轮询状态，只按固定阶段继续等待，不能把底层
        # socket 异常正文写进验收证据或误判为业务接口失败。
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise AcceptanceFailure(stage, "health_timeout")


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造仅在当前 HTTP 调用存活的认证与 request_id Header。"""

    # Bearer 只进入当前请求内存，调用方不得打印返回对象或把它写入证据文件。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _login(
    client: httpx.Client,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    request_id: str,
) -> str:
    """通过真实 Control 登录并返回内存 Token；响应正文不写入证据或日志。"""

    response = client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    _require(response.status_code == 200, "http.login", "unexpected_status")
    payload = response.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    _require(isinstance(token, str) and len(token) > 40, "http.login", "token_missing")
    # 登录 Token 只交给当前验收调用链，最终 JSON、日志和异常均不得持久化其明文。
    return token


def _assert_error(
    response: httpx.Response,
    status: int,
    code: str,
    stage: str,
    expected_request_id: str,
) -> dict[str, object]:
    """验证统一错误状态、固定码及 request/trace ID，并返回脱敏链路证据。"""

    _require(response.status_code == status, stage, "status")
    payload = response.json()
    valid = (
        isinstance(payload, dict)
        and payload.get("code") == code
        and payload.get("request_id") == expected_request_id
        and isinstance(payload.get("trace_id"), str)
        and bool(payload.get("trace_id"))
    )
    _require(valid, stage, "contract")
    # 错误证据只保留固定合同字段和链路 ID，禁止把完整响应、建议或诊断正文写入 JSON。
    return {
        "status": status,
        "code": code,
        "request_id": expected_request_id,
        "trace_id": str(payload["trace_id"]),
    }


def _revoke_developer_chat_permission(graph: RuntimeGraph) -> None:
    """合法撤销隔离租户 Developer 的 chat.use，并同步 revision/hash 供实时门禁复核。"""

    connection = connect_database(graph.database_url)
    try:
        role = connection.execute(
            """
            SELECT role_id,permission_revision FROM roles
            WHERE tenant_id=? AND code='developer'
            """,
            (graph.tenant_id,),
        ).fetchone()
        permission = connection.execute(
            """
            SELECT permission_id FROM permissions
            WHERE tenant_id=? AND code='chat.use'
            """,
            (graph.tenant_id,),
        ).fetchone()
        _require(role is not None and permission is not None, "permission.revoke", "missing")
        connection.execute(
            """
            DELETE FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=?
            """,
            (graph.tenant_id, role["role_id"], permission["permission_id"]),
        )
        remaining = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (graph.tenant_id, role["role_id"]),
            ).fetchall()
        }
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(remaining),
                now,
                graph.tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
    # 撤权必须全有或全无，避免关系已删除但认证 revision 未推进。
    except (sqlite3.Error, AcceptanceFailure):
        connection.rollback()
        raise
    finally:
        connection.close()


def _exercise_http(
    control_port: int,
    graph: RuntimeGraph,
) -> dict[str, object]:
    """执行主流程、重复、匿名、跨租户、清空和实时撤权的真实 HTTP 验收。"""

    with httpx.Client(
        base_url=f"http://{LOOPBACK_HOST}:{control_port}",
        timeout=httpx.Timeout(15.0, connect=3.0),
        follow_redirects=False,
    ) as client:
        anonymous = client.get(
            "/chat/options", headers={"X-Request-ID": "req_prd066_runtime_anonymous"}
        )
        anonymous_evidence = _assert_error(
            anonymous,
            401,
            "auth.token_missing",
            "http.anonymous",
            "req_prd066_runtime_anonymous",
        )
        developer_token = _login(
            client,
            tenant_slug="default",
            email=graph.developer_email,
            password=DEVELOPER_PASSWORD,
            request_id="req_prd066_runtime_developer_login",
        )
        second_token = _login(
            client,
            tenant_slug=graph.second_tenant_slug,
            email=graph.second_email,
            password=SECOND_PASSWORD,
            request_id="req_prd066_runtime_second_login",
        )
        options = client.get(
            "/chat/options",
            headers=_headers(developer_token, "req_prd066_runtime_options"),
        )
        _require(options.status_code == 200, "http.options", "status")
        options_payload = options.json()
        _require(
            any(item.get("availability") == "ready" for item in options_payload["projects"]),
            "http.options",
            "project_unavailable",
        )
        created = client.post(
            "/chat/sessions",
            json={
                "project_id": graph.project_id,
                "environment_id": graph.environment_id,
                "model_alias_id": graph.model_alias_id,
            },
            headers=_headers(developer_token, "req_prd066_runtime_create"),
        )
        _require(created.status_code == 201, "http.create", "status")
        created_payload = created.json()
        chat_session_id = created_payload["session"]["chat_session_id"]
        client_message_id = "66666666-6666-4666-8666-666666666666"
        succeeded = client.post(
            f"/chat/sessions/{chat_session_id}/messages",
            json={
                "client_message_id": client_message_id,
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(developer_token, MESSAGE_REQUEST_ID),
        )
        _require(succeeded.status_code == 200, "http.message", "status")
        success_payload = succeeded.json()
        execution = success_payload.get("execution", {})
        success_contract = (
            success_payload.get("assistant", {}).get("content") == ASSISTANT_CANARY
            and execution.get("request_id") == MESSAGE_REQUEST_ID
            and isinstance(execution.get("trace_id"), str)
            and bool(execution.get("trace_id"))
            and success_payload.get("request_id") == MESSAGE_REQUEST_ID
            and success_payload.get("trace_id") == execution.get("trace_id")
            and execution.get("input_tokens") == 11
            and execution.get("output_tokens") == 7
            and execution.get("total_tokens") == 18
            and execution.get("cost_cny") == "0.002500"
            and execution.get("policy", {}).get("decision") == "allow"
            and execution.get("cache_status") in {"not_configured", "miss"}
        )
        _require(success_contract, "http.message", "summary_contract")
        duplicate = client.post(
            f"/chat/sessions/{chat_session_id}/messages",
            json={
                "client_message_id": client_message_id,
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(developer_token, "req_prd066_runtime_duplicate"),
        )
        duplicate_evidence = _assert_error(
            duplicate,
            409,
            "chat.message_duplicate",
            "http.duplicate",
            "req_prd066_runtime_duplicate",
        )
        cross_tenant = client.delete(
            f"/chat/sessions/{chat_session_id}",
            headers=_headers(second_token, "req_prd066_runtime_cross_tenant"),
        )
        cross_tenant_evidence = _assert_error(
            cross_tenant,
            404,
            "chat.session_not_found",
            "http.cross_tenant",
            "req_prd066_runtime_cross_tenant",
        )
        cleared = client.delete(
            f"/chat/sessions/{chat_session_id}",
            headers=_headers(developer_token, "req_prd066_runtime_clear"),
        )
        _require(cleared.status_code == 204, "http.clear", "status")
        _revoke_developer_chat_permission(graph)
        revoked = client.get(
            "/chat/options",
            headers=_headers(developer_token, "req_prd066_runtime_revoked"),
        )
        revoked_evidence = _assert_error(
            revoked,
            403,
            "auth.permission_denied",
            "http.revoked",
            "req_prd066_runtime_revoked",
        )
        navigation = client.get(
            "/console/navigation",
            headers=_headers(developer_token, "req_prd066_runtime_navigation"),
        )
        _require(navigation.status_code == 200, "http.navigation", "status")
        item_ids = {item["id"] for item in navigation.json()["navigation_items"]}
        _require("enterprise-chat" not in item_ids, "http.navigation", "chat_not_revoked")
        return {
            "anonymous": anonymous_evidence,
            "options_status": options.status_code,
            "create_status": created.status_code,
            "message_status": succeeded.status_code,
            "message_request_id": MESSAGE_REQUEST_ID,
            "message_trace_id": str(execution["trace_id"]),
            "duplicate": duplicate_evidence,
            "cross_tenant": cross_tenant_evidence,
            "clear_status": cleared.status_code,
            "revoked": revoked_evidence,
            "navigation_removed_after_revoke": True,
        }


def _database_evidence(graph: RuntimeGraph) -> dict[str, object]:
    """读取 tenant-qualified 治理事实和关闭状态，不返回正文、凭据或内部资源 ID。"""

    connection = connect_database(graph.database_url)
    try:
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        request_log = connection.execute(
            """
            SELECT request_id,trace_id,status,endpoint,policy_decision,policy_reason_code,
                   policy_snapshot_status,
                   dlp_status,dlp_action,dlp_match_count,fallback_triggered,
                   routing_attempt_count,final_routing_attempt_id
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, MESSAGE_REQUEST_ID),
        ).fetchone()
        usage = connection.execute(
            """
            SELECT COUNT(*) AS row_count,SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,SUM(total_tokens) AS total_tokens,
                   SUM(cache_tokens) AS cache_tokens,SUM(cost_cny) AS cost_cny,
                   MIN(estimated) AS min_estimated,MAX(estimated) AS max_estimated
            FROM usage_ledger WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, MESSAGE_REQUEST_ID),
        ).fetchone()
        routing = connection.execute(
            """
            SELECT COUNT(*) AS row_count,
                   SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded_count,
                   SUM(CASE WHEN usage_status='recorded' THEN 1 ELSE 0 END) AS recorded_count
            FROM routing_attempts WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, MESSAGE_REQUEST_ID),
        ).fetchone()
        budget = connection.execute(
            """
            SELECT COUNT(*) AS reservation_count,
                   SUM(reservation.reserved_cost_cny) AS reserved_cost_cny,
                   SUM(
                     CASE WHEN EXISTS (
                       SELECT 1 FROM usage_ledger usage
                       WHERE usage.tenant_id=reservation.tenant_id
                         AND usage.request_id=reservation.request_id
                         AND usage.estimated=0
                     ) THEN 1 ELSE 0 END
                   ) AS settled_by_formal_usage_count
            FROM budget_reservations reservation
            WHERE reservation.tenant_id=? AND reservation.request_id=?
            """,
            (graph.tenant_id, MESSAGE_REQUEST_ID),
        ).fetchone()
        dlp_match_count = connection.execute(
            """
            SELECT COUNT(*) FROM dlp_matches WHERE tenant_id=? AND request_id=?
            """,
            (graph.tenant_id, MESSAGE_REQUEST_ID),
        ).fetchone()[0]
        chat_state = connection.execute(
            """
            SELECT
              SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_sessions,
              SUM(CASE WHEN status='cleared' THEN 1 ELSE 0 END) AS cleared_sessions
            FROM chat_sessions WHERE tenant_id=?
            """,
            (graph.tenant_id,),
        ).fetchone()
        issued_grants = connection.execute(
            """
            SELECT COUNT(*) FROM chat_execution_grants
            WHERE tenant_id=? AND status='issued'
            """,
            (graph.tenant_id,),
        ).fetchone()[0]
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        _require(request_log is not None, "database.request_log", "missing")
        _require(
            request_log["status"] == "success"
            and request_log["request_id"] == MESSAGE_REQUEST_ID
            and isinstance(request_log["trace_id"], str)
            and bool(request_log["trace_id"])
            and request_log["endpoint"] == "/internal/chat/completions"
            and request_log["policy_decision"] == "allow"
            and request_log["dlp_status"] == "matched"
            and request_log["dlp_action"] == "log_only"
            and int(request_log["dlp_match_count"]) == 1,
            "database.request_log",
            "governance_incomplete",
        )
        _require(
            int(usage["row_count"] or 0) >= 1
            and int(usage["input_tokens"] or 0) == 11
            and int(usage["output_tokens"] or 0) == 7
            and int(usage["total_tokens"] or 0) == 18
            and int(usage["min_estimated"] or 0) == 0
            and int(usage["max_estimated"] or 0) == 0,
            "database.usage",
            "invalid",
        )
        _require(
            int(routing["row_count"] or 0) >= 1
            and int(routing["succeeded_count"] or 0) == 1
            and int(routing["recorded_count"] or 0) == 1,
            "database.routing",
            "invalid",
        )
        _require(
            int(budget["reservation_count"] or 0) >= 1
            and int(budget["settled_by_formal_usage_count"] or 0)
            == int(budget["reservation_count"] or 0),
            "database.budget",
            "invalid",
        )
        _require(
            integrity == "ok"
            and foreign_key_violations == 0
            and int(chat_state["active_sessions"] or 0) == 0
            and int(issued_grants) == 0,
            "database.closeout",
            "invalid",
        )
        return {
            "migration_count": int(migration[0]),
            "latest_migration": str(migration[1]),
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "request_log": {
                "request_id": request_log["request_id"],
                "trace_id": request_log["trace_id"],
                "status": request_log["status"],
                "endpoint": request_log["endpoint"],
                "policy_decision": request_log["policy_decision"],
                "policy_reason_code": request_log["policy_reason_code"],
                "policy_snapshot_status": request_log["policy_snapshot_status"],
                "dlp_status": request_log["dlp_status"],
                "dlp_action": request_log["dlp_action"],
                "dlp_match_count": int(request_log["dlp_match_count"]),
                "fallback_triggered": bool(request_log["fallback_triggered"]),
                "routing_attempt_count": int(request_log["routing_attempt_count"]),
                "has_final_routing_attempt": bool(request_log["final_routing_attempt_id"]),
            },
            "usage": {
                "rows": int(usage["row_count"]),
                "input_tokens": int(usage["input_tokens"]),
                "output_tokens": int(usage["output_tokens"]),
                "total_tokens": int(usage["total_tokens"]),
                "cache_tokens": int(usage["cache_tokens"] or 0),
                # 即使这里只生成证据，也沿用财务字段的十进制定点语义，避免二进制浮点
                # 在不同 Python/SQLite 组合下产生末位漂移。
                "cost_cny": format(Decimal(str(usage["cost_cny"])), ".6f"),
                "all_formal": True,
            },
            "routing_attempts": int(routing["row_count"]),
            "budget_reservations": int(budget["reservation_count"]),
            "budget_reservations_settled_by_formal_usage": int(
                budget["settled_by_formal_usage_count"]
            ),
            "dlp_matches": int(dlp_match_count),
            "active_chat_sessions": int(chat_state["active_sessions"] or 0),
            "cleared_chat_sessions": int(chat_state["cleared_sessions"] or 0),
            "issued_grants": int(issued_grants),
        }
    finally:
        connection.close()


def _privacy_hits(run_directory: Path, graph: RuntimeGraph) -> dict[str, int]:
    """扫描隔离数据库和服务日志中的六类明文金丝雀，返回每类文件命中总数。"""

    sensitive_values = (
        PROMPT_CANARY.encode("utf-8"),
        ASSISTANT_CANARY.encode("utf-8"),
        PROVIDER_KEY.encode("utf-8"),
        VIRTUAL_KEY.encode("utf-8"),
        INTERNAL_CHAT_TOKEN.encode("utf-8"),
        AUTH_TOKEN_SECRET.encode("utf-8"),
        ADMIN_PASSWORD.encode("utf-8"),
        DEVELOPER_PASSWORD.encode("utf-8"),
        SECOND_PASSWORD.encode("utf-8"),
    )
    database_path = Path(graph.database_url.removeprefix("sqlite:///"))
    database_files = [
        path
        for path in (database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))
        if path.exists()
    ]
    log_files = list((run_directory / "logs").glob("*.log"))

    def count(files: list[Path]) -> int:
        """按二进制读取有界验收文件，统计任意敏感值出现次数。"""

        total = 0
        for path in files:
            data = path.read_bytes()
            total += sum(data.count(value) for value in sensitive_values)
        return total

    return {"database": count(database_files), "service_logs": count(log_files)}


def _write_evidence(run_directory: Path, payload: dict[str, object]) -> Path:
    """以同目录原子替换写入脱敏 JSON，并返回正式证据路径。"""

    target = run_directory / "runtime-evidence.json"
    temporary = run_directory / "runtime-evidence.pending.json"
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """解析可选安全 run-id；证据根和服务地址不允许由命令行重定向。"""

    parser = argparse.ArgumentParser(prog="prd066-runtime-acceptance")
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行隔离迁移、真实服务、HTTP、治理事实、隐私和关闭终态验收。"""

    arguments = _parse_arguments(argv)
    run_id = _safe_run_id(arguments.run_id)
    run_directory = _create_run_directory(run_id)
    provider = MockProvider()
    processes: list[ManagedProcess] = []
    failure: AcceptanceFailure | None = None
    evidence: dict[str, object] | None = None
    try:
        provider_base_url = provider.start()
        graph = _prepare_database(run_directory, provider_base_url)
        gateway_port, control_port, console_port = (_free_port() for _ in range(3))
        _require(
            len({gateway_port, control_port, console_port}) == 3,
            "config.ports",
            "collision",
        )
        environment = _service_environment(
            run_directory,
            graph,
            provider_base_url,
            gateway_port,
            control_port,
            console_port,
        )
        processes.append(
            _start_process(
                "gateway",
                [
                    str(PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_gateway.app:app",
                    "--host",
                    LOOPBACK_HOST,
                    "--port",
                    str(gateway_port),
                    "--no-access-log",
                ],
                cwd=REPOSITORY_ROOT,
                environment=environment,
                run_directory=run_directory,
            )
        )
        processes.append(
            _start_process(
                "control",
                [
                    str(PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_control.app:app",
                    "--host",
                    LOOPBACK_HOST,
                    "--port",
                    str(control_port),
                    "--no-access-log",
                    "--no-proxy-headers",
                ],
                cwd=REPOSITORY_ROOT,
                environment=environment,
                run_directory=run_directory,
            )
        )
        node = shutil.which("node", path=environment.get("PATH"))
        _require(node is not None, "service.console", "node_missing")
        processes.append(
            _start_process(
                "console",
                [node, "scripts/dev-server.mjs"],
                cwd=REPOSITORY_ROOT / "console-web",
                environment=environment,
                run_directory=run_directory,
            )
        )
        health = {
            "gateway": _wait_http(
                f"http://{LOOPBACK_HOST}:{gateway_port}/health",
                processes[0],
                "health.gateway",
            ),
            "control": _wait_http(
                f"http://{LOOPBACK_HOST}:{control_port}/health",
                processes[1],
                "health.control",
            ),
            "console": _wait_http(
                f"http://{LOOPBACK_HOST}:{console_port}/",
                processes[2],
                "health.console",
            ),
        }
        http_evidence = _exercise_http(control_port, graph)
        _require(provider.call_count == 1, "provider", "call_count")
        _require(provider.invalid_count == 0, "provider", "invalid_request")
        database_evidence = _database_evidence(graph)
        # WAL checkpoint 在隐私扫描前把最终状态收敛，扫描仍同时覆盖残留 WAL/SHM（若存在）。
        connection = connect_database(graph.database_url)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        privacy = _privacy_hits(run_directory, graph)
        _require(
            privacy["database"] == 0 and privacy["service_logs"] == 0,
            "privacy",
            "plaintext_detected",
        )
        evidence = {
            "status": "passed",
            "prd_id": "PRD-066",
            "run_id": run_id,
            "services": health,
            "http": http_evidence,
            "governance": database_evidence,
            "provider": {"calls": provider.call_count, "invalid_requests": 0},
            "privacy": {
                "database_plaintext_hits": privacy["database"],
                "service_log_plaintext_hits": privacy["service_logs"],
            },
            "closeout": {
                "active_chat_sessions": database_evidence["active_chat_sessions"],
                "issued_grants": database_evidence["issued_grants"],
            },
            "all_108_prds_complete": False,
        }
    except AcceptanceFailure as exc:
        failure = exc
    # 未分类错误只能转换为固定失败，避免 traceback 带出正文、Token 或数据库路径。
    except Exception as exc:
        # 仅保留异常类名帮助定位脚本缺陷；异常正文和 traceback 可能携带 SQL、正文或本机路径。
        failure = AcceptanceFailure("runtime", type(exc).__name__.lower())
    finally:
        for item in reversed(processes):
            try:
                _stop_process(item)
            # 清理只允许处理当前验收器持有的精确 PID；失败时收敛为固定分类，既不
            # 扩大到用户进程，也不把系统异常或命令行环境中的凭据写入证据。
            except (OSError, subprocess.SubprocessError):
                if failure is None:
                    failure = AcceptanceFailure("closeout.process", "stop_failed")
        provider.stop()

    if failure is not None:
        _write_evidence(
            run_directory,
            {
                "status": "failed",
                "prd_id": "PRD-066",
                "run_id": run_id,
                "stage": failure.stage,
                "code": failure.code,
                "all_108_prds_complete": False,
            },
        )
        print(json.dumps({"status": "failed", "stage": failure.stage, "code": failure.code}))
        return 1

    _require(evidence is not None, "evidence", "missing")
    evidence["closeout"]["owned_processes_running"] = sum(
        1 for item in processes if item.process.poll() is None
    )
    target = _write_evidence(run_directory, evidence)
    digest = hashlib.sha256(target.read_bytes()).hexdigest().upper()
    print(
        json.dumps(
            {
                "status": "passed",
                "run_id": run_id,
                "evidence_sha256": digest,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
