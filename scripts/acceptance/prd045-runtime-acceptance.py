"""PRD-045 策略快照版本化与发布的真实三服务运行验收。

脚本只从环境变量读取管理员密码、内部服务 Token 和 Virtual Key。敏感值只驻留
当前进程内存；输出仅包含版本、HTTP 状态、计数和布尔验收证据。验收会通过公开
Control/Gateway API 创建临时策略、用户和 Webhook Endpoint，不直接伪造业务结果。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from metahub_shared.runtime.webhooks import verify_webhook_signature

# 运行验收只接受这些公开错误字段，失败消息不得拼接响应正文、Token 或用户输入。
SAFE_ERROR_FIELDS = ("code", "request_id", "trace_id")
# 调用日志 DTO 绝不能出现这些正文或认证字段；比较时统一转为小写并去掉连接符。
FORBIDDEN_LOG_KEYS = {
    "authorization",
    "canonical_expression",
    "condition_expr",
    "encrypted_key",
    "encrypted_secret",
    "messages",
    "plain_value",
    "prompt",
    "provider_key",
    "response",
    "signing_secret",
    "virtual_key",
}


class AcceptanceWebhookServer(ThreadingHTTPServer):
    """保存本地验收接收状态的 Webhook Server。

    signing_secret 只在创建 Endpoint 后写入内存。received 仅保存事件 ID、版本、
    payload 键名和签名校验结果，不保存原始请求体或签名材料。
    """

    daemon_threads = True

    def __init__(self) -> None:
        """绑定随机回环端口，避免占用产品服务端口或把接收器暴露到局域网。"""

        super().__init__(("127.0.0.1", 0), AcceptanceWebhookHandler)
        self.signing_secret: str | None = None
        self.received: list[dict[str, Any]] = []


class AcceptanceWebhookHandler(BaseHTTPRequestHandler):
    """接收并校验 MetaHub HMAC Webhook，响应中不反射任何请求内容。"""

    def do_POST(self) -> None:  # noqa: N802
        """有界读取单次事件，完成签名和安全 payload 摘要校验后返回 204。"""

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 1 or length > 1_048_576:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length)
        server = self.server
        assert isinstance(server, AcceptanceWebhookServer)
        secret = server.signing_secret or ""
        signature = self.headers.get("X-MetaHub-Signature", "")
        try:
            payload = json.loads(body.decode("utf-8"))
        # Webhook 非 UTF-8 或非 JSON 正文不能进入验签证据，也不能把原始字节回显到日志。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        event_payload = payload.get("payload") if isinstance(payload, dict) else None
        safe_payload = event_payload if isinstance(event_payload, dict) else {}
        server.received.append(
            {
                "event_id": str(payload.get("event_id", "")),
                "event_type": str(payload.get("event_type", "")),
                "version": safe_payload.get("version"),
                "payload_keys": sorted(safe_payload),
                "signature_valid": bool(secret)
                and verify_webhook_signature(signature, secret, body),
            }
        )
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """关闭默认访问日志，避免 URL 或接收端异常正文进入验收产物。"""

        return


class RunningWebhookReceiver:
    """以 context manager 管理本地 Webhook 线程和确定性关闭。"""

    def __init__(self) -> None:
        """创建尚未启动的随机端口接收器。"""

        self.server = AcceptanceWebhookServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> AcceptanceWebhookServer:
        """启动接收线程并返回可写 signing_secret 的 Server。"""

        self.thread.start()
        return self.server

    def __exit__(self, *_exc_info: object) -> None:
        """停止 HTTP 循环并回收线程，防止验收结束后残留测试端口。"""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RuntimeAcceptance:
    """编排 PRD-045 公开 API、Gateway 热路径、RBAC 和数据库证据。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """加载非敏感地址并把必需凭据保存在当前对象内存。"""

        self.args = args
        self.admin_password = required_environment("METAHUB_ADMIN_PASSWORD")
        self.internal_token = required_environment("METAHUB_INTERNAL_CONFIG_TOKEN")
        self.virtual_key = required_environment("METAHUB_VIRTUAL_KEY")
        self.model = "default-chat"
        self.suffix = secrets.token_hex(5)
        self.prompt_sentinel = f"PRD045_PROMPT_SENTINEL_{self.suffix}"
        self.condition_expr = 'env == "dev" and model == "default-chat"'
        self.control = httpx.Client(
            base_url=args.control_url,
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        )
        self.gateway = httpx.Client(
            base_url=args.gateway_url,
            timeout=30,
            trust_env=False,
            follow_redirects=False,
        )
        self.public_response_bodies: list[str] = []
        self.admin_token = ""
        self.tenant_id = ""
        self.ephemeral_secrets: dict[str, str] = {}
        self.temporary_user_ids: list[str] = []
        self.temporary_tokens: list[str] = []
        self.mask_policies_to_restore: list[tuple[str, int]] = []
        self.acceptance_policy_id: str | None = None
        self.acceptance_policy_version: int | None = None
        self.acceptance_provider_key_id: str | None = None
        self.legacy_provider_key_ids: list[str] = []
        self.webhook_id: str | None = None

    def close(self) -> None:
        """关闭两个 HTTP 连接池，避免验收后继续持有 Bearer Header。"""

        self.control.close()
        self.gateway.close()

    def run(self, receiver: AcceptanceWebhookServer) -> dict[str, Any]:
        """顺序执行 v1/v2/v3、异常、权限、日志和敏感扫描并返回安全摘要。"""

        health = self._verify_health()
        self._assert_local_acceptance_scope()
        self.admin_token, self.tenant_id = self._login_admin()
        invalid_candidate = self._prepare_candidate_baseline()
        rbac = self._verify_runtime_rbac()
        provider = self._make_provider_healthy()
        stale_webhooks_disabled = self._disable_stale_runtime_webhooks()
        webhook_id, webhook_secret = self._create_webhook(receiver)
        policy = self._create_acceptance_policy()

        current_v0, head_v0, candidate_v0 = self._current_snapshot()
        baseline_version = current_version(current_v0)
        assert baseline_version == 0, "runtime acceptance requires a dedicated empty head"
        v1 = self._publish(
            head_v0,
            candidate_v0,
            reason=f"PRD045 runtime v1 {self.suffix}",
            request_id=f"req_prd045_runtime_v1_{self.suffix}",
        )
        assert v1["snapshot_version"] == baseline_version + 1
        idempotency = self._verify_publish_idempotency(head_v0, candidate_v0, v1)
        internal_v1 = self._verify_internal_contract(v1["snapshot_version"])
        gateway_v1 = self._wait_for_gateway_version(v1["snapshot_version"], "allow")

        updated_policy = self._change_policy_action(policy, "deny")
        current_v1, head_v1, candidate_v1 = self._current_snapshot()
        assert current_v1["candidate_matches_current"] is False
        v2 = self._publish(
            head_v1,
            candidate_v1,
            reason=f"PRD045 runtime v2 deny {self.suffix}",
            request_id=f"req_prd045_runtime_v2_{self.suffix}",
        )
        assert v2["snapshot_version"] == v1["snapshot_version"] + 1
        request_id_reuse = self._verify_request_id_reuse(gateway_v1["request_id"])
        gateway_v2 = self._wait_for_gateway_version(v2["snapshot_version"], "deny")

        v3 = self._rollback(
            v1["policy_snapshot_id"],
            head_etag=f'"{v2["snapshot_version"]}"',
            reason=f"PRD045 runtime rollback {self.suffix}",
            request_id=f"req_prd045_runtime_v3_{self.suffix}",
        )
        assert v3["snapshot_version"] == v2["snapshot_version"] + 1
        assert v3["content_hash"] == v1["content_hash"]
        assert v3["artifact_hash"] not in {v1["artifact_hash"], v2["artifact_hash"]}
        assert v3["rollback_source_snapshot_version"] == v1["snapshot_version"]
        internal_v3 = self._verify_internal_contract(v3["snapshot_version"])
        gateway_v3 = self._wait_for_gateway_version(v3["snapshot_version"], "allow")

        history = self._verify_history(v1, v2, v3)
        request_logs = self._verify_request_log_filters(gateway_v1, gateway_v2, gateway_v3)
        webhook = self._verify_webhook_events(receiver, v1, v2, v3)
        database = self._database_evidence(
            webhook_id,
            v1,
            v2,
            v3,
            gateway_v1,
            gateway_v2,
            gateway_v3,
        )
        response_scan = self._scan_public_responses(webhook_secret)
        return {
            "status": "passed",
            "health": health,
            "invalid_candidate": invalid_candidate,
            "rbac": rbac,
            "provider": provider,
            "stale_webhooks_disabled": stale_webhooks_disabled,
            "versions": {
                "baseline": baseline_version,
                "v1": public_snapshot(v1),
                "v2": public_snapshot(v2),
                "v3": public_snapshot(v3),
            },
            "idempotency": idempotency,
            "request_id_reuse": request_id_reuse,
            "internal": {"v1": internal_v1, "v3": internal_v3},
            "gateway": {
                "v1": public_gateway_result(gateway_v1),
                "v2": public_gateway_result(gateway_v2),
                "v3": public_gateway_result(gateway_v3),
            },
            "history": history,
            "request_logs": request_logs,
            "webhook": webhook,
            "database": database,
            "response_sensitive_scan": response_scan,
            "policy_version_after_patch": updated_policy["version"],
        }

    def _verify_health(self) -> dict[str, int]:
        """验证 Control、Gateway 和 Console 三个本地服务是否就绪。

        返回：仅含公开服务名与 HTTP 状态码的映射。
        约束：任一服务非 200 立即失败，且不得回显响应正文或运行配置。
        """

        statuses: dict[str, int] = {}
        for name, url in (
            ("control", f"{self.args.control_url}/health"),
            ("gateway", f"{self.args.gateway_url}/health"),
            ("console", self.args.console_url),
        ):
            response = httpx.get(url, timeout=10, trust_env=False, follow_redirects=False)
            assert response.status_code == 200, f"{name} health HTTP {response.status_code}"
            statuses[name] = response.status_code
        return statuses

    def _assert_local_acceptance_scope(self) -> None:
        """拒绝非回环服务或工作区外数据库，防止验收脚本误改共享环境。"""

        assert self.args.confirm_local_state_mutation, (
            "pass --confirm-local-state-mutation for this disposable local acceptance run"
        )
        assert 5 <= self.args.gateway_wait_seconds <= 120, (
            "gateway wait must be between 5 and 120 seconds"
        )
        loopback_hosts = {"127.0.0.1", "::1", "localhost"}
        for name, value in (
            ("control", self.args.control_url),
            ("gateway", self.args.gateway_url),
            ("console", self.args.console_url),
        ):
            host = (urlsplit(value).hostname or "").lower()
            assert host in loopback_hosts, f"{name} acceptance URL must be loopback"
        database = self.args.database.resolve()
        workspace = Path.cwd().resolve()
        assert database.is_relative_to(workspace), "acceptance database must be in workspace"

    def _login_admin(self) -> tuple[str, str]:
        """使用真实登录接口恢复 admin Token 和认证租户，不输出登录响应正文。"""

        response = self.control.post(
            "/auth/login",
            json={
                "tenant_slug": self.args.tenant_slug,
                "email": self.args.admin_email,
                "password": self.admin_password,
            },
        )
        body = expect_json(response, 200, require_ids=True)
        token = str(body.get("access_token", ""))
        tenant_id = str(body.get("user", {}).get("tenant_id", ""))
        assert token and tenant_id, "admin login missing safe identity fields"
        self.ephemeral_secrets["admin_token"] = token
        # Token 只交给当前验收对象的内存调用链，tenant_id 则用于所有数据库清理追加租户条件。
        return token, tenant_id

    def _auth_headers(self, *, request_id: str | None = None) -> dict[str, str]:
        """构造内存 Bearer Header；调用方只能额外传入非敏感 request_id。"""

        headers = {"Authorization": f"Bearer {self.admin_token}"}
        # 稳定 request_id 只用于幂等验收，不允许调用方通过此入口追加任意敏感 Header。
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _control_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        expected_code: str | None = None,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        record_body: bool = True,
    ) -> tuple[dict[str, Any], httpx.Response]:
        """调用 Control JSON API，并只在安全端点保存响应供泄露扫描。"""

        response = self.control.request(method, path, headers=headers, json=payload)
        body = expect_json(
            response,
            expected_status,
            expected_code=expected_code,
            require_ids=True,
        )
        if record_body:
            self.public_response_bodies.append(response.text)
        return body, response

    def _prepare_candidate_baseline(self) -> dict[str, Any]:
        """先证明 active mask 失败关闭，再禁用 mask 和旧验收策略。"""

        listed, _ = self._control_json(
            "GET",
            "/policies",
            expected_status=200,
            headers=self._auth_headers(),
        )
        policies = listed.get("policies", [])
        active_masks = [
            policy
            for policy in policies
            if policy.get("status") == "active"
            and policy.get("action", {}).get("type") == "mask"
        ]
        current, response = self._control_json(
            "GET",
            "/policy-snapshots/current",
            expected_status=200,
            headers=self._auth_headers(),
        )
        invalid_code = None
        if active_masks:
            assert current["candidate_status"] == "invalid"
            failed_headers = self._auth_headers(
                request_id=f"req_prd045_invalid_mask_{self.suffix}"
            )
            failed_headers.update(
                {
                    "If-Match": response.headers["ETag"],
                    "If-Policy-Candidate-Match": f'"{"0" * 64}"',
                }
            )
            failed, _ = self._control_json(
                "POST",
                "/policy-snapshots/publish",
                expected_status=422,
                expected_code="policy_snapshot.action_unsupported",
                headers=failed_headers,
                payload={"reason": "reject unsupported mask"},
            )
            invalid_code = failed["code"]

        cleanup = [
            policy
            for policy in policies
            if policy.get("status") == "active"
            and (
                policy.get("action", {}).get("type") == "mask"
                or str(policy.get("name", "")).startswith("PRD045 runtime acceptance")
            )
        ]
        active_mask_ids = {policy["policy_id"] for policy in active_masks}
        for policy in cleanup:
            headers = self._auth_headers(
                request_id=f"req_prd045_cleanup_{policy['policy_id']}"
            )
            headers["If-Match"] = f'"{policy["version"]}"'
            updated, _ = self._control_json(
                "PATCH",
                f"/policies/{policy['policy_id']}",
                expected_status=200,
                headers=headers,
                payload={"status": "disabled"},
            )
            if policy["policy_id"] in active_mask_ids:
                self.mask_policies_to_restore.append(
                    (policy["policy_id"], int(updated["policy"]["version"]))
                )
        return {
            "active_mask_count": len(active_masks),
            "rejection_code": invalid_code,
            "cleanup_count": len(cleanup),
        }

    def _verify_runtime_rbac(self) -> dict[str, Any]:
        """通过 admin 创建临时角色，并证明后端拒绝快照读写越权。"""

        tokens: dict[str, str] = {}
        statuses: dict[str, int] = {}
        for role in ("developer", "viewer"):
            password = f"Prd045!{secrets.token_urlsafe(20)}"
            email = f"prd045-runtime-{role}-{self.suffix}@metahub.local"
            self.ephemeral_secrets[f"{role}_password"] = password
            created, _ = self._control_json(
                "POST",
                "/users",
                expected_status=201,
                headers=self._auth_headers(
                    request_id=f"req_prd045_user_{role}_{self.suffix}"
                ),
                payload={
                    "email": email,
                    "display_name": f"PRD045 runtime {role}",
                    "password": password,
                    "role": role,
                },
            )
            assert created.get("user", {}).get("role") == role
            self.temporary_user_ids.append(str(created["user"]["user_id"]))
            login = self.control.post(
                "/auth/login",
                json={
                    "tenant_slug": self.args.tenant_slug,
                    "email": email,
                    "password": password,
                },
            )
            login_body = expect_json(login, 200, require_ids=True)
            tokens[role] = str(login_body["access_token"])
            self.temporary_tokens.append(tokens[role])
            self.ephemeral_secrets[f"{role}_token"] = tokens[role]

        backend_codes: list[str] = []
        for role, token in tokens.items():
            role_headers = {"Authorization": f"Bearer {token}"}
            requests = (
                ("current_get", "GET", "/policy-snapshots/current", None),
                ("history_get", "GET", "/policy-snapshots", None),
                (
                    "publish_post",
                    "POST",
                    "/policy-snapshots/publish",
                    {"reason": "permission boundary"},
                ),
                (
                    "rollback_post",
                    "POST",
                    "/policy-snapshots/psnap_not_owned/rollback",
                    {"reason": "permission boundary"},
                ),
            )
            for label, method, path, payload in requests:
                response = self.control.request(
                    method,
                    path,
                    headers=role_headers,
                    json=payload,
                )
                body = expect_json(
                    response,
                    403,
                    expected_code="auth.permission_denied",
                    require_ids=True,
                )
                statuses[f"{role}_{label}"] = response.status_code
                backend_codes.append(str(body["code"]))
                self.public_response_bodies.append(response.text)
        anonymous = self.control.get("/policy-snapshots/current")
        anonymous_body = expect_json(
            anonymous,
            401,
            expected_code="auth.token_missing",
            require_ids=True,
        )
        statuses["anonymous_current_get"] = anonymous.status_code
        backend_codes.append(str(anonymous_body["code"]))
        self.public_response_bodies.append(anonymous.text)
        return {
            "statuses": statuses,
            "backend_codes": backend_codes,
        }

    def _make_provider_healthy(self) -> dict[str, Any]:
        """通过 KeyPool API 创建可解密本地 Key，再探测并更新 Provider 健康状态。

        历史开发库可能仍含早期 ``local-placeholder``，它能保留迁移历史但不能被
        Gateway 解密。验收先禁用当前 active Key，再用公开管理 API 创建只供本地
        fake upstream 使用的随机 Key，证明密文落库与数据面解密链路都真实可用。
        """

        listed, _ = self._control_json(
            "GET", "/providers", expected_status=200, headers=self._auth_headers()
        )
        provider = next(
            item
            for item in listed.get("providers", [])
            if item.get("provider_code") == self.args.provider_code
        )
        provider_host = (urlsplit(str(provider["base_url"])).hostname or "").lower()
        assert provider_host in {"127.0.0.1", "::1", "localhost"}, (
            "runtime acceptance provider must be loopback"
        )
        connection = sqlite3.connect(self.args.database)
        legacy_key_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT provider_credential_id FROM provider_credentials "
                "WHERE tenant_id=? AND provider_id=? AND status='active' "
                "AND encrypted_key NOT LIKE 'aesgcm-v1:%'",
                (self.tenant_id, provider["provider_id"]),
            )
        }
        connection.close()
        keys, _ = self._control_json(
            "GET",
            f"/providers/{provider['provider_id']}/keys",
            expected_status=200,
            headers=self._auth_headers(),
        )
        disabled_count = 0
        for item in keys.get("provider_keys", []):
            owned_acceptance_key = str(item.get("name", "")).startswith(
                "PRD045 local acceptance "
            )
            legacy_key = str(item.get("provider_key_id", "")) in legacy_key_ids
            if item.get("status") != "active" or not (owned_acceptance_key or legacy_key):
                continue
            self._control_json(
                "PATCH",
                f"/provider-keys/{item['provider_key_id']}",
                expected_status=200,
                headers=self._auth_headers(
                    request_id=f"req_prd045_key_disable_{disabled_count}_{self.suffix}"
                ),
                payload={"status": "disabled"},
            )
            if legacy_key:
                self.legacy_provider_key_ids.append(str(item["provider_key_id"]))
            disabled_count += 1
        provider_key = f"sk-prd045-local-{secrets.token_urlsafe(24)}"
        self.ephemeral_secrets["provider_key"] = provider_key
        created_key, _ = self._control_json(
            "POST",
            f"/providers/{provider['provider_id']}/keys",
            expected_status=201,
            headers=self._auth_headers(
                request_id=f"req_prd045_key_create_{self.suffix}"
            ),
            payload={
                "name": f"PRD045 local acceptance {self.suffix}",
                "plain_value": provider_key,
                "weight": 100,
            },
        )
        assert created_key.get("provider_key", {}).get("status") == "active"
        self.acceptance_provider_key_id = str(
            created_key["provider_key"]["provider_key_id"]
        )
        tested, _ = self._control_json(
            "POST",
            f"/providers/{provider['provider_id']}/test",
            expected_status=200,
            headers=self._auth_headers(
                request_id=f"req_prd045_provider_test_{self.suffix}"
            ),
            payload={"timeout_seconds": 5},
        )
        result = tested.get("test_result", {})
        assert result.get("status") == "success", "provider test did not succeed"
        return {
            "provider_code": provider["provider_code"],
            "status": result["status"],
            "http_status": result.get("http_status"),
            "active_keys_disabled": disabled_count,
            "encrypted_key_created": True,
        }

    def _disable_stale_runtime_webhooks(self) -> int:
        """禁用此前异常中断留下的随机端口 Endpoint。

        当前 Webhook 管理 API 尚无禁用入口，因此只按认证 tenant_id 和本脚本专用
        名称前缀更新 active 行；既有客户 Endpoint 和其他租户数据不在修改范围内。
        """

        connection = sqlite3.connect(self.args.database)
        cursor = connection.execute(
            "UPDATE webhook_endpoints SET status='disabled', updated_at=? "
            "WHERE tenant_id=? AND status='active' AND name LIKE 'PRD045 runtime %'",
            (datetime.now(UTC).isoformat().replace("+00:00", "Z"), self.tenant_id),
        )
        connection.commit()
        disabled_count = int(cursor.rowcount)
        connection.close()
        return disabled_count

    def _create_webhook(
        self, receiver: AcceptanceWebhookServer
    ) -> tuple[str, str]:
        """创建只订阅快照发布的临时 Endpoint，并把一次性 Secret 交给接收器。"""

        host, port = receiver.server_address
        created, _ = self._control_json(
            "POST",
            "/webhooks",
            expected_status=200,
            headers=self._auth_headers(
                request_id=f"req_prd045_webhook_create_{self.suffix}"
            ),
            payload={
                "name": f"PRD045 runtime {self.suffix}",
                "endpoint_url": f"http://{host}:{port}/policy-snapshot",
                "event_types": ["policy_snapshot.published"],
                "status": "active",
                "timeout_ms": 2000,
            },
            record_body=False,
        )
        secret = str(created.get("signing_secret", ""))
        webhook_id = str(created.get("webhook", {}).get("webhook_endpoint_id", ""))
        assert secret and webhook_id
        receiver.signing_secret = secret
        self.ephemeral_secrets["webhook_secret"] = secret
        self.webhook_id = webhook_id
        # Secret 仅返回当前进程内做 HMAC 验证，公开结果和日志始终只输出验签布尔值。
        return webhook_id, secret

    def _create_acceptance_policy(self) -> dict[str, Any]:
        """创建最高优先级的匹配 allow 策略，作为 v1 的候选 A。"""

        created, _ = self._control_json(
            "POST",
            "/policies",
            expected_status=201,
            headers=self._auth_headers(
                request_id=f"req_prd045_policy_create_{self.suffix}"
            ),
            payload={
                "name": f"PRD045 runtime acceptance {self.suffix}",
                "description": "PRD-045 runtime acceptance policy",
                "status": "active",
                "priority": 1,
                "rule": {"condition_expr": self.condition_expr},
                "action": {"type": "allow"},
            },
        )
        policy = created["policy"]
        assert policy["version"] == 1 and policy["action"]["type"] == "allow"
        self.acceptance_policy_id = str(policy["policy_id"])
        self.acceptance_policy_version = int(policy["version"])
        return policy

    def _current_snapshot(self) -> tuple[dict[str, Any], str, str]:
        """读取 current 和发布双 CAS；候选必须处于可发布状态。"""

        body, response = self._control_json(
            "GET",
            "/policy-snapshots/current",
            expected_status=200,
            headers=self._auth_headers(),
        )
        assert body["candidate_status"] == "valid"
        head = response.headers.get("ETag", "")
        candidate = response.headers.get("X-Policy-Candidate-ETag", "")
        assert_strong_etag(head, "head")
        assert_strong_etag(candidate, "candidate")
        return body, head, candidate

    def _publish(
        self,
        head_etag: str,
        candidate_etag: str,
        *,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """使用 head+candidate 双 CAS 发布，并返回公共快照摘要。"""

        headers = self._auth_headers(request_id=request_id)
        headers.update(
            {"If-Match": head_etag, "If-Policy-Candidate-Match": candidate_etag}
        )
        body, _ = self._control_json(
            "POST",
            "/policy-snapshots/publish",
            expected_status=201,
            headers=headers,
            payload={"reason": reason},
        )
        assert body["idempotent_replay"] is False
        return body["policy_snapshot"]

    def _verify_publish_idempotency(
        self,
        head_etag: str,
        candidate_etag: str,
        v1: dict[str, Any],
    ) -> dict[str, Any]:
        """复用同一 request_id 成功回放，并覆盖指纹冲突、no-change 和 candidate 冲突。"""

        request_id = f"req_prd045_runtime_v1_{self.suffix}"
        reason = f"PRD045 runtime v1 {self.suffix}"
        headers = self._auth_headers(request_id=request_id)
        headers.update(
            {"If-Match": head_etag, "If-Policy-Candidate-Match": candidate_etag}
        )
        replay, _ = self._control_json(
            "POST",
            "/policy-snapshots/publish",
            expected_status=201,
            headers=headers,
            payload={"reason": reason},
        )
        assert replay["idempotent_replay"] is True
        assert replay["policy_snapshot"]["policy_snapshot_id"] == v1["policy_snapshot_id"]

        conflict, _ = self._control_json(
            "POST",
            "/policy-snapshots/publish",
            expected_status=409,
            expected_code="policy_snapshot.idempotency_conflict",
            headers=headers,
            payload={"reason": f"changed {reason}"},
        )
        current, latest_head, latest_candidate = self._current_snapshot()
        assert current["candidate_matches_current"] is True
        no_change_headers = self._auth_headers(
            request_id=f"req_prd045_no_change_{self.suffix}"
        )
        no_change_headers.update(
            {
                "If-Match": latest_head,
                "If-Policy-Candidate-Match": latest_candidate,
            }
        )
        no_change, _ = self._control_json(
            "POST",
            "/policy-snapshots/publish",
            expected_status=409,
            expected_code="policy_snapshot.no_changes",
            headers=no_change_headers,
            payload={"reason": "no change must not create a version"},
        )
        wrong_headers = self._auth_headers(
            request_id=f"req_prd045_candidate_conflict_{self.suffix}"
        )
        wrong_headers.update(
            {
                "If-Match": latest_head,
                "If-Policy-Candidate-Match": f'"{"f" * 64}"',
            }
        )
        wrong, _ = self._control_json(
            "POST",
            "/policy-snapshots/publish",
            expected_status=409,
            expected_code="policy_snapshot.candidate_conflict",
            headers=wrong_headers,
            payload={"reason": "wrong candidate CAS"},
        )
        return {
            "replay": True,
            "conflict_code": conflict["code"],
            "no_change_code": no_change["code"],
            "candidate_conflict_code": wrong["code"],
        }

    def _verify_internal_contract(self, expected_version: int) -> dict[str, Any]:
        """验证当前租户内部快照接口的认证、版本与缓存合同。

        参数：expected_version 为预期的已发布快照版本。
        返回：401/403/200/304、快照版本与 ETag 一致性摘要。
        约束：内部 Token、完整快照 wire 和策略 DSL 不得进入公开证据。
        """

        path = f"/internal/policy-snapshots/{self.tenant_id}/current"
        missing = self.control.get(path)
        missing_body = expect_json(
            missing,
            401,
            expected_code="auth.internal_token_required",
            require_ids=True,
        )
        wrong = self.control.get(path, headers={"X-Internal-Token": "wrong-token"})
        wrong_body = expect_json(
            wrong,
            403,
            expected_code="auth.internal_token_invalid",
            require_ids=True,
        )
        fetched = self.control.get(
            path, headers={"X-Internal-Token": self.internal_token}
        )
        fetched_body = expect_json(fetched, 200, require_ids=False)
        assert fetched_body["snapshot"]["snapshot_version"] == expected_version
        assert fetched.headers.get("Cache-Control") == "no-store"
        etag = fetched.headers.get("ETag", "")
        assert_strong_etag(etag, "internal")
        cached = self.control.get(
            path,
            headers={
                "X-Internal-Token": self.internal_token,
                "If-None-Match": etag,
            },
        )
        assert cached.status_code == 304 and cached.content == b""
        assert cached.headers.get("ETag") == etag
        self.public_response_bodies.extend([missing.text, wrong.text, fetched.text])
        return {
            "version": expected_version,
            "missing_status": missing.status_code,
            "wrong_status": wrong.status_code,
            "fetch_status": fetched.status_code,
            "not_modified_status": cached.status_code,
            "etag_reused": cached.headers.get("ETag") == etag,
            "codes": [missing_body["code"], wrong_body["code"]],
        }

    def _wait_for_gateway_version(
        self, expected_version: int, expected_decision: str
    ) -> dict[str, Any]:
        """轮询真实 Gateway 新请求，直到日志证明期望版本和 allow/deny 决策。"""

        deadline = time.monotonic() + self.args.gateway_wait_seconds
        attempt = 0
        expected_status = 200 if expected_decision == "allow" else 403
        while time.monotonic() < deadline:
            attempt += 1
            request_id = (
                f"req_prd045_gateway_v{expected_version}_{attempt}_{self.suffix}"
            )
            response = self.gateway.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.virtual_key}",
                    "X-Request-ID": request_id,
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": self.prompt_sentinel}],
                },
            )
            self.public_response_bodies.append(response.text)
            body = json_body_or_empty(response)
            if response.status_code != expected_status:
                time.sleep(0.5)
                continue
            if expected_decision == "deny" and body.get("code") != "gateway.policy_denied":
                time.sleep(0.5)
                continue
            detail = self._request_log_detail(request_id)
            log = detail["request_log"]["log"]
            if (
                log.get("snapshot_version") == expected_version
                and log.get("policy_decision") == expected_decision
            ):
                assert_no_forbidden_keys(detail)
                if expected_decision == "allow":
                    assert log["status"] == "success"
                else:
                    assert log["status"] == "failed"
                    assert log["error_code"] == "gateway.policy_denied"
                return {
                    "request_id": request_id,
                    "status": response.status_code,
                    "snapshot_version": log["snapshot_version"],
                    "snapshot_status": log["policy_snapshot_status"],
                    "decision": log["policy_decision"],
                    "error_code": log.get("error_code"),
                }
            time.sleep(0.5)
        # 轮询窗口耗尽必须失败，不能把尚未收敛的 Gateway 版本误记为运行验收通过。
        raise AssertionError(
            f"Gateway did not reach v{expected_version}/{expected_decision} in time"
        )

    def _request_log_detail(self, request_id: str) -> dict[str, Any]:
        """读取当前租户单条日志详情，响应只能包含安全治理元数据。"""

        body, _ = self._control_json(
            "GET",
            f"/request-logs/{request_id}",
            expected_status=200,
            headers=self._auth_headers(),
        )
        return body

    def _change_policy_action(
        self, policy: dict[str, Any], action_type: str
    ) -> dict[str, Any]:
        """用 Policy If-Match 把候选 A 改成 B，形成下一次发布的内容漂移。"""

        headers = self._auth_headers(
            request_id=f"req_prd045_policy_{action_type}_{self.suffix}"
        )
        headers["If-Match"] = f'"{policy["version"]}"'
        body, _ = self._control_json(
            "PATCH",
            f"/policies/{policy['policy_id']}",
            expected_status=200,
            headers=headers,
            payload={"action": {"type": action_type}},
        )
        assert body["policy"]["action"]["type"] == action_type
        if body["policy"]["policy_id"] == self.acceptance_policy_id:
            self.acceptance_policy_version = int(body["policy"]["version"])
        return body["policy"]

    def _verify_request_id_reuse(self, request_id: str) -> dict[str, Any]:
        """复用 v1 request_id 必须在 Provider 前返回 409，且首次日志版本保持不变。"""

        response = self.gateway.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.virtual_key}",
                "X-Request-ID": request_id,
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": "request id reuse"}],
            },
        )
        body = expect_json(
            response,
            409,
            expected_code="gateway.request_id_reused",
            require_ids=True,
        )
        self.public_response_bodies.append(response.text)
        first = self._request_log_detail(request_id)["request_log"]["log"]
        return {
            "status": response.status_code,
            "code": body["code"],
            "first_version_preserved": first["snapshot_version"],
        }

    def _rollback(
        self,
        snapshot_id: str,
        *,
        head_etag: str,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """按当前 head CAS 复制上一版内容，生成新的单调回滚版本。"""

        headers = self._auth_headers(request_id=request_id)
        headers["If-Match"] = head_etag
        body, _ = self._control_json(
            "POST",
            f"/policy-snapshots/{snapshot_id}/rollback",
            expected_status=201,
            headers=headers,
            payload={"reason": reason},
        )
        return body["policy_snapshot"]

    def _verify_history(
        self, v1: dict[str, Any], v2: dict[str, Any], v3: dict[str, Any]
    ) -> dict[str, Any]:
        """验证历史按版本倒序，且 current head 与回滚来源保持可解释。"""

        body, _ = self._control_json(
            "GET",
            "/policy-snapshots?limit=20&offset=0",
            expected_status=200,
            headers=self._auth_headers(),
        )
        versions = [item["snapshot_version"] for item in body["policy_snapshots"]]
        expected = [v3["snapshot_version"], v2["snapshot_version"], v1["snapshot_version"]]
        assert versions[:3] == expected
        return {"latest_versions": versions[:3], "total": body["total"]}

    def _verify_request_log_filters(
        self,
        gateway_v1: dict[str, Any],
        gateway_v2: dict[str, Any],
        gateway_v3: dict[str, Any],
    ) -> dict[str, Any]:
        """按版本和 request_id 查询三条真实日志，并复核安全 Policy 摘要。"""

        counts: dict[str, int] = {}
        for item in (gateway_v1, gateway_v2, gateway_v3):
            version = item["snapshot_version"]
            path = (
                "/request-logs?snapshot_version="
                f"{version}&request_id={item['request_id']}&limit=20&offset=0"
            )
            body, _ = self._control_json(
                "GET", path, expected_status=200, headers=self._auth_headers()
            )
            assert body["total"] == 1
            row = body["request_logs"][0]
            assert row["snapshot_version"] == version
            assert row["policy_decision"] == item["decision"]
            assert_no_forbidden_keys(body)
            counts[f"v{version}"] = body["total"]
        invalid, _ = self._control_json(
            "GET",
            "/request-logs?snapshot_version=0",
            expected_status=422,
            expected_code="request_log.query_invalid",
            headers=self._auth_headers(),
        )
        return {"filtered_counts": counts, "invalid_filter_code": invalid["code"]}

    def _verify_webhook_events(
        self,
        receiver: AcceptanceWebhookServer,
        v1: dict[str, Any],
        v2: dict[str, Any],
        v3: dict[str, Any],
    ) -> dict[str, Any]:
        """等待 v1/v2/v3 签名事件并验证 payload 只含安全发布元数据。"""

        expected_versions = {
            v1["snapshot_version"],
            v2["snapshot_version"],
            v3["snapshot_version"],
        }
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            received_versions = {
                item["version"]
                for item in receiver.received
                if item["event_type"] == "policy_snapshot.published"
            }
            if expected_versions.issubset(received_versions):
                break
            time.sleep(0.2)
        events = [
            item
            for item in receiver.received
            if item["event_type"] == "policy_snapshot.published"
            and item["version"] in expected_versions
        ]
        assert {item["version"] for item in events} == expected_versions
        assert all(item["signature_valid"] for item in events)
        allowed_keys = {
            "content_hash",
            "operation",
            "policy_count",
            "previous_version",
            "rollback_source_version",
            "version",
        }
        assert all(set(item["payload_keys"]) == allowed_keys for item in events)
        return {
            "versions": sorted(expected_versions),
            "signed_event_count": len(events),
            "all_signatures_valid": True,
            "payload_keys": sorted(allowed_keys),
        }

    def _database_evidence(
        self,
        webhook_id: str,
        v1: dict[str, Any],
        v2: dict[str, Any],
        v3: dict[str, Any],
        gateway_v1: dict[str, Any],
        gateway_v2: dict[str, Any],
        gateway_v3: dict[str, Any],
    ) -> dict[str, Any]:
        """从运行库核对 head/event/claim/Usage、不可变版本和敏感值零命中。"""

        connection = sqlite3.connect(self.args.database)
        connection.row_factory = sqlite3.Row
        versions = (v1["snapshot_version"], v2["snapshot_version"], v3["snapshot_version"])
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM policy_snapshots WHERE tenant_id=? "
            "AND snapshot_version IN (?, ?, ?)",
            (self.tenant_id, *versions),
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM policy_snapshot_events WHERE tenant_id=? "
            "AND snapshot_version IN (?, ?, ?)",
            (self.tenant_id, *versions),
        ).fetchone()[0]
        head = connection.execute(
            "SELECT current_snapshot_version FROM policy_snapshot_heads WHERE tenant_id=?",
            (self.tenant_id,),
        ).fetchone()
        request_ids = (
            gateway_v1["request_id"],
            gateway_v2["request_id"],
            gateway_v3["request_id"],
        )
        claim_rows = connection.execute(
            "SELECT request_id, snapshot_version, policy_snapshot_status "
            "FROM gateway_request_claims WHERE tenant_id=? AND request_id IN (?, ?, ?)",
            (self.tenant_id, *request_ids),
        ).fetchall()
        deny_attempts = connection.execute(
            "SELECT COUNT(*) FROM routing_attempts WHERE tenant_id=? AND request_id=?",
            (self.tenant_id, gateway_v2["request_id"]),
        ).fetchone()[0]
        success_usage = connection.execute(
            "SELECT COUNT(*) FROM usage_ledger WHERE tenant_id=? AND request_id IN (?, ?)",
            (self.tenant_id, gateway_v1["request_id"], gateway_v3["request_id"]),
        ).fetchone()[0]
        webhook_success = connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE tenant_id=? "
            "AND webhook_endpoint_id=? AND event_type='policy_snapshot.published' "
            "AND status='success'",
            (self.tenant_id, webhook_id),
        ).fetchone()[0]
        sensitive_values = {
            "admin_password": self.admin_password,
            "internal_token": self.internal_token,
            "virtual_key": self.virtual_key,
            "prompt_sentinel": self.prompt_sentinel,
            **self.ephemeral_secrets,
        }
        sensitive_hits = scan_database_values(connection, sensitive_values)
        forbidden_artifact_hits = scan_selected_tables(
            connection,
            {
                "prompt": self.prompt_sentinel,
                "policy_expression": self.condition_expr,
            },
            (
                "audit_logs",
                "console_page_events",
                "debug_console_events",
                "policy_snapshot_events",
                "request_logs",
                "webhook_deliveries",
                "webhook_delivery_attempts",
            ),
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.close()
        assert snapshot_count == 3 and event_count == 3
        assert head is not None and head[0] == v3["snapshot_version"]
        assert len(claim_rows) == 3
        assert {row["snapshot_version"] for row in claim_rows} == set(versions)
        assert deny_attempts == 0
        assert success_usage == 2
        assert webhook_success >= 3
        assert all(count == 0 for count in sensitive_hits.values())
        assert all(count == 0 for count in forbidden_artifact_hits.values())
        assert integrity == "ok" and foreign_keys == 0
        return {
            "snapshot_rows": snapshot_count,
            "event_rows": event_count,
            "head_version": head[0],
            "claim_rows": len(claim_rows),
            "deny_routing_attempts": deny_attempts,
            "successful_usage_rows": success_usage,
            "webhook_success_rows": webhook_success,
            "sensitive_hits": sensitive_hits,
            "forbidden_artifact_hits": forbidden_artifact_hits,
            "integrity_check": integrity,
            "foreign_key_rows": foreign_keys,
        }

    def _scan_public_responses(self, webhook_secret: str) -> dict[str, int]:
        """确认除一次性认证/Secret 响应外，所有公开响应都不反射敏感输入。"""

        combined = "\n".join(self.public_response_bodies)
        values = {
            "admin_password": self.admin_password,
            "internal_token": self.internal_token,
            "virtual_key": self.virtual_key,
            "prompt_sentinel": self.prompt_sentinel,
            "webhook_secret": webhook_secret,
            **self.ephemeral_secrets,
        }
        hits = {label: combined.count(value) for label, value in values.items() if value}
        assert all(count == 0 for count in hits.values())
        return hits

    def cleanup_resources(self) -> dict[str, Any]:
        """在成功和异常路径恢复临时账号、Policy、Key 与随机端口 Webhook。

        v1/v2/v3、事件、日志和 Usage 属于验收证据，必须保留；临时用户、active
        测试 Policy、验收 Key 和随机端口 Endpoint 不应继续生效。若已经形成 head，
        清理会在恢复 mask 前发布一个不含临时 allow 的安全恢复版本。
        清理异常只记录固定操作名与异常类型，不输出 URL、Token、正文或底层消息。
        """

        result: dict[str, Any] = {
            "acceptance_policy_disabled": False,
            "acceptance_provider_key_disabled": False,
            "legacy_provider_keys_restored": 0,
            "mask_policies_restored": 0,
            "recovery_snapshot_version": None,
            "temporary_sessions_logged_out": 0,
            "temporary_users_disabled": 0,
            "webhook_disabled": False,
            "errors": [],
        }
        # 随机端口接收器即将关闭，先阻止后续恢复版本向失效地址投递。
        if self.tenant_id and self.webhook_id:
            try:
                connection = sqlite3.connect(self.args.database)
                cursor = connection.execute(
                    "UPDATE webhook_endpoints SET status='disabled', updated_at=? "
                    "WHERE tenant_id=? AND webhook_endpoint_id=?",
                    (
                        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        self.tenant_id,
                        self.webhook_id,
                    ),
                )
                connection.commit()
                connection.close()
                result["webhook_disabled"] = cursor.rowcount == 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"webhook_cleanup:{type(exc).__name__}")
        for token in self.temporary_tokens:
            try:
                response = self.control.post(
                    "/auth/logout", headers={"Authorization": f"Bearer {token}"}
                )
                expect_json(response, 200, require_ids=True)
                result["temporary_sessions_logged_out"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"session_logout:{type(exc).__name__}")
        # 只禁用本轮登记的 Provider Key；缺少精确 ID 时宁可跳过，也不能影响租户其他密钥。
        if self.admin_token and self.acceptance_provider_key_id:
            try:
                self._control_json(
                    "PATCH",
                    f"/provider-keys/{self.acceptance_provider_key_id}",
                    expected_status=200,
                    headers=self._auth_headers(
                        request_id=f"req_prd045_key_cleanup_{self.suffix}"
                    ),
                    payload={"status": "disabled"},
                )
                result["acceptance_provider_key_disabled"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"provider_key:{type(exc).__name__}")
        for provider_key_id in self.legacy_provider_key_ids:
            try:
                self._control_json(
                    "PATCH",
                    f"/provider-keys/{provider_key_id}",
                    expected_status=200,
                    headers=self._auth_headers(
                        request_id=f"req_prd045_key_restore_{provider_key_id}"
                    ),
                    payload={"status": "active"},
                )
                result["legacy_provider_keys_restored"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"legacy_provider_key:{type(exc).__name__}")
        # 临时 Policy 必须同时具备 ID 和最新 CAS 版本，避免用过期 If-Match 改写其他状态。
        if self.admin_token and self.acceptance_policy_id and self.acceptance_policy_version:
            try:
                headers = self._auth_headers(
                    request_id=f"req_prd045_policy_cleanup_{self.suffix}"
                )
                headers["If-Match"] = f'"{self.acceptance_policy_version}"'
                self._control_json(
                    "PATCH",
                    f"/policies/{self.acceptance_policy_id}",
                    expected_status=200,
                    headers=headers,
                    payload={"status": "disabled"},
                )
                result["acceptance_policy_disabled"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"acceptance_policy:{type(exc).__name__}")
        # 只有确认临时 Policy 已禁用后才允许发布恢复版，防止把验收规则重新固化进 head。
        if self.admin_token and result["acceptance_policy_disabled"]:
            try:
                current, head_etag, candidate_etag = self._current_snapshot()
                if (
                    current.get("current_snapshot") is not None
                    and current.get("candidate_matches_current") is False
                ):
                    recovered = self._publish(
                        head_etag,
                        candidate_etag,
                        reason=f"PRD045 runtime cleanup {self.suffix}",
                        request_id=f"req_prd045_recovery_{self.suffix}",
                    )
                    result["recovery_snapshot_version"] = recovered["snapshot_version"]
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"recovery_snapshot:{type(exc).__name__}")
        for policy_id, version in self.mask_policies_to_restore:
            try:
                headers = self._auth_headers(
                    request_id=f"req_prd045_mask_restore_{policy_id}"
                )
                headers["If-Match"] = f'"{version}"'
                self._control_json(
                    "PATCH",
                    f"/policies/{policy_id}",
                    expected_status=200,
                    headers=headers,
                    payload={"status": "active"},
                )
                result["mask_policies_restored"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"mask_policy:{type(exc).__name__}")
        # 数据库兜底必须使用 tenant_id 与本轮 user_id 白名单，禁止按邮箱模糊匹配跨租户禁用。
        if self.tenant_id and self.temporary_user_ids:
            try:
                connection = sqlite3.connect(self.args.database)
                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                placeholders = ",".join("?" for _ in self.temporary_user_ids)
                cursor = connection.execute(
                    "UPDATE users SET status='disabled', updated_at=? "
                    f"WHERE tenant_id=? AND user_id IN ({placeholders})",
                    (now, self.tenant_id, *self.temporary_user_ids),
                )
                result["temporary_users_disabled"] = int(cursor.rowcount)
                connection.commit()
                connection.close()
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"database_cleanup:{type(exc).__name__}")
        return result


