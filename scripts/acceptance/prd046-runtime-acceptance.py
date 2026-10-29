"""PRD-046 策略单请求预演的真实三服务运行验收。

脚本只通过公开 Control API 创建临时用户和 Policy，不直接伪造 Dry-Run 结果。
数据库连接只用于验收前后取证，以及在公开 API 尚无用户禁用入口时精确清理本轮
临时用户。管理员密码只从环境变量读取；Prompt、密码和 Token 均不会写入输出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
from metahub_shared.db.security import hash_token
from metahub_shared.runtime.webhooks import verify_webhook_signature

# HTTP 失败诊断只允许输出这三个稳定字段，避免正文、认证材料或内部异常被打印。
SAFE_ERROR_FIELDS = ("code", "request_id", "trace_id")

# 下列正则只匹配“凭据值或真实运行故障”，不把配置键名、统一错误码中的 error 字样、
# 以及预期的 401/403/409/422 访问日志误报为泄漏或服务故障。
LOG_AUTHORIZATION_BEARER_PATTERN = re.compile(
    rb"(?i)\bauthorization\b[^\r\n]{0,32}\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
)
LOG_PASSWORD_VALUE_PATTERN = re.compile(
    rb"(?i)(?:\b(?:password|passwd)\b[\"']?\s*[:=]\s*[\"']?"
    rb"(?!redacted\b|<redacted>|\*{3,}|null\b|none\b)[^\s,\"'}]{8,}|"
    rb"Prd046![A-Za-z0-9_-]{8,})"
)
LOG_TOKEN_VALUE_PATTERN = re.compile(
    rb"(?i)\b(?:access_token|auth_token|internal_token)\b[\"']?\s*[:=]\s*[\"']?"
    rb"(?!redacted\b|<redacted>|\*{3,}|null\b|none\b)[A-Za-z0-9._~+/=-]{8,}"
)
LOG_PROVIDER_VIRTUAL_KEY_PATTERN = re.compile(
    rb"(?i)(?:\bmhv_[A-Za-z0-9_-]{8,}|\bsk[-_][A-Za-z0-9._-]{8,})"
)
LOG_PRD046_PROMPT_PATTERN = re.compile(rb"PRD046_PROMPT_SENTINEL_[A-Za-z0-9_-]{6,}")
LOG_GOVERNANCE_BODY_PATTERN = re.compile(
    rb"(?i)\b(?:prompt|messages|condition_expr|condition_ast(?:_json)?|canonical_expression)"
    rb"\b[\"']?\s*[:=]\s*[\"']?(?!redacted\b|<redacted>|omitted\b|null\b|none\b)"
    rb"[^\s,\"'}]{4,}"
)
LOG_TRACEBACK_PATTERN = re.compile(rb"(?i)traceback \(most recent call last\):")
LOG_ERROR_LEVEL_PATTERN = re.compile(
    rb"(?im)(?:^ERROR(?:\s|:)|\bERROR:\s|"
    rb"\b(?:level|severity)\s*[=:]\s*[\"']?ERROR\b)"
)
LOG_HTTP_5XX_PATTERN = re.compile(
    rb"(?i)(?:HTTP/\d(?:\.\d)?[\"\s]+5\d\d\b|"
    rb"\b(?:status|status_code|http_status)\s*[=:]\s*[\"']?5\d\d\b|"
    rb"\b5\d\d\s+(?:Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout)\b)"
)

# 这些表代表真实 Gateway 调用、路由、用量、DLP 与预算事实。部分历史库可能没有
# 独立事件表；不存在的表会作为 not_present 证据保留，而不是被误报为零行表。
ZERO_COST_TABLES = (
    "gateway_request_claims",
    "virtual_keys",
    "request_logs",
    "usage_ledger",
    "price_snapshots",
    "routing_decisions",
    "routing_attempts",
    "dlp_matches",
    "budget_evaluations",
    "budget_reservations",
    "budget_blocks",
    "budget_block_events",
    "budget_alert_events",
)

DRY_FACT_TABLES = (
    "policy_explanations",
    "policy_events",
    "audit_logs",
)

DRY_RUN_EVENT_FIELDS = frozenset(
    {
        "policy_explanation_id",
        "request_id",
        "policy_set_fingerprint",
        "decision",
        "decision_reason_code",
        "matched_policy_id",
        "route_status",
        "dlp_status",
        "budget_status",
        "would_error_code",
        "checked_policy_count",
        "runtime_compatible",
        "coverage_complete",
    }
)
AUDIT_EVENT_FIELDS = frozenset(
    {"audit_log_id", "action", "object_type", "object_id", "changed_fields"}
)

# 所有管理写请求保持有限 20 秒超时；超时后不得立即单次回查，而要经过 Session
# 认证围栏、完整 settle 窗口和最终数据库收敛，避免迟到提交落在 cleanup 之后。
MUTATION_HTTP_TIMEOUT = httpx.Timeout(20.0)
UNCERTAIN_MUTATION_SETTLE_SECONDS = 20.0


class AcceptanceWebhookServer(ThreadingHTTPServer):
    """保存回环验收事件的有界安全摘要，不保留原始正文或签名材料。"""

    daemon_threads = True

    def __init__(self) -> None:
        """绑定随机回环端口，并按 URL path 隔离成功与失败 Endpoint 的 Secret。"""

        super().__init__(("127.0.0.1", 0), AcceptanceWebhookHandler)
        self.signing_secrets: dict[str, str] = {}
        self.received: list[dict[str, Any]] = []
        self._provider_calls = 0
        self._lock = threading.Lock()

    def record(self, item: dict[str, Any]) -> None:
        """线程安全保存白名单事件摘要，供主验收线程等待并核对。"""

        with self._lock:
            self.received.append(item)

    def snapshot(self) -> list[dict[str, Any]]:
        """返回接收摘要副本，避免验收线程遍历时与 HTTP 线程竞争。"""

        with self._lock:
            return [dict(item) for item in self.received]

    def record_provider_call(self) -> None:
        """只累计临时 Provider 的入站次数，不读取或保存可能包含 Prompt 的请求正文。"""

        with self._lock:
            self._provider_calls += 1

    def provider_call_count(self) -> int:
        """线程安全返回临时 Provider 的真实网络调用次数，作为零调用独立证据。"""

        with self._lock:
            return self._provider_calls


class AcceptanceWebhookHandler(BaseHTTPRequestHandler):
    """校验真实 MetaHub HMAC，并用 204/409 构造成功与失败投递。"""

    def do_POST(self) -> None:  # noqa: N802
        """有界读取 JSON，只保存事件 ID、类型、payload 键和验签布尔值。"""

        server = self.server
        assert isinstance(server, AcceptanceWebhookServer)
        # 临时上游路径一旦被访问就只累计次数并立即失败，绝不读取或保存模型请求正文。
        if self.path.startswith("/provider"):
            server.record_provider_call()
            self.send_response(503)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 1 or length > 1_048_576:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length)
        try:
            envelope = json.loads(body.decode("utf-8"))
        # 非 UTF-8 或非 JSON 正文不参与验签与留档，统一以 400 拒绝以免解析异常泄露正文。
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        safe_payload = payload if isinstance(payload, dict) else {}
        secret = server.signing_secrets.get(self.path, "")
        signature = self.headers.get("X-MetaHub-Signature", "")
        server.record(
            {
                "path": self.path,
                "event_id": str(envelope.get("event_id", "")),
                "event_type": str(envelope.get("event_type", "")),
                "object_id": str(envelope.get("object_id", "")),
                "request_id": safe_payload.get("request_id"),
                "action": safe_payload.get("action"),
                "object_type": safe_payload.get("object_type"),
                "payload_keys": sorted(safe_payload),
                "signature_valid": bool(secret)
                and verify_webhook_signature(signature, secret, body),
            }
        )
        # 409 是确定性的非 2xx 失败，不会让最终服务日志隐私扫描误报真实 5xx。
        self.send_response(409 if self.path == "/fail" else 204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """记录任何针对临时 Provider 的 GET 探测，并返回固定不可用状态。"""

        server = self.server
        assert isinstance(server, AcceptanceWebhookServer)
        # 任何上游健康探测也属于真实网络访问，必须纳入同一个零调用计数器。
        if self.path.startswith("/provider"):
            server.record_provider_call()
        self.send_response(503 if self.path.startswith("/provider") else 404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """关闭标准库访问日志，避免 URL 或协议细节进入验收输出。"""

        return


class RunningWebhookReceiver:
    """以 context manager 管理回环接收线程，异常路径也能释放随机端口。"""

    def __init__(self) -> None:
        """构造尚未启动的回环 Server 和守护线程，供上下文管理器统一释放。"""

        self.server = AcceptanceWebhookServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> AcceptanceWebhookServer:
        """启动接收器并返回可登记一次性 Secret 的服务对象。"""

        self.thread.start()
        return self.server

    def __exit__(self, *_exc_info: object) -> None:
        """停止循环、关闭监听并等待线程结束，不残留验收端口。"""

        try:
            self.server.shutdown()
        finally:
            try:
                self.server.server_close()
            finally:
                self.thread.join(timeout=5)
                # 线程未退出意味着端口或请求仍可能存活，必须让整轮验收失败而非静默泄漏。
                if self.thread.is_alive():
                    raise RuntimeError("acceptance receiver did not stop")


class RuntimeAcceptance:
    """编排 PRD-046 真实 API、RBAC、零成本、隐私和清理验收。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """读取非敏感 CLI 配置，并把凭据限制在当前进程内存。"""

        self.args = args
        self.admin_password = required_environment("METAHUB_ADMIN_PASSWORD")
        self.suffix = secrets.token_hex(5)
        # Prompt sentinel 不进入任何公开结果；随机后缀只用于避免误命中历史数据。
        self.prompt_sentinel = f"PRD046_PROMPT_SENTINEL_{self.suffix}"
        self.prompt_values: set[str] = {self.prompt_sentinel}
        self.data_class = f"prd046_acceptance_{self.suffix}"
        # 创建 intent 在任何 API 请求前固定；cleanup 以 tenant + 唯一业务键回查，
        # 即使服务端提交后响应丢失，也不依赖仅存在于响应中的持久 ID。
        self.user_email_by_role = {
            role: f"prd046-runtime-{role}-{self.suffix}@metahub.local"
            for role in ("developer", "viewer")
        }
        self.project_intent = {
            "project_code": f"prd046-{self.suffix}",
            "name": f"PRD046 runtime {self.suffix}",
        }
        self.provider_intent = {
            "provider_code": f"prd046_{self.suffix}",
            "name": f"PRD046 runtime {self.suffix}",
            "base_url": "",
        }
        self.model_intent = {
            "internal_model_id": f"prd046-runtime-{self.suffix}",
            "display_name": f"PRD046 runtime {self.suffix}",
        }
        self.alias_intent = {
            "alias": f"prd046-{self.suffix}",
            "description": f"PRD-046 runtime acceptance {self.suffix}",
        }
        self.dlp_intent = {
            "name": f"PRD046 runtime {self.suffix}",
            "marker": f"prd046-dlp-marker-{self.suffix}",
        }
        self.policy_intent = {
            "name": f"PRD046 runtime acceptance {self.suffix}",
            "description": f"PRD-046 runtime acceptance policy {self.suffix}",
        }
        self.webhook_intents: dict[str, dict[str, str]] = {}
        self.budget_intent: dict[str, str] | None = None
        self.admin_login_intent = {
            "user_agent": f"MetaHub-PRD046-Acceptance/{self.suffix}",
            "tenant_id": "",
            "user_id": "",
        }
        self.admin_login_baseline_session_ids: set[str] = set()
        self.control = httpx.Client(
            base_url=args.control_url,
            timeout=20,
            trust_env=False,
            follow_redirects=False,
        )
        self.admin_token = ""
        self.admin_session_id = ""
        self.admin_user_id = ""
        self.tenant_id = ""
        self.scope_validated = False
        self.database_runtime_bound = False
        self.uncertain_mutation = False
        self.uncertain_mutation_since: float | None = None
        self.public_response_bodies: list[str] = []
        self.temporary_tokens: list[str] = []
        self.temporary_passwords: list[str] = []
        self.temporary_user_ids: list[str] = []
        self.temporary_user_id_by_role: dict[str, str] = {}
        self.disabled_policies: list[tuple[str, int]] = []
        self.disabled_dlp_rule_ids: list[str] = []
        self.disabled_budget_rules: list[dict[str, str]] = []
        self.acceptance_policy_id: str | None = None
        self.acceptance_policy_version: int | None = None
        self.created_project_id: str | None = None
        self.acceptance_dlp_rule_id: str | None = None
        self.acceptance_budget_rule: dict[str, str] | None = None
        self.acceptance_provider_id: str | None = None
        self.acceptance_model_id: str | None = None
        self.acceptance_model_alias_id: str | None = None
        self.webhook_endpoint_ids: list[str] = []
        self.webhook_endpoint_by_path: dict[str, str] = {}

    def close(self) -> None:
        """关闭 HTTP 连接池，防止验收结束后继续持有认证 Header。"""

        self.control.close()

    def run(self, receiver: AcceptanceWebhookServer) -> dict[str, Any]:
        """执行完整验收并返回不含凭据、Prompt 或完整业务正文的 JSON 摘要。"""

        # 任何网络访问和数据库写入前先收紧到工作区回环环境，避免参数误指共享服务。
        self._assert_local_scope()
        health = self._verify_health()
        migration = self._verify_database_contract()
        self._prepare_admin_login_intent()
        (
            self.admin_token,
            self.tenant_id,
            self.admin_user_id,
            self.admin_session_id,
        ) = self._login_admin()
        self._verify_database_runtime_binding()
        context = self._locate_context()
        rbac_tokens = self._create_role_users()
        isolated_count = self._isolate_active_candidate()
        isolated_dlp_count = self._isolate_active_dlp_rules()
        isolated_budget_count = self._isolate_active_budget_rules(context)
        context = self._create_acceptance_route(context, receiver)
        self._assert_no_provider_calls(receiver)
        policy = self._create_policy("allow")
        self._create_webhook(
            receiver,
            path="/ok",
            event_types=["policy.dry_run_executed", "audit.recorded"],
        )

        baseline = self._snapshot_database()
        allow_request_id = f"req_prd046_allow_{self.suffix}"
        allow = self._dry_run(
            context,
            request_id=allow_request_id,
            expected_decision="allow",
            expected_outcome="would_allow",
            expected_route="selected",
            expected_dlp="clean",
            expected_budget="no_rules",
            expected_error=None,
            expected_model_id=self.acceptance_model_id,
            expected_provider_id=self.acceptance_provider_id,
            expected_dlp_rule_id=None,
            expected_budget_rule_id=None,
        )
        self._assert_no_provider_calls(receiver)
        after_allow = self._snapshot_database()
        allow_delta = assert_phase_delta(baseline, after_allow, dry_fact_delta=1)
        allow_consistency = self._verify_three_facts(allow_request_id, allow)
        allow_webhooks = self._verify_signed_webhooks(
            receiver,
            allow_request_id,
            allow,
            require_failed_delivery=False,
        )

        invalid = self._verify_invalid_payload(context)
        rbac = self._verify_rbac(context, rbac_tokens)
        reused = self._verify_request_id_recovery(context, allow_request_id)
        reused["stable_webhook_recovery"] = self._verify_webhook_recovery_idempotency(
            receiver, allow_request_id
        )
        self._assert_no_provider_calls(receiver)
        after_negative = self._snapshot_database()
        negative_delta = assert_phase_delta(after_allow, after_negative, dry_fact_delta=0)

        # 失败 Endpoint 只订阅 Dry-Run；其 409 会形成三次真实失败 attempt，但不能改变 API 200。
        self._create_webhook(
            receiver,
            path="/fail",
            event_types=["policy.dry_run_executed"],
        )
        webhook_failure_baseline = self._snapshot_database()
        webhook_failure_request_id = f"req_prd046_webhook_failure_{self.suffix}"
        webhook_failure = self._dry_run(
            context,
            request_id=webhook_failure_request_id,
            expected_decision="allow",
            expected_outcome="would_allow",
            expected_route="selected",
            expected_dlp="clean",
            expected_budget="no_rules",
            expected_error=None,
            expected_model_id=self.acceptance_model_id,
            expected_provider_id=self.acceptance_provider_id,
            expected_dlp_rule_id=None,
            expected_budget_rule_id=None,
        )
        self._assert_no_provider_calls(receiver)
        after_webhook_failure = self._snapshot_database()
        webhook_failure_delta = assert_phase_delta(
            webhook_failure_baseline, after_webhook_failure, dry_fact_delta=1
        )
        webhook_failure_consistency = self._verify_three_facts(
            webhook_failure_request_id, webhook_failure
        )
        failed_webhooks = self._verify_signed_webhooks(
            receiver,
            webhook_failure_request_id,
            webhook_failure,
            require_failed_delivery=True,
        )

        self._create_dlp_block_rule()
        dlp_baseline = self._snapshot_database()
        dlp_request_id = f"req_prd046_dlp_block_{self.suffix}"
        dlp_block = self._dry_run(
            context,
            request_id=dlp_request_id,
            expected_decision="allow",
            expected_outcome="would_block",
            expected_route="selected",
            expected_dlp="blocked",
            expected_budget="not_evaluated",
            expected_error="gateway.dlp_blocked",
            expected_model_id=self.acceptance_model_id,
            expected_provider_id=self.acceptance_provider_id,
            expected_dlp_rule_id=self.acceptance_dlp_rule_id,
            expected_budget_rule_id=None,
            prompt=f"{self.prompt_sentinel} {self._dlp_keyword()}",
        )
        self._assert_no_provider_calls(receiver)
        after_dlp = self._snapshot_database()
        dlp_delta = assert_phase_delta(dlp_baseline, after_dlp, dry_fact_delta=1)
        dlp_consistency = self._verify_three_facts(dlp_request_id, dlp_block)
        self._disable_acceptance_dlp_rule()

        self._create_budget_block_rule(context)
        budget_baseline = self._snapshot_database()
        budget_request_id = f"req_prd046_budget_block_{self.suffix}"
        budget_block = self._dry_run(
            context,
            request_id=budget_request_id,
            expected_decision="allow",
            expected_outcome="would_block",
            expected_route="selected",
            expected_dlp="clean",
            expected_budget="exhausted",
            expected_error="gateway.budget_exhausted",
            expected_model_id=self.acceptance_model_id,
            expected_provider_id=self.acceptance_provider_id,
            expected_dlp_rule_id=None,
            expected_budget_rule_id=str(
                (self.acceptance_budget_rule or {}).get("budget_rule_id", "")
            ),
        )
        self._assert_no_provider_calls(receiver)
        after_budget = self._snapshot_database()
        budget_delta = assert_phase_delta(budget_baseline, after_budget, dry_fact_delta=1)
        budget_consistency = self._verify_three_facts(budget_request_id, budget_block)
        self._disable_acceptance_budget_rule()

        self._disable_acceptance_model()
        route_baseline = self._snapshot_database()
        route_request_id = f"req_prd046_route_unavailable_{self.suffix}"
        route_unavailable = self._dry_run(
            context,
            request_id=route_request_id,
            expected_decision="allow",
            expected_outcome="would_error",
            expected_route="unavailable",
            expected_dlp="not_evaluated",
            expected_budget="not_evaluated",
            expected_error="gateway.model_disabled",
            expected_model_id=self.acceptance_model_id,
            expected_provider_id=self.acceptance_provider_id,
            expected_dlp_rule_id=None,
            expected_budget_rule_id=None,
        )
        self._assert_no_provider_calls(receiver)
        after_route = self._snapshot_database()
        route_delta = assert_phase_delta(route_baseline, after_route, dry_fact_delta=1)
        route_consistency = self._verify_three_facts(route_request_id, route_unavailable)

        policy = self._patch_policy_action(policy, "deny")
        deny_baseline = self._snapshot_database()
        deny_request_id = f"req_prd046_deny_{self.suffix}"
        deny = self._dry_run(
            context,
            request_id=deny_request_id,
            expected_decision="deny",
            expected_outcome="would_deny",
            expected_route="not_evaluated",
            expected_dlp="not_evaluated",
            expected_budget="not_evaluated",
            expected_error="gateway.policy_denied",
            expected_model_id=None,
            expected_provider_id=None,
            expected_dlp_rule_id=None,
            expected_budget_rule_id=None,
        )
        self._assert_no_provider_calls(receiver)
        after_deny = self._snapshot_database()
        deny_delta = assert_phase_delta(deny_baseline, after_deny, dry_fact_delta=1)
        deny_consistency = self._verify_three_facts(deny_request_id, deny)

        return {
            "status": "passed",
            "health": health,
            "migration": migration,
            "context": {
                "tenant_matches_authentication": True,
                "environment_active": True,
                "project_active": True,
                "model_resolvable": True,
                "temporary_project_created": bool(self.created_project_id),
                "database_bound_to_control_session": True,
            },
            "candidate_isolation": {
                "preexisting_active_policies_temporarily_disabled": isolated_count,
                "preexisting_active_dlp_rules_temporarily_disabled": isolated_dlp_count,
                "preexisting_active_budget_rules_temporarily_disabled": isolated_budget_count,
                "acceptance_policy_created": True,
                "acceptance_route_created": True,
            },
            "allow": public_dry_run(allow),
            "webhook_failure": public_dry_run(webhook_failure),
            "route_unavailable": public_dry_run(route_unavailable),
            "dlp_block": public_dry_run(dlp_block),
            "budget_block": public_dry_run(budget_block),
            "deny": public_dry_run(deny),
            "invalid_payload": invalid,
            "rbac": rbac,
            "request_id_recovery": reused,
            "database_phases": {
                "allow": allow_delta,
                "negative_and_recovery": negative_delta,
                "webhook_failure": webhook_failure_delta,
                "route_unavailable": route_delta,
                "dlp_block": dlp_delta,
                "budget_block": budget_delta,
                "deny": deny_delta,
            },
            "three_fact_consistency": {
                "allow": allow_consistency,
                "webhook_failure": webhook_failure_consistency,
                "route_unavailable": route_consistency,
                "dlp_block": dlp_consistency,
                "budget_block": budget_consistency,
                "deny": deny_consistency,
            },
            "webhooks": {
                "successful_delivery": allow_webhooks,
                "failed_delivery_does_not_change_http_200": failed_webhooks,
            },
            "provider_network_probe": {
                "call_count": receiver.provider_call_count(),
                "zero_calls": True,
            },
        }

    def _assert_no_provider_calls(self, receiver: AcceptanceWebhookServer) -> None:
        """断言临时 Provider 回环探针零入站，独立于 Dry-Run 响应中的自报布尔值。"""

        assert receiver.provider_call_count() == 0, (
            "Dry-Run unexpectedly called the temporary Provider receiver"
        )

    def _verify_health(self) -> dict[str, int]:
        """确认 Gateway、Control 和 Console 均由真实本地服务响应 HTTP 200。"""

        statuses: dict[str, int] = {}
        for name, url in (
            ("gateway", f"{self.args.gateway_url}/health"),
            ("control", f"{self.args.control_url}/health"),
            ("console", self.args.console_url),
        ):
            response = httpx.get(url, timeout=10, trust_env=False, follow_redirects=False)
            assert response.status_code == 200, f"{name} health HTTP {response.status_code}"
            statuses[name] = response.status_code
        return statuses

    def _assert_local_scope(self) -> None:
        """拒绝非回环地址或工作区外数据库，避免误改共享环境。"""

        assert self.args.confirm_local_state_mutation, (
            "pass --confirm-local-state-mutation for this disposable local acceptance run"
        )
        loopback = {"127.0.0.1", "::1", "localhost"}
        for name, value in (
            ("gateway", self.args.gateway_url),
            ("control", self.args.control_url),
            ("console", self.args.console_url),
        ):
            assert (urlsplit(value).hostname or "").lower() in loopback, (
                f"{name} acceptance URL must be loopback"
            )
        database = self.args.database.resolve()
        assert database.exists() and database.is_file(), "acceptance database must exist"
        artifact_root = (Path.cwd().resolve() / "artifacts" / "prd046").resolve()
        assert database.is_relative_to(artifact_root), (
            "acceptance database must be inside artifacts/prd046"
        )
        role_paths = self._role_log_files()
        resolved_by_role: dict[str, set[Path]] = {}
        for role, paths in role_paths.items():
            # 三个服务必须分别提供至少一份非空日志，不能用一个空文件代表“零泄漏”。
            assert paths, f"provide at least one --{role}-log-file"
            assert all(path.exists() and path.is_file() for path in paths), (
                f"every {role} log file must exist"
            )
            assert any(path.stat().st_size > 0 for path in paths), (
                f"at least one {role} log file must be non-empty"
            )
            resolved_by_role[role] = {path.resolve() for path in paths}
        # 同一文件不能冒充多个服务的独立日志证据；额外 --log-file 只用于补充扫描。
        assert not (
            resolved_by_role["gateway"] & resolved_by_role["control"]
            or resolved_by_role["gateway"] & resolved_by_role["console"]
            or resolved_by_role["control"] & resolved_by_role["console"]
        ), "gateway/control/console log files must be distinct"
        assert all(path.exists() and path.is_file() for path in self.args.log_file), (
            "every additional acceptance log file must exist"
        )
        self.scope_validated = True

    def _role_log_files(self) -> dict[str, tuple[Path, ...]]:
        """返回按服务角色分组的日志路径，供前置证据门禁和最终扫描共用。"""

        return {
            "gateway": tuple(self.args.gateway_log_file),
            "control": tuple(self.args.control_log_file),
            "console": tuple(self.args.console_log_file),
        }

    def _all_log_files(self) -> tuple[Path, ...]:
        """去重汇总三服务与额外日志，避免同一文件被重复扫描后夸大命中数。"""

        ordered = [
            *self.args.gateway_log_file,
            *self.args.control_log_file,
            *self.args.console_log_file,
            *self.args.log_file,
        ]
        return tuple(dict.fromkeys(path.resolve() for path in ordered))

    def _verify_database_contract(self) -> dict[str, Any]:
        """验证 0035 已应用且三类安全事实表存在，不修改数据库。"""

        connection = self._connect_database()
        try:
            tables = existing_tables(connection)
            assert set(DRY_FACT_TABLES).issubset(tables), "PRD-046 fact tables are missing"
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            version = int(row[0] or 0)
            assert version >= 35, "database migration 0035 has not been applied"
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            assert integrity == "ok" and foreign_keys == 0
            return {
                "highest_version": version,
                "integrity_check": integrity,
                "foreign_key_violations": foreign_keys,
            }
        finally:
            connection.close()

    def _login_admin(self) -> tuple[str, str, str, str]:
        """通过真实登录接口获取 admin 身份，不输出登录正文或 Token。"""

        try:
            response = self.control.post(
                "/auth/login",
                headers={
                    "User-Agent": self.admin_login_intent["user_agent"],
                    "X-Request-ID": f"req_prd046_admin_login_{self.suffix}",
                },
                json={
                    "tenant_slug": self.args.tenant_slug,
                    "email": self.args.admin_email,
                    "password": self.admin_password,
                },
                timeout=MUTATION_HTTP_TIMEOUT,
            )
        # 完整请求后的传输断开可能对应服务端已提交 Session，cleanup 必须等待后按 UA intent 撤销。
        except httpx.TransportError:
            self._mark_uncertain_mutation()
            raise
        body = expect_json(response, 200, require_ids=True)
        user = body.get("user") if isinstance(body.get("user"), dict) else {}
        token = str(body.get("access_token", ""))
        tenant_id = str(user.get("tenant_id", ""))
        user_id = str(user.get("user_id", ""))
        session_id = str(body.get("session_id", ""))
        assert token and tenant_id and user_id and session_id and user.get("role") == "admin"
        assert tenant_id == self.admin_login_intent["tenant_id"]
        assert user_id == self.admin_login_intent["user_id"]
        # Token 仅交给当前对象的内存调用链；租户和用户 ID 用于所有后续取证限定。
        return token, tenant_id, user_id, session_id

    def _prepare_admin_login_intent(self) -> None:
        """登录前解析本地管理员并登记唯一 User-Agent，供响应丢失后的 Session 清理。"""

        connection = self._connect_database()
        try:
            row = connection.execute(
                """
                SELECT t.tenant_id, u.user_id
                FROM tenants t
                JOIN users u ON u.tenant_id=t.tenant_id
                WHERE t.slug=? AND t.status='active' AND lower(u.email)=lower(?)
                  AND u.role='admin' AND u.status='active'
                """,
                (self.args.tenant_slug, self.args.admin_email),
            ).fetchall()
            assert len(row) == 1, "local acceptance admin intent is ambiguous"
            self.admin_login_intent["tenant_id"] = str(row[0][0])
            self.admin_login_intent["user_id"] = str(row[0][1])
            sessions = connection.execute(
                """
                SELECT session_id FROM sessions
                WHERE tenant_id=? AND user_id=? AND user_agent=?
                """,
                (
                    self.admin_login_intent["tenant_id"],
                    self.admin_login_intent["user_id"],
                    self.admin_login_intent["user_agent"],
                ),
            ).fetchall()
            self.admin_login_baseline_session_ids = {str(item[0]) for item in sessions}
            # 随机 UA 理论上不应存在；若命中则拒绝，避免把历史验收 Session 当成本轮资源。
            assert not self.admin_login_baseline_session_ids
        finally:
            connection.close()

    def _mark_uncertain_mutation(self) -> None:
        """记录最后一次写传输断开，cleanup 必须等待完整超时窗口后再首次回查。"""

        self.uncertain_mutation = True
        self.uncertain_mutation_since = time.monotonic()

    def _verify_database_runtime_binding(self) -> None:
        """用本次随机登录 Session 证明验收数据库就是 Control API 的当前运行库。

        登录产生的 session_id 与 Token 哈希均为本轮唯一值；只有两者在指定 SQLite 内、
        且 tenant/user/status 同时一致时，后续按持久 ID 取证和兜底清理才具有安全边界。
        """

        connection = self._connect_database()
        try:
            row = connection.execute(
                """
                SELECT tenant_id, user_id, token_hash, status
                FROM sessions WHERE session_id=?
                """,
                (self.admin_session_id,),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None, "acceptance database is not bound to the Control API"
        assert row[0] == self.tenant_id and row[1] == self.admin_user_id
        assert row[2] == hash_token(self.admin_token) and row[3] == "active"
        self.database_runtime_bound = True

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        """构造只存在于内存的管理员认证 Header。"""

        headers = {"Authorization": f"Bearer {self.admin_token}"}
        # 客户端 request_id 只用于 exactly-once 验收，调用方不能借此注入其他 Header。
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _json_request(
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
        """调用 JSON API，并用固定白名单字段报告 HTTP 失败。"""

        normalized_method = method.upper()
        is_mutation = normalized_method in {"POST", "PUT", "PATCH", "DELETE"}
        timeout = MUTATION_HTTP_TIMEOUT if is_mutation else self.control.timeout
        try:
            response = self.control.request(
                method,
                path,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        # 写请求传输断开后先保留所有 intent，cleanup 等待完整窗口后再处理迟到提交。
        except httpx.TransportError:
            if is_mutation:
                self._mark_uncertain_mutation()
            raise
        body = expect_json(
            response,
            expected_status,
            expected_code=expected_code,
            require_ids=True,
        )
        if record_body:
            self.public_response_bodies.append(response.text)
        return body, response

    def _locate_context(self) -> dict[str, str]:
        """定位 active Environment/Provider，并创建隔离预算所需的临时 Project。"""

        environments, _ = self._json_request(
            "GET", "/environments?status=active", expected_status=200, headers=self._headers()
        )
        active_environments = environments.get("environments", [])
        assert active_environments, "tenant has no active environment"
        environment_id = str(active_environments[0]["environment_id"])
        environment_code = str(active_environments[0]["code"])
        created, _ = self._json_request(
            "POST",
            "/projects",
            expected_status=201,
            headers=self._headers(f"req_prd046_project_{self.suffix}"),
            payload={
                "project_code": self.project_intent["project_code"],
                "name": self.project_intent["name"],
                "description": f"PRD-046 runtime acceptance {self.suffix}",
            },
        )
        project_id = str(created["project"]["project_id"])
        self.created_project_id = project_id

        return {
            "environment_id": environment_id,
            "environment": environment_code,
            "project_id": project_id,
            "model": "pending-acceptance-route",
        }

    def _create_role_users(self) -> dict[str, str]:
        """用公开用户 API 创建 developer/viewer，并返回仅供当前测试使用的 Token。"""

        tokens: dict[str, str] = {}
        for role in ("developer", "viewer"):
            password = f"Prd046!{secrets.token_urlsafe(20)}"
            self.temporary_passwords.append(password)
            email = self.user_email_by_role[role]
            created, _ = self._json_request(
                "POST",
                "/users",
                expected_status=201,
                headers=self._headers(f"req_prd046_user_{role}_{self.suffix}"),
                payload={
                    "email": email,
                    "display_name": f"PRD046 runtime {role}",
                    "password": password,
                    "role": role,
                },
            )
            user_id = str(created["user"]["user_id"])
            self.temporary_user_ids.append(user_id)
            self.temporary_user_id_by_role[role] = user_id
            try:
                login = self.control.post(
                    "/auth/login",
                    headers={
                        "User-Agent": f"MetaHub-PRD046-{role}/{self.suffix}",
                        "X-Request-ID": f"req_prd046_login_{role}_{self.suffix}",
                    },
                    json={
                        "tenant_slug": self.args.tenant_slug,
                        "email": email,
                        "password": password,
                    },
                    timeout=MUTATION_HTTP_TIMEOUT,
                )
            # 临时用户登录若传输断开，也必须先等待服务端收敛再按用户 intent 撤销全部 Session。
            except httpx.TransportError:
                self._mark_uncertain_mutation()
                raise
            login_body = expect_json(login, 200, require_ids=True)
            token = str(login_body.get("access_token", ""))
            assert token
            tokens[role] = token
            self.temporary_tokens.append(token)
        # 两个 Token 只交给 RBAC 攻击步骤，完成后由 finally 清理路径逐一注销。
        return tokens

    def _isolate_active_candidate(self) -> int:
        """临时禁用原 active Policy，确保 allow/deny 不受既有同优先级规则干扰。"""

        listed, _ = self._json_request(
            "GET", "/policies?status=active", expected_status=200, headers=self._headers()
        )
        for policy in listed.get("policies", []):
            policy_id = str(policy["policy_id"])
            original_version = int(policy["version"])
            headers = self._headers(f"req_prd046_isolate_{policy_id}")
            headers["If-Match"] = f'"{original_version}"'
            # 恢复账本必须先于请求登记；即使服务端已提交但响应在网络中丢失，finally
            # 仍能按 ID 回读当前版本并恢复原 active 状态。
            self.disabled_policies.append((policy_id, original_version))
            self._json_request(
                "PATCH",
                f"/policies/{policy_id}",
                expected_status=200,
                headers=headers,
                payload={"status": "disabled"},
            )
        return len(self.disabled_policies)

    def _isolate_active_dlp_rules(self) -> int:
        """临时禁用 active DLP，避免客户 catch-all 规则污染确定性 allow 基线。"""

        listed, _ = self._json_request(
            "GET", "/dlp/rules?status=active", expected_status=200, headers=self._headers()
        )
        for rule in listed.get("dlp_rules", []):
            rule_id = str(rule["dlp_rule_id"])
            # DLP 更新没有 CAS；请求前登记 ID 后，清理阶段可用幂等 active Patch 覆盖响应丢失。
            self.disabled_dlp_rule_ids.append(rule_id)
            self._json_request(
                "PATCH",
                f"/dlp/rules/{rule_id}",
                expected_status=200,
                headers=self._headers(f"req_prd046_isolate_dlp_{rule_id}"),
                payload={"status": "disabled"},
            )
        return len(self.disabled_dlp_rule_ids)

    def _isolate_active_budget_rules(self, context: dict[str, str]) -> int:
        """仅禁用所选 Environment 下会命中本轮 tenant/project 的 active 预算。"""

        listed, _ = self._json_request(
            "GET",
            f"/budgets?environment_id={context['environment_id']}&status=active",
            expected_status=200,
            headers=self._headers(),
        )
        for rule in listed.get("budget_rules", []):
            scope_type = str(rule["scope_type"])
            scope_id = str(rule["scope_id"])
            # 其他 scope 不会命中本轮请求，跳过可避免扰动客户 Virtual Key 或应用预算。
            if scope_type not in {"tenant", "project"}:
                continue
            if scope_type == "project" and scope_id != context["project_id"]:
                continue
            identity = {
                "budget_rule_id": str(rule["budget_rule_id"]),
                "scope_type": scope_type,
                "scope_id": scope_id,
                "environment_id": str(rule["environment_id"]),
            }
            # 预算以完整 scope 身份幂等 upsert；请求前落恢复账本可覆盖提交成功但响应丢失。
            self.disabled_budget_rules.append(identity)
            self._json_request(
                "PATCH",
                "/budgets",
                expected_status=200,
                headers=self._headers(
                    f"req_prd046_isolate_budget_{identity['budget_rule_id']}"
                ),
                payload={
                    "scope_type": scope_type,
                    "scope_id": scope_id,
                    "environment_id": identity["environment_id"],
                    "status": "disabled",
                },
            )
        return len(self.disabled_budget_rules)

    def _create_acceptance_route(
        self,
        context: dict[str, str],
        receiver: AcceptanceWebhookServer,
    ) -> dict[str, str]:
        """创建独立 Provider、CNY Chat Model 和 Alias，不依赖客户路由健康状态。"""

        host, port = receiver.server_address
        self.provider_intent["base_url"] = f"http://{host}:{port}/provider/v1"
        self.alias_intent["environment_id"] = context["environment_id"]
        internal_model_id = self.model_intent["internal_model_id"]
        provider_body, _ = self._json_request(
            "POST",
            "/providers",
            expected_status=201,
            headers=self._headers(f"req_prd046_provider_{self.suffix}"),
            payload={
                "provider_code": self.provider_intent["provider_code"],
                "name": self.provider_intent["name"],
                "provider_type": "openai_compatible",
                "base_url": self.provider_intent["base_url"],
                "auth_type": "none",
                "description": "PRD-046 runtime acceptance; never called",
                "status": "active",
            },
        )
        self.acceptance_provider_id = str(provider_body["provider"]["provider_id"])
        model_body, _ = self._json_request(
            "POST",
            "/models",
            expected_status=201,
            headers=self._headers(f"req_prd046_model_{self.suffix}"),
            payload={
                "internal_model_id": internal_model_id,
                "provider_id": self.acceptance_provider_id,
                "provider_model_id": internal_model_id,
                "display_name": self.model_intent["display_name"],
                "capabilities": {"chat": True, "stream": False, "embeddings": False},
                "context_window": 4096,
                "input_price_per_1k": 1,
                "output_price_per_1k": 1,
                "currency": "CNY",
                "description": "PRD-046 runtime acceptance",
                "status": "active",
            },
        )
        self.acceptance_model_id = str(model_body["model"]["model_id"])
        alias = self.alias_intent["alias"]
        alias_body, _ = self._json_request(
            "POST",
            "/model-aliases",
            expected_status=201,
            headers=self._headers(f"req_prd046_alias_{self.suffix}"),
            payload={
                "alias": alias,
                "environment_id": context["environment_id"],
                "default_model_id": self.acceptance_model_id,
                "description": self.alias_intent["description"],
                "status": "active",
            },
        )
        self.acceptance_model_alias_id = str(
            alias_body["model_alias"]["model_alias_id"]
        )
        return {**context, "model": alias}

    def _create_webhook(
        self,
        receiver: AcceptanceWebhookServer,
        *,
        path: str,
        event_types: list[str],
    ) -> str:
        """创建临时回环 Endpoint，并只把一次性 Secret 登记到接收器内存。"""

        host, port = receiver.server_address
        webhook_name = f"PRD046 runtime {path[1:]} {self.suffix}"
        endpoint_url = f"http://{host}:{port}{path}"
        # name + path 在请求前组成唯一 intent，响应丢失时 cleanup 仍能精确回查 Endpoint。
        self.webhook_intents[path] = {
            "name": webhook_name,
            "endpoint_url": endpoint_url,
        }
        body, _ = self._json_request(
            "POST",
            "/webhooks",
            expected_status=200,
            headers=self._headers(f"req_prd046_webhook_{path[1:]}_{self.suffix}"),
            payload={
                "name": webhook_name,
                "endpoint_url": endpoint_url,
                "event_types": event_types,
                "status": "active",
                "timeout_ms": 2000,
            },
            record_body=False,
        )
        endpoint_id = str(body.get("webhook", {}).get("webhook_endpoint_id", ""))
        secret = str(body.get("signing_secret", ""))
        assert endpoint_id and secret
        self.webhook_endpoint_ids.append(endpoint_id)
        self.webhook_endpoint_by_path[path] = endpoint_id
        receiver.signing_secrets[path] = secret
        # Secret 也加入内存敏感集合，最终响应/数据库/服务日志扫描必须证明零明文。
        self.temporary_passwords.append(secret)
        return endpoint_id

    def _dlp_keyword(self) -> str:
        """返回仅作为预置规则的随机关键词；它不是完整 Prompt 或其 hash。"""

        return self.dlp_intent["marker"]

    def _create_dlp_block_rule(self) -> None:
        """创建只命中本轮专用关键词的 active block 规则。"""

        body, _ = self._json_request(
            "POST",
            "/dlp/rules",
            expected_status=200,
            headers=self._headers(f"req_prd046_dlp_create_{self.suffix}"),
            payload={
                "name": self.dlp_intent["name"],
                "rule_type": "keyword",
                "pattern": self._dlp_keyword(),
                "action": "block",
                "status": "active",
                "priority": 1,
                "description": f"PRD-046 runtime acceptance {self.suffix}",
            },
        )
        self.acceptance_dlp_rule_id = str(body["dlp_rule"]["dlp_rule_id"])

    def _disable_acceptance_dlp_rule(self) -> None:
        """通过公开 API 禁用本轮 DLP；失败时保留 ID 交给 finally 再尝试。"""

        if not self.acceptance_dlp_rule_id:
            return
        self._json_request(
            "PATCH",
            f"/dlp/rules/{self.acceptance_dlp_rule_id}",
            expected_status=200,
            headers=self._headers(f"req_prd046_dlp_disable_{self.suffix}"),
            payload={"status": "disabled"},
            record_body=False,
        )

    def _create_budget_block_rule(self, context: dict[str, str]) -> None:
        """为临时 Project 创建一微元硬预算，确保 32 输出 Token 的估算必定耗尽。"""

        self.budget_intent = {
            "scope_type": "project",
            "scope_id": context["project_id"],
            "environment_id": context["environment_id"],
            "name": f"PRD046 runtime {self.suffix}",
        }
        body, _ = self._json_request(
            "PATCH",
            "/budgets",
            expected_status=200,
            headers=self._headers(f"req_prd046_budget_create_{self.suffix}"),
            payload={
                **self.budget_intent,
                "daily_budget_cny": "0.000001",
                "warning_threshold_percent": 80,
                "exhausted_action": "block",
                "status": "active",
            },
        )
        rule = body["budget_rule"]
        self.acceptance_budget_rule = {
            "budget_rule_id": str(rule["budget_rule_id"]),
            "scope_type": str(rule["scope_type"]),
            "scope_id": str(rule["scope_id"]),
            "environment_id": str(rule["environment_id"]),
        }

    def _disable_acceptance_budget_rule(self) -> None:
        """按完整 scope 身份禁用临时预算，避免模糊更新其他规则。"""

        if self.acceptance_budget_rule is None:
            return
        rule = self.acceptance_budget_rule
        self._json_request(
            "PATCH",
            "/budgets",
            expected_status=200,
            headers=self._headers(f"req_prd046_budget_disable_{self.suffix}"),
            payload={
                "scope_type": rule["scope_type"],
                "scope_id": rule["scope_id"],
                "environment_id": rule["environment_id"],
                "status": "disabled",
            },
            record_body=False,
        )

    def _disable_acceptance_model(self) -> None:
        """禁用临时 Alias 的唯一 Model，以构造已执行但无候选的路由阶段。"""

        assert self.acceptance_model_id
        self._json_request(
            "PATCH",
            f"/models/{self.acceptance_model_id}",
            expected_status=200,
            headers=self._headers(f"req_prd046_model_disable_{self.suffix}"),
            payload={"status": "disabled"},
            record_body=False,
        )

    def _create_policy(self, action: str) -> dict[str, Any]:
        """创建仅匹配本轮 data_class 的最高优先级临时 Policy。"""

        created, _ = self._json_request(
            "POST",
            "/policies",
            expected_status=201,
            headers=self._headers(f"req_prd046_policy_{self.suffix}"),
            payload={
                "name": self.policy_intent["name"],
                "description": self.policy_intent["description"],
                "status": "active",
                "priority": 1,
                "rule": {"condition_expr": f'data_class == "{self.data_class}"'},
                "action": {"type": action},
            },
        )
        policy = created["policy"]
        assert policy["action"]["type"] == action and policy["status"] == "active"
        self.acceptance_policy_id = str(policy["policy_id"])
        self.acceptance_policy_version = int(policy["version"])
        return policy

    def _patch_policy_action(
        self, policy: dict[str, Any], action: str
    ) -> dict[str, Any]:
        """使用强 CAS 把临时 Policy 从 allow 切换为 deny，防止并发静默覆盖。"""

        headers = self._headers(f"req_prd046_policy_deny_{self.suffix}")
        headers["If-Match"] = f'"{int(policy["version"])}"'
        updated, _ = self._json_request(
            "PATCH",
            f"/policies/{policy['policy_id']}",
            expected_status=200,
            headers=headers,
            payload={"action": {"type": action}},
        )
        result = updated["policy"]
        assert result["action"]["type"] == action
        self.acceptance_policy_version = int(result["version"])
        return result

    def _payload(
        self, context: dict[str, str], *, prompt: str | None = None
    ) -> dict[str, Any]:
        """构造严格 DTO 允许的完整请求；Prompt 只驻留本次调用栈。"""

        actual_prompt = prompt if prompt is not None else self.prompt_sentinel
        self.prompt_values.add(actual_prompt)
        return {
            "prompt": actual_prompt,
            "model": context["model"],
            "project_id": context["project_id"],
            "environment_id": context["environment_id"],
            "data_class": self.data_class,
            "max_output_tokens": 32,
        }

    def _dry_run(
        self,
        context: dict[str, str],
        *,
        request_id: str,
        expected_decision: str,
        expected_outcome: str,
        expected_route: str,
        expected_dlp: str,
        expected_budget: str,
        expected_error: str | None,
        expected_model_id: str | None,
        expected_provider_id: str | None,
        expected_dlp_rule_id: str | None,
        expected_budget_rule_id: str | None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """调用真实 Dry-Run，并验证 HTTP 200、精确短路状态和零调用标志。"""

        body, response = self._json_request(
            "POST",
            "/policies/dry-run",
            expected_status=200,
            headers=self._headers(request_id),
            payload=self._payload(context, prompt=prompt),
        )
        explanation = body.get("policy_explanation", {})
        assert body.get("request_id") == request_id
        assert response.headers.get("X-Request-ID") == request_id
        assert response.headers.get("X-Trace-ID") == body.get("trace_id")
        assert response.headers.get("Cache-Control") == "no-store"
        assert explanation.get("tenant_id") == self.tenant_id
        assert explanation.get("status") == "completed"
        assert explanation.get("simulation_only") is True
        assert explanation.get("gateway_parity") == "partial"
        assert explanation.get("coverage_complete") is False
        assert explanation.get("provider_called") is False
        assert explanation.get("usage_recorded") is False
        assert explanation.get("cost_recorded") is False
        assert explanation.get("policy", {}).get("decision") == expected_decision
        assert explanation.get("policy", {}).get("matched_policy_id") == self.acceptance_policy_id
        assert explanation.get("outcome") == expected_outcome
        assert explanation.get("would_error_code") == expected_error
        route = explanation.get("route", {})
        dlp = explanation.get("dlp", {})
        budget = explanation.get("budget", {})
        assert route.get("status") == expected_route
        assert dlp.get("status") == expected_dlp
        assert budget.get("status") == expected_budget
        # 成功路由必须精确指向本轮临时模型和上游，不能只凭状态误验客户资源。
        if expected_route == "selected":
            assert expected_model_id and expected_provider_id
            assert route.get("selected_model_id") == expected_model_id
            assert route.get("provider_id") == expected_provider_id
        # 不可用路由仍必须保留本轮临时候选身份，但不能把它标记为最终选中结果。
        elif expected_route == "unavailable":
            candidates = route.get("candidates", [])
            assert expected_model_id and expected_provider_id
            assert any(
                item.get("model_id") == expected_model_id
                and item.get("provider_id") == expected_provider_id
                for item in candidates
                if isinstance(item, dict)
            )
        else:
            assert route.get("selected_model_id") is None
            assert route.get("provider_id") is None
        dlp_matches = dlp.get("matches", [])
        assert isinstance(dlp_matches, list)
        if expected_dlp_rule_id:
            assert [item.get("dlp_rule_id") for item in dlp_matches] == [
                expected_dlp_rule_id
            ]
        else:
            assert dlp_matches == []
        budget_rules = budget.get("rules", [])
        assert isinstance(budget_rules, list)
        if expected_budget_rule_id:
            assert [item.get("budget_rule_id") for item in budget_rules] == [
                expected_budget_rule_id
            ]
        else:
            assert budget_rules == []
        assert self.prompt_sentinel not in response.text
        return body

    def _verify_invalid_payload(self, context: dict[str, str]) -> dict[str, Any]:
        """证明未知 tenant 字段被 422 拒绝且错误不反射 Prompt。"""

        payload = self._payload(context)
        payload["tenant_id"] = self.tenant_id
        body, response = self._json_request(
            "POST",
            "/policies/dry-run",
            expected_status=422,
            expected_code="policy.dry_run_payload_invalid",
            headers=self._headers(f"req_prd046_invalid_{self.suffix}"),
            payload=payload,
        )
        assert response.headers.get("Cache-Control") == "no-store"
        assert self.prompt_sentinel not in response.text
        return {"status": response.status_code, "code": body["code"], "prompt_absent": True}

    def _verify_rbac(
        self, context: dict[str, str], tokens: dict[str, str]
    ) -> dict[str, Any]:
        """直接攻击后端入口，证明 developer/viewer 403 且匿名 401。"""

        statuses: dict[str, int] = {}
        codes: dict[str, str] = {}
        for role in ("developer", "viewer"):
            request_id = f"req_prd046_{role}_{self.suffix}"
            response = self.control.post(
                "/policies/dry-run",
                headers={
                    "Authorization": f"Bearer {tokens[role]}",
                    "X-Request-ID": request_id,
                },
                json=self._payload(context),
            )
            body = expect_json(
                response,
                403,
                expected_code="auth.permission_denied",
                require_ids=True,
            )
            self.public_response_bodies.append(response.text)
            assert body["request_id"] == request_id
            assert self.prompt_sentinel not in response.text
            statuses[role] = response.status_code
            codes[role] = str(body["code"])
        anonymous = self.control.post("/policies/dry-run", json=self._payload(context))
        anonymous_body = expect_json(
            anonymous, 401, expected_code="auth.token_missing", require_ids=True
        )
        self.public_response_bodies.append(anonymous.text)
        assert self.prompt_sentinel not in anonymous.text
        statuses["anonymous"] = anonymous.status_code
        codes["anonymous"] = str(anonymous_body["code"])
        return {"statuses": statuses, "codes": codes, "prompt_absent": True}

    def _verify_request_id_recovery(
        self, context: dict[str, str], request_id: str
    ) -> dict[str, Any]:
        """复用已完成 request_id，要求稳定 409 且三事实不重复。"""

        body, response = self._json_request(
            "POST",
            "/policies/dry-run",
            expected_status=409,
            expected_code="policy.dry_run_request_id_reused",
            headers=self._headers(request_id),
            payload=self._payload(context),
        )
        assert body["request_id"] == request_id
        assert response.headers.get("X-Request-ID") == request_id
        assert response.headers.get("X-Trace-ID") == body.get("trace_id")
        assert response.headers.get("Cache-Control") == "no-store"
        assert self.prompt_sentinel not in response.text
        return {
            "status": response.status_code,
            "code": body["code"],
            "request_id_preserved": True,
            "prompt_absent": True,
        }

    def _snapshot_database(self) -> dict[str, dict[str, Any]]:
        """逐表计算行数和内容摘要，既能发现插入也能发现原行被更新。"""

        connection = self._connect_database()
        try:
            tables = existing_tables(connection)
            snapshot: dict[str, dict[str, Any]] = {}
            for table in (*ZERO_COST_TABLES, *DRY_FACT_TABLES):
                if table not in tables:
                    snapshot[table] = {"present": False, "rows": 0, "digest": None}
                    continue
                where = ""
                parameters: tuple[object, ...] = ()
                if table == "policy_explanations":
                    where = " WHERE tenant_id=?"
                    parameters = (self.tenant_id,)
                elif table == "policy_events":
                    where = (
                        " WHERE tenant_id=? AND event_type='policy.dry_run_executed'"
                    )
                    parameters = (self.tenant_id,)
                elif table == "audit_logs":
                    where = " WHERE tenant_id=? AND action='policy.dry_run_executed'"
                    parameters = (self.tenant_id,)
                rows = connection.execute(
                    f"SELECT * FROM {quoted_identifier(table)}{where} ORDER BY rowid",
                    parameters,
                ).fetchall()
                digest = hashlib.sha256()
                for row in rows:
                    # repr 仅进入内存摘要，绝不打印，避免历史 Prompt/密钥出现在证据中。
                    digest.update(repr(tuple(row)).encode("utf-8", errors="replace"))
                    digest.update(b"\n")
                snapshot[table] = {
                    "present": True,
                    "rows": len(rows),
                    "digest": digest.hexdigest(),
                }

            # Dry-Run 不得触碰 Provider/credential 的健康、last_used 或错误状态；对整表
            # 摘要能覆盖未来新增状态列，同时摘要值本身不会泄露 encrypted_key。
            for table in ("providers", "provider_credentials"):
                rows = connection.execute(
                    f"SELECT * FROM {quoted_identifier(table)} ORDER BY rowid"
                ).fetchall()
                digest = hashlib.sha256()
                for row in rows:
                    digest.update(repr(tuple(row)).encode("utf-8", errors="replace"))
                    digest.update(b"\n")
                snapshot[f"{table}_state"] = {
                    "present": True,
                    "rows": len(rows),
                    "digest": digest.hexdigest(),
                }
            return snapshot
        finally:
            connection.close()

    def _verify_three_facts(
        self, request_id: str, response_body: dict[str, Any]
    ) -> dict[str, Any]:
        """权威回读 explanation/event/audit，并逐字段证明三事实一致。"""

        explanation = response_body["policy_explanation"]
        connection = self._connect_database()
        connection.row_factory = sqlite3.Row
        try:
            explanation_rows = connection.execute(
                """
                SELECT * FROM policy_explanations
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT * FROM policy_events
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND event_type='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchall()
            audit_rows = connection.execute(
                """
                SELECT * FROM audit_logs
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND action='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchall()
            assert len(explanation_rows) == len(event_rows) == len(audit_rows) == 1
            row, event, audit = explanation_rows[0], event_rows[0], audit_rows[0]
            payload = json.loads(str(event["payload_json"]))
            audit_before = json.loads(str(audit["before_summary_json"]))
            audit_after = json.loads(str(audit["after_summary_json"]))
            assert audit_before == {}
            assert row["policy_explanation_id"] == explanation["policy_explanation_id"]
            assert row["trace_id"] == response_body["trace_id"]
            assert event["trace_id"] == audit["trace_id"] == row["trace_id"]
            assert event["policy_id"] == row["matched_policy_id"]
            assert audit["object_type"] == "policy_explanation"
            assert audit["object_id"] == row["policy_explanation_id"]
            expected = {
                "policy_explanation_id": row["policy_explanation_id"],
                "policy_set_fingerprint": row["policy_set_fingerprint"],
                "decision": row["decision"],
                "decision_reason_code": row["reason_code"],
                "matched_policy_id": row["matched_policy_id"],
                "route_status": row["route_status"],
                "dlp_status": row["dlp_status"],
                "budget_status": row["budget_status"],
                "would_error_code": row["would_error_code"],
                "checked_policy_count": row["checked_policy_count"],
                "runtime_compatible": bool(row["runtime_compatible"]),
                "coverage_complete": bool(row["coverage_complete"]),
            }
            assert payload["request_id"] == request_id
            assert all(payload[key] == value for key, value in expected.items())
            assert all(audit_after[key] == value for key, value in expected.items())
            return {
                "rows": {"explanation": 1, "event": 1, "audit": 1},
                "request_trace_object_match": True,
                "decision_and_stage_summary_match": True,
            }
        finally:
            connection.close()

    def _verify_signed_webhooks(
        self,
        receiver: AcceptanceWebhookServer,
        request_id: str,
        response_body: dict[str, Any],
        *,
        require_failed_delivery: bool,
    ) -> dict[str, Any]:
        """核对真实 HMAC、精确 payload、持久 event_id 和可选三次失败投递。"""

        connection = self._connect_database()
        try:
            policy_event = connection.execute(
                """
                SELECT policy_event_id FROM policy_events
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND event_type='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchone()
            audit = connection.execute(
                """
                SELECT audit_log_id FROM audit_logs
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND action='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchone()
            assert policy_event is not None and audit is not None
            dry_event_id = f"evt_{policy_event[0]}"
            audit_event_id = f"evt_{audit[0]}"
            success_endpoint_id = self.webhook_endpoint_by_path["/ok"]
            success_rows = connection.execute(
                """
                SELECT event_id, event_type, status, attempt_count
                FROM webhook_deliveries
                WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id IN (?, ?)
                ORDER BY event_type
                """,
                (
                    self.tenant_id,
                    success_endpoint_id,
                    dry_event_id,
                    audit_event_id,
                ),
            ).fetchall()
            assert len(success_rows) == 2
            assert {str(row[0]) for row in success_rows} == {
                dry_event_id,
                audit_event_id,
            }
            assert all(row[2] == "success" and int(row[3]) == 1 for row in success_rows)

            failed_attempts = 0
            if require_failed_delivery:
                failure_endpoint_id = self.webhook_endpoint_by_path["/fail"]
                failure = connection.execute(
                    """
                    SELECT webhook_delivery_id, event_id, status, attempt_count
                    FROM webhook_deliveries
                    WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id=?
                    """,
                    (self.tenant_id, failure_endpoint_id, dry_event_id),
                ).fetchone()
                assert failure is not None
                assert failure[1] == dry_event_id
                assert failure[2] == "failed" and int(failure[3]) == 3
                attempts = connection.execute(
                    """
                    SELECT http_status, status FROM webhook_delivery_attempts
                    WHERE tenant_id=? AND webhook_delivery_id=?
                    ORDER BY attempt_no
                    """,
                    (self.tenant_id, failure[0]),
                ).fetchall()
                assert len(attempts) == 3
                assert all(int(row[0]) == 409 and row[1] == "failed" for row in attempts)
                failed_attempts = len(attempts)
        finally:
            connection.close()

        expected_explanation_id = str(
            response_body["policy_explanation"]["policy_explanation_id"]
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            received = receiver.snapshot()
            success_dry = [
                item
                for item in received
                if item["path"] == "/ok" and item["event_id"] == dry_event_id
            ]
            success_audit = [
                item
                for item in received
                if item["path"] == "/ok" and item["event_id"] == audit_event_id
            ]
            failure_dry = [
                item
                for item in received
                if item["path"] == "/fail" and item["event_id"] == dry_event_id
            ]
            if success_dry and success_audit and (
                not require_failed_delivery or len(failure_dry) == 3
            ):
                break
            time.sleep(0.05)
        assert len(success_dry) == len(success_audit) == 1
        assert all(item["signature_valid"] for item in (*success_dry, *success_audit))
        assert success_dry[0]["event_type"] == "policy.dry_run_executed"
        assert success_dry[0]["object_id"] == expected_explanation_id
        assert success_dry[0]["request_id"] == request_id
        assert set(success_dry[0]["payload_keys"]) == DRY_RUN_EVENT_FIELDS
        assert success_audit[0]["event_type"] == "audit.recorded"
        assert success_audit[0]["object_id"] == expected_explanation_id
        assert success_audit[0]["action"] == "policy.dry_run_executed"
        assert success_audit[0]["object_type"] == "policy_explanation"
        assert set(success_audit[0]["payload_keys"]) == AUDIT_EVENT_FIELDS
        if require_failed_delivery:
            assert len(failure_dry) == 3
            assert len({item["event_id"] for item in failure_dry}) == 1
            assert all(item["signature_valid"] for item in failure_dry)
            assert all(
                set(item["payload_keys"]) == DRY_RUN_EVENT_FIELDS
                for item in failure_dry
            )
        return {
            "success_event_count": 2,
            "all_signatures_valid": True,
            "dry_run_payload_exact": True,
            "audit_payload_exact": True,
            "stable_persistent_event_id": True,
            "failed_attempt_count": failed_attempts,
            "failed_delivery_http_status": 409 if require_failed_delivery else None,
            "dry_run_http_status": 200,
        }

    def _verify_webhook_recovery_idempotency(
        self, receiver: AcceptanceWebhookServer, request_id: str
    ) -> dict[str, Any]:
        """证明重复 request_id 复用持久 event_id，既不新建事实也不重复出网。"""

        connection = self._connect_database()
        try:
            event = connection.execute(
                """
                SELECT policy_event_id FROM policy_events
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND event_type='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchone()
            audit = connection.execute(
                """
                SELECT audit_log_id FROM audit_logs
                WHERE tenant_id=? AND actor_user_id=? AND request_id=?
                  AND action='policy.dry_run_executed'
                """,
                (self.tenant_id, self.admin_user_id, request_id),
            ).fetchone()
            assert event is not None and audit is not None
            event_ids = {f"evt_{event[0]}", f"evt_{audit[0]}"}
            rows = connection.execute(
                """
                SELECT event_id, status, attempt_count FROM webhook_deliveries
                WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id IN (?, ?)
                  AND delivery_kind='original'
                """,
                (
                    self.tenant_id,
                    self.webhook_endpoint_by_path["/ok"],
                    *sorted(event_ids),
                ),
            ).fetchall()
            assert len(rows) == 2 and {str(row[0]) for row in rows} == event_ids
            assert all(row[1] == "success" and int(row[2]) == 1 for row in rows)
        finally:
            connection.close()
        received = [
            item
            for item in receiver.snapshot()
            if item["path"] == "/ok" and item["event_id"] in event_ids
        ]
        assert len(received) == 2 and all(item["signature_valid"] for item in received)
        return {
            "persistent_event_ids_reused": True,
            "delivery_row_count": 2,
            "network_send_count": 2,
            "duplicate_network_send_count": 0,
        }

    def verify_privacy(self) -> dict[str, Any]:
        """在资源清理后扫描响应、数据库和日志，证明正文、凭据及运行故障均零命中。"""

        # DLP 场景会在基础 sentinel 后追加命中关键词，因此必须扫描本轮发送过的
        # 每个完整 Prompt 及其摘要；只检查基础 sentinel 会漏掉完整正文 hash 落库。
        prompt_values = set(self.prompt_values)
        prompt_hashes = {
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            for value in prompt_values
        }
        sensitive_values = {
            self.admin_password,
            self.admin_token,
            *self.temporary_passwords,
            *self.temporary_tokens,
            *metahub_environment_secret_values(),
        }
        # 过短值容易在时间戳、ID 或普通文本中误命中；产品凭据合同也不允许如此短的秘密。
        sensitive_values = {value for value in sensitive_values if len(value) >= 8}
        assert all(
            all(value not in body for value in prompt_values)
            and all(value not in body for value in prompt_hashes)
            for body in self.public_response_bodies
        )
        response_sensitive_hits = sum(
            body.count(value)
            for body in self.public_response_bodies
            for value in sensitive_values
        )
        response_wire = "\n".join(self.public_response_bodies).encode(
            "utf-8", errors="replace"
        )
        response_auth_hits = len(LOG_AUTHORIZATION_BEARER_PATTERN.findall(response_wire))
        response_key_hits = len(LOG_PROVIDER_VIRTUAL_KEY_PATTERN.findall(response_wire))
        assert response_sensitive_hits == 0, "a known credential was reflected in a response"
        assert response_auth_hits == 0, "an Authorization Bearer value was reflected in a response"
        assert response_key_hits == 0, "a Provider or Virtual Key value was reflected in a response"
        connection = self._connect_database()
        try:
            database_hits = sum(
                scan_database_value(connection, value) for value in prompt_values
            )
            database_hash_hits = sum(
                scan_database_value(connection, value) for value in prompt_hashes
            )
            database_sensitive_hits = sum(
                scan_database_value(connection, value) for value in sensitive_values
            )
        finally:
            connection.close()
        assert database_hits == 0, "Prompt sentinel was persisted in the database"
        assert database_hash_hits == 0, "Prompt hash was persisted in the database"
        assert database_sensitive_hits == 0, "a known credential was persisted in plaintext"
        log_hits = 0
        log_hash_hits = 0
        log_policy_hits = {
            "known_sensitive_value_hits": 0,
            "authorization_bearer_hits": 0,
            "password_value_hits": 0,
            "token_value_hits": 0,
            "provider_virtual_key_hits": 0,
            "prd046_prompt_sentinel_hits": 0,
            "governance_body_value_hits": 0,
            "traceback_hits": 0,
            "error_level_hits": 0,
            "http_5xx_hits": 0,
        }
        scanned_files = 0
        for path in self._all_log_files():
            assert path.exists() and path.is_file(), f"log file not found: {path.name}"
            scanned_files += 1
            log_hits += sum(
                scan_file_value(path, value.encode("utf-8"))
                for value in prompt_values
            )
            log_hash_hits += sum(
                scan_file_value(path, value.encode("ascii"))
                for value in prompt_hashes
            )
            scanned = scan_log_policy(path, sensitive_values)
            for key, value in scanned.items():
                log_policy_hits[key] += value
        assert log_hits == 0, "Prompt sentinel was found in a service log"
        assert log_hash_hits == 0, "Prompt hash was found in a service log"
        assert all(value == 0 for value in log_policy_hits.values()), (
            "a credential, traceback, ERROR level or HTTP 5xx was found in service logs"
        )
        return {
            "response_hits": 0,
            "response_sensitive_value_hits": response_sensitive_hits,
            "response_authorization_bearer_hits": response_auth_hits,
            "response_provider_virtual_key_hits": response_key_hits,
            "database_hits": database_hits,
            "database_hash_hits": database_hash_hits,
            "database_sensitive_value_hits": database_sensitive_hits,
            "log_hits": log_hits,
            "log_hash_hits": log_hash_hits,
            "log_policy_hits": log_policy_hits,
            "log_files_scanned": scanned_files,
            "log_roles_scanned": {
                role: len(paths) for role, paths in self._role_log_files().items()
            },
            "known_sensitive_values_checked": len(sensitive_values),
            "prompt_values_checked": len(prompt_values),
            "prompt_hashes_checked": len(prompt_hashes),
            "prompt_hash_persisted": False,
        }

    def _connect_database(self) -> sqlite3.Connection:
        """打开指定本地 SQLite 库；业务写入仍只允许经公开 API 完成。"""

        connection = sqlite3.connect(self.args.database, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _unique_intent_row(
        self,
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[object, ...],
        label: str,
    ) -> dict[str, Any] | None:
        """按 tenant 与唯一 intent 回查至多一行，歧义时失败关闭而不猜测清理目标。"""

        rows = connection.execute(query, parameters).fetchall()
        assert len(rows) <= 1, f"ambiguous cleanup intent: {label}"
        return dict(rows[0]) if rows else None

    def _resolve_created_resource_intents(self) -> dict[str, Any]:
        """从已绑定运行库回查所有临时资源，覆盖创建提交后响应丢失的清理窗口。"""

        assert self.database_runtime_bound and self.tenant_id
        connection = self._connect_database()
        connection.row_factory = sqlite3.Row
        try:
            users = {
                role: self._unique_intent_row(
                    connection,
                    "SELECT user_id, status FROM users WHERE tenant_id=? AND email=?",
                    (self.tenant_id, email),
                    f"user_{role}",
                )
                for role, email in self.user_email_by_role.items()
            }
            webhooks = {
                path: self._unique_intent_row(
                    connection,
                    """
                    SELECT webhook_endpoint_id, status FROM webhook_endpoints
                    WHERE tenant_id=? AND name=? AND endpoint_url=?
                    """,
                    (self.tenant_id, intent["name"], intent["endpoint_url"]),
                    f"webhook_{path[1:]}",
                )
                for path, intent in self.webhook_intents.items()
            }
            budget = None
            if self.budget_intent is not None:
                budget = self._unique_intent_row(
                    connection,
                    """
                    SELECT budget_rule_id, scope_type, scope_id, environment_id, status
                    FROM budget_rules
                    WHERE tenant_id=? AND scope_type=? AND scope_id=?
                      AND environment_id=? AND name=?
                    """,
                    (
                        self.tenant_id,
                        self.budget_intent["scope_type"],
                        self.budget_intent["scope_id"],
                        self.budget_intent["environment_id"],
                        self.budget_intent["name"],
                    ),
                    "budget",
                )
            alias = None
            if self.alias_intent.get("environment_id"):
                alias = self._unique_intent_row(
                    connection,
                    """
                    SELECT model_alias_id, status FROM model_aliases
                    WHERE tenant_id=? AND alias=? AND environment_id=?
                    """,
                    (
                        self.tenant_id,
                        self.alias_intent["alias"],
                        self.alias_intent["environment_id"],
                    ),
                    "model_alias",
                )
            return {
                "users": users,
                "project": self._unique_intent_row(
                    connection,
                    """
                    SELECT project_id, status FROM projects
                    WHERE tenant_id=? AND project_code=?
                    """,
                    (self.tenant_id, self.project_intent["project_code"]),
                    "project",
                ),
                "provider": self._unique_intent_row(
                    connection,
                    """
                    SELECT provider_id, status FROM providers
                    WHERE tenant_id=? AND provider_code=?
                    """,
                    (self.tenant_id, self.provider_intent["provider_code"]),
                    "provider",
                ),
                "model": self._unique_intent_row(
                    connection,
                    """
                    SELECT model_id, status FROM models
                    WHERE tenant_id=? AND model_alias=?
                    """,
                    (self.tenant_id, self.model_intent["internal_model_id"]),
                    "model",
                ),
                "alias": alias,
                "webhooks": webhooks,
                "dlp": self._unique_intent_row(
                    connection,
                    """
                    SELECT dlp_rule_id, status FROM dlp_rules
                    WHERE tenant_id=? AND name=? AND pattern=?
                    """,
                    (
                        self.tenant_id,
                        self.dlp_intent["name"],
                        self.dlp_intent["marker"],
                    ),
                    "dlp",
                ),
                "budget": budget,
                "policy": self._unique_intent_row(
                    connection,
                    """
                    SELECT policy_id, status, version FROM policies
                    WHERE tenant_id=? AND name=? AND description=?
                    """,
                    (
                        self.tenant_id,
                        self.policy_intent["name"],
                        self.policy_intent["description"],
                    ),
                    "policy",
                ),
            }
        finally:
            connection.close()

    def _coalesce_intent_id(
        self,
        captured_id: str | None,
        resolved_row: dict[str, Any] | None,
        id_field: str,
        label: str,
    ) -> str | None:
        """合并响应 ID 与 intent 回查 ID；两者冲突时拒绝清理，避免误伤其他资源。"""

        identifiers = {
            value
            for value in (
                str(captured_id or ""),
                str((resolved_row or {}).get(id_field, "")),
            )
            if value
        }
        assert len(identifiers) <= 1, f"cleanup intent id mismatch: {label}"
        return next(iter(identifiers), None)

    def _coalesce_user_cleanup_ids(
        self,
        user_rows: dict[str, dict[str, Any] | None],
        *,
        intent_lookup_completed: bool,
        errors: list[str],
    ) -> tuple[set[str], set[str]]:
        """逐角色合并 User ID；任一角色冲突时整角色跳过，不形成宽松 ID 并集。"""

        user_ids: set[str] = set()
        blocked_roles: set[str] = set()
        for role in self.user_email_by_role:
            try:
                row = user_rows.get(role)
                captured_id = self.temporary_user_id_by_role.get(role)
                if intent_lookup_completed and captured_id:
                    assert row is not None
                user_id = self._coalesce_intent_id(
                    captured_id,
                    row,
                    "user_id",
                    f"user_{role}",
                )
                if user_id:
                    user_ids.add(user_id)
            except Exception as exc:  # noqa: BLE001
                # 冲突角色的两个候选都不得进入最终 UPDATE 白名单。
                blocked_roles.add(role)
                errors.append(f"user_intent_{role}:{type(exc).__name__}")
        return user_ids, blocked_roles

    def _resolve_admin_login_intent_sessions(self) -> dict[str, str]:
        """回查唯一 UA 相对基线新增的管理员 Session，并在响应丢失时建立运行库绑定。"""

        tenant_id = self.admin_login_intent["tenant_id"]
        user_id = self.admin_login_intent["user_id"]
        # 登录前身份尚未成功登记时没有安全查询边界，必须保持空结果且禁止模糊扫描。
        if not tenant_id or not user_id:
            return {}
        connection = self._connect_database()
        try:
            rows = connection.execute(
                """
                SELECT session_id, status FROM sessions
                WHERE tenant_id=? AND user_id=? AND user_agent=?
                """,
                (tenant_id, user_id, self.admin_login_intent["user_agent"]),
            ).fetchall()
        finally:
            connection.close()
        sessions = {
            str(row[0]): str(row[1])
            for row in rows
            if str(row[0]) not in self.admin_login_baseline_session_ids
        }
        # 单次登录 intent 最多创建一条 Session；多行意味着身份不再唯一，不能批量撤销。
        assert len(sessions) <= 1, "admin login intent resolved multiple sessions"
        if sessions and not self.database_runtime_bound:
            self.tenant_id = tenant_id
            self.admin_user_id = user_id
            self.admin_session_id = next(iter(sessions))
            # 随机 UA、租户、管理员和基线差集共同证明迟到 Session 来自本轮 Control 请求。
            self.database_runtime_bound = True
        return sessions

    def _revoke_admin_login_intent_sessions(self) -> int:
        """事务内撤销本轮唯一 UA 新增 Session，形成后续最终数据库收敛的认证围栏。"""

        sessions = self._resolve_admin_login_intent_sessions()
        # 不确定登录首轮尚无事实时再等待完整窗口，防止撤销动作早于迟到插入。
        if self.uncertain_mutation and not sessions:
            # 登录响应丢失且首轮仍未见 Session 时再等一个完整窗口，避免围栏落在迟到 INSERT 之前。
            deadline = time.monotonic() + UNCERTAIN_MUTATION_SETTLE_SECONDS
            while (remaining := deadline - time.monotonic()) > 0:
                time.sleep(min(remaining, 0.25))
        tenant_id = self.admin_login_intent["tenant_id"]
        user_id = self.admin_login_intent["user_id"]
        # 登录 intent 尚未登记表示请求从未发出，此时没有可建立的认证围栏。
        if not tenant_id or not user_id:
            return 0
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect_database()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_id, status FROM sessions
                WHERE tenant_id=? AND user_id=? AND user_agent=?
                """,
                (tenant_id, user_id, self.admin_login_intent["user_agent"]),
            ).fetchall()
            sessions = {
                str(row[0]): str(row[1])
                for row in rows
                if str(row[0]) not in self.admin_login_baseline_session_ids
            }
            assert len(sessions) <= 1, "admin login intent resolved multiple sessions"
            # 响应丢失时用唯一 UA 差集补建数据库绑定，后续仍只允许精确 intent 清理。
            if sessions and not self.database_runtime_bound:
                self.tenant_id = tenant_id
                self.admin_user_id = user_id
                self.admin_session_id = next(iter(sessions))
                self.database_runtime_bound = True
            changed = 0
            # 只有唯一差集 Session 存在时才拼接 ID 白名单；空集合仍提交写事务作为顺序围栏。
            if sessions:
                identifiers = sorted(sessions)
                placeholders = ",".join("?" for _ in identifiers)
                cursor = connection.execute(
                    "UPDATE sessions SET status='revoked', "
                    "revoked_at=COALESCE(revoked_at, ?), updated_at=? "
                    f"WHERE tenant_id=? AND user_id=? AND session_id IN ({placeholders}) "
                    "AND status='active'",
                    (now, now, tenant_id, user_id, *identifiers),
                )
                changed = int(cursor.rowcount)
            connection.commit()
            return changed
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _final_database_reconcile(
        self, blocked_user_roles: set[str]
    ) -> tuple[int, int]:
        """认证围栏后按 intent 二次收敛状态，并只读断言临时活动残留为零。"""

        if not self.database_runtime_bound:
            sessions = self._resolve_admin_login_intent_sessions()
            active_sessions = sum(status == "active" for status in sessions.values())
            assert active_sessions == 0, "active admin login intent session remains"
            # 未建立运行库绑定时只允许不存在活动登录事实，不能扫描或写入其他业务资源。
            return 0, active_sessions
        intents = self._resolve_created_resource_intents()
        rows_to_disable: list[tuple[str, str, str]] = []
        captured_budget_id = (
            (self.acceptance_budget_rule or {}).get("budget_rule_id") or None
        )
        scalar_resources = (
            (
                "project",
                "projects",
                "project_id",
                self.created_project_id,
                intents.get("project"),
            ),
            (
                "provider",
                "providers",
                "provider_id",
                self.acceptance_provider_id,
                intents.get("provider"),
            ),
            (
                "model",
                "models",
                "model_id",
                self.acceptance_model_id,
                intents.get("model"),
            ),
            (
                "model_alias",
                "model_aliases",
                "model_alias_id",
                self.acceptance_model_alias_id,
                intents.get("alias"),
            ),
            (
                "dlp",
                "dlp_rules",
                "dlp_rule_id",
                self.acceptance_dlp_rule_id,
                intents.get("dlp"),
            ),
            (
                "budget",
                "budget_rules",
                "budget_rule_id",
                captured_budget_id,
                intents.get("budget"),
            ),
        )
        for label, table, id_field, captured_id, row in scalar_resources:
            if captured_id:
                assert row is not None, f"final cleanup intent missing: {label}"
            resource_id = self._coalesce_intent_id(
                captured_id, row, id_field, f"final_{label}"
            )
            if resource_id:
                rows_to_disable.append((table, id_field, resource_id))
        final_user_ids: set[str] = set()
        for role in self.user_email_by_role:
            # 首轮身份冲突的角色在最终收敛也必须跳过，不能重新采用数据库单边 ID。
            if role in blocked_user_roles:
                continue
            row = intents.get("users", {}).get(role)
            captured_id = self.temporary_user_id_by_role.get(role)
            if captured_id:
                assert row is not None, f"final cleanup intent missing: user_{role}"
            user_id = self._coalesce_intent_id(
                captured_id, row, "user_id", f"final_user_{role}"
            )
            if user_id:
                final_user_ids.add(user_id)
                rows_to_disable.append(("users", "user_id", user_id))
        for path in self.webhook_intents:
            row = intents.get("webhooks", {}).get(path)
            captured_id = self.webhook_endpoint_by_path.get(path)
            if captured_id:
                assert row is not None, f"final cleanup intent missing: webhook_{path[1:]}"
            endpoint_id = self._coalesce_intent_id(
                captured_id,
                row,
                "webhook_endpoint_id",
                f"final_webhook_{path[1:]}",
            )
            if endpoint_id:
                rows_to_disable.append(
                    ("webhook_endpoints", "webhook_endpoint_id", endpoint_id)
                )
        policy = intents.get("policy")
        if self.acceptance_policy_id:
            assert policy is not None, "final cleanup intent missing: policy"
        policy_id = self._coalesce_intent_id(
            self.acceptance_policy_id,
            policy,
            "policy_id",
            "final_policy",
        )

        connection: sqlite3.Connection | None = None
        changed = 0
        try:
            connection = self._connect_database()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            connection.execute("BEGIN IMMEDIATE")
            for table, id_field, resource_id in rows_to_disable:
                cursor = connection.execute(
                    f"UPDATE {quoted_identifier(table)} SET status='disabled', updated_at=? "
                    f"WHERE tenant_id=? AND {quoted_identifier(id_field)}=? "
                    "AND status<>'disabled'",
                    (now, self.tenant_id, resource_id),
                )
                changed += int(cursor.rowcount)
            if policy_id is not None:
                # Policy 触发器要求每次状态写同时精确推进版本；紧急收敛也不能绕过该约束。
                cursor = connection.execute(
                    """
                    UPDATE policies
                    SET status='disabled', version=version+1, updated_by=?, updated_at=?
                    WHERE tenant_id=? AND policy_id=? AND status='active'
                    """,
                    (
                        self.admin_login_intent["user_id"],
                        now,
                        self.tenant_id,
                        policy_id,
                    ),
                )
                changed += int(cursor.rowcount)
            user_ids = sorted(final_user_ids)
            if user_ids:
                placeholders = ",".join("?" for _ in user_ids)
                cursor = connection.execute(
                    "UPDATE sessions SET status='revoked', "
                    "revoked_at=COALESCE(revoked_at, ?), updated_at=? "
                    f"WHERE tenant_id=? AND user_id IN ({placeholders}) AND status='active'",
                    (now, now, self.tenant_id, *sorted(user_ids)),
                )
                changed += int(cursor.rowcount)
            for policy_id, _original_version in self.disabled_policies:
                cursor = connection.execute(
                    """
                    UPDATE policies
                    SET status='active', version=version+1, updated_by=?, updated_at=?
                    WHERE tenant_id=? AND policy_id=? AND status='disabled'
                    """,
                    (
                        self.admin_login_intent["user_id"],
                        now,
                        self.tenant_id,
                        policy_id,
                    ),
                )
                changed += int(cursor.rowcount)
            for rule_id in self.disabled_dlp_rule_ids:
                cursor = connection.execute(
                    "UPDATE dlp_rules SET status='active', updated_at=? "
                    "WHERE tenant_id=? AND dlp_rule_id=? AND status='disabled'",
                    (now, self.tenant_id, rule_id),
                )
                changed += int(cursor.rowcount)
            for rule in self.disabled_budget_rules:
                cursor = connection.execute(
                    "UPDATE budget_rules SET status='active', updated_at=? "
                    "WHERE tenant_id=? AND budget_rule_id=? AND status='disabled'",
                    (now, self.tenant_id, rule["budget_rule_id"]),
                )
                changed += int(cursor.rowcount)
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

        final_intents = self._resolve_created_resource_intents()
        active_residue = 0
        for key, value in final_intents.items():
            rows = value.values() if key in {"users", "webhooks"} else (value,)
            active_residue += sum(
                row is not None and row.get("status") == "active" for row in rows
            )
        admin_sessions = self._resolve_admin_login_intent_sessions()
        active_residue += sum(status == "active" for status in admin_sessions.values())
        assert active_residue == 0, "active cleanup intent residue remains"

        connection = self._connect_database()
        try:
            # 原有治理配置必须全部恢复 active；最终断言只按预登记 ID，不扫描或修改其他租户。
            for policy_id, _version in self.disabled_policies:
                row = connection.execute(
                    "SELECT status FROM policies WHERE tenant_id=? AND policy_id=?",
                    (self.tenant_id, policy_id),
                ).fetchone()
                assert row is not None and row[0] == "active"
            for rule_id in self.disabled_dlp_rule_ids:
                row = connection.execute(
                    "SELECT status FROM dlp_rules WHERE tenant_id=? AND dlp_rule_id=?",
                    (self.tenant_id, rule_id),
                ).fetchone()
                assert row is not None and row[0] == "active"
            for rule in self.disabled_budget_rules:
                row = connection.execute(
                    "SELECT status FROM budget_rules WHERE tenant_id=? AND budget_rule_id=?",
                    (self.tenant_id, rule["budget_rule_id"]),
                ).fetchone()
                assert row is not None and row[0] == "active"
        finally:
            connection.close()
        return changed, active_residue

    def _wait_for_uncertain_mutation_settle(self) -> None:
        """写传输断开后等待至少一个原超时窗口，再执行首次 intent 清理。"""

        if not self.uncertain_mutation:
            return
        assert self.uncertain_mutation_since is not None
        deadline = self.uncertain_mutation_since + UNCERTAIN_MUTATION_SETTLE_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            # 短片等待允许 Ctrl-C 及时生效，同时确保总等待不少于完整客户端超时窗口。
            time.sleep(min(remaining, 0.25))

    def _wait_after_authentication_fence(self) -> None:
        """撤销 mutation Session 后再等一窗口，让已完成鉴权的在途处理先于最终 DB 收敛。"""

        if not self.uncertain_mutation:
            return
        deadline = time.monotonic() + UNCERTAIN_MUTATION_SETTLE_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(remaining, 0.25))

    def cleanup_resources(self) -> dict[str, Any]:
        """精确禁用临时治理资源、恢复原 active 配置并撤销全部验收会话。"""

        result: dict[str, Any] = {
            "acceptance_policy_disabled": False,
            "preexisting_policies_restored": 0,
            "acceptance_dlp_rule_disabled": False,
            "preexisting_dlp_rules_restored": 0,
            "acceptance_budget_rule_disabled": False,
            "preexisting_budget_rules_restored": 0,
            "acceptance_model_alias_disabled": False,
            "acceptance_model_disabled": False,
            "acceptance_provider_disabled": False,
            "webhook_endpoints_disabled": 0,
            "temporary_sessions_logged_out": 0,
            "temporary_sessions_revoked": 0,
            "temporary_users_disabled": 0,
            "temporary_project_disabled": False,
            "admin_session_logged_out": False,
            "admin_sessions_resolved_by_intent": 0,
            "admin_sessions_revoked_by_fence": 0,
            "final_reconcile_rows": 0,
            "active_intent_residue": None,
            "intent_lookup_completed": False,
            "intent_resources_resolved": 0,
            "errors": [],
        }
        try:
            self._wait_for_uncertain_mutation_settle()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"mutation_settle:{type(exc).__name__}")
        try:
            admin_intent_sessions = self._resolve_admin_login_intent_sessions()
            result["admin_sessions_resolved_by_intent"] = len(admin_intent_sessions)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"admin_session_intent:{type(exc).__name__}")
        intents: dict[str, Any] = {}
        if self.database_runtime_bound:
            try:
                intents = self._resolve_created_resource_intents()
                result["intent_lookup_completed"] = True
                result["intent_resources_resolved"] = sum(
                    1
                    for key, value in intents.items()
                    if key not in {"users", "webhooks"} and value is not None
                ) + sum(value is not None for value in intents.get("users", {}).values()) + sum(
                    value is not None for value in intents.get("webhooks", {}).values()
                )
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"intent_lookup:{type(exc).__name__}")

        # Webhook 必须在任何会产生审计事件的 API 清理前停用；即使创建响应丢失，也按 intent ID 回收。
        webhook_rows = intents.get("webhooks", {})
        webhook_ids: set[str] = set()
        try:
            for path in self.webhook_intents:
                endpoint_id = self._coalesce_intent_id(
                    self.webhook_endpoint_by_path.get(path),
                    webhook_rows.get(path),
                    "webhook_endpoint_id",
                    f"webhook_{path[1:]}",
                )
                if endpoint_id:
                    webhook_ids.add(endpoint_id)
            assert set(self.webhook_endpoint_ids).issubset(webhook_ids)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"webhook_intent:{type(exc).__name__}")
        # 只有认证租户和精确回查 ID 同时存在时才允许执行端点状态写入。
        if self.tenant_id and webhook_ids:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._connect_database()
                placeholders = ",".join("?" for _ in webhook_ids)
                cursor = connection.execute(
                    "UPDATE webhook_endpoints SET status='disabled', updated_at=? "
                    f"WHERE tenant_id=? AND webhook_endpoint_id IN ({placeholders}) "
                    "AND status='active'",
                    (
                        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        self.tenant_id,
                        *sorted(webhook_ids),
                    ),
                )
                connection.commit()
                result["webhook_endpoints_disabled"] = int(cursor.rowcount)
            except Exception as exc:  # noqa: BLE001
                if connection is not None:
                    connection.rollback()
                result["errors"].append(f"webhook_cleanup:{type(exc).__name__}")
            finally:
                if connection is not None:
                    connection.close()

        # 临时与原 Policy 共用一次权威列表回读；CAS 始终使用清理时最新版本。
        policy_states: dict[str, dict[str, Any]] | None = None
        if self.admin_token:
            try:
                listed, _ = self._json_request(
                    "GET",
                    "/policies?status=all",
                    expected_status=200,
                    headers=self._headers(f"req_prd046_cleanup_policy_list_{self.suffix}"),
                    record_body=False,
                )
                policy_states = {
                    str(item["policy_id"]): item
                    for item in listed.get("policies", [])
                    if isinstance(item, dict) and item.get("policy_id")
                }
                matches = [
                    item
                    for item in policy_states.values()
                    if item.get("name") == self.policy_intent["name"]
                    and item.get("description") == self.policy_intent["description"]
                ]
                assert len(matches) <= 1
                listed_policy_id = str(matches[0]["policy_id"]) if matches else None
                intent_policy = intents.get("policy")
                captured_or_listed_id = self._coalesce_intent_id(
                    self.acceptance_policy_id,
                    {"policy_id": listed_policy_id} if listed_policy_id else None,
                    "policy_id",
                    "policy_list",
                )
                policy_id = self._coalesce_intent_id(
                    captured_or_listed_id,
                    intent_policy,
                    "policy_id",
                    "policy",
                )
                if policy_id:
                    current = policy_states.get(policy_id)
                    assert current is not None
                    if current.get("status") == "active":
                        headers = self._headers(f"req_prd046_cleanup_policy_{self.suffix}")
                        headers["If-Match"] = f'"{int(current["version"])}"'
                        self._json_request(
                            "PATCH",
                            f"/policies/{policy_id}",
                            expected_status=200,
                            headers=headers,
                            payload={"status": "disabled"},
                            record_body=False,
                        )
                    else:
                        assert current.get("status") == "disabled"
                result["acceptance_policy_disabled"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"acceptance_policy:{type(exc).__name__}")
        else:
            result["acceptance_policy_disabled"] = True
        if policy_states is not None:
            for policy_id, original_version in self.disabled_policies:
                try:
                    current = policy_states.get(policy_id)
                    assert current is not None, "isolated policy disappeared before cleanup"
                    current_version = int(current["version"])
                    assert current_version >= original_version
                    if current.get("status") == "disabled":
                        headers = self._headers(f"req_prd046_restore_{policy_id}")
                        headers["If-Match"] = f'"{current_version}"'
                        self._json_request(
                            "PATCH",
                            f"/policies/{policy_id}",
                            expected_status=200,
                            headers=headers,
                            payload={"status": "active"},
                            record_body=False,
                        )
                    else:
                        assert current.get("status") == "active"
                    result["preexisting_policies_restored"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(f"policy_restore:{type(exc).__name__}")

        dlp_row = intents.get("dlp")
        try:
            dlp_id = self._coalesce_intent_id(
                self.acceptance_dlp_rule_id, dlp_row, "dlp_rule_id", "dlp"
            )
            # 临时规则仍活动时才调用后端禁用，已禁用状态保持幂等且不制造额外审计。
            if self.admin_token and dlp_id and (dlp_row or {}).get("status") != "disabled":
                self._json_request(
                    "PATCH",
                    f"/dlp/rules/{dlp_id}",
                    expected_status=200,
                    headers=self._headers(f"req_prd046_cleanup_dlp_{self.suffix}"),
                    payload={"status": "disabled"},
                    record_body=False,
                )
            result["acceptance_dlp_rule_disabled"] = True
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"acceptance_dlp:{type(exc).__name__}")
        for rule_id in self.disabled_dlp_rule_ids:
            try:
                self._json_request(
                    "PATCH",
                    f"/dlp/rules/{rule_id}",
                    expected_status=200,
                    headers=self._headers(f"req_prd046_restore_dlp_{rule_id}"),
                    payload={"status": "active"},
                    record_body=False,
                )
                result["preexisting_dlp_rules_restored"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"dlp_restore:{type(exc).__name__}")

        budget_row = intents.get("budget")
        captured_budget_id = (
            (self.acceptance_budget_rule or {}).get("budget_rule_id") or None
        )
        try:
            budget_id = self._coalesce_intent_id(
                captured_budget_id, budget_row, "budget_rule_id", "budget"
            )
            budget_identity = budget_row or self.acceptance_budget_rule or self.budget_intent
            if (
                self.admin_token
                and budget_id
                and budget_identity is not None
                and (budget_row or {}).get("status") != "disabled"
            ):
                self._json_request(
                    "PATCH",
                    "/budgets",
                    expected_status=200,
                    headers=self._headers(f"req_prd046_cleanup_budget_{self.suffix}"),
                    payload={
                        "scope_type": budget_identity["scope_type"],
                        "scope_id": budget_identity["scope_id"],
                        "environment_id": budget_identity["environment_id"],
                        "status": "disabled",
                    },
                    record_body=False,
                )
            result["acceptance_budget_rule_disabled"] = True
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"acceptance_budget:{type(exc).__name__}")
        for rule in self.disabled_budget_rules:
            try:
                self._json_request(
                    "PATCH",
                    "/budgets",
                    expected_status=200,
                    headers=self._headers(
                        f"req_prd046_restore_budget_{rule['budget_rule_id']}"
                    ),
                    payload={
                        "scope_type": rule["scope_type"],
                        "scope_id": rule["scope_id"],
                        "environment_id": rule["environment_id"],
                        "status": "active",
                    },
                    record_body=False,
                )
                result["preexisting_budget_rules_restored"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"budget_restore:{type(exc).__name__}")

        # 路由资源按 Alias -> Model -> Provider 逆序停用，每一步都校验响应 ID 与 intent ID 一致。
        route_resources = (
            (
                "model_alias",
                self.acceptance_model_alias_id,
                intents.get("alias"),
                "model_alias_id",
                "model-aliases",
                "acceptance_model_alias_disabled",
            ),
            (
                "model",
                self.acceptance_model_id,
                intents.get("model"),
                "model_id",
                "models",
                "acceptance_model_disabled",
            ),
            (
                "provider",
                self.acceptance_provider_id,
                intents.get("provider"),
                "provider_id",
                "providers",
                "acceptance_provider_disabled",
            ),
        )
        for label, captured_id, row, id_field, api_path, result_field in route_resources:
            try:
                resource_id = self._coalesce_intent_id(
                    captured_id, row, id_field, label
                )
                # 每个路由资源只在仍活动且身份完整时停用，避免对未知资源发出模糊请求。
                if self.admin_token and resource_id and (row or {}).get("status") != "disabled":
                    self._json_request(
                        "PATCH",
                        f"/{api_path}/{resource_id}",
                        expected_status=200,
                        headers=self._headers(f"req_prd046_cleanup_{label}_{self.suffix}"),
                        payload={"status": "disabled"},
                        record_body=False,
                    )
                result[result_field] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{label}_cleanup:{type(exc).__name__}")

        for token in self.temporary_tokens:
            try:
                response = self.control.post(
                    "/auth/logout", headers={"Authorization": f"Bearer {token}"}
                )
                expect_json(response, 200, require_ids=True)
                result["temporary_sessions_logged_out"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"session_logout:{type(exc).__name__}")

        user_rows = intents.get("users", {})
        user_ids, blocked_user_roles = self._coalesce_user_cleanup_ids(
            user_rows,
            intent_lookup_completed=bool(result["intent_lookup_completed"]),
            errors=result["errors"],
        )
        # 用户管理 API 当前没有禁用入口；事务严格限定绑定 tenant 与 intent 白名单。
        if self.tenant_id and user_ids:
            connection = None
            try:
                connection = self._connect_database()
                now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                placeholders = ",".join("?" for _ in user_ids)
                sessions = connection.execute(
                    "UPDATE sessions SET status='revoked', revoked_at=COALESCE(revoked_at, ?), "
                    "updated_at=? WHERE tenant_id=? "
                    f"AND user_id IN ({placeholders}) AND status='active'",
                    (now, now, self.tenant_id, *sorted(user_ids)),
                )
                cursor = connection.execute(
                    "UPDATE users SET status='disabled', updated_at=? "
                    f"WHERE tenant_id=? AND user_id IN ({placeholders})",
                    (now, self.tenant_id, *sorted(user_ids)),
                )
                connection.commit()
                result["temporary_sessions_revoked"] = int(sessions.rowcount)
                result["temporary_users_disabled"] = int(cursor.rowcount)
            except Exception as exc:  # noqa: BLE001
                if connection is not None:
                    connection.rollback()
                result["errors"].append(f"user_cleanup:{type(exc).__name__}")
            finally:
                if connection is not None:
                    connection.close()

        project_row = intents.get("project")
        try:
            project_id = self._coalesce_intent_id(
                self.created_project_id, project_row, "project_id", "project"
            )
        except Exception as exc:  # noqa: BLE001
            project_id = None
            result["errors"].append(f"project_intent:{type(exc).__name__}")
        # Project 无公开禁用入口；独立事务异常时回滚并关闭，不能持锁阻断后续隐私扫描。
        if self.tenant_id and project_id:
            connection = None
            try:
                connection = self._connect_database()
                cursor = connection.execute(
                    "UPDATE projects SET status='disabled', updated_at=? "
                    "WHERE tenant_id=? AND project_id=?",
                    (
                        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        self.tenant_id,
                        project_id,
                    ),
                )
                connection.commit()
                result["temporary_project_disabled"] = cursor.rowcount == 1
            except Exception as exc:  # noqa: BLE001
                if connection is not None:
                    connection.rollback()
                result["errors"].append(f"project_cleanup:{type(exc).__name__}")
            finally:
                if connection is not None:
                    connection.close()
        elif project_id is None:
            result["temporary_project_disabled"] = True
        # 所有需要管理员权限的恢复动作完成后，最后注销本轮管理员 Session。
        if self.admin_token:
            try:
                response = self.control.post("/auth/logout", headers=self._headers())
                expect_json(response, 200, require_ids=True)
                result["admin_session_logged_out"] = True
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"admin_logout:{type(exc).__name__}")
        try:
            result["admin_sessions_revoked_by_fence"] = (
                self._revoke_admin_login_intent_sessions()
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"admin_session_fence:{type(exc).__name__}")
        try:
            self._wait_after_authentication_fence()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"post_fence_settle:{type(exc).__name__}")
        try:
            changed, residue = self._final_database_reconcile(blocked_user_roles)
            result["final_reconcile_rows"] = changed
            result["active_intent_residue"] = residue
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"cleanup_residue:{type(exc).__name__}")
        self.uncertain_mutation = False
        return result


