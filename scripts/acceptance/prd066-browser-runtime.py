"""PRD-066 开发环境真实 Edge 验收编排器。

本脚本只补齐浏览器验收所需的最小开发租户资源，并复用已经运行在
``localhost:8000/8001/5173`` 的 Gateway、Control 和 Console。上游由本进程在数据库
已批准的回环 Endpoint 上临时提供；请求正文、响应正文、Provider Key、登录密码和内部
Token 均不写入文件、证据或控制台。浏览器流程由同目录的 Node 验收器执行。
"""

from __future__ import annotations

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
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from metahub_shared.db import connect_database
from metahub_shared.db.security import hash_password


# 所有可写证据和数据库都固定在仓库内；调用方 cwd 与环境变量不能把路径重定向到外部。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = REPOSITORY_ROOT / ".data" / "metahub-dev-prd031.db"
ARTIFACT_ROOT = REPOSITORY_ROOT / ".artifacts" / "prd066-browser"
BROWSER_SCRIPT = REPOSITORY_ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs"

# 专用稳定 ID 使准备步骤幂等；这些字段不包含账号、密钥或业务正文。
DEVELOPER_USER_ID = "user_prd066_browser_developer"
DEVELOPER_EMAIL = "prd066-runtime-developer@example.invalid"
PROJECT_ID = "prj_prd066_browser"
PROJECT_CODE = "prd066-browser"
MODEL_ALIAS_ID = "mal_prd066_browser"
MODEL_ALIAS = "prd066-browser-chat"


class BrowserRuntimeFailure(RuntimeError):
    """保存可公开的固定失败阶段和分类，不携带底层异常正文。"""

    def __init__(self, stage: str, code: str) -> None:
        """用源码内固定 stage/code 构造安全失败；不接收自由文本参数。"""

        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


class BrowserMockProvider:
    """在数据库批准的回环地址提供内存态 OpenAI-compatible 响应。"""

    def __init__(self, base_url: str) -> None:
        """解析并冻结回环 Endpoint；禁止把 URL 保存到证据或异常信息。"""

        parsed = urlsplit(str(base_url or "").strip())
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "http"
            or hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            # 验收 Provider 只能监听本机明文回环；任何远端或带凭据 URL 都会扩大数据边界。
            raise BrowserRuntimeFailure("provider.endpoint", "not_loopback_http")
        try:
            port = parsed.port or 80
        # 非法端口的原始 URL 可能含敏感 Endpoint，只转换为固定分类。
        except ValueError as exc:
            raise BrowserRuntimeFailure("provider.endpoint", "invalid_port") from exc
        if not 1 <= port <= 65535:
            # 越界端口不能交给 socket 自由报错，防止底层错误正文进入验收输出。
            raise BrowserRuntimeFailure("provider.endpoint", "invalid_port")
        base_path = parsed.path.rstrip("/")
        self._host = "127.0.0.1"
        self._port = port
        self._chat_path = f"{base_path}/chat/completions" or "/chat/completions"
        self._assistant_text = f"prd066-browser-response-{secrets.token_urlsafe(24)}"
        self.call_count = 0
        self.invalid_count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """独占监听已批准回环端口；端口被占用时失败关闭，不终止其他进程。"""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            """校验最小 Chat 合同并返回规范 Usage；不保留请求 Header 或正文。"""

            def do_POST(self) -> None:  # noqa: N802
                """处理一次非流式 Chat；畸形输入只增加匿名计数。"""

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 512 * 1024:
                        # 有界读取防止浏览器验收入口被超大 Provider 请求耗尽内存。
                        raise ValueError
                    raw = self.rfile.read(length)
                    payload = json.loads(raw.decode("utf-8"))
                    messages = payload.get("messages") if isinstance(payload, dict) else None
                    last_message = messages[-1] if isinstance(messages, list) and messages else None
                    authorization = self.headers.get("Authorization", "")
                    valid = (
                        self.path == owner._chat_path
                        and authorization.startswith("Bearer ")
                        and len(authorization) > len("Bearer ")
                        and payload.get("stream") is False
                        and payload.get("model") == "local-chat-model"
                        and isinstance(last_message, dict)
                        and last_message.get("role") == "user"
                        and isinstance(last_message.get("content"), str)
                        and bool(last_message["content"].strip())
                    )
                # 所有解析/形状异常统一为匿名 Provider 失败，不回显正文或自由异常。
                except (IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                    valid = False
                if not valid:
                    owner.invalid_count += 1
                    self._write_json(400, {"error": {"message": "invalid request"}})
                    return
                owner.call_count += 1
                self._write_json(
                    200,
                    {
                        "id": "chatcmpl_prd066_browser",
                        "object": "chat.completion",
                        "model": "local-chat-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": owner._assistant_text,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 13,
                            "completion_tokens": 5,
                            "total_tokens": 18,
                            "prompt_tokens_details": {"cached_tokens": 0},
                        },
                    },
                )

            def _write_json(self, status: int, payload: dict[str, object]) -> None:
                """返回固定 JSON；响应不复制任何来访正文、Header 或 Endpoint。"""

                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                """关闭标准 access log，避免未来 URL 或 Header 旁路进入控制台。"""

                return

        try:
            self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        # 监听失败可能包含 Endpoint 或系统错误，只公开固定端口不可用分类。
        except OSError as exc:
            # 绝不为了抢占回环端口结束未知进程；由调用方决定如何修复本地服务配置。
            raise BrowserRuntimeFailure("provider.listen", "port_unavailable") from exc
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """只关闭本对象创建的 Server 和线程，绝不枚举或终止外部进程。"""

        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；stage/code 必须是源码内固定字符串。"""

    if not condition:
        raise BrowserRuntimeFailure(stage, code)


def _utc_now() -> str:
    """返回迁移触发器接受的 canonical UTC 秒级时间。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _database_url() -> str:
    """返回固定开发数据库 URL，不接受调用方覆盖。"""

    return f"sqlite:///{DATABASE_PATH.as_posix()}"


