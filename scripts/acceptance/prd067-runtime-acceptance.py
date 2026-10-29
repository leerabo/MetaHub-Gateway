"""PRD-067 开发库真实 HTTP、治理事实与隐私验收器。

本脚本只连接固定的本地 Gateway/Control 和开发 SQLite 数据库，不启动远端连接，也不把
Prompt、变量值、Response、登录口令或 Token 写入证据。受控 OpenAI-compatible Provider 可通过
``--serve-provider`` 模式单独启动；它只保留内存计数并关闭 access log。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from metahub_shared.db import connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256

# 所有地址和可写路径固定在仓库及回环网卡内，命令行不能把验收流量或证据重定向到外部。
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = REPOSITORY_ROOT / ".data" / "metahub-dev-prd031.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH.as_posix()}"
ARTIFACT_ROOT = REPOSITORY_ROOT / ".artifacts" / "prd067-runtime"
SERVICE_LOG_ROOT = REPOSITORY_ROOT / ".tmp" / "prd067-live"
CONTROL_URL = "http://127.0.0.1:8001"
GATEWAY_HEALTH_URL = "http://127.0.0.1:8000/health"
CONSOLE_URL = "http://127.0.0.1:5173/"
PROVIDER_HOST = "127.0.0.1"
PROVIDER_PORT = 11434

# 管理员口令来自仓库既有本地开发约定；新增测试身份每批使用随机口令，只保存 PBKDF2 hash。
ADMIN_EMAIL = "admin@metahub.local"
ADMIN_PASSWORD = "MetaHubLocal!2026"
VIEWER_USER_ID = "user_prd067_runtime_viewer"
VIEWER_EMAIL = "prd067-runtime-viewer@example.invalid"
SECOND_TENANT_ID = "tenant_prd067_runtime"
SECOND_TENANT_SLUG = "prd067-runtime"
SECOND_USER_ID = "user_prd067_runtime_second_admin"
SECOND_EMAIL = "prd067-runtime-second@example.invalid"
PRIMARY_PROJECT_ID = "prj_prd066_browser"
PRIMARY_MODEL_ALIAS_ID = "mal_prd066_browser"
OTHER_PROJECT_ID = "prj_prd067_runtime_other"


class AcceptanceFailure(RuntimeError):
    """只携带源码内固定阶段和分类，避免自由异常复制敏感请求内容。"""

    def __init__(self, stage: str, code: str) -> None:
        """保存安全失败坐标；调用方不得把 HTTP 正文或数据库异常拼入参数。"""

        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


@dataclass(frozen=True)
class RuntimeGraph:
    """保存本批次 HTTP 调用需要的 owner 资源和仅驻留内存的测试口令。"""

    tenant_id: str
    environment_id: str
    provider_id: str
    viewer_password: str
    second_password: str


def _require(condition: bool, stage: str, code: str) -> None:
    """执行失败关闭断言；失败输出始终使用固定 stage/code。"""

    if not condition:
        raise AcceptanceFailure(stage, code)


def _utc_now() -> str:
    """返回迁移触发器接受的 canonical UTC 秒级时间。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request_id(run_token: str, stage: str) -> str:
    """生成稳定可追踪且不含业务正文的验收 request_id。"""

    # 只拼接随机批次和固定阶段，不允许 Prompt、用户输入或资源名称进入链路标识。
    return f"req_prd067_{run_token}_{stage}"