def assert_phase_delta(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    dry_fact_delta: int,
) -> dict[str, Any]:
    """断言单阶段零成本表不变，三类安全事实按预期精确增长。"""

    evidence: dict[str, Any] = {}
    for table in (*ZERO_COST_TABLES, "providers_state", "provider_credentials_state"):
        left, right = before[table], after[table]
        assert left["present"] == right["present"]
        assert left["rows"] == right["rows"] and left["digest"] == right["digest"], (
            f"zero-cost table changed: {table}"
        )
        evidence[table] = {
            "present": left["present"],
            "before": left["rows"],
            "after": right["rows"],
            "delta": 0,
            "content_unchanged": True,
        }
    for table in DRY_FACT_TABLES:
        left, right = before[table], after[table]
        assert left["present"] and right["present"]
        delta = int(right["rows"]) - int(left["rows"])
        assert delta == dry_fact_delta, f"unexpected {table} delta: {delta}"
        if dry_fact_delta == 0:
            assert left["digest"] == right["digest"], f"existing {table} facts changed"
        evidence[table] = {
            "present": True,
            "before": left["rows"],
            "after": right["rows"],
            "delta": delta,
            "existing_content_unchanged": dry_fact_delta == 0,
        }
    return evidence


def public_dry_run(body: dict[str, Any]) -> dict[str, Any]:
    """提取可公开的模拟状态，不输出 request_id、模型、项目或规则正文。"""

    explanation = body["policy_explanation"]
    return {
        "http_status": 200,
        "status": explanation["status"],
        "outcome": explanation["outcome"],
        "decision": explanation["policy"]["decision"],
        "would_error_code": explanation.get("would_error_code"),
        "route_status": explanation["route"]["status"],
        "dlp_status": explanation["dlp"]["status"],
        "budget_status": explanation["budget"]["status"],
        "provider_called": explanation["provider_called"],
        "usage_recorded": explanation["usage_recorded"],
        "cost_recorded": explanation["cost_recorded"],
        "request_id_present": bool(body.get("request_id")),
        "trace_id_present": bool(body.get("trace_id")),
        "no_store": True,
    }