def _prepare_browser_graph(developer_password: str) -> str:
    """幂等创建浏览器专用 Developer、项目、别名和唯一 Project Key。

    参数：只驻留当前进程内存的随机 Developer 密码。返回：数据库已批准的 Provider
    回环 URL，仅交给内存 mock，不进入控制台和证据。所有写入均带可信 tenant_id。
    """

    connection = connect_database(_database_url())
    try:
        connection.execute("BEGIN IMMEDIATE")
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        _require(
            migration is not None and int(migration[0]) == 58 and migration[1] == "0058",
            "database.migration",
            "not_0058",
        )
        tenant = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default' AND status='active'"
        ).fetchone()
        _require(tenant is not None, "database.tenant", "missing")
        tenant_id = str(tenant["tenant_id"])
        admin = connection.execute(
            """
            SELECT user_id FROM users
            WHERE tenant_id=? AND email='admin@metahub.local' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        environment = connection.execute(
            """
            SELECT environment_id,code FROM environments
            WHERE tenant_id=? AND code='dev' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        model = connection.execute(
            """
            SELECT m.model_id,p.base_url FROM models m
            JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            WHERE m.tenant_id=? AND m.model_alias='default-chat' AND m.status='active'
              AND p.provider_code='local_openai' AND p.status='active'
            """,
            (tenant_id,),
        ).fetchone()
        _require(
            all(item is not None for item in (admin, environment, model)),
            "database.graph",
            "seed_resource_missing",
        )
        now = _utc_now()
        existing_user = connection.execute(
            "SELECT user_id,role FROM users WHERE tenant_id=? AND email=?",
            (tenant_id, DEVELOPER_EMAIL),
        ).fetchone()
        if existing_user is None:
            connection.execute(
                """
                INSERT INTO users(
                  user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,'developer','active',?,?)
                """,
                (
                    DEVELOPER_USER_ID,
                    tenant_id,
                    DEVELOPER_EMAIL,
                    "PRD066 Browser Developer",
                    hash_password(developer_password),
                    now,
                    now,
                ),
            )
        else:
            _require(
                str(existing_user["user_id"]) == DEVELOPER_USER_ID
                and str(existing_user["role"]) == "developer",
                "database.developer",
                "identity_conflict",
            )
            # 每批随机口令使旧验收凭据自动失效；账号角色和 tenant 绝不由客户端传入。
            connection.execute(
                """
                UPDATE users SET password_hash=?,status='active',updated_at=?
                WHERE tenant_id=? AND user_id=? AND role='developer'
                """,
                (hash_password(developer_password), now, tenant_id, DEVELOPER_USER_ID),
            )
        # 每批密码轮换时同步撤销旧登录 Session，避免历史浏览器 Token 在新批次继续有效。
        connection.execute(
            """
            UPDATE sessions
            SET status='revoked',revoked_at=COALESCE(revoked_at,?),updated_at=?
            WHERE tenant_id=? AND user_id=? AND status='active'
            """,
            (now, now, tenant_id, DEVELOPER_USER_ID),
        )
        project = connection.execute(
            "SELECT project_id FROM projects WHERE tenant_id=? AND project_code=?",
            (tenant_id, PROJECT_CODE),
        ).fetchone()
        if project is None:
            connection.execute(
                """
                INSERT INTO projects(
                  project_id,tenant_id,project_code,name,description,owner_user_id,status,
                  created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',?,?,?,?)
                """,
                (
                    PROJECT_ID,
                    tenant_id,
                    PROJECT_CODE,
                    "PRD066 Browser",
                    "",
                    admin["user_id"],
                    admin["user_id"],
                    admin["user_id"],
                    now,
                    now,
                ),
            )
        else:
            _require(
                str(project["project_id"]) == PROJECT_ID,
                "database.project",
                "identity_conflict",
            )
            connection.execute(
                """
                UPDATE projects SET status='active',updated_by=?,updated_at=?
                WHERE tenant_id=? AND project_id=?
                """,
                (admin["user_id"], now, tenant_id, PROJECT_ID),
            )
        alias = connection.execute(
            "SELECT model_alias_id FROM model_aliases WHERE tenant_id=? AND alias=?",
            (tenant_id, MODEL_ALIAS),
        ).fetchone()
        if alias is None:
            connection.execute(
                """
                INSERT INTO model_aliases(
                  model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
                  fallback_model_id,status,description,created_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,NULL,'active',?,?,?,?)
                """,
                (
                    MODEL_ALIAS_ID,
                    tenant_id,
                    MODEL_ALIAS,
                    environment["code"],
                    environment["environment_id"],
                    model["model_id"],
                    "PRD066 browser governed Chat",
                    admin["user_id"],
                    now,
                    now,
                ),
            )
        else:
            _require(
                str(alias["model_alias_id"]) == MODEL_ALIAS_ID,
                "database.alias",
                "identity_conflict",
            )
            connection.execute(
                """
                UPDATE model_aliases
                SET environment=?,environment_id=?,default_model_id=?,status='active',updated_at=?
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (
                    environment["code"],
                    environment["environment_id"],
                    model["model_id"],
                    now,
                    tenant_id,
                    MODEL_ALIAS_ID,
                ),
            )
        bound_keys = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND project_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (tenant_id, PROJECT_ID, environment["environment_id"], environment["code"]),
        ).fetchall()
        if not bound_keys:
            candidate = connection.execute(
                """
                SELECT virtual_key_id FROM virtual_keys
                WHERE tenant_id=? AND project_id IS NULL AND status='active'
                  AND (environment_id IS NULL OR environment_id=?)
                ORDER BY virtual_key_id LIMIT 1
                """,
                (tenant_id, environment["environment_id"]),
            ).fetchone()
            _require(candidate is not None, "database.virtual_key", "unassigned_key_missing")
            connection.execute(
                """
                UPDATE virtual_keys SET project_id=?,environment_id=?,updated_at=?
                WHERE tenant_id=? AND virtual_key_id=? AND project_id IS NULL AND status='active'
                """,
                (
                    PROJECT_ID,
                    environment["environment_id"],
                    now,
                    tenant_id,
                    candidate["virtual_key_id"],
                ),
            )
            _require(
                connection.execute("SELECT changes()").fetchone()[0] == 1,
                "database.virtual_key",
                "bind_race",
            )
        else:
            _require(len(bound_keys) == 1, "database.virtual_key", "not_unique")
        permission = connection.execute(
            """
            SELECT 1 FROM users u
            JOIN roles r ON r.tenant_id=u.tenant_id AND r.code=u.role AND r.status='active'
            JOIN role_permissions rp ON rp.tenant_id=r.tenant_id AND rp.role_id=r.role_id
            JOIN permissions p ON p.tenant_id=rp.tenant_id AND p.permission_id=rp.permission_id
            WHERE u.tenant_id=? AND u.user_id=? AND p.code='chat.use' AND p.status='active'
            """,
            (tenant_id, DEVELOPER_USER_ID),
        ).fetchone()
        _require(permission is not None, "database.permission", "chat_use_missing")
        connection.commit()
        return str(model["base_url"])
    except BrowserRuntimeFailure:
        if connection.in_transaction:
            connection.rollback()
        raise
    # 准备事务的 SQL/约束正文可能带资源 ID，只对外保留稳定数据库分类。
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        # cause 仅驻留当前调用栈供本地调试，CLI 不输出 traceback 或异常正文。
        raise BrowserRuntimeFailure("database.prepare", "sqlite_error") from exc
    finally:
        connection.close()


def _free_debug_port() -> int:
    """在 Edge 验收允许的 19000-19999 范围选择当前空闲端口。"""

    for _ in range(100):
        port = 19000 + secrets.randbelow(1000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            # 端口竞争只尝试下一个允许端口，不结束或探测占用它的外部进程。
            except OSError:
                continue
        return port
    raise BrowserRuntimeFailure("browser.debug_port", "unavailable")


def _edge_path() -> Path:
    """定位已安装的 Microsoft Edge；只返回可执行文件，不读取用户 Profile。"""

    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BrowserRuntimeFailure("browser.edge", "missing")


def _run_id() -> str:
    """生成固定格式的单层证据批次名，避免覆盖历史验收。"""

    stamp = datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    # 随机后缀避免同秒并发批次碰撞，值不与租户、用户或正文关联。
    return f"{stamp}-{secrets.token_hex(3)}"


def _preflight_services() -> None:
    """确认三项既有本地服务可达；响应正文不会写入证据或异常。"""

    for name, url in (
        ("gateway", "http://localhost:8000/health"),
        ("control", "http://localhost:8001/health"),
        ("console", "http://localhost:5173/"),
    ):
        try:
            response = httpx.get(url, timeout=5.0, follow_redirects=False)
        # 网络异常正文可能含本机 Endpoint，只转换为固定服务不可达分类。
        except httpx.HTTPError as exc:
            raise BrowserRuntimeFailure(f"service.{name}", "unreachable") from exc
        _require(response.status_code == 200, f"service.{name}", "unhealthy")


def _cleanup_runtime_state() -> dict[str, int | str]:
    """在浏览器关闭后收敛专用用户的活动 Chat 和 issued grant。

    只更新固定 tenant+owner 图，不删除用量、RequestLog 或治理事实。成功消息和已消费 grant
    保持可追踪；页面正文从未进入这些表。
    """

    connection = connect_database(_database_url())
    try:
        now = _utc_now()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE chat_execution_grants SET status='revoked',revoked_at=?
            WHERE tenant_id=(SELECT tenant_id FROM users WHERE user_id=?)
              AND owner_user_id=? AND status='issued'
            """,
            (now, DEVELOPER_USER_ID, DEVELOPER_USER_ID),
        )
        connection.execute(
            """
            UPDATE chat_sessions
            SET status='cleared',cleared_at=?,state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=(SELECT tenant_id FROM users WHERE user_id=?)
              AND owner_user_id=? AND status='active'
              AND NOT EXISTS(
                SELECT 1 FROM chat_messages m
                WHERE m.tenant_id=chat_sessions.tenant_id
                  AND m.chat_session_id=chat_sessions.chat_session_id
                  AND m.status='in_flight'
              )
            """,
            (now, now, DEVELOPER_USER_ID, DEVELOPER_USER_ID),
        )
        active = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_sessions WHERE owner_user_id=? AND status='active'",
                (DEVELOPER_USER_ID,),
            ).fetchone()[0]
        )
        issued = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_execution_grants WHERE owner_user_id=? AND status='issued'",
                (DEVELOPER_USER_ID,),
            ).fetchone()[0]
        )
        in_flight = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE owner_user_id=? AND status='in_flight'",
                (DEVELOPER_USER_ID,),
            ).fetchone()[0]
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.commit()
        return {
            "active_chat_sessions": active,
            "issued_grants": issued,
            "in_flight_messages": in_flight,
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
        }
    # 清理失败必须回滚，不能留下“部分清空”却发布通过证据。
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        # SQL 和资源 ID 不进入 CLI；cause 只在当前内存调用链中保留。
        raise BrowserRuntimeFailure("database.cleanup", "sqlite_error") from exc
    finally:
        connection.close()