def _serve_provider() -> int:
    """在固定回环端口提供最小 OpenAI-compatible 非流式 Chat 响应。

    Provider 不保存 Header 或正文；响应正文每次进程启动随机生成，验收器会把收到的值加入明文
    扫描集合，从而证明 Response 没有进入数据库和普通服务日志。
    """

    assistant_text = f"PRD067_ASSISTANT_{secrets.token_hex(24)}"

    # Handler 只在当前回环进程存活，不持久化请求对象或任何敏感 Header。
    class Handler(BaseHTTPRequestHandler):
        """校验受控 Chat 请求并关闭默认访问日志，确保敏感正文只存在于内存调用栈。"""

        def do_POST(self) -> None:  # noqa: N802
            """有界读取一次请求；任何形状错误只返回固定 Provider 错误。"""

            valid = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                _require(1 <= length <= 512 * 1024, "provider.request", "size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                messages = payload.get("messages") if isinstance(payload, dict) else None
                last = messages[-1] if isinstance(messages, list) and messages else None
                valid = bool(
                    self.path == "/v1/chat/completions"
                    and self.headers.get("Authorization", "").startswith("Bearer ")
                    and payload.get("stream") is False
                    and payload.get("model") == "local-chat-model"
                    and isinstance(last, dict)
                    and last.get("role") == "user"
                    and isinstance(last.get("content"), str)
                    and last["content"].strip()
                )
            # Provider 解析失败必须统一映射，不能把原始正文或解析器异常返回给调用方。
            except (AcceptanceFailure, IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                # 解析异常可能间接包含请求形状，统一收敛为固定 400，不写 traceback 或正文。
                valid = False
            if not valid:
                self._write_json(400, {"error": {"message": "invalid request"}})
                return
            self._write_json(
                200,
                {
                    "id": "chatcmpl_prd067_runtime",
                    "object": "chat.completion",
                    "model": "local-chat-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": assistant_text},
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
            """返回固定 JSON，不复制来访 Header、URL 参数或请求正文。"""

            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """关闭 BaseHTTPRequestHandler 默认日志，避免正文或 Endpoint 旁路落盘。"""

            return

    server = ThreadingHTTPServer((PROVIDER_HOST, PROVIDER_PORT), Handler)
    print("prd067-provider-ready", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


def _prepare_runtime_graph() -> RuntimeGraph:
    """复核 0059 开发库并幂等准备 Viewer、第二租户和不适用 Project。

    资源准备只在服务启动前后共享的本地开发库内执行。固定 owner 由数据库恢复；随机口令只进入
    当前调用栈和 password hash，旧测试 Session 同事务撤销，避免历史 Token 干扰实时权限验收。
    """

    viewer_password = f"Prd067-Viewer-Aa1!{secrets.token_urlsafe(24)}"
    second_password = f"Prd067-Second-Aa1!{secrets.token_urlsafe(24)}"
    connection = connect_database(DATABASE_URL)
    try:
        connection.execute("BEGIN IMMEDIATE")
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        _require(
            migration is not None and int(migration[0]) == 59 and migration[1] == "0059",
            "database.migration",
            "not_0059",
        )
        tenant = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default' AND status='active'"
        ).fetchone()
        _require(tenant is not None, "database.default_tenant", "missing")
        tenant_id = str(tenant["tenant_id"])
        admin = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND email=? AND status='active'",
            (tenant_id, ADMIN_EMAIL),
        ).fetchone()
        project = connection.execute(
            "SELECT project_id FROM projects WHERE tenant_id=? AND project_id=? AND status='active'",
            (tenant_id, PRIMARY_PROJECT_ID),
        ).fetchone()
        alias = connection.execute(
            """
            SELECT model_alias_id,environment_id FROM model_aliases
            WHERE tenant_id=? AND model_alias_id=? AND status='active'
            """,
            (tenant_id, PRIMARY_MODEL_ALIAS_ID),
        ).fetchone()
        provider = connection.execute(
            """
            SELECT provider_id FROM providers
            WHERE tenant_id=? AND provider_code='local_openai' AND status='active'
            """,
            (tenant_id,),
        ).fetchone()
        _require(
            admin is not None and project is not None and alias is not None and provider is not None,
            "database.graph",
            "missing",
        )
        environment_id = str(alias["environment_id"])
        active_keys = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM virtual_keys
                WHERE tenant_id=? AND project_id=? AND environment_id=? AND status='active'
                """,
                (tenant_id, PRIMARY_PROJECT_ID, environment_id),
            ).fetchone()[0]
        )
        _require(active_keys == 1, "database.project_key", "not_unique")
        now = _utc_now()
        _upsert_user(
            connection,
            tenant_id=tenant_id,
            user_id=VIEWER_USER_ID,
            email=VIEWER_EMAIL,
            display_name="PRD067 Runtime Viewer",
            role="viewer",
            password=viewer_password,
            now=now,
        )
        other = connection.execute(
            "SELECT project_id FROM projects WHERE tenant_id=? AND project_code='prd067-runtime-other'",
            (tenant_id,),
        ).fetchone()
        if other is None:
            connection.execute(
                """
                INSERT INTO projects(
                  project_id,tenant_id,project_code,name,description,owner_user_id,status,
                  created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'active',?,?,?,?)
                """,
                (
                    OTHER_PROJECT_ID,
                    tenant_id,
                    "prd067-runtime-other",
                    "PRD067 Other Project",
                    "",
                    admin["user_id"],
                    admin["user_id"],
                    admin["user_id"],
                    now,
                    now,
                ),
            )
        else:
            _require(str(other["project_id"]) == OTHER_PROJECT_ID, "database.other_project", "conflict")
            connection.execute(
                "UPDATE projects SET status='active',updated_by=?,updated_at=? WHERE tenant_id=? AND project_id=?",
                (admin["user_id"], now, tenant_id, OTHER_PROJECT_ID),
            )
        second = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug=?",
            (SECOND_TENANT_SLUG,),
        ).fetchone()
        if second is None:
            connection.execute(
                """
                INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
                VALUES(?,?,?,'internal_only','active',?,?)
                """,
                (SECOND_TENANT_ID, SECOND_TENANT_SLUG, "PRD067 Runtime", now, now),
            )
        else:
            _require(str(second["tenant_id"]) == SECOND_TENANT_ID, "database.second_tenant", "conflict")
            connection.execute(
                "UPDATE tenants SET status='active',updated_at=? WHERE tenant_id=?",
                (now, SECOND_TENANT_ID),
            )
        _upsert_user(
            connection,
            tenant_id=SECOND_TENANT_ID,
            user_id=SECOND_USER_ID,
            email=SECOND_EMAIL,
            display_name="PRD067 Runtime Second Admin",
            role="admin",
            password=second_password,
            now=now,
        )
        connection.commit()
        # 口令仍只在当前验收进程内传递，返回对象不会被序列化到正式证据。
        return RuntimeGraph(
            tenant_id,
            environment_id,
            str(provider["provider_id"]),
            viewer_password,
            second_password,
        )
    except AcceptanceFailure:
        if connection.in_transaction:
            connection.rollback()
        raise
    # SQLite 错误可能包含表名、路径或约束正文，统一转换为固定分类并保持事务原子回滚。
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        # 固定错误码不拼接底层异常；cause 只服务当前本地调用栈调试。
        raise AcceptanceFailure("database.prepare", "sqlite_error") from exc
    finally:
        connection.close()


def _upsert_user(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    user_id: str,
    email: str,
    display_name: str,
    role: str,
    password: str,
    now: str,
) -> None:
    """幂等写入一个固定测试身份，并撤销该身份的历史登录 Session。"""

    existing = connection.execute(
        "SELECT user_id,role FROM users WHERE tenant_id=? AND email=?",
        (tenant_id, email),
    ).fetchone()
    password_hash = hash_password(password)
    if existing is None:
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?, 'active',?,?)
            """,
            (user_id, tenant_id, email, display_name, password_hash, role, now, now),
        )
    else:
        _require(
            str(existing["user_id"]) == user_id and str(existing["role"]) == role,
            "database.test_user",
            "identity_conflict",
        )
        connection.execute(
            """
            UPDATE users SET password_hash=?,display_name=?,status='active',updated_at=?
            WHERE tenant_id=? AND user_id=? AND role=?
            """,
            (password_hash, display_name, now, tenant_id, user_id, role),
        )
    connection.execute(
        """
        UPDATE sessions SET status='revoked',revoked_at=COALESCE(revoked_at,?),updated_at=?
        WHERE tenant_id=? AND user_id=? AND status='active'
        """,
        (now, now, tenant_id, user_id),
    )


# 认证 Header 的构造集中在一个函数内，避免测试调用意外加入 tenant 或角色伪造字段。
def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造只含登录会话和稳定链路标识的公开 API Header，禁止客户端注入租户身份。"""

    # Token 只返回给 httpx 当前调用，不打印、不落盘，也不加入证据对象。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _json_response(
    response: httpx.Response,
    status: int,
    stage: str,
    *,
    request_id: str | None = None,
    no_store: bool = True,
) -> dict[str, Any]:
    """校验状态、统一链路标识和 no-store，再返回当前调用栈内 JSON。"""

    _require(response.status_code == status, stage, "status")
    try:
        payload = response.json()
    # JSON 解析异常不输出响应正文，防止意外把定义或模型响应复制到失败证据。
    except (TypeError, ValueError) as exc:
        raise AcceptanceFailure(stage, "json") from exc
    _require(isinstance(payload, dict), stage, "shape")
    # 调用方传入 request_id 时必须逐字回传，证明统一链路信封没有被中间层替换。
    if request_id is not None:
        _require(payload.get("request_id") == request_id, stage, "request_id")
    _require(isinstance(payload.get("trace_id"), str) and payload["trace_id"], stage, "trace_id")
    if no_store:
        _require(response.headers.get("cache-control") == "no-store", stage, "cache_control")
    return payload


def _error_response(
    response: httpx.Response,
    status: int,
    code: str,
    stage: str,
    request_id: str,
) -> dict[str, Any]:
    """校验目录化错误，不读取或输出自由 message/detail。"""

    payload = _json_response(response, status, stage, request_id=request_id)
    _require(payload.get("code") == code, stage, "code")
    return {"status": status, "code": code, "request_id": request_id}


def _login(
    client: httpx.Client,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    request_id: str,
) -> str:
    """登录一个测试身份并只在当前进程内返回 Session Token。"""

    response = client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    # PRD-067 的 ``no-store`` 门禁属于 Prompt 模板和企业 Chat 业务接口；既有登录
    # 合同只冻结统一 request/trace 信封，不能让本专项凭空扩大 PRD-059 的响应头合同。
    payload = _json_response(
        response,
        200,
        "http.login",
        request_id=request_id,
        no_store=False,
    )
    token = payload.get("access_token")
    _require(isinstance(token, str) and bool(token), "http.login", "token")
    # Session Token 只向当前函数调用者返回，不进入异常、日志或最终 JSON 证据。
    return token


def _logout_acceptance_session(
    client: httpx.Client,
    token: str,
    request_id: str,
    cleanup_state: dict[str, object],
) -> None:
    """通过正式认证 API 撤销一个验收 Session，并累计不含 Token 的清理计数。"""

    payload = _json_response(
        client.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": request_id},
        ),
        200,
        "cleanup.logout",
        request_id=request_id,
        no_store=False,
    )
    _require(payload.get("logged_out") is True, "cleanup.logout", "not_logged_out")
    cleanup_state["sessions_logged_out"] = int(cleanup_state["sessions_logged_out"]) + 1


def _disable_runtime_provider_key(
    client: httpx.Client,
    admin_token: str,
    provider_key_id: str,
    request_id: str,
    cleanup_state: dict[str, object],
) -> None:
    """通过后端权限与审计链禁用本轮一次性 Provider Key，禁止测试凭据长期 active。"""

    payload = _json_response(
        client.patch(
            f"/provider-keys/{provider_key_id}",
            json={"status": "disabled"},
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": request_id},
        ),
        200,
        "cleanup.provider_key",
        request_id=request_id,
        no_store=False,
    )
    _require(
        isinstance(payload.get("provider_key"), dict)
        and payload["provider_key"].get("status") == "disabled",
        "cleanup.provider_key",
        "not_disabled",
    )
    cleanup_state["provider_key_disabled"] = True


def _create_payload(
    *,
    name: str,
    content: str,
    project_ids: list[str],
    alias_ids: list[str],
) -> dict[str, object]:
    """构造一个单变量、selected Project 的完整不可变模板定义。"""

    return {
        "name": name,
        "description": "PRD-067 真实运行验收",
        "content": f"{content}: ${{customer_name}}",
        "variables": [
            {"name": "customer_name", "label": "客户名称", "required": True, "max_chars": 80}
        ],
        "project_scope": "selected",
        "project_ids": project_ids,
        "recommended_model_alias_ids": alias_ids,
        "change_summary": "创建真实运行验收版本",
    }


def _exercise_http(
    graph: RuntimeGraph,
    *,
    run_token: str,
    template_canary: str,
    variable_canary: str,
) -> tuple[dict[str, object], list[str], list[bytes]]:
    """执行模板管理、异常、权限、租户与两次真实 Gateway Chat 主流程。"""

    sensitive_values: list[bytes] = [
        template_canary.encode("utf-8"),
        variable_canary.encode("utf-8"),
        ADMIN_PASSWORD.encode("utf-8"),
        graph.viewer_password.encode("utf-8"),
        graph.second_password.encode("utf-8"),
    ]
    # 本批次通过真实 Control API 生成一把随机 Provider Key，既证明 Control/Gateway
    # 使用相同加密域，也避免开发库中历史密文因旧 Auth secret 失效而污染模板验收。
    # 明文只存活于本函数，并纳入数据库及服务日志扫描，证据只登记轮换布尔值。
    provider_key_canary = f"sk-prd067-{secrets.token_urlsafe(32)}"
    sensitive_values.append(provider_key_canary.encode("utf-8"))
    gateway_request_ids: list[str] = []
    cleanup_state: dict[str, object] = {
        "provider_key_disabled": False,
        "sessions_logged_out": 0,
    }
    # ExitStack 在 HTTP Client 关闭前逆序执行：先禁用一次性 Key，再注销三个 Session。
    with (
        httpx.Client(base_url=CONTROL_URL, timeout=15.0, follow_redirects=False) as client,
        ExitStack() as cleanup_stack,
    ):
        admin_token = _login(
            client,
            tenant_slug="default",
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            request_id=_request_id(run_token, "login_admin"),
        )
        cleanup_stack.callback(
            _logout_acceptance_session,
            client,
            admin_token,
            _request_id(run_token, "logout_admin"),
            cleanup_state,
        )
        viewer_token = _login(
            client,
            tenant_slug="default",
            email=VIEWER_EMAIL,
            password=graph.viewer_password,
            request_id=_request_id(run_token, "login_viewer"),
        )
        cleanup_stack.callback(
            _logout_acceptance_session,
            client,
            viewer_token,
            _request_id(run_token, "logout_viewer"),
            cleanup_state,
        )
        second_token = _login(
            client,
            tenant_slug=SECOND_TENANT_SLUG,
            email=SECOND_EMAIL,
            password=graph.second_password,
            request_id=_request_id(run_token, "login_second"),
        )
        # 三个 Bearer Token 也属于敏感明文；注销后继续扫描数据库和服务日志，证明它们只驻留内存。
        sensitive_values.extend(
            [
                admin_token.encode("utf-8"),
                viewer_token.encode("utf-8"),
                second_token.encode("utf-8"),
            ]
        )
        cleanup_stack.callback(
            _logout_acceptance_session,
            client,
            second_token,
            _request_id(run_token, "logout_second"),
            cleanup_state,
        )

        provider_key_id = _request_id(run_token, "provider_key")
        provider_key = _json_response(
            client.post(
                f"/providers/{graph.provider_id}/keys",
                json={
                    "name": f"PRD067 Runtime {run_token}",
                    "plain_value": provider_key_canary,
                    "weight": 1000,
                },
                headers=_headers(admin_token, provider_key_id),
            ),
            201,
            "http.provider_key",
            request_id=provider_key_id,
            no_store=False,
        )
        _require(
            isinstance(provider_key.get("provider_key"), dict)
            and provider_key["provider_key"].get("status") == "active"
            and isinstance(provider_key["provider_key"].get("provider_key_id"), str)
            and bool(provider_key["provider_key"]["provider_key_id"]),
            "http.provider_key",
            "inactive",
        )
        cleanup_stack.callback(
            _disable_runtime_provider_key,
            client,
            admin_token,
            str(provider_key["provider_key"]["provider_key_id"]),
            _request_id(run_token, "disable_provider_key"),
            cleanup_state,
        )

        viewer_read_id = _request_id(run_token, "viewer_read")
        viewer_read = _error_response(
            client.get("/prompt-templates", headers=_headers(viewer_token, viewer_read_id)),
            403,
            "auth.permission_denied",
            "http.viewer_read",
            viewer_read_id,
        )
        viewer_write_id = _request_id(run_token, "viewer_write")
        viewer_write = _error_response(
            client.post(
                "/prompt-templates",
                content=b"{not-json",
                headers={**_headers(viewer_token, viewer_write_id), "Content-Type": "application/json"},
            ),
            403,
            "auth.permission_denied",
            "http.viewer_write",
            viewer_write_id,
        )

        options_id = _request_id(run_token, "template_options")
        options = _json_response(
            client.get("/prompt-templates/options", headers=_headers(admin_token, options_id)),
            200,
            "http.template_options",
            request_id=options_id,
        )
        _require(
            PRIMARY_PROJECT_ID in {item["project_id"] for item in options.get("projects", [])}
            and OTHER_PROJECT_ID in {item["project_id"] for item in options.get("projects", [])}
            and PRIMARY_MODEL_ALIAS_ID
            in {item["model_alias_id"] for item in options.get("model_aliases", [])},
            "http.template_options",
            "resource_missing",
        )

        primary_payload = _create_payload(
            name=f"PRD067 Runtime {run_token}",
            content=template_canary,
            project_ids=[PRIMARY_PROJECT_ID],
            alias_ids=[PRIMARY_MODEL_ALIAS_ID],
        )
        create_id = _request_id(run_token, "create")
        created = _json_response(
            client.post("/prompt-templates", json=primary_payload, headers=_headers(admin_token, create_id)),
            201,
            "http.create",
            request_id=create_id,
        )
        _require(template_canary not in created.get("template", {}).__repr__(), "http.create", "plaintext")
        template_id = str(created["template"]["prompt_template_id"])
        version_v1 = str(created["version"]["prompt_template_version_id"])
        _require(created["template"]["metadata_revision"] == 1, "http.create", "revision")

        list_id = _request_id(run_token, "list")
        listing = _json_response(
            client.get("/prompt-templates?status=active&limit=100&offset=0", headers=_headers(admin_token, list_id)),
            200,
            "http.list",
            request_id=list_id,
        )
        _require(template_id in {item["prompt_template_id"] for item in listing["prompt_templates"]}, "http.list", "missing")
        detail_id = _request_id(run_token, "detail")
        detail = _json_response(
            client.get(f"/prompt-templates/{template_id}", headers=_headers(admin_token, detail_id)),
            200,
            "http.detail",
            request_id=detail_id,
        )
        _require(detail["versions"][0]["prompt_template_version_id"] == version_v1, "http.detail", "version")
        definition_id = _request_id(run_token, "definition")
        definition = _json_response(
            client.get(
                f"/prompt-templates/{template_id}/versions/{version_v1}",
                headers=_headers(admin_token, definition_id),
            ),
            200,
            "http.definition",
            request_id=definition_id,
        )
        _require(definition["definition"]["content"].startswith(template_canary), "http.definition", "content")

        publish_id = _request_id(run_token, "publish_v2")
        v2_payload = {
            **{key: value for key, value in primary_payload.items() if key not in {"name", "description"}},
            "expected_revision": 1,
            "change_summary": "发布真实运行验收 v2",
        }
        published = _json_response(
            client.post(
                f"/prompt-templates/{template_id}/versions",
                json=v2_payload,
                headers=_headers(admin_token, publish_id),
            ),
            201,
            "http.publish",
            request_id=publish_id,
        )
        version_v2 = str(published["version"]["prompt_template_version_id"])
        _require(
            published["version"]["version_number"] == 2
            and published["template"]["metadata_revision"] == 2,
            "http.publish",
            "revision",
        )
        stale_patch_id = _request_id(run_token, "stale_patch")
        stale_patch = _error_response(
            client.patch(
                f"/prompt-templates/{template_id}",
                json={"expected_revision": 1, "status": "disabled"},
                headers=_headers(admin_token, stale_patch_id),
            ),
            409,
            "prompt_template.revision_conflict",
            "http.stale_patch",
            stale_patch_id,
        )

        other_payload = _create_payload(
            name=f"PRD067 Not Applicable {run_token}",
            content=f"{template_canary}_OTHER",
            project_ids=[OTHER_PROJECT_ID],
            alias_ids=[PRIMARY_MODEL_ALIAS_ID],
        )
        other_id = _request_id(run_token, "create_other")
        other_created = _json_response(
            client.post("/prompt-templates", json=other_payload, headers=_headers(admin_token, other_id)),
            201,
            "http.create_other",
            request_id=other_id,
        )
        other_version = str(other_created["version"]["prompt_template_version_id"])

        second_payload = {
            "name": f"PRD067 Second {run_token}",
            "description": "",
            "content": "第二租户固定模板",
            "variables": [],
            "project_scope": "all",
            "project_ids": [],
            "recommended_model_alias_ids": [],
            "change_summary": "创建第二租户版本",
        }
        second_create_id = _request_id(run_token, "second_create")
        second_created = _json_response(
            client.post(
                "/prompt-templates", json=second_payload, headers=_headers(second_token, second_create_id)
            ),
            201,
            "http.second_create",
            request_id=second_create_id,
        )
        second_template_id = str(second_created["template"]["prompt_template_id"])
        cross_id = _request_id(run_token, "cross_tenant")
        cross = _error_response(
            client.get(f"/prompt-templates/{second_template_id}", headers=_headers(admin_token, cross_id)),
            404,
            "prompt_template.not_found",
            "http.cross_tenant",
            cross_id,
        )
        missing_id = _request_id(run_token, "missing")
        missing = _error_response(
            client.get(
                "/prompt-templates/ptpl_00000000000000000000000000000000",
                headers=_headers(admin_token, missing_id),
            ),
            404,
            "prompt_template.not_found",
            "http.missing",
            missing_id,
        )

        session_id_header = _request_id(run_token, "session")
        session = _json_response(
            client.post(
                "/chat/sessions",
                json={
                    "project_id": PRIMARY_PROJECT_ID,
                    "environment_id": graph.environment_id,
                    "model_alias_id": PRIMARY_MODEL_ALIAS_ID,
                },
                headers=_headers(viewer_token, session_id_header),
            ),
            201,
            "http.chat_session",
            request_id=session_id_header,
        )
        chat_session_id = str(session["session"]["chat_session_id"])
        chat_options_id = _request_id(run_token, "chat_options")
        chat_options = _json_response(
            client.get("/chat/options", headers=_headers(viewer_token, chat_options_id)),
            200,
            "http.chat_options",
            request_id=chat_options_id,
        )
        offered = {item["prompt_template_version_id"] for item in chat_options["prompt_templates"]}
        _require(version_v2 in offered and other_version in offered, "http.chat_options", "template_missing")

        invalid_id = _request_id(run_token, "invalid_variables")
        invalid_variables = _error_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [],
                    "prompt_template_version_id": version_v2,
                    "template_variables": {},
                },
                headers=_headers(viewer_token, invalid_id),
            ),
            422,
            "prompt_template.variables_invalid",
            "http.invalid_variables",
            invalid_id,
        )
        stale_id = _request_id(run_token, "stale_version")
        stale_version = _error_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [],
                    "prompt_template_version_id": version_v1,
                    "template_variables": {"customer_name": variable_canary},
                },
                headers=_headers(viewer_token, stale_id),
            ),
            409,
            "prompt_template.version_conflict",
            "http.stale_version",
            stale_id,
        )
        not_applicable_id = _request_id(run_token, "not_applicable")
        not_applicable = _error_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [],
                    "prompt_template_version_id": other_version,
                    "template_variables": {"customer_name": variable_canary},
                },
                headers=_headers(viewer_token, not_applicable_id),
            ),
            409,
            "prompt_template.not_applicable",
            "http.not_applicable",
            not_applicable_id,
        )

        disable_id = _request_id(run_token, "disable")
        disabled = _json_response(
            client.patch(
                f"/prompt-templates/{template_id}",
                json={"expected_revision": 2, "status": "disabled"},
                headers=_headers(admin_token, disable_id),
            ),
            200,
            "http.disable",
            request_id=disable_id,
        )
        _require(disabled["template"]["metadata_revision"] == 3, "http.disable", "revision")
        disabled_chat_id = _request_id(run_token, "disabled_chat")
        disabled_chat = _error_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [],
                    "prompt_template_version_id": version_v2,
                    "template_variables": {"customer_name": variable_canary},
                },
                headers=_headers(viewer_token, disabled_chat_id),
            ),
            409,
            "prompt_template.disabled",
            "http.disabled_chat",
            disabled_chat_id,
        )
        enable_id = _request_id(run_token, "enable")
        enabled = _json_response(
            client.patch(
                f"/prompt-templates/{template_id}",
                json={"expected_revision": 3, "status": "active"},
                headers=_headers(admin_token, enable_id),
            ),
            200,
            "http.enable",
            request_id=enable_id,
        )
        _require(enabled["template"]["metadata_revision"] == 4, "http.enable", "revision")

        success_id = _request_id(run_token, "chat_success")
        succeeded = _json_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [],
                    "prompt_template_version_id": version_v2,
                    "template_variables": {"customer_name": variable_canary},
                },
                headers=_headers(viewer_token, success_id),
            ),
            200,
            "http.chat_success",
            request_id=success_id,
        )
        _require(succeeded["rendered_user"] == f"{template_canary}: {variable_canary}", "http.chat_success", "rendered")
        first_assistant = str(succeeded["assistant"]["content"])
        sensitive_values.extend(
            [succeeded["rendered_user"].encode("utf-8"), first_assistant.encode("utf-8")]
        )
        first_gateway_request_id = str(succeeded["execution"]["request_id"])
        gateway_request_ids.append(first_gateway_request_id)

        matrix_id = _request_id(run_token, "matrix")
        matrix = _json_response(
            client.get("/permissions", headers=_headers(admin_token, matrix_id)),
            200,
            "http.matrix",
            request_id=matrix_id,
            # 本调用只借 PRD-059 既有后端 CAS 接口执行实时撤权；其历史响应头不属于
            # PRD-067 合同，不能据此放宽模板或 Chat 任一响应的 no-store 门禁。
            no_store=False,
        )
        viewer_role = next((role for role in matrix["roles"] if role["code"] == "viewer"), None)
        _require(viewer_role is not None, "http.matrix", "viewer_missing")
        original_codes = list(viewer_role["permission_codes"])
        _require("prompt_template.use" in original_codes, "http.matrix", "use_missing")
        revoke_id = _request_id(run_token, "revoke_use")
        revoked = _json_response(
            client.patch(
                f"/roles/{viewer_role['role_id']}/permissions",
                json={
                    "expected_revision": viewer_role["permission_revision"],
                    "permission_codes": [code for code in original_codes if code != "prompt_template.use"],
                },
                headers=_headers(admin_token, revoke_id),
            ),
            200,
            "http.revoke_use",
            request_id=revoke_id,
            no_store=False,
        )
        _require(revoked["changed"] is True, "http.revoke_use", "not_changed")
        before_denied = _execution_counts(graph.tenant_id)
        try:
            revoked_options_id = _request_id(run_token, "revoked_options")
            revoked_options = _json_response(
                client.get("/chat/options", headers=_headers(viewer_token, revoked_options_id)),
                200,
                "http.revoked_options",
                request_id=revoked_options_id,
            )
            _require(revoked_options["prompt_templates"] == [], "http.revoked_options", "not_empty")
            denied_id = _request_id(run_token, "revoked_send")
            revoked_send = _error_response(
                client.post(
                    f"/chat/sessions/{chat_session_id}/messages",
                    json={
                        "client_message_id": str(uuid.uuid4()),
                        "history": [
                            {"role": "user", "content": succeeded["rendered_user"]},
                            {"role": "assistant", "content": first_assistant},
                        ],
                        "prompt_template_version_id": version_v2,
                        "template_variables": {"customer_name": variable_canary},
                    },
                    headers=_headers(viewer_token, denied_id),
                ),
                403,
                "auth.permission_denied",
                "http.revoked_send",
                denied_id,
            )
            _require(_execution_counts(graph.tenant_id) == before_denied, "http.revoked_send", "wrote_facts")
        finally:
            # 恢复使用同一后端 CAS 接口；即使撤权断言失败也不能把开发库留在降权状态。
            restore_id = _request_id(run_token, "restore_use")
            restored = _json_response(
                client.patch(
                    f"/roles/{viewer_role['role_id']}/permissions",
                    json={
                        "expected_revision": revoked["role"]["permission_revision"],
                        "permission_codes": original_codes,
                    },
                    headers=_headers(admin_token, restore_id),
                ),
                200,
                "http.restore_use",
                request_id=restore_id,
                no_store=False,
            )
            _require(restored["changed"] is True, "http.restore_use", "not_changed")

        restored_options_id = _request_id(run_token, "restored_options")
        restored_options = _json_response(
            client.get("/chat/options", headers=_headers(viewer_token, restored_options_id)),
            200,
            "http.restored_options",
            request_id=restored_options_id,
        )
        _require(
            version_v2
            in {item["prompt_template_version_id"] for item in restored_options["prompt_templates"]},
            "http.restored_options",
            "template_missing",
        )
        restored_success_id = _request_id(run_token, "restored_success")
        restored_success = _json_response(
            client.post(
                f"/chat/sessions/{chat_session_id}/messages",
                json={
                    "client_message_id": str(uuid.uuid4()),
                    "history": [
                        {"role": "user", "content": succeeded["rendered_user"]},
                        {"role": "assistant", "content": first_assistant},
                    ],
                    "prompt_template_version_id": version_v2,
                    "template_variables": {"customer_name": variable_canary},
                },
                headers=_headers(viewer_token, restored_success_id),
            ),
            200,
            "http.restored_success",
            request_id=restored_success_id,
        )
        second_assistant = str(restored_success["assistant"]["content"])
        sensitive_values.extend(
            [restored_success["rendered_user"].encode("utf-8"), second_assistant.encode("utf-8")]
        )
        gateway_request_ids.append(str(restored_success["execution"]["request_id"]))
        clear_id = _request_id(run_token, "clear_session")
        cleared = client.delete(
            f"/chat/sessions/{chat_session_id}",
            headers=_headers(viewer_token, clear_id),
        )
        _require(cleared.status_code == 204, "http.clear_session", "status")
        _require(cleared.headers.get("cache-control") == "no-store", "http.clear_session", "cache_control")

        return (
            {
                "management": {
                    "provider_key_rotated": True,
                    "create": 201,
                    "list": 200,
                    "detail": 200,
                    "definition": 200,
                    "publish_v2": 201,
                    "disable": 200,
                    "enable": 200,
                    "viewer_read": viewer_read,
                    "viewer_write": viewer_write,
                    "cross_tenant": cross,
                    "missing": missing,
                    "stale_patch": stale_patch,
                },
                "chat": {
                    "session": 201,
                    "invalid_variables": invalid_variables,
                    "stale_version": stale_version,
                    "not_applicable": not_applicable,
                    "disabled": disabled_chat,
                    "success_calls": 2,
                    "revoked_send": revoked_send,
                    "permission_restored": True,
                    "clear_session": 204,
                },
                "cleanup": cleanup_state,
            },
            gateway_request_ids,
            sensitive_values,
        )


def _execution_counts(tenant_id: str) -> tuple[int, int, int]:
    """返回专用 Viewer 的 Message、Grant、Invocation 数量，用于证明撤权零写入。"""

    connection = connect_database(DATABASE_URL)
    try:
        message_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE tenant_id=? AND owner_user_id=?",
                (tenant_id, VIEWER_USER_ID),
            ).fetchone()[0]
        )
        grant_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_execution_grants WHERE tenant_id=? AND owner_user_id=?",
                (tenant_id, VIEWER_USER_ID),
            ).fetchone()[0]
        )
        invocation_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_prompt_template_invocations WHERE tenant_id=? AND actor_user_id=?",
                (tenant_id, VIEWER_USER_ID),
            ).fetchone()[0]
        )
        return message_count, grant_count, invocation_count
    finally:
        connection.close()


# 治理证据必须按同一个 Gateway request_id 交叉关联，不能用全租户总数掩盖链路缺失。
def _governance_evidence(
    graph: RuntimeGraph,
    request_ids: list[str],
    *,
    run_token: str,
) -> dict[str, object]:
    """复核调用、模板审计/事件及验收器自建资源的完整治理终态。"""

    connection = connect_database(DATABASE_URL)
    try:
        tracked: list[dict[str, object]] = []
        for request_id in request_ids:
            request_log = connection.execute(
                """
                SELECT status,endpoint,policy_decision,routing_attempt_count,trace_id
                FROM request_logs WHERE tenant_id=? AND request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()
            usage = connection.execute(
                """
                SELECT COUNT(*) AS rows,SUM(total_tokens) AS total_tokens,SUM(cost_cny) AS cost_cny,
                       MIN(estimated) AS min_estimated,MAX(estimated) AS max_estimated
                FROM usage_ledger WHERE tenant_id=? AND request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()
            routing = connection.execute(
                """
                SELECT COUNT(*) AS rows,SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded
                FROM routing_attempts WHERE tenant_id=? AND request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()
            invocation = connection.execute(
                """
                SELECT status,failure_code,completed_at FROM chat_prompt_template_invocations
                WHERE tenant_id=? AND gateway_request_id=?
                """,
                (graph.tenant_id, request_id),
            ).fetchone()
            finops = connection.execute(
                """
                SELECT COUNT(*) AS rows,SUM(normalized_cny_micros) AS cost_micros
                FROM finops_ledger_entries
                WHERE tenant_id=? AND source_request_id=? AND status='posted'
                """,
                (graph.tenant_id, request_id),
            ).fetchone()
            _require(
                request_log is not None
                and request_log["status"] == "success"
                and request_log["endpoint"] == "/internal/chat/completions"
                and request_log["policy_decision"] == "allow",
                "database.request_log",
                "invalid",
            )
            _require(
                int(usage["rows"] or 0) >= 1
                and int(usage["total_tokens"] or 0) == 18
                and float(usage["cost_cny"] or 0) > 0
                and int(usage["min_estimated"] or 0) == 0
                and int(usage["max_estimated"] or 0) == 0,
                "database.usage",
                "invalid",
            )
            _require(
                int(routing["rows"] or 0) >= 1
                and int(routing["succeeded"] or 0) == 1
                and invocation is not None
                and invocation["status"] == "succeeded"
                and invocation["failure_code"] is None
                and invocation["completed_at"] is not None,
                "database.invocation",
                "invalid",
            )
            _require(
                int(finops["rows"] or 0) >= 1 and int(finops["cost_micros"] or 0) > 0,
                "database.cost",
                "invalid",
            )
            tracked.append(
                {
                    "request_id": request_id,
                    "trace_id": str(request_log["trace_id"]),
                    "request_log_status": "success",
                    "usage_rows": int(usage["rows"]),
                    "total_tokens": int(usage["total_tokens"]),
                    "cost_cny": format(float(usage["cost_cny"]), ".6f"),
                    "finops_entries": int(finops["rows"]),
                    "invocation_status": "succeeded",
                }
            )

        # 模板管理写必须形成精确 AuditLog 数量；失败、越权和 stale 请求不能伪造成功审计。
        # ``_`` 在 LIKE 中本是单字符通配符；显式转义后只匹配本批固定 request_id 前缀。
        request_pattern = f"req\\_prd067\\_{run_token}\\_%"
        audit_rows = connection.execute(
            """
            SELECT action,COUNT(*) AS rows
            FROM audit_logs
            WHERE tenant_id=? AND request_id LIKE ? ESCAPE '\\'
              AND action IN (
                'prompt_template.created',
                'prompt_template.updated',
                'prompt_template.version_published'
              )
            GROUP BY action ORDER BY action
            """,
            (graph.tenant_id, request_pattern),
        ).fetchall()
        audit_actions = {str(row["action"]): int(row["rows"]) for row in audit_rows}
        _require(
            audit_actions
            == {
                # 主模板和“其他 Project”模板各创建一次；主模板随后再发布 v2。
                "prompt_template.created": 2,
                "prompt_template.updated": 2,
                "prompt_template.version_published": 3,
            },
            "database.template_audit",
            "invalid",
        )

        # 主模板 v1/v2 和“其他 Project”模板 v1 各有一个不可变 source event，并与审计精确绑定。
        event_rows = connection.execute(
            """
            SELECT event.delivery_status,COUNT(*) AS rows,
                   SUM(CASE WHEN audit.audit_log_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_rows
            FROM prompt_template_events event
            LEFT JOIN audit_logs audit
              ON audit.tenant_id=event.tenant_id
             AND audit.audit_log_id=event.publication_audit_log_id
             AND audit.action='prompt_template.version_published'
             AND audit.object_type='prompt_template_version'
             AND audit.object_id=event.prompt_template_version_id
             AND audit.request_id=event.request_id
             AND audit.trace_id=event.trace_id
            WHERE event.tenant_id=? AND event.request_id LIKE ? ESCAPE '\\'
              AND event.event_type='prompt_template.published'
            GROUP BY event.delivery_status ORDER BY event.delivery_status
            """,
            (graph.tenant_id, request_pattern),
        ).fetchall()
        event_statuses = {str(row["delivery_status"]): int(row["rows"]) for row in event_rows}
        event_count = sum(event_statuses.values())
        linked_event_count = sum(int(row["linked_rows"] or 0) for row in event_rows)
        _require(
            event_count == 3 and linked_event_count == 3,
            "database.template_event",
            "invalid",
        )

        # 一次性 Provider Key 必须由正式 API 禁用并留下创建/更新审计，不能只在证据里写布尔值。
        provider_cleanup = connection.execute(
            """
            SELECT credential.status,
              (SELECT COUNT(*) FROM audit_logs audit
               WHERE audit.tenant_id=credential.tenant_id
                 AND audit.object_type='provider_key'
                 AND audit.object_id=credential.provider_credential_id
                 AND audit.action='provider_key.created') AS created_audits,
              (SELECT COUNT(*) FROM audit_logs audit
               WHERE audit.tenant_id=credential.tenant_id
                 AND audit.object_type='provider_key'
                 AND audit.object_id=credential.provider_credential_id
                 AND audit.action='provider_key.updated') AS updated_audits
            FROM provider_credentials credential
            WHERE credential.tenant_id=? AND credential.name=?
            """,
            (graph.tenant_id, f"PRD067 Runtime {run_token}"),
        ).fetchone()
        _require(
            provider_cleanup is not None
            and provider_cleanup["status"] == "disabled"
            and int(provider_cleanup["created_audits"] or 0) == 1
            and int(provider_cleanup["updated_audits"] or 0) >= 1,
            "database.provider_key_cleanup",
            "invalid",
        )

        # 专用 Viewer/第二租户用户在准备阶段已撤销旧会话，因此本轮 logout 后必须没有 active 残留。
        active_runtime_sessions = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM sessions
                WHERE status='active' AND (
                  (tenant_id=? AND user_id=?) OR (tenant_id=? AND user_id=?)
                )
                """,
                (graph.tenant_id, VIEWER_USER_ID, SECOND_TENANT_ID, SECOND_USER_ID),
            ).fetchone()[0]
        )
        _require(active_runtime_sessions == 0, "database.session_cleanup", "active")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        migration = connection.execute(
            "SELECT COUNT(*),MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        _require(integrity == "ok" and foreign_keys == 0, "database.integrity", "invalid")
        return {
            "migration_count": int(migration[0]),
            "latest_migration": str(migration[1]),
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "tracked_requests": tracked,
            "template_audit_actions": audit_actions,
            "prompt_template_events": {
                "rows": event_count,
                "audit_linked_rows": linked_event_count,
                "delivery_statuses": event_statuses,
            },
            "provider_key_cleanup": {
                "status": "disabled",
                "created_audits": int(provider_cleanup["created_audits"]),
                "updated_audits": int(provider_cleanup["updated_audits"]),
            },
            "active_runtime_sessions": active_runtime_sessions,
        }
    finally:
        connection.close()


def _privacy_evidence(sensitive_values: list[bytes]) -> dict[str, int]:
    """扫描开发数据库、截断后的 WAL 与本轮服务日志中的明文金丝雀。"""

    connection = connect_database(DATABASE_URL)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    wal_path = Path(f"{DATABASE_PATH}-wal")
    shm_path = Path(f"{DATABASE_PATH}-shm")
    database_files = [path for path in (DATABASE_PATH, wal_path) if path.exists()]
    # SQLite ``-shm`` 只保存 WAL 索引与锁元数据，不保存数据库业务页；Windows 上服务运行时
    # 该文件会被独占锁定，强行读取既不增加 Prompt 隐私证明，也会把正常锁语义误判成失败。
    # 主库和 checkpoint 后的 WAL 才是可能承载业务值的持久文件，必须逐字节完成 canary 扫描。
    _require(not wal_path.exists() or wal_path.stat().st_size == 0, "privacy.wal", "not_truncated")
    log_files = [path for path in SERVICE_LOG_ROOT.glob("*.log") if path.is_file()]

    def scan(files: list[Path]) -> tuple[int, int]:
        """二进制扫描有界本地文件，返回敏感命中数和实际读取字节数。"""

        hits = 0
        scanned_bytes = 0
        for path in files:
            data = path.read_bytes()
            scanned_bytes += len(data)
            hits += sum(data.count(value) for value in sensitive_values if value)
        return hits, scanned_bytes

    database_hits, database_bytes = scan(database_files)
    log_hits, service_log_bytes = scan(log_files)
    control_log_bytes = sum(
        path.stat().st_size for path in log_files if path.name.startswith("control.")
    )
    gateway_log_bytes = sum(
        path.stat().st_size for path in log_files if path.name.startswith("gateway.")
    )
    # 命中为 0 只有在确实读取了主库以及 Control/Gateway 非空日志时才是有效隐私证据。
    _require(
        database_bytes > 0
        and service_log_bytes > 0
        and control_log_bytes > 0
        and gateway_log_bytes > 0,
        "privacy.scan",
        "empty_evidence",
    )
    _require(database_hits == 0 and log_hits == 0, "privacy.scan", "plaintext_detected")
    return {
        "database_hits": database_hits,
        "database_bytes": database_bytes,
        "service_log_hits": log_hits,
        "service_log_bytes": service_log_bytes,
        "service_log_files": len(log_files),
        "control_log_bytes": control_log_bytes,
        "gateway_log_bytes": gateway_log_bytes,
        "shm_metadata_excluded": int(shm_path.exists()),
    }


def _ensure_viewer_use_restored(tenant_id: str) -> None:
    """异常兜底：确保 Viewer 最终仍有 prompt_template.use 并同步矩阵 hash。

    正常路径使用公开 PATCH API 完成撤销与恢复并留下审计；本函数只在进程退出前复核最终状态。
    若 API 中途失败导致关系缺失，则以单事务恢复最小关系，避免验收器污染开发租户。
    """

    connection = connect_database(DATABASE_URL)
    try:
        connection.execute("BEGIN IMMEDIATE")
        role = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='viewer'",
            (tenant_id,),
        ).fetchone()
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='prompt_template.use'",
            (tenant_id,),
        ).fetchone()
        _require(role is not None and permission is not None, "cleanup.viewer_use", "catalog_missing")
        relation = connection.execute(
            """
            SELECT 1 FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=?
            """,
            (tenant_id, role["role_id"], permission["permission_id"]),
        ).fetchone()
        if relation is None:
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO role_permissions(
                  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"rperm_{uuid.uuid4().hex}",
                    tenant_id,
                    role["role_id"],
                    permission["permission_id"],
                    now,
                    now,
                ),
            )
            codes = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT permission.code FROM role_permissions relation
                    JOIN permissions permission
                      ON permission.tenant_id=relation.tenant_id
                     AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                    """,
                    (tenant_id, role["role_id"]),
                ).fetchall()
            }
            connection.execute(
                """
                UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
                WHERE tenant_id=? AND role_id=?
                """,
                (
                    int(role["permission_revision"]) + 1,
                    permission_matrix_sha256(codes),
                    now,
                    tenant_id,
                    role["role_id"],
                ),
            )
        connection.commit()
    # 兜底修复也必须原子回滚；不能留下关系已写入而矩阵 hash 尚未更新的半状态。
    except (AcceptanceFailure, sqlite3.Error):
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _service_health() -> dict[str, int]:
    """确认 Gateway、Control 和 Console 三个实际本地服务均为 HTTP 200。"""

    results: dict[str, int] = {}
    for name, url in (
        ("gateway", GATEWAY_HEALTH_URL),
        ("control", f"{CONTROL_URL}/health"),
        ("console", CONSOLE_URL),
    ):
        try:
            response = httpx.get(url, timeout=5.0, follow_redirects=False)
        # 网络异常可能带本机 URL 或代理详情，只公开固定服务名和不可达分类。
        except httpx.HTTPError as exc:
            raise AcceptanceFailure(f"service.{name}", "unreachable") from exc
        _require(response.status_code == 200, f"service.{name}", "unhealthy")
        results[name] = 200
    return results