def required_environment(name: str) -> str:
    """读取必需环境变量；缺失时只报告变量名，不枚举环境内容。"""

    value = str(os.getenv(name, "")).strip()
    # 缺失配置只能返回变量名，不能打印环境快照或相邻凭据值。
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def expect_json(
    response: httpx.Response,
    expected_status: int,
    *,
    expected_code: str | None = None,
    require_ids: bool,
) -> dict[str, Any]:
    """校验对象 JSON；失败信息只包含固定错误字段，绝不拼接响应正文。"""

    try:
        parsed = response.json()
    # 非 JSON 错误响应统一收敛为空对象，后续只按状态码和安全白名单诊断。
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    body = parsed if isinstance(parsed, dict) else {}
    # 状态不符时严禁拼接 response.text，避免 Prompt 或上游异常进入控制台输出。
    if response.status_code != expected_status:
        safe = {field: body.get(field) for field in SAFE_ERROR_FIELDS}
        raise AssertionError(
            f"HTTP {response.status_code}, expected {expected_status}, safe={safe}"
        )
    # 错误码不符只比较稳定 code，不输出 message、details 或原始 input。
    if expected_code is not None and body.get("code") != expected_code:
        raise AssertionError(
            f"error code {body.get('code')!r}, expected {expected_code!r}"
        )
    if require_ids:
        assert body.get("request_id") and body.get("trace_id"), (
            "response missing request/trace id"
        )
    return body