def required_environment(name: str) -> str:
    """读取必需环境变量；缺失时只报告变量名，不输出其他环境内容。"""

    value = str(os.getenv(name, "")).strip()
    if not value:
        # 缺失配置只报告变量名，不打印环境快照或其他可能包含凭据的变量值。
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def expect_json(
    response: httpx.Response,
    expected_status: int,
    *,
    expected_code: str | None = None,
    require_ids: bool,
) -> dict[str, Any]:
    """校验 JSON 响应，只用白名单字段构造失败消息，避免正文泄露。"""

    body = json_body_or_empty(response)
    if response.status_code != expected_status:
        safe = {field: body.get(field) for field in SAFE_ERROR_FIELDS}
        # HTTP 不符时只输出统一错误白名单，绝不拼接响应原文、Header 或模型内容。
        raise AssertionError(
            f"HTTP {response.status_code}, expected {expected_status}, safe={safe}"
        )
    if expected_code is not None and body.get("code") != expected_code:
        # 错误码不符只比较稳定 code；诊断正文和上游原始错误不进入失败输出。
        raise AssertionError(
            f"error code {body.get('code')!r}, expected {expected_code!r}"
        )
    if require_ids:
        assert body.get("request_id") and body.get("trace_id"), "response missing request/trace id"
    return body