def _write_evidence(run_id: str, payload: dict[str, object]) -> tuple[Path, str]:
    """原子写入只含状态、计数和 request_id 的脱敏 JSON，并返回 SHA-256。"""

    import hashlib

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    run_directory = ARTIFACT_ROOT / run_id
    _require(not run_directory.exists(), "evidence.directory", "already_exists")
    run_directory.mkdir()
    target = run_directory / "runtime-evidence.json"
    temporary = run_directory / "runtime-evidence.pending.json"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target, hashlib.sha256(target.read_bytes()).hexdigest().upper()


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """解析固定 Provider 模式；验收地址、数据库和证据根不可由调用方覆盖。"""

    parser = argparse.ArgumentParser(prog="prd067-runtime-acceptance")
    parser.add_argument("--serve-provider", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行真实 HTTP 验收，最终只输出脱敏证据路径、摘要哈希或固定失败分类。"""

    arguments = _parse_arguments(argv)
    # Provider 模式与验收模式互斥，避免同一进程边监听边修改开发库。
    if arguments.serve_provider:
        return _serve_provider()
    run_token = secrets.token_hex(5)
    run_id = datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ-") + run_token
    template_canary = f"PRD067_TEMPLATE_{secrets.token_hex(24)}"
    variable_canary = f"PRD067_VARIABLE_{secrets.token_hex(24)}"
    graph: RuntimeGraph | None = None
    try:
        services = _service_health()
        graph = _prepare_runtime_graph()
        http_evidence, gateway_request_ids, sensitive_values = _exercise_http(
            graph,
            run_token=run_token,
            template_canary=template_canary,
            variable_canary=variable_canary,
        )
        cleanup = http_evidence.get("cleanup")
        _require(
            isinstance(cleanup, dict)
            and cleanup.get("provider_key_disabled") is True
            and cleanup.get("sessions_logged_out") == 3,
            "cleanup.final_state",
            "incomplete",
        )
        # 后台写入当前实现为同步事务；短轮询仅吸收 Windows 文件系统/连接关闭时序，不扩大测试范围。
        governance: dict[str, object] | None = None
        for _ in range(20):
            try:
                governance = _governance_evidence(
                    graph,
                    gateway_request_ids,
                    run_token=run_token,
                )
                break
            except AcceptanceFailure:
                time.sleep(0.1)
        _require(governance is not None, "database.governance", "not_ready")
        privacy = _privacy_evidence(sensitive_values)
        evidence = {
            "status": "passed",
            "prd_id": "PRD-067",
            "run_id": run_id,
            "services": services,
            "http": http_evidence,
            "governance": governance,
            "privacy": privacy,
            "all_108_prds_complete": False,
        }
        target, digest = _write_evidence(run_id, evidence)
        print(
            json.dumps(
                {
                    "status": "passed",
                    "prd_id": "PRD-067",
                    "evidence": str(target),
                    "sha256": digest,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except AcceptanceFailure as exc:
        print(
            json.dumps(
                {"status": "failed", "stage": exc.stage, "code": exc.code},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    # 预期外的网络、文件和数据库异常都必须脱敏，不能打印默认 traceback。
    except (httpx.HTTPError, OSError, sqlite3.Error) as exc:
        # 底层异常可能包含 URL、路径或响应片段，只输出固定 runtime 分类。
        print(
            json.dumps(
                {"status": "failed", "stage": "runtime", "code": "unexpected_error"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        exc.__traceback__ = None
        return 1
    finally:
        if graph is not None:
            try:
                _ensure_viewer_use_restored(graph.tenant_id)
            # 清理异常只能吞掉固定分类；此处绝不能覆盖已完成的主失败输出或泄露矩阵细节。
            except (AcceptanceFailure, sqlite3.Error):
                # 主失败已输出固定分类；清理失败不能把 SQL/路径或权限集合再次暴露到 stderr。
                pass


if __name__ == "__main__":
    raise SystemExit(main())