def existing_tables(connection: sqlite3.Connection) -> set[str]:
    """读取当前 SQLite 表名集合，供受控常量表选择使用。"""

    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def quoted_identifier(value: str) -> str:
    """安全引用来自 SQLite schema 或脚本常量的标识符。"""

    return '"' + value.replace('"', '""') + '"'


def scan_database_value(connection: sqlite3.Connection, value: str) -> int:
    """扫描全部业务表文本列，返回敏感 sentinel 的命中行数。"""

    hits = 0
    for table in sorted(existing_tables(connection)):
        columns = [
            str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({quoted_identifier(table)})"
            )
        ]
        if not columns:
            continue
        predicates = " OR ".join(
            f"instr(CAST({quoted_identifier(column)} AS TEXT), ?) > 0"
            for column in columns
        )
        row = connection.execute(
            f"SELECT COUNT(*) FROM {quoted_identifier(table)} WHERE {predicates}",
            tuple(value for _ in columns),
        ).fetchone()
        hits += int(row[0])
    return hits


def scan_file_value(path: Path, needle: bytes) -> int:
    """分块扫描单个日志文件，并保留跨块匹配所需的短尾部。"""

    hits = 0
    overlap = max(0, len(needle) - 1)
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            data = tail + chunk
            hits += data.count(needle)
            tail = data[-overlap:] if overlap else b""
    return hits