def json_body_or_empty(response: httpx.Response) -> dict[str, Any]:
    """把对象 JSON 解析为 dict；空正文或数组响应统一返回空对象。"""

    try:
        body = response.json()
    # 非 JSON 响应统一视为空对象，调用方只会基于状态码和白名单字段构造错误。
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def current_version(current: dict[str, Any]) -> int:
    """从 current 响应读取 head 版本；未发布基线返回 0。"""

    snapshot = current.get("current_snapshot")
    return int(snapshot["snapshot_version"]) if isinstance(snapshot, dict) else 0


def assert_strong_etag(value: str, name: str) -> None:
    """拒绝空值、弱 ETag 或含换行值，防止运行验收意外降级无条件读取。"""

    assert value.startswith('"') and value.endswith('"'), f"{name} ETag is not strong"
    assert "\r" not in value and "\n" not in value and not value.startswith('W/"')


def normalize_key(value: object) -> str:
    """规范化字段名，统一下划线、连接符和大小写后执行敏感键比较。"""

    return str(value).lower().replace("-", "_")


def assert_no_forbidden_keys(value: object) -> None:
    """递归校验调用日志 DTO 不包含正文、DSL、Token 或密钥字段。"""

    if isinstance(value, dict):
        for key, child in value.items():
            assert normalize_key(key) not in FORBIDDEN_LOG_KEYS, f"forbidden log key: {key}"
            assert_no_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_forbidden_keys(child)