def _write_runtime_evidence(
    artifact_directory: Path,
    *,
    provider_calls: int,
    invalid_provider_calls: int,
    cleanup: dict[str, int | str],
) -> None:
    """写入只含固定状态和计数的编排证据，不记录身份、正文、路径或 Endpoint。"""

    evidence = {
        "status": "passed",
        "prd_id": "PRD-066",
        "browser": "Microsoft Edge",
        "services": {"gateway": 200, "control": 200, "console": 200},
        "provider": {"calls": provider_calls, "invalid_requests": invalid_provider_calls},
        "closeout": cleanup,
    }
    target = artifact_directory / "browser-runtime-evidence.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest().upper()
    print(
        json.dumps(
            {
                "status": "passed",
                "prd_id": "PRD-066",
                "browser_evidence_sha256": digest,
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    """准备资源、运行真实 Edge、收敛状态并发布脱敏证据。"""

    provider: BrowserMockProvider | None = None
    artifact_directory: Path | None = None
    cleanup: dict[str, int | str] | None = None
    failure: BrowserRuntimeFailure | None = None
    try:
        _preflight_services()
        admin_password = os.environ.get("METAHUB_ACCEPTANCE_ADMIN_PASSWORD", "")
        _require(len(admin_password) >= 12, "config.admin", "password_missing")
        developer_password = f"Prd066-Browser-Aa1!{secrets.token_urlsafe(32)}"
        provider = BrowserMockProvider(_prepare_browser_graph(developer_password))
        provider.start()
        node = shutil.which("node")
        _require(node is not None, "browser.node", "missing")
        artifact_directory = ARTIFACT_ROOT / _run_id()
        environment = os.environ.copy()
        environment.update(
            {
                "METAHUB_EDGE_PATH": str(_edge_path()),
                "METAHUB_EDGE_DEBUG_PORT": str(_free_debug_port()),
                "METAHUB_ACCEPTANCE_ARTIFACT_DIR": str(artifact_directory),
                "METAHUB_ACCEPTANCE_RUNTIME_PASSWORD": developer_password,
                "METAHUB_ACCEPTANCE_DEVELOPER_EMAIL": DEVELOPER_EMAIL,
            }
        )
        result = subprocess.run(
            [node, str(BROWSER_SCRIPT)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
        )
        _require(result.returncode == 0, "browser.acceptance", "failed")
    except BrowserRuntimeFailure as exc:
        failure = exc
    # 子进程异常可能包含命令行环境，只保留固定进程错误并禁止 traceback 输出。
    except (OSError, subprocess.SubprocessError) as exc:
        failure = BrowserRuntimeFailure("browser.runtime", "process_error")
        failure.__cause__ = exc
    finally:
        # 只停止当前对象拥有的回环 Server，绝不枚举或结束既有开发服务。
        if provider is not None:
            provider.stop()
        try:
            cleanup = _cleanup_runtime_state()
        except BrowserRuntimeFailure as exc:
            if failure is None:
                failure = exc

    if failure is None:
        try:
            _require(
                provider is not None and artifact_directory is not None,
                "runtime",
                "state_missing",
            )
            _require(provider.call_count == 2, "provider", "call_count")
            _require(provider.invalid_count == 0, "provider", "invalid_request")
            _require(cleanup is not None, "database.cleanup", "missing")
            _require(
                cleanup == {
                    "active_chat_sessions": 0,
                    "issued_grants": 0,
                    "in_flight_messages": 0,
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                },
                "database.cleanup",
                "not_clean",
            )
            _write_runtime_evidence(
                artifact_directory,
                provider_calls=provider.call_count,
                invalid_provider_calls=provider.invalid_count,
                cleanup=cleanup,
            )
        except BrowserRuntimeFailure as exc:
            # 最终计数或清理断言也必须使用固定分类，不能退回含本机路径的 traceback。
            failure = exc
    if failure is not None:
        print(
            json.dumps(
                {"status": "failed", "stage": failure.stage, "code": failure.code},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