def metahub_environment_secret_values() -> tuple[str, ...]:
    """读取当前进程内 MetaHub 密码/Token/Key/Secret 值，仅供精确零命中扫描。"""

    markers = ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "VIRTUAL_KEY", "PROVIDER_KEY")
    values = {
        value
        for name, value in os.environ.items()
        if name.upper().startswith("METAHUB_")
        and any(marker in name.upper() for marker in markers)
        and value
    }
    return tuple(sorted(values))


def scan_log_policy(path: Path, sensitive_values: set[str]) -> dict[str, int]:
    """逐行扫描日志中的真实凭据形态、异常栈、ERROR 级别和 HTTP 5xx。"""

    encoded_values = tuple(value.encode("utf-8") for value in sorted(sensitive_values))
    hits = {
        "known_sensitive_value_hits": 0,
        "authorization_bearer_hits": 0,
        "password_value_hits": 0,
        "token_value_hits": 0,
        "provider_virtual_key_hits": 0,
        "prd046_prompt_sentinel_hits": 0,
        "governance_body_value_hits": 0,
        "traceback_hits": 0,
        "error_level_hits": 0,
        "http_5xx_hits": 0,
    }
    with path.open("rb") as stream:
        for line in stream:
            hits["known_sensitive_value_hits"] += sum(
                line.count(value) for value in encoded_values
            )
            hits["authorization_bearer_hits"] += len(
                LOG_AUTHORIZATION_BEARER_PATTERN.findall(line)
            )
            hits["password_value_hits"] += len(LOG_PASSWORD_VALUE_PATTERN.findall(line))
            hits["token_value_hits"] += len(LOG_TOKEN_VALUE_PATTERN.findall(line))
            hits["provider_virtual_key_hits"] += len(
                LOG_PROVIDER_VIRTUAL_KEY_PATTERN.findall(line)
            )
            hits["prd046_prompt_sentinel_hits"] += len(
                LOG_PRD046_PROMPT_PATTERN.findall(line)
            )
            hits["governance_body_value_hits"] += len(
                LOG_GOVERNANCE_BODY_PATTERN.findall(line)
            )
            hits["traceback_hits"] += len(LOG_TRACEBACK_PATTERN.findall(line))
            hits["error_level_hits"] += len(LOG_ERROR_LEVEL_PATTERN.findall(line))
            hits["http_5xx_hits"] += len(LOG_HTTP_5XX_PATTERN.findall(line))
    return hits