def quoted_identifier(value: str) -> str:
    """引用来自 SQLite schema 的标识符，避免动态敏感扫描把名字当 SQL。"""

    return '"' + value.replace('"', '""') + '"'


def scan_database_values(
    connection: sqlite3.Connection, values: dict[str, str]
) -> dict[str, int]:
    """扫描全部业务表文本表示，返回每类敏感值的命中行数。"""

    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    results = {label: 0 for label in values}
    for table in tables:
        columns = [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({quoted_identifier(table)})")
        ]
        if not columns:
            continue
        predicates = " OR ".join(
            f"instr(CAST({quoted_identifier(column)} AS TEXT), ?) > 0"
            for column in columns
        )
        for label, sensitive in values.items():
            if not sensitive:
                continue
            parameters = tuple(sensitive for _ in columns)
            count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_identifier(table)} WHERE {predicates}",
                parameters,
            ).fetchone()[0]
            results[label] += int(count)
    return results


def scan_selected_tables(
    connection: sqlite3.Connection,
    values: dict[str, str],
    tables: tuple[str, ...],
) -> dict[str, int]:
    """只扫描日志、审计和 Webhook 表，证明 DSL/Prompt 未越过允许存储边界。"""

    results = {label: 0 for label in values}
    for table in tables:
        columns = [
            row[1]
            for row in connection.execute(f"PRAGMA table_info({quoted_identifier(table)})")
        ]
        predicates = " OR ".join(
            f"instr(CAST({quoted_identifier(column)} AS TEXT), ?) > 0"
            for column in columns
        )
        for label, sensitive in values.items():
            parameters = tuple(sensitive for _ in columns)
            count = connection.execute(
                f"SELECT COUNT(*) FROM {quoted_identifier(table)} WHERE {predicates}",
                parameters,
            ).fetchone()[0]
            results[label] += int(count)
    return results


