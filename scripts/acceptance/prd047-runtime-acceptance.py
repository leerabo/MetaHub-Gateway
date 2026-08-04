"""PRD-047 成本优先/可用性优先路由的真实三服务运行验收。

脚本只面向本机独立验收数据库。它通过公开 Control API 创建两条真实回环 Provider
路线，再从 Gateway 发起请求，并用数据库事实、Control 查询和 HMAC Webhook 三种独立
证据核对选择结果。Prompt 仅发往回环 Provider，脚本不把正文、Virtual Key 或一次性
Webhook Secret 写入制品。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import AbstractContextManager, closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.webhooks import verify_webhook_signature

# 管理写和模型调用均限定为本地短请求；超时代表运行验收失败，不能无限等待掩盖死锁。
HTTP_TIMEOUT = httpx.Timeout(20.0)
EVENT_WAIT_SECONDS = 12.0


class AcceptanceServer(ThreadingHTTPServer):
    """同时承载两个 OpenAI-compatible 上游和一个验收 Webhook 接收端。"""

    daemon_threads = True

    def __init__(self, prompt_sentinel: str) -> None:
        """绑定随机回环端口，并初始化只保存安全摘要的线程共享状态。"""

        super().__init__(("127.0.0.1", 0), AcceptanceHandler)
        self.prompt_sentinel = prompt_sentinel
        self.signing_secret = ""
        self._provider_calls: list[dict[str, str]] = []
        self._webhook_events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record_provider_call(self, provider: str, model: str) -> None:
        """只记录 Provider 标签和上游模型名，禁止保存包含 Prompt 的请求对象。"""

        with self._lock:
            self._provider_calls.append({"provider": provider, "model": model})

    def provider_calls(self) -> list[dict[str, str]]:
        """返回 Provider 调用安全摘要的副本，避免与 HTTP 工作线程竞争。"""

        with self._lock:
            return [dict(item) for item in self._provider_calls]

    def record_webhook_event(self, event: dict[str, Any]) -> None:
        """保存已经过签名和敏感正文检查的 Webhook 白名单摘要。"""

        with self._lock:
            self._webhook_events.append(dict(event))

    def webhook_events(self) -> list[dict[str, Any]]:
        """返回 Webhook 安全摘要副本，供主线程按 request_id 等待。"""

        with self._lock:
            return [dict(item) for item in self._webhook_events]


class AcceptanceHandler(BaseHTTPRequestHandler):
    """提供固定模型响应并验证 ``route.policy_selected`` 的真实签名。"""

    def do_GET(self) -> None:  # noqa: N802
        """为 Provider 连接检查返回固定模型目录；其他路径不暴露服务信息。"""

        if self.path.endswith("/models"):
            self._json_response(
                200,
                {"object": "list", "data": [{"id": "prd047-a"}, {"id": "prd047-b"}]},
            )
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        """按路径分派模型调用或 Webhook；正文读取始终受 1 MiB 硬上限保护。"""

        raw = self._read_body()
        if raw is None:
            return
        if self.path == "/hook":
            self._handle_webhook(raw)
            return
        if self.path in {
            "/upstream-a/v1/chat/completions",
            "/upstream-b/v1/chat/completions",
        }:
            self._handle_provider(raw)
            return
        self._json_response(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        """关闭 BaseHTTPRequestHandler 默认日志，避免把 URL 或未来敏感参数写到 stderr。"""

        return

    def _read_body(self) -> bytes | None:
        """有界读取请求正文；非法长度直接返回 413 且不分配无界内存。"""

        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        # 非法 Content-Length 不能进入正文读取，否则可能绕过一 MiB 内存上限。
        except ValueError:
            length = -1
        if length < 1 or length > 1_048_576:
            self._json_response(413, {"error": "body_too_large"})
            return None
        return self.rfile.read(length)

    def _handle_provider(self, raw: bytes) -> None:
        """解析内存中的 OpenAI 请求并返回稳定 Usage，解析结果不保留 Prompt。"""

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            body = json.loads(raw.decode("utf-8"))
        # Provider 夹具只接受严格 UTF-8 JSON，失败响应不得回显原始 Prompt 正文。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        if not isinstance(body, dict):
            self._json_response(400, {"error": "invalid_object"})
            return
        if server.prompt_sentinel.encode("utf-8") not in raw:
            # 没有真实收到随机 Prompt 时不能伪造上游调用成功证据。
            self._json_response(400, {"error": "prompt_missing"})
            return
        provider = "a" if "/upstream-a/" in self.path else "b"
        model = str(body.get("model", ""))
        # 只把路由标识写入共享状态；body 在函数返回后释放，绝不进入验收制品。
        server.record_provider_call(provider, model)
        self._json_response(
            200,
            {
                "id": f"chatcmpl_prd047_{provider}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"selected-{provider}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        )

    def _handle_webhook(self, raw: bytes) -> None:
        """验证 HMAC、事件类型与敏感正文，持久化接收端安全摘要后返回 204。"""

        server = self.server
        assert isinstance(server, AcceptanceServer)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        # Webhook 解析失败只返回固定错误，不把签名载荷或内部事件正文写入日志。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "invalid_json"})
            return
        if not isinstance(envelope, dict) or server.prompt_sentinel.encode("utf-8") in raw:
            self._json_response(400, {"error": "unsafe_event"})
            return
        signature = self.headers.get("X-MetaHub-Signature", "")
        payload = envelope.get("payload")
        safe_payload = payload if isinstance(payload, dict) else {}
        signature_valid = bool(server.signing_secret) and verify_webhook_signature(
            signature,
            server.signing_secret,
            raw,
        )
        server.record_webhook_event(
            {
                "event_id": str(envelope.get("event_id", "")),
                "event_type": str(envelope.get("event_type", "")),
                "trigger": safe_payload.get("trigger"),
                "request_id": safe_payload.get("request_id"),
                "routing_mode": safe_payload.get("routing_mode"),
                "selected_model_id": safe_payload.get("selected_model_id"),
                "payload_keys": sorted(safe_payload),
                "signature_valid": signature_valid,
                "privacy_ok": server.prompt_sentinel not in raw.decode("utf-8", errors="ignore"),
            }
        )
        self.send_response(204 if signature_valid else 401)
        self.end_headers()

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        """返回 UTF-8 JSON，并固定 Content-Length 让客户端不会等待连接关闭。"""

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class RunningAcceptanceServer(AbstractContextManager[AcceptanceServer]):
    """在上下文内启动回环服务，并保证异常路径也关闭监听线程。"""

    def __init__(self, prompt_sentinel: str) -> None:
        """保存不可写盘的 Prompt 哨兵，实际服务在进入上下文时创建。"""

        self.prompt_sentinel = prompt_sentinel
        self.server: AcceptanceServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> AcceptanceServer:
        """启动 daemon HTTP 线程并返回可查询安全摘要的服务对象。"""

        self.server = AcceptanceServer(self.prompt_sentinel)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_exc_info: object) -> None:
        """停止服务并等待工作线程退出，避免验收结束后留下端口监听。"""

        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            # 一次性 HMAC Secret 在接收器关闭后立即清空，避免长生命周期对象继续持有。
            self.server.signing_secret = ""
        if self.thread is not None:
            self.thread.join(timeout=5)


def budget_settlement_summary(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    budget_rule_id: str,
) -> dict[str, bool | int]:
    """按 tenant/request/rule 三键核对结算事实，并只返回布尔与计数安全摘要。"""

    reservations = connection.execute(
        """
        SELECT
          CASE WHEN r.reserved_cost_cny > 0 THEN 1 ELSE 0 END AS reserved_positive,
          CASE WHEN r.outstanding_cost_cny = 0 THEN 1 ELSE 0 END AS outstanding_zero,
          br.status AS rule_status
        FROM budget_reservations r
        JOIN budget_rules br
          ON br.tenant_id=r.tenant_id AND br.budget_rule_id=r.budget_rule_id
        WHERE r.tenant_id=? AND r.request_id=? AND r.budget_rule_id=?
        """,
        (tenant_id, request_id, budget_rule_id),
    ).fetchall()
    usage_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id=?",
            (tenant_id, request_id),
        ).fetchone()[0]
    )
    # 精确一条规则预留和一条 Usage 才能证明真实成功结算；断言不读取或回显金额原值。
    assert len(reservations) == 1, "budget settlement reservation count mismatch"
    row = reservations[0]
    assert int(row["reserved_positive"]) == 1, "budget reservation must be positive"
    assert int(row["outstanding_zero"]) == 1, "budget outstanding must be settled"
    assert row["rule_status"] == "active", "budget rule must be active during evidence"
    assert usage_count == 1, "budget settlement usage count mismatch"
    return {
        "reservation_count": 1,
        "usage_count": usage_count,
        "reserved_positive": True,
        "outstanding_zero": True,
        "tenant_request_rule_scoped": True,
    }


def disable_runtime_budget_rule(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    scope_id: str,
    rule_name: str,
    budget_rule_id: str | None,
    now: str,
) -> int:
    """失败恢复时按完整 owner 身份禁用临时规则，并返回精确变更行数。

    服务端响应中的规则 ID 可能在响应损坏或中间件异常时不可信，因此先用 ID 与
    owner 联合定位；零行时再退回请求前登记的完整 owner intent。两个步骤都拒绝
    多行更新，避免错误 ID 阻止清理，也避免扩大到租户级批量禁用。
    """

    # 任一 owner 维度缺失时拒绝猜测或扩大查询范围，由已登记 intent 的其他证据负责排障。
    if not all((tenant_id, environment_id, scope_id, rule_name)):
        return 0
    owner_clauses = [
        "tenant_id=?",
        "environment_id=?",
        "scope_type='virtual_key'",
        "scope_id=?",
        "name=?",
        "status!='disabled'",
    ]
    owner_parameters: list[object] = [tenant_id, environment_id, scope_id, rule_name]
    changed = 0
    if budget_rule_id:
        # 第一阶段必须同时匹配服务端 ID 和全部 owner 字段，禁止仅凭全局 ID 清理资源。
        identified = connection.execute(
            "UPDATE budget_rules SET status='disabled', updated_at=? "
            f"WHERE {' AND '.join(owner_clauses)} AND budget_rule_id=?",
            (now, *owner_parameters, budget_rule_id),
        )
        identified_changed = max(identified.rowcount, 0)
        assert identified_changed in {0, 1}, "budget ID cleanup changed unexpected rows"
        changed += identified_changed

    if changed == 0:
        # 错误的非空响应 ID 不能让真实 active 规则遗留；仅按请求前登记的完整 owner 回退。
        owner_fallback = connection.execute(
            "UPDATE budget_rules SET status='disabled', updated_at=? "
            f"WHERE {' AND '.join(owner_clauses)}",
            (now, *owner_parameters),
        )
        fallback_changed = max(owner_fallback.rowcount, 0)
        assert fallback_changed in {0, 1}, "budget owner cleanup changed unexpected rows"
        changed += fallback_changed

    # 每个阶段及累计结果都必须至多一行；超过一行说明唯一 owner 图已经损坏。
    assert changed in {0, 1}, "budget cleanup changed unexpected rows"
    return changed


class RuntimeAcceptance:
    """编排 PRD-047 主流程、异常、权限、租户、追踪和隐私验收。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """建立本地 HTTP 客户端；敏感 Token 只保存在对象内存并在进程退出时释放。"""

        self.args = args
        self.suffix = args.suffix
        self.prompt_sentinel = f"prd047-prompt-{secrets.token_urlsafe(24)}"
        self.control = httpx.Client(base_url=args.control_url, timeout=HTTP_TIMEOUT)
        self.gateway = httpx.Client(base_url=args.gateway_url, timeout=HTTP_TIMEOUT)
        self.admin_token = ""
        self.role_tokens: dict[str, str] = {}
        self.resource_ids: dict[str, str] = {}
        self.request_ids: list[str] = []
        # Budget intent 在公开 API 响应异常时仍提供完整租户与 scope owner，
        # 仅用于失败清理且不保存金额或密钥。
        self.budget_intent: dict[str, str] = {}

    def close(self) -> None:
        """关闭连接池并清空进程内凭据；Token 不进入返回值、日志或状态文件。"""

        self.control.close()
        self.gateway.close()
        self.admin_token = ""
        self.role_tokens.clear()
        self.args.admin_password = ""
        self.args.other_admin_password = ""

    def run(self, receiver: AcceptanceServer) -> dict[str, Any]:
        """执行完整验收并返回只含稳定 ID、计数和布尔结论的安全摘要。"""

        self._verify_service_health()
        self._prepare_second_tenant()
        self.admin_token = self._login(
            self.args.tenant_slug,
            self.args.admin_email,
            self.args.admin_password,
            "admin",
        )
        environment = self._default_environment(self.admin_token)
        self.role_tokens = self._create_role_users()
        route = self._create_route(self.admin_token, environment, receiver, prefix="default")
        other_alias_id = self._create_other_tenant_alias(receiver)
        virtual_key = self._create_virtual_key(environment["environment_id"])
        budget = self._create_runtime_budget(environment["environment_id"])
        self._create_webhook(receiver)
        rbac = self._verify_rbac_and_tenant(route, other_alias_id)
        cost = self._verify_cost_first(route, virtual_key, receiver)
        budget_settlement = self._assert_budget_settlement(cost["request_id"], budget)
        budget_settlement.update(self._disable_budget_after_evidence(budget))
        availability = self._verify_availability_first(
            route, virtual_key, receiver, cost["version"]
        )
        no_keypool = self._verify_no_keypool(route, virtual_key, receiver)
        self._disable_webhook_after_evidence()
        privacy = self._verify_privacy()
        return {
            "status": "passed",
            "suffix": self.suffix,
            "resource_ids": dict(self.resource_ids),
            "request_ids": list(self.request_ids),
            "rbac_and_tenant": rbac,
            "cost_first": cost,
            "budget_settlement": budget_settlement,
            "availability_first": availability,
            "no_keypool": no_keypool,
            "privacy": privacy,
        }

    def _verify_service_health(self) -> None:
        """确认 Gateway、Control 和 Console 均来自本机并已真实启动。"""

        for client, path in ((self.gateway, "/health"), (self.control, "/health")):
            response = client.get(path)
            assert response.status_code == 200, response.text
        console = httpx.get(self.args.console_url, timeout=HTTP_TIMEOUT)
        assert console.status_code == 200, console.text

    def _prepare_second_tenant(self) -> None:
        """只用 SQL 建立第二租户认证根；后续业务资源仍通过该租户公开 API 创建。"""

        tenant_id = f"tenant_prd047_{self.suffix}"
        user_id = f"user_prd047_other_{self.suffix}"
        # connection 上下文负责异常回滚，closing 负责 Windows 下及时释放数据库文件句柄。
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO tenants(
                  tenant_id, slug, name, service_mode, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'internal_only', 'active', datetime('now'), datetime('now'))
                """,
                (tenant_id, self.args.other_tenant_slug, f"PRD047 other {self.suffix}"),
            )
            connection.execute(
                """
                INSERT INTO users(
                  user_id, tenant_id, email, display_name, password_hash, role, status,
                  created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'admin', 'active', datetime('now'), datetime('now'))
                """,
                (
                    user_id,
                    tenant_id,
                    self.args.other_admin_email,
                    "PRD047 Other Admin",
                    hash_password(self.args.other_admin_password),
                ),
            )
        self.resource_ids["other_tenant_id"] = tenant_id
        self.resource_ids["other_admin_user_id"] = user_id

    def _login(self, slug: str, email: str, password: str, label: str) -> str:
        """登录指定租户并返回进程内 Token；响应正文不会写入任何验收制品。"""

        request_id = f"req_prd047_login_{label}_{self.suffix}"
        response = self.control.post(
            "/auth/login",
            headers={"X-Request-ID": request_id},
            json={"tenant_slug": slug, "email": email, "password": password},
        )
        body = expect_json(response, 200, request_id)
        token = str(body.get("access_token", ""))
        # 登录响应按公开 LoginResponse 合同平铺 session_id；只有 /auth/me 才返回嵌套 session。
        # 验收脚本必须严格消费公开 wire，避免错误夹具掩盖 Console 与认证 API 的兼容性回归。
        session_id = str(body.get("session_id", ""))
        # Session 已由服务端创建时先登记稳定 ID，防止缺 Token 的异常响应造成会话遗留。
        if session_id:
            self.resource_ids[f"{label}_session_id"] = session_id
        assert token and session_id
        # 明文 Token 仅返回当前编排对象内存，最终由 close 清空，不写入资源账本。
        return token

    def _headers(self, token: str, request_id: str | None = None) -> dict[str, str]:
        """构造认证头；tenant 永远不由客户端头或正文提供。"""

        headers = {"Authorization": f"Bearer {token}"}
        # 写请求显式携带链路 ID，响应必须由 expect_json 原样核对，不能只检查非空。
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _default_environment(self, token: str) -> dict[str, str]:
        """读取默认租户 dev Environment，避免根据显示名称或数据库固定 ID 猜测。"""

        request_id = f"req_prd047_default_environment_{self.suffix}"
        body = expect_json(
            self.control.get(
                "/environments",
                headers=self._headers(token, request_id),
            ),
            200,
            request_id,
        )
        item = next(
            environment for environment in body["environments"] if environment["code"] == "dev"
        )
        return {"environment_id": str(item["environment_id"]), "code": str(item["code"])}

    def _create_role_users(self) -> dict[str, str]:
        """通过公开用户 API 创建 developer/viewer，并实际登录得到权限攻击 Token。"""

        tokens: dict[str, str] = {}
        for role in ("developer", "viewer"):
            email = f"prd047-{role}-{self.suffix}@metahub.local"
            password = f"Prd047!{secrets.token_urlsafe(18)}"
            request_id = f"req_prd047_user_{role}_{self.suffix}"
            created = expect_json(
                self.control.post(
                    "/users",
                    headers=self._headers(self.admin_token, request_id),
                    json={
                        "email": email,
                        "display_name": f"PRD047 runtime {role}",
                        "password": password,
                        "role": role,
                    },
                ),
                201,
                request_id,
            )
            self.resource_ids[f"{role}_user_id"] = str(created["user"]["user_id"])
            tokens[role] = self._login(self.args.tenant_slug, email, password, role)
        # 返回值只包含内存 Token，调用完成后由 close 主动清空且绝不进入状态制品。
        return tokens

    def _create_route(
        self,
        token: str,
        environment: dict[str, str],
        receiver: AcceptanceServer,
        *,
        prefix: str,
    ) -> dict[str, Any]:
        """创建两 Provider、两模型和主备 Alias；价格只使用带版本的 CNY 目录事实。"""

        host, port = receiver.server_address
        ids: dict[str, str] = {}
        for label, price in (("a", 9), ("b", 1)):
            provider_code = f"prd047_{prefix}_{label}_{self.suffix}"
            provider_request_id = f"req_prd047_provider_{prefix}_{label}_{self.suffix}"
            provider = expect_json(
                self.control.post(
                    "/providers",
                    headers=self._headers(token, provider_request_id),
                    json={
                        "provider_code": provider_code,
                        "name": f"PRD047 {prefix} Provider {label.upper()}",
                        "provider_type": "openai_compatible",
                        "base_url": f"http://{host}:{port}/upstream-{label}/v1",
                        "auth_type": "none",
                        "status": "active",
                    },
                ),
                201,
                provider_request_id,
            )
            provider_id = str(provider["provider"]["provider_id"])
            model_request_id = f"req_prd047_model_{prefix}_{label}_{self.suffix}"
            model = expect_json(
                self.control.post(
                    "/models",
                    headers=self._headers(token, model_request_id),
                    json={
                        "internal_model_id": f"prd047-{prefix}-{label}-{self.suffix}",
                        "provider_id": provider_id,
                        "provider_model_id": f"prd047-{label}",
                        "display_name": f"PRD047 {prefix} Model {label.upper()}",
                        "capabilities": {"chat": True, "stream": True, "embeddings": False},
                        "context_window": 8192,
                        "input_price_per_1k": price,
                        "output_price_per_1k": price,
                        "currency": "CNY",
                        "price_schema_version": f"prd047-{label}-v1",
                        "status": "active",
                    },
                ),
                201,
                model_request_id,
            )
            ids[f"provider_{label}_id"] = provider_id
            ids[f"model_{label}_id"] = str(model["model"]["model_id"])
            resource_prefix = "" if prefix == "default" else "other_"
            # 每创建一个对象就登记稳定 ID，确保后续任一步失败仍可按白名单逆序禁用。
            self.resource_ids[f"{resource_prefix}provider_{label}_id"] = provider_id
            self.resource_ids[f"{resource_prefix}model_{label}_id"] = ids[
                f"model_{label}_id"
            ]
        alias_request_id = f"req_prd047_alias_{prefix}_{self.suffix}"
        alias = expect_json(
            self.control.post(
                "/model-aliases",
                headers=self._headers(token, alias_request_id),
                json={
                    "alias": f"prd047-{prefix}-{self.suffix}",
                    "environment_id": environment["environment_id"],
                    "default_model_id": ids["model_a_id"],
                    "fallback_model_id": ids["model_b_id"],
                    "description": "PRD-047 runtime acceptance",
                    "status": "active",
                },
            ),
            201,
            alias_request_id,
        )["model_alias"]
        ids.update(
            {
                "alias_id": str(alias["model_alias_id"]),
                "alias": str(alias["alias"]),
                "policy_id": str(alias["routing_policy"]["routing_policy_id"]),
                "version": int(alias["routing_policy"]["version"]),
            }
        )
        resource_prefix = "" if prefix == "default" else "other_"
        self.resource_ids[f"{resource_prefix}alias_id"] = ids["alias_id"]
        self.resource_ids[f"{resource_prefix}routing_policy_id"] = ids["policy_id"]
        return ids

    def _create_other_tenant_alias(self, receiver: AcceptanceServer) -> str:
        """用第二租户 admin 创建业务图，供默认租户执行真实 path-ID 防枚举攻击。"""

        token = self._login(
            self.args.other_tenant_slug,
            self.args.other_admin_email,
            self.args.other_admin_password,
            "other_admin",
        )
        environment_request_id = f"req_prd047_other_environment_{self.suffix}"
        environment = expect_json(
            self.control.get(
                "/environments?code=dev",
                headers=self._headers(token, environment_request_id),
            ),
            200,
            environment_request_id,
        )["environments"]
        # 新租户由数据库触发器原子创建默认 dev；这里必须经第二租户 API 上下文读取，
        # 既避免重复 POST 制造租户内 code 冲突，也验证默认租户看不到该 Environment。
        assert len(environment) == 1 and environment[0]["code"] == "dev"
        environment = environment[0]
        self.resource_ids["other_environment_id"] = str(environment["environment_id"])
        route = self._create_route(
            token,
            {"environment_id": str(environment["environment_id"]), "code": "dev"},
            receiver,
            prefix="other",
        )
        self.resource_ids.update(
            {
                "other_provider_a_id": route["provider_a_id"],
                "other_provider_b_id": route["provider_b_id"],
                "other_model_a_id": route["model_a_id"],
                "other_model_b_id": route["model_b_id"],
                "other_alias_id": route["alias_id"],
                "other_routing_policy_id": route["policy_id"],
            }
        )
        return route["alias_id"]

    def _create_virtual_key(self, environment_id: str) -> str:
        """创建一次性 Gateway Key；明文只返回调用方内存，状态文件仅保存内部 ID。"""

        request_id = f"req_prd047_virtual_key_{self.suffix}"
        body = expect_json(
            self.control.post(
                "/virtual-keys",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "name": f"PRD047 runtime {self.suffix}",
                    "environment_id": environment_id,
                    "description": "PRD-047 isolated acceptance",
                },
            ),
            201,
            request_id,
        )
        self.resource_ids["virtual_key_id"] = str(body["virtual_key"]["virtual_key_id"])
        plain_value = str(body.get("plain_value", ""))
        assert plain_value.startswith("mhv_")
        return plain_value

    def _create_runtime_budget(self, environment_id: str) -> dict[str, str]:
        """通过公开 API 给一次性 Virtual Key 创建高额度 active CNY 预算。"""

        virtual_key_id = self.resource_ids["virtual_key_id"]
        with closing(self._connect()) as connection:
            owner = connection.execute(
                """
                SELECT tenant_id, environment_id, status
                FROM virtual_keys
                WHERE virtual_key_id=? AND environment_id=?
                """,
                (virtual_key_id, environment_id),
            ).fetchone()
        assert owner is not None and owner["status"] == "active"
        rule_name = f"PRD047 runtime budget {self.suffix}"
        # 请求前先登记完整 owner intent；若服务已提交但响应损坏，失败清理仍能精确定位唯一 scope。
        self.budget_intent = {
            "tenant_id": str(owner["tenant_id"]),
            "environment_id": environment_id,
            "scope_id": virtual_key_id,
            "rule_name": rule_name,
        }
        request_id = f"req_prd047_budget_{self.suffix}"
        body = expect_json(
            self.control.patch(
                "/budgets",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "scope_type": "virtual_key",
                    "scope_id": virtual_key_id,
                    "environment_id": environment_id,
                    "name": rule_name,
                    "daily_budget_cny": "1000000",
                    "warning_threshold_percent": 80,
                    "exhausted_action": "block",
                    "status": "active",
                },
            ),
            200,
            request_id,
        )
        rule = body.get("budget_rule")
        assert isinstance(rule, dict), "budget API response missing rule"
        budget_rule_id = str(rule.get("budget_rule_id", ""))
        # 服务端 ID 一经出现立即进入资源账本，后续字段断言失败也不会留下 active 规则。
        if budget_rule_id:
            self.resource_ids["budget_rule_id"] = budget_rule_id
            self.budget_intent["budget_rule_id"] = budget_rule_id
        assert budget_rule_id
        assert rule.get("tenant_id") == self.budget_intent["tenant_id"]
        assert rule.get("environment_id") == environment_id
        assert rule.get("scope_type") == "virtual_key" and rule.get("scope_id") == virtual_key_id
        assert rule.get("name") == rule_name and rule.get("status") == "active"
        assert str(rule.get("daily_budget_cny")) == "1000000"
        return dict(self.budget_intent)

    def _assert_budget_settlement(
        self,
        request_id: str,
        budget: dict[str, str],
    ) -> dict[str, bool | int]:
        """核对 cost_first 成功请求的预留、零 outstanding 与唯一 Usage 事实。"""

        with closing(self._connect()) as connection:
            return budget_settlement_summary(
                connection,
                tenant_id=budget["tenant_id"],
                request_id=request_id,
                budget_rule_id=budget["budget_rule_id"],
            )

    def _disable_budget_after_evidence(
        self,
        budget: dict[str, str],
    ) -> dict[str, bool | int]:
        """取证后通过公开 API 禁用临时预算，并用数据库精确复核唯一终态。"""

        request_id = f"req_prd047_budget_disable_{self.suffix}"
        body = expect_json(
            self.control.patch(
                "/budgets",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "scope_type": "virtual_key",
                    "scope_id": budget["scope_id"],
                    "environment_id": budget["environment_id"],
                    "status": "disabled",
                },
            ),
            200,
            request_id,
        )
        rule = body.get("budget_rule")
        assert isinstance(rule, dict), "budget disable response missing rule"
        assert rule.get("budget_rule_id") == budget["budget_rule_id"]
        assert rule.get("tenant_id") == budget["tenant_id"]
        assert rule.get("status") == "disabled"
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT status FROM budget_rules
                WHERE tenant_id=? AND budget_rule_id=? AND environment_id=?
                  AND scope_type='virtual_key' AND scope_id=? AND name=?
                """,
                (
                    budget["tenant_id"],
                    budget["budget_rule_id"],
                    budget["environment_id"],
                    budget["scope_id"],
                    budget["rule_name"],
                ),
            ).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "disabled"
        return {"rule_disabled": True, "disabled_count": 1}

    def _create_webhook(self, receiver: AcceptanceServer) -> None:
        """订阅唯一 PRD-047 事件，并把一次性 Secret 只交给回环接收器内存。"""

        host, port = receiver.server_address
        request_id = f"req_prd047_webhook_{self.suffix}"
        body = expect_json(
            self.control.post(
                "/webhooks",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "name": f"PRD047 runtime {self.suffix}",
                    "endpoint_url": f"http://{host}:{port}/hook",
                    "event_types": ["route.policy_selected"],
                    "timeout_ms": 2000,
                    "status": "active",
                },
            ),
            200,
            request_id,
        )
        self.resource_ids["webhook_endpoint_id"] = str(
            body["webhook"]["webhook_endpoint_id"]
        )
        receiver.signing_secret = str(body.get("signing_secret", ""))
        assert receiver.signing_secret

    def _verify_rbac_and_tenant(
        self, route: dict[str, Any], other_alias_id: str
    ) -> dict[str, bool]:
        """验证路由与预算写权限、读权限以及跨租户资源统一不可见。"""

        path = f"/model-aliases/{route['alias_id']}/routing-mode"
        developer_deny_id = f"req_prd047_dev_deny_{self.suffix}"
        viewer_deny_id = f"req_prd047_viewer_deny_{self.suffix}"
        developer_list_id = f"req_prd047_dev_list_{self.suffix}"
        viewer_list_id = f"req_prd047_viewer_list_{self.suffix}"
        cross_tenant_id = f"req_prd047_cross_tenant_{self.suffix}"
        budget_developer_id = f"req_prd047_budget_dev_deny_{self.suffix}"
        budget_viewer_id = f"req_prd047_budget_viewer_deny_{self.suffix}"
        budget_cross_tenant_id = f"req_prd047_budget_cross_tenant_{self.suffix}"
        budget_developer_trace_id = f"trc_prd047_budget_dev_deny_{self.suffix}"
        budget_viewer_trace_id = f"trc_prd047_budget_viewer_deny_{self.suffix}"
        budget_cross_tenant_trace_id = f"trc_prd047_budget_cross_tenant_{self.suffix}"

        budget_owner = (
            self.budget_intent["tenant_id"],
            self.budget_intent["environment_id"],
            self.budget_intent["scope_id"],
            self.budget_intent["rule_name"],
        )
        budget_rule_id = self.budget_intent["budget_rule_id"]
        with closing(self._connect()) as connection:
            budget_count_before = int(
                connection.execute("SELECT COUNT(*) FROM budget_rules").fetchone()[0]
            )
            target_before = connection.execute(
                """
                SELECT budget_rule_id, status FROM budget_rules
                WHERE tenant_id=? AND environment_id=? AND scope_type='virtual_key'
                  AND scope_id=? AND name=?
                """,
                budget_owner,
            ).fetchall()
        assert len(target_before) == 1, "budget RBAC target owner must be unique"
        assert (
            target_before[0]["budget_rule_id"] == budget_rule_id
            and target_before[0]["status"] == "active"
        ), "budget RBAC target state mismatch"

        # 权限攻击复用现有规则的值；即使权限门禁回归，也不会凭空制造第二条 scope 规则。
        budget_payload = {
            "scope_type": "virtual_key",
            "scope_id": self.budget_intent["scope_id"],
            "environment_id": self.budget_intent["environment_id"],
            "name": self.budget_intent["rule_name"],
            "daily_budget_cny": "1000000",
            "warning_threshold_percent": 80,
            "exhausted_action": "block",
            "status": "active",
        }
        developer_denied = self.control.patch(
            path,
            headers=self._headers(self.role_tokens["developer"], developer_deny_id),
            content=b"x" * 9000,
        )
        viewer_denied = self.control.patch(
            path,
            headers=self._headers(self.role_tokens["viewer"], viewer_deny_id),
            json={"routing_mode": "cost_first", "expected_version": route["version"]},
        )
        developer_list = self.control.get(
            "/model-aliases?environment=dev",
            headers=self._headers(self.role_tokens["developer"], developer_list_id),
        )
        viewer_list = self.control.get(
            "/model-aliases",
            headers=self._headers(self.role_tokens["viewer"], viewer_list_id),
        )
        cross_tenant = self.control.patch(
            f"/model-aliases/{other_alias_id}/routing-mode",
            headers=self._headers(self.admin_token, cross_tenant_id),
            json={"routing_mode": "cost_first", "expected_version": 1},
        )
        budget_developer_denied = self.control.patch(
            "/budgets",
            headers={
                **self._headers(self.role_tokens["developer"], budget_developer_id),
                "X-Trace-ID": budget_developer_trace_id,
            },
            json=budget_payload,
        )
        budget_viewer_denied = self.control.patch(
            "/budgets",
            headers={
                **self._headers(self.role_tokens["viewer"], budget_viewer_id),
                "X-Trace-ID": budget_viewer_trace_id,
            },
            json=budget_payload,
        )
        # 当前租户管理员携带第二租户 Environment，但继续使用当前租户 Key，必须在环境解析阶段返回 404。
        budget_cross_tenant_payload = {
            **budget_payload,
            "environment_id": self.resource_ids["other_environment_id"],
        }
        budget_cross_tenant = self.control.patch(
            "/budgets",
            headers={
                **self._headers(self.admin_token, budget_cross_tenant_id),
                "X-Trace-ID": budget_cross_tenant_trace_id,
            },
            json=budget_cross_tenant_payload,
        )
        assert (
            expect_json(developer_denied, 403, developer_deny_id)["code"]
            == "auth.permission_denied"
        )
        assert (
            expect_json(viewer_denied, 403, viewer_deny_id)["code"]
            == "auth.permission_denied"
        )
        developer_body = expect_json(developer_list, 200, developer_list_id)
        assert route["alias"] in developer_list.text and other_alias_id not in developer_list.text
        assert developer_body["model_aliases"]
        assert (
            expect_json(viewer_list, 403, viewer_list_id)["code"]
            == "auth.permission_denied"
        )
        assert (
            expect_json(cross_tenant, 404, cross_tenant_id)["code"]
            == "model_alias.not_found"
        )
        assert (
            expect_json(
                budget_developer_denied,
                403,
                budget_developer_id,
                budget_developer_trace_id,
            )["code"]
            == "auth.permission_denied"
        )
        assert (
            expect_json(
                budget_viewer_denied,
                403,
                budget_viewer_id,
                budget_viewer_trace_id,
            )["code"]
            == "auth.permission_denied"
        )
        assert (
            expect_json(
                budget_cross_tenant,
                404,
                budget_cross_tenant_id,
                budget_cross_tenant_trace_id,
            )["code"]
            == "environment.not_found"
        )

        with closing(self._connect()) as connection:
            budget_count_after = int(
                connection.execute("SELECT COUNT(*) FROM budget_rules").fetchone()[0]
            )
            target_after = connection.execute(
                """
                SELECT budget_rule_id, status FROM budget_rules
                WHERE tenant_id=? AND environment_id=? AND scope_type='virtual_key'
                  AND scope_id=? AND name=?
                """,
                budget_owner,
            ).fetchall()
        assert budget_count_after == budget_count_before, "budget RBAC attacks created residue"
        assert len(target_after) == 1, "budget RBAC target owner changed"
        assert (
            target_after[0]["budget_rule_id"] == budget_rule_id
            and target_after[0]["status"] == "active"
        ), "budget RBAC target changed"
        return {
            "developer_read": True,
            "developer_write_denied_before_body": True,
            "viewer_read_write_denied": True,
            "cross_tenant_not_found": True,
            "budget_developer_write_denied": True,
            "budget_viewer_write_denied": True,
            "budget_cross_tenant_environment_not_found": True,
            "budget_attack_no_extra_rules": True,
            "budget_target_rule_unique": True,
        }

    def _verify_cost_first(
        self,
        route: dict[str, Any],
        virtual_key: str,
        receiver: AcceptanceServer,
    ) -> dict[str, Any]:
        """配置 cost_first，验证精确幂等/CAS，并核对低成本模型的全链路归因。"""

        path = f"/model-aliases/{route['alias_id']}/routing-mode"
        mutation_request_id = f"req_prd047_mode_cost_{self.suffix}"
        payload = {"routing_mode": "cost_first", "expected_version": route["version"]}
        changed = expect_json(
            self.control.patch(
                path,
                headers=self._headers(self.admin_token, mutation_request_id),
                json=payload,
            ),
            200,
            mutation_request_id,
        )
        repeated = expect_json(
            self.control.patch(
                path,
                headers=self._headers(self.admin_token, mutation_request_id),
                json=payload,
            ),
            200,
            mutation_request_id,
        )
        version = int(changed["routing_policy"]["version"])
        assert version == int(route["version"]) + 1
        assert repeated["routing_policy"] == changed["routing_policy"]
        stale_request_id = f"req_prd047_mode_stale_{self.suffix}"
        stale = self.control.patch(
            path,
            headers=self._headers(self.admin_token, stale_request_id),
            json={"routing_mode": "cost_first", "expected_version": route["version"]},
        )
        assert expect_json(stale, 409, stale_request_id)["code"] == (
            "model_alias.routing_policy_version_conflict"
        )
        conflict = self.control.patch(
            path,
            headers=self._headers(self.admin_token, mutation_request_id),
            json={"routing_mode": "availability_first", "expected_version": route["version"]},
        )
        assert expect_json(conflict, 409, mutation_request_id)["code"] == (
            "model_alias.routing_policy_request_conflict"
        )

        request_id = f"req_prd047_cost_{self.suffix}"
        before_calls = len(receiver.provider_calls())
        response = self._gateway_chat(virtual_key, route["alias"], request_id)
        assert response["choices"][0]["message"]["content"] == "selected-b"
        calls = receiver.provider_calls()[before_calls:]
        assert calls == [{"provider": "b", "model": "prd047-b"}]
        detail = self._request_log_detail(request_id)
        decision = detail["routing_decision"]
        candidates = detail["routing_candidates"]
        assert decision["routing_mode"] == "cost_first"
        assert decision["routing_policy_version"] == version
        assert decision["selected_model_id"] == route["model_b_id"]
        assert decision["price_as_of"] and decision["estimated_input_tokens"] is not None
        assert len(candidates) == 2
        selected = next(item for item in candidates if item["status"] == "selected")
        standby = next(item for item in candidates if item["status"] == "standby")
        assert selected["model_id"] == route["model_b_id"] and selected["rank"] == 1
        assert standby["model_id"] == route["model_a_id"] and standby["rank"] == 2
        assert float(selected["estimated_cost_amount"]) < float(standby["estimated_cost_amount"])
        self._assert_usage_attributed(request_id, route["model_b_id"], route["provider_b_id"])
        event = self._wait_for_request_event(receiver, request_id)
        assert event["routing_mode"] == "cost_first"
        assert event["selected_model_id"] == route["model_b_id"]
        self.request_ids.append(request_id)
        return {
            "request_id": request_id,
            "version": version,
            "selected_model_id": route["model_b_id"],
            "candidate_count": 2,
            "webhook_signature_valid": True,
            "usage_attributed": True,
        }

    def _verify_availability_first(
        self,
        route: dict[str, Any],
        virtual_key: str,
        receiver: AcceptanceServer,
        current_version: int,
    ) -> dict[str, Any]:
        """把 default Provider 置为新鲜 unhealthy，验证 availability_first 选择健康备用。"""

        request_id = f"req_prd047_mode_availability_{self.suffix}"
        changed = expect_json(
            self.control.patch(
                f"/model-aliases/{route['alias_id']}/routing-mode",
                headers=self._headers(self.admin_token, request_id),
                json={
                    "routing_mode": "availability_first",
                    "expected_version": current_version,
                },
            ),
            200,
            request_id,
        )
        version = int(changed["routing_policy"]["version"])
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE providers
                SET health_status='unhealthy', health_updated_at=?
                WHERE provider_id=?
                """,
                (now, route["provider_a_id"]),
            )
            connection.execute(
                """
                UPDATE providers
                SET health_status='healthy', health_updated_at=?
                WHERE provider_id=?
                """,
                (now, route["provider_b_id"]),
            )

        gateway_request_id = f"req_prd047_availability_{self.suffix}"
        before_calls = len(receiver.provider_calls())
        response = self._gateway_chat(virtual_key, route["alias"], gateway_request_id)
        assert response["choices"][0]["message"]["content"] == "selected-b"
        assert receiver.provider_calls()[before_calls:] == [{"provider": "b", "model": "prd047-b"}]
        detail = self._request_log_detail(gateway_request_id)
        decision = detail["routing_decision"]
        candidates = detail["routing_candidates"]
        rejected = next(item for item in candidates if item["model_id"] == route["model_a_id"])
        assert decision["routing_mode"] == "availability_first"
        assert decision["selected_model_id"] == route["model_b_id"]
        assert decision["price_as_of"] is None and decision["estimated_input_tokens"] is None
        assert rejected["status"] == "rejected"
        assert rejected["reason_code"] == "routing.candidate.provider_unhealthy"
        event = self._wait_for_request_event(receiver, gateway_request_id)
        assert event["routing_mode"] == "availability_first" and event["signature_valid"]
        self.request_ids.append(gateway_request_id)
        return {
            "request_id": gateway_request_id,
            "version": version,
            "selected_model_id": route["model_b_id"],
            "fresh_unhealthy_rejected": True,
            "price_not_evaluated": True,
        }

    def _verify_no_keypool(
        self,
        route: dict[str, Any],
        virtual_key: str,
        receiver: AcceptanceServer,
    ) -> dict[str, Any]:
        """把两 Provider 改为 bearer 且不配置 Key，验证失败关闭、零上游调用和失败日志。"""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE providers
                SET auth_type='bearer', health_status='unknown', health_updated_at=NULL
                WHERE provider_id IN (?, ?)
                """,
                (route["provider_a_id"], route["provider_b_id"]),
            )
        request_id = f"req_prd047_no_keypool_{self.suffix}"
        before_calls = len(receiver.provider_calls())
        try:
            response = self.gateway.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {virtual_key}",
                    "X-Request-ID": request_id,
                },
                json={
                    "model": route["alias"],
                    "messages": [{"role": "user", "content": self.prompt_sentinel}],
                    "max_tokens": 32,
                },
            )
            body = expect_json(response, 503, request_id)
            assert body["code"] == "gateway.no_route_available"
        finally:
            # 即使断言失败也恢复 auth_type，避免后续浏览器页面看到被验收破坏的候选图。
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "UPDATE providers SET auth_type='none' WHERE provider_id IN (?, ?)",
                    (route["provider_a_id"], route["provider_b_id"]),
                )
        assert len(receiver.provider_calls()) == before_calls
        detail = self._request_log_detail(request_id)
        assert detail["log"]["status"] == "failed"
        assert detail["log"]["error_code"] == "gateway.no_route_available"
        assert detail["routing_decision"]["decision_status"] == "failed"
        assert all(
            candidate["reason_code"] == "routing.candidate.keypool_unavailable"
            for candidate in detail["routing_candidates"]
        )
        self.request_ids.append(request_id)
        return {
            "request_id": request_id,
            "error_code": "gateway.no_route_available",
            "provider_calls": 0,
            "failed_request_tracked": True,
        }

    def _gateway_chat(self, virtual_key: str, model: str, request_id: str) -> dict[str, Any]:
        """发送真实 Gateway Chat；Prompt 哨兵只存在于内存和回环网络请求。"""

        response = self.gateway.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {virtual_key}",
                "X-Request-ID": request_id,
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self.prompt_sentinel},
                            {"type": "text", "text": " structured"},
                        ],
                    }
                ],
                "max_completion_tokens": 64,
            },
        )
        return expect_json(response, 200, request_id)

    def _request_log_detail(self, request_id: str) -> dict[str, Any]:
        """用 developer Token 读取当前租户强类型 Candidate 详情并执行敏感字段扫描。"""

        detail_request_id = f"req_prd047_log_detail_{len(self.request_ids)}_{self.suffix}"
        response = self.control.get(
            f"/request-logs/{request_id}",
            headers=self._headers(self.role_tokens["developer"], detail_request_id),
        )
        body = expect_json(response, 200, detail_request_id)
        assert self.prompt_sentinel not in response.text
        detail = body["request_log"]
        assert isinstance(detail.get("routing_candidates"), list)
        return detail

    def _assert_usage_attributed(self, request_id: str, model_id: str, provider_id: str) -> None:
        """从事实库核对 Usage 经 RequestLog 归因实际选中模型和 Provider。"""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT rl.model_id, rl.provider_id, COUNT(ul.usage_ledger_id) AS usage_count
                FROM request_logs rl
                LEFT JOIN usage_ledger ul
                  ON ul.tenant_id=rl.tenant_id AND ul.request_id=rl.request_id
                WHERE rl.request_id=?
                GROUP BY rl.model_id, rl.provider_id
                """,
                (request_id,),
            ).fetchone()
        assert row is not None
        assert row["model_id"] == model_id and row["provider_id"] == provider_id
        assert int(row["usage_count"]) == 1

    def _wait_for_request_event(
        self,
        receiver: AcceptanceServer,
        request_id: str,
    ) -> dict[str, Any]:
        """有界等待异步 Webhook，验证签名、事件 ID、触发类型和隐私字段。"""

        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            event = next(
                (
                    item
                    for item in receiver.webhook_events()
                    if item.get("request_id") == request_id
                    and item.get("trigger") == "request_selected"
                ),
                None,
            )
            if event is not None:
                assert event["event_type"] == "route.policy_selected"
                assert str(event["event_id"]).startswith("rpe_")
                assert event["signature_valid"] is True and event["privacy_ok"] is True
                forbidden = {"prompt", "response", "token", "provider_key", "base_url"}
                assert forbidden.isdisjoint(set(event["payload_keys"]))
                return event
            time.sleep(0.1)
        # 超过有界等待仍无事件说明异步投递链失败，错误只携带安全 request_id。
        raise AssertionError(f"route.policy_selected not received for {request_id}")

    def _verify_privacy(self) -> dict[str, int]:
        """扫描全部用户表文本列，证明 Prompt 哨兵未进入业务数据库。"""

        hits = 0
        with closing(self._connect()) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table in tables:
                columns = connection.execute(
                    f"PRAGMA table_info({quote_identifier(table)})"
                ).fetchall()
                for column in columns:
                    column_name = str(column[1])
                    query = (
                        f"SELECT COUNT(*) FROM {quote_identifier(table)} "
                        f"WHERE instr(CAST({quote_identifier(column_name)} AS TEXT), ?) > 0"
                    )
                    hits += int(
                        connection.execute(query, (self.prompt_sentinel,)).fetchone()[0]
                    )
        assert hits == 0
        return {"database_prompt_hits": hits}

    def _disable_webhook_after_evidence(self) -> None:
        """事件验收完成后禁用回环 Endpoint，避免浏览器阶段向已关闭接收器重试。"""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE webhook_endpoints
                SET status='disabled', updated_at=?
                WHERE webhook_endpoint_id=?
                """,
                (
                    datetime.now(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    self.resource_ids["webhook_endpoint_id"],
                ),
            )

    def cleanup_after_failure(self) -> dict[str, int]:
        """按已登记稳定 ID 禁用部分创建的资源，供 Runtime 中途失败时兜底收口。

        只处理本进程创建或登录得到的对象；默认租户管理员用户本身不在用户清单中，
        仅撤销本次 Session。事务内按入口、路由图、身份根逆序禁用，任何失败整体回滚。
        """

        identifiers = self.resource_ids
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        counts = {
            "sessions": 0,
            "users": 0,
            "aliases": 0,
            "models": 0,
            "providers": 0,
            "virtual_keys": 0,
            "webhooks": 0,
            "budget_rules": 0,
            "environments": 0,
            "tenants": 0,
        }
        with closing(self._connect()) as connection, connection:
            revoke_session = connection.cursor()
            for session_id in (
                value
                for key, value in identifiers.items()
                if key.endswith("_session_id") and value
            ):
                revoke_session.execute(
                    """
                    UPDATE sessions
                    SET status='revoked', revoked_at=COALESCE(revoked_at, ?), updated_at=?
                    WHERE session_id=? AND status!='revoked'
                    """,
                    (now, now, session_id),
                )
                counts["sessions"] += max(revoke_session.rowcount, 0)

            # Budget 必须先于一次性 Key 禁用；使用资源账本 ID 并同时复验完整 owner intent。
            if self.budget_intent:
                counts["budget_rules"] = disable_runtime_budget_rule(
                    connection,
                    tenant_id=self.budget_intent.get("tenant_id", ""),
                    environment_id=self.budget_intent.get("environment_id", ""),
                    scope_id=self.budget_intent.get("scope_id", ""),
                    rule_name=self.budget_intent.get("rule_name", ""),
                    budget_rule_id=identifiers.get("budget_rule_id"),
                    now=now,
                )

            for key, query, count_key in (
                (
                    "webhook_endpoint_id",
                    "UPDATE webhook_endpoints SET status='disabled', updated_at=? "
                    "WHERE webhook_endpoint_id=? AND status!='disabled'",
                    "webhooks",
                ),
                (
                    "virtual_key_id",
                    "UPDATE virtual_keys SET status='disabled', "
                    "disabled_at=COALESCE(disabled_at, ?), updated_at=? "
                    "WHERE virtual_key_id=? AND status!='disabled'",
                    "virtual_keys",
                ),
            ):
                object_id = identifiers.get(key)
                if not object_id:
                    continue
                parameters = (now, object_id) if key == "webhook_endpoint_id" else (
                    now,
                    now,
                    object_id,
                )
                cursor = connection.execute(query, parameters)
                counts[count_key] += max(cursor.rowcount, 0)

            for alias_id in (
                identifiers.get("alias_id"),
                identifiers.get("other_alias_id"),
            ):
                if not alias_id:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE model_aliases
                    SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                    WHERE model_alias_id=? AND status!='disabled'
                    """,
                    (now, now, alias_id),
                )
                counts["aliases"] += max(cursor.rowcount, 0)
            for model_id in (
                identifiers.get("model_a_id"),
                identifiers.get("model_b_id"),
                identifiers.get("other_model_a_id"),
                identifiers.get("other_model_b_id"),
            ):
                # 部分创建路径允许 ID 缺失，只能跳过未登记项，不能扫描或猜测其他模型。
                if not model_id:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE models
                    SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                    WHERE model_id=? AND status!='disabled'
                    """,
                    (now, now, model_id),
                )
                counts["models"] += max(cursor.rowcount, 0)
            for provider_id in (
                identifiers.get("provider_a_id"),
                identifiers.get("provider_b_id"),
                identifiers.get("other_provider_a_id"),
                identifiers.get("other_provider_b_id"),
            ):
                # Provider 清理同样限定稳定 ID 白名单，缺失项不能扩大到租户级批量更新。
                if not provider_id:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE providers
                    SET status='disabled', disabled_at=COALESCE(disabled_at, ?), updated_at=?
                    WHERE provider_id=? AND status!='disabled'
                    """,
                    (now, now, provider_id),
                )
                counts["providers"] += max(cursor.rowcount, 0)

            environment_id = identifiers.get("other_environment_id")
            if environment_id:
                cursor = connection.execute(
                    """
                    UPDATE environments SET status='disabled', updated_at=?
                    WHERE environment_id=? AND status!='disabled'
                    """,
                    (now, environment_id),
                )
                counts["environments"] += max(cursor.rowcount, 0)
            for user_id in (
                identifiers.get("developer_user_id"),
                identifiers.get("viewer_user_id"),
                identifiers.get("other_admin_user_id"),
            ):
                if not user_id:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE users SET status='disabled', updated_at=?
                    WHERE user_id=? AND status!='disabled'
                    """,
                    (now, user_id),
                )
                counts["users"] += max(cursor.rowcount, 0)
            tenant_id = identifiers.get("other_tenant_id")
            # 第二租户只在已成功登记精确 ID 时软禁用，默认租户绝不参与该分支。
            if tenant_id:
                cursor = connection.execute(
                    """
                    UPDATE tenants SET status='disabled', updated_at=?
                    WHERE tenant_id=? AND status!='disabled'
                    """,
                    (now, tenant_id),
                )
                counts["tenants"] += max(cursor.rowcount, 0)
        return counts

    def _connect(self) -> sqlite3.Connection:
        """连接显式独立验收库并启用外键，禁止退回开发库或内存库。"""

        connection = sqlite3.connect(self.args.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def expect_json(
    response: httpx.Response,
    expected_status: int,
    expected_request_id: str | None = None,
    expected_trace_id: str | None = None,
) -> dict[str, Any]:
    """断言 HTTP 状态和 JSON object；指定的链路 ID 必须由服务原样返回。"""

    try:
        body = response.json()
    # 非 JSON 响应只报告状态和媒体类型，避免 HTML/代理正文携带路径或凭据后被回显。
    except json.JSONDecodeError as exc:
        content_type = response.headers.get("content-type", "missing")
        raise AssertionError(
            f"response is not JSON: status={response.status_code}, content_type={content_type}"
        ) from exc
    if not isinstance(body, dict):
        # 不回显结构异常正文；登录和 Key 创建响应可能包含仅返回一次的敏感值。
        raise AssertionError(f"response JSON must be object: status={response.status_code}")
    if response.status_code != expected_status:
        # 状态异常只输出统一错误码和链路 ID，禁止把完整响应中的 Token/Key 写到终端。
        raise AssertionError(
            "unexpected response status: "
            f"actual={response.status_code}, expected={expected_status}, "
            f"code={body.get('code', '-')}, request_id={body.get('request_id', '-')}"
        )
    # Control DTO 在 JSON 中返回链路 ID；OpenAI-compatible 成功响应保持协议正文，改由响应头返回。
    response_request_id = body.get("request_id") or response.headers.get("X-Request-ID")
    response_trace_id = body.get("trace_id") or response.headers.get("X-Trace-ID")
    assert isinstance(response_request_id, str) and response_request_id, (
        "response request_id missing"
    )
    assert isinstance(response_trace_id, str) and response_trace_id, "response trace_id missing"
    # 调用方提供链路 ID 时必须精确回显，失败信息只包含安全 ID，不附带响应对象。
    if expected_request_id is not None:
        assert response_request_id == expected_request_id, (
            f"request_id mismatch: actual={response_request_id}, expected={expected_request_id}"
        )
    # 调用方显式发送 trace_id 时必须原样回显，才能证明三次权限攻击各自可独立追踪。
    if expected_trace_id is not None:
        assert response_trace_id == expected_trace_id, (
            f"trace_id mismatch: actual={response_trace_id}, expected={expected_trace_id}"
        )
    return body


def quote_identifier(value: str) -> str:
    """安全引用来自 sqlite_master/PRAGMA 的标识符，避免隐私扫描拼接可执行 SQL。"""

    return '"' + value.replace('"', '""') + '"'


def assert_loopback_url(value: str, label: str) -> None:
    """限制服务地址为 HTTP 回环，防止验收脚本误操作外部部署。"""

    parsed = urlparse(value)
    assert parsed.scheme == "http", f"{label} must use http"
    assert parsed.hostname in {"127.0.0.1", "localhost"}, f"{label} must be loopback"


def parse_args() -> argparse.Namespace:
    """解析运行参数，并验证数据库和制品路径都位于本仓库 PRD-047 隔离目录。"""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--control-url", default=os.getenv("METAHUB_CONTROL_API_URL", "http://127.0.0.1:8001")
    )
    parser.add_argument(
        "--gateway-url", default=os.getenv("METAHUB_GATEWAY_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--console-url", default=os.getenv("METAHUB_CONSOLE_URL", "http://127.0.0.1:5173")
    )
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument(
        "--suffix",
        default=datetime.now(UTC).strftime("%Y%m%dt%H%M%S").lower() + secrets.token_hex(3),
    )
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument("--admin-password", default=os.getenv("METAHUB_PRD047_ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--other-tenant-slug",
        default=os.getenv("METAHUB_PRD047_OTHER_TENANT_SLUG", ""),
    )
    parser.add_argument("--other-admin-email", default="admin@prd047-other.local")
    parser.add_argument(
        "--other-admin-password",
        default=os.getenv(
            "METAHUB_PRD047_OTHER_ADMIN_PASSWORD",
            f"Prd047Other!{secrets.token_urlsafe(18)}",
        ),
    )
    args = parser.parse_args()
    assert re.fullmatch(r"[a-z0-9]{8,32}", args.suffix), (
        "suffix must contain 8-32 lowercase letters or digits"
    )
    # 未显式提供时生成本轮唯一 slug，避免软禁用历史租户仍占用唯一约束。
    if not args.other_tenant_slug:
        args.other_tenant_slug = f"prd047-other-{args.suffix}"
    for label in ("control_url", "gateway_url", "console_url"):
        assert_loopback_url(getattr(args, label), label)
    assert args.admin_password, "METAHUB_PRD047_ADMIN_PASSWORD or --admin-password is required"
    artifact_root = (Path.cwd().resolve() / "artifacts" / "prd047").resolve()
    database_path = args.database_path.resolve()
    state_output = args.state_output.resolve()
    assert database_path.is_file(), "acceptance database does not exist"
    assert artifact_root in database_path.parents, "database must be inside artifacts/prd047"
    assert artifact_root in state_output.parents, "state output must be inside artifacts/prd047"
    assert database_path != state_output, "state output must not overwrite acceptance database"
    assert database_path.suffix == ".db", "acceptance database must use .db suffix"
    assert state_output.suffix == ".json", "state output must use .json suffix"
    args.database_path = database_path
    args.state_output = state_output
    return args


def main() -> int:
    """运行验收、原子写入脱敏 JSON 状态并把相同摘要输出到终端。"""

    args = parse_args()
    # 删除旧成功状态，确保本轮失败时 Browser 不会误用上一轮资源 ID。
    args.state_output.unlink(missing_ok=True)
    temporary = args.state_output.with_name(
        f".{args.state_output.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    acceptance = RuntimeAcceptance(args)
    try:
        with RunningAcceptanceServer(acceptance.prompt_sentinel) as receiver:
            result = acceptance.run(receiver)
        args.state_output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.state_output)
        print(encoded, end="")
        return 0
    except BaseException:
        # Runtime 未产出可供 Browser 清理的状态时，必须由当前进程禁用已登记的部分资源。
        temporary.unlink(missing_ok=True)
        acceptance.cleanup_after_failure()
        raise
    finally:
        acceptance.close()


if __name__ == "__main__":
    raise SystemExit(main())