def parse_args() -> argparse.Namespace:
    """解析非敏感服务、数据库和日志路径；密码/Token 禁止通过 CLI 传入。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--control-url", default="http://127.0.0.1:8001")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    parser.add_argument("--console-url", default="http://127.0.0.1:5173/")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument(
        "--gateway-log-file",
        action="append",
        type=Path,
        default=[],
        help="可重复提供 Gateway 日志；至少一份必须非空",
    )
    parser.add_argument(
        "--control-log-file",
        action="append",
        type=Path,
        default=[],
        help="可重复提供 Control API 日志；至少一份必须非空",
    )
    parser.add_argument(
        "--console-log-file",
        action="append",
        type=Path,
        default=[],
        help="可重复提供 Console 日志；至少一份必须非空",
    )
    parser.add_argument(
        "--log-file",
        action="append",
        type=Path,
        default=[],
        help="可重复提供额外日志；不能替代三项服务各自的必需日志",
    )
    parser.add_argument("--confirm-local-state-mutation", action="store_true")
    return parser.parse_args()


def main() -> int:
    """执行验收并始终输出结构化 JSON；任何失败都返回非零退出码。"""

    if sys.flags.optimize:
        print(
            json.dumps(
                {"status": "failed", "error_type": "OptimizedPythonRejected"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    acceptance: RuntimeAcceptance | None = None
    result: dict[str, Any] | None = None
    cleanup: dict[str, Any] = {}
    privacy: dict[str, Any] = {
        "status": "not_run",
        "scope_validated": False,
    }
    failure: Exception | None = None
    try:
        acceptance = RuntimeAcceptance(parse_args())
        with RunningWebhookReceiver() as receiver:
            result = acceptance.run(receiver)
    # 只收敛可诊断运行异常；argparse --help/SystemExit 与 Ctrl-C 保留标准 CLI 语义。
    except Exception as exc:  # noqa: BLE001
        failure = exc
    finally:
        if acceptance is not None:
            try:
                cleanup = acceptance.cleanup_resources()
            except Exception as exc:  # noqa: BLE001
                cleanup = {
                    "errors": [f"cleanup_unhandled:{type(exc).__name__}"],
                }
                if failure is None:
                    failure = exc
            # 主流程失败也必须在已验证本地 scope 上独立扫描；扫描异常只输出固定类型。
            if acceptance.scope_validated:
                try:
                    privacy = {
                        "status": "passed",
                        "scope_validated": True,
                        **acceptance.verify_privacy(),
                    }
                except Exception as exc:  # noqa: BLE001
                    privacy = {
                        "status": "failed",
                        "scope_validated": True,
                        "error_type": type(exc).__name__,
                    }
                    if failure is None:
                        failure = exc
            try:
                acceptance.close()
            except Exception as exc:  # noqa: BLE001
                # 连接池释放异常也只能进入固定类型摘要，不能绕过最终结构化失败输出。
                cleanup.setdefault("errors", []).append(
                    f"client_close:{type(exc).__name__}"
                )
                if failure is None:
                    failure = exc
    if failure is not None:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(failure).__name__,
                    "cleanup": cleanup,
                    "privacy": privacy,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    assert result is not None
    result["cleanup"] = cleanup
    result["privacy"] = privacy
    # 清理失败必须把整次验收降级为失败，不能留下临时权限主体后仍报告 passed。
    if cleanup.get("errors"):
        result["status"] = "failed"
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