def public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """提取不含原因、操作者和完整 hash 的版本关系摘要。"""

    return {
        "version": snapshot["snapshot_version"],
        "operation": snapshot["operation"],
        "policy_count": snapshot["policy_count"],
        "rollback_source_version": snapshot.get("rollback_source_snapshot_version"),
    }


def public_gateway_result(result: dict[str, Any]) -> dict[str, Any]:
    """去掉 request_id，只输出 Gateway HTTP、快照状态、决策和安全错误码。"""

    return {
        "status": result["status"],
        "snapshot_version": result["snapshot_version"],
        "snapshot_status": result["snapshot_status"],
        "decision": result["decision"],
        "error_code": result["error_code"],
    }


def parse_args() -> argparse.Namespace:
    """解析非敏感服务地址和运行库路径；凭据禁止通过命令行参数传入。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--control-url", default="http://127.0.0.1:8001")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    parser.add_argument("--console-url", default="http://127.0.0.1:5173/")
    parser.add_argument("--database", type=Path, default=Path(".data/metahub-dev-prd031.db"))
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument("--provider-code", default="local_openai")
    parser.add_argument("--gateway-wait-seconds", type=float, default=30)
    parser.add_argument("--confirm-local-state-mutation", action="store_true")
    return parser.parse_args()


def main() -> None:
    """执行验收并把可公开证据打印为 JSON；任一断言失败均返回非零退出码。"""

    if sys.flags.optimize:
        # 优化模式会移除 assert，必须在任何服务写操作前拒绝运行，防止产生虚假通过证据。
        raise RuntimeError("PRD-045 acceptance refuses optimized Python; assertions are required")
    args = parse_args()
    acceptance = RuntimeAcceptance(args)
    result: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {}
    try:
        with RunningWebhookReceiver() as receiver:
            result = acceptance.run(receiver)
    finally:
        cleanup = acceptance.cleanup_resources()
        acceptance.close()
    assert result is not None
    assert not cleanup.get("errors"), "acceptance cleanup did not complete"
    result["cleanup"] = cleanup
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
