"""PRD-049 调用反馈与采纳率记录的真实三服务运行验收。

脚本只接受 ``artifacts/prd049/<batch>`` 内已经完成 0039 迁移的独立 SQLite
数据库，并连接调用方已经启动的 Gateway、Control API 与 Console。它复用 PRD-048
验收脚本中经过测试的本地 Provider 和基础资源装配，但反馈 API、权限、防枚举、幂等、
Webhook、统计隔离与数据库证据均在本文件独立断言。

密码、Session Token、Virtual Key、Webhook Secret、Prompt 和 Response 只存在于内存；
成功或异常退出都会撤销本批次已登记的 Session、临时用户、Key 和 Webhook。状态文件只
保存稳定 ID、计数和布尔结论，不保存任何凭据或业务正文。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

# PRD-048 已覆盖本地 Provider、统一 wire、用户/模型/Key 装配和 HMAC 接收器；复用这些
# 基础设施可以让本脚本聚焦 PRD-049 合同，同时避免两套安全清理和诊断逻辑发生漂移。
_BASE_PATH = Path(__file__).with_name("prd048-runtime-acceptance.py")
_BASE_SPEC = importlib.util.spec_from_file_location("prd049_prd048_base", _BASE_PATH)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
# 基础模块只提供验收夹具能力，不会执行其命令行入口或 PRD-048 验收流程。
_BASE = importlib.util.module_from_spec(_BASE_SPEC)
sys.modules[_BASE_SPEC.name] = _BASE
_BASE_SPEC.loader.exec_module(_BASE)

# 真实 HTTP 请求和异步投递必须有硬超时，避免服务故障时验收无限等待。
HTTP_TIMEOUT = httpx.Timeout(20.0)
EVENT_WAIT_SECONDS = 12.0
# 这些字段属于 PRD-048 路由质量事实，逐请求反馈写入不得修改其值或版本。
PRD048_ISOLATED_FIELDS = (
    "sample_count",
    "success_count",
    "error_count",
    "latency_sample_count",
    "total_latency_ms",
    "manual_quality_bps",
    "statistics_version",
    "feedback_version",
    "formula_version",
)

# 0039 将 PRD-049 三类业务事实和已反馈父记录的冲突写入统一收紧为数据库拒绝；
# Runtime 必须核对精确名称，避免只凭 schema_migrations 版本号误判约束已经生效。
PRD049_HARDENING_TRIGGERS = frozenset(
    {
        "trg_request_feedback_conflict_insert_prd049",
        "trg_request_feedback_event_conflict_insert_prd049",
        "trg_request_log_feedback_parent_conflict_insert_prd049",
        "trg_request_log_feedback_parent_conflict_update_prd049",
        "trg_routing_attempt_feedback_parent_conflict_insert_prd049",
        "trg_routing_attempt_feedback_parent_conflict_update_prd049",
    }
)


def _utc_window() -> tuple[str, str]:
    """返回覆盖本次验收的 canonical UTC 闭开窗口。

    返回值为秒精度 ``from_time`` 和 ``to_time``；窗口向前后各扩一分钟，避免数据库
    创建时间恰好位于 API 闭开边界时产生偶发漏计。
    """

    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(minutes=1)
    end = now + timedelta(minutes=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _load_base() -> Any:
    """返回已加载的 PRD-048 基础模块，供轻量单测核对复用边界。"""

    return _BASE


# 运行编排对象集中管理内存凭据和资源账本，确保任一异常都能从同一 finally 收口。
class RuntimeAcceptance:
    """编排 PRD-049 真实接口、事件投递、数据库证据与敏感信息隐私验收。"""

    def __init__(self, args: argparse.Namespace) -> None:
        """初始化客户端、基础装配器和只驻留内存的敏感值。

        参数 ``args`` 已通过回环 URL、独立数据库和同批次日志路径校验。构造函数不写
        数据库；资源只有在 ``run`` 中真实 API 成功返回后才逐项登记，供 finally 清理。
        """

        self.args = args
        self.base = _BASE.RuntimeAcceptance(args)
        self.control = self.base.control
        self.gateway = self.base.gateway
        self.suffix = args.suffix
        self.resource_ids = self.base.resource_ids
        self.virtual_keys: list[str] = []
        self.log_paths: tuple[Path, ...] = tuple(args.log_path)

    def close(self) -> None:
        """关闭 HTTP 连接池并主动清空所有长生命周期凭据引用。"""

        self.virtual_keys.clear()
        self.base.close()

    def sensitive_values(self) -> list[str]:
        """返回状态、SQLite 与日志中都禁止出现的本批次敏感明文。"""

        return [value for value in [*self.base.sensitive_values(), *self.virtual_keys] if value]

    def run(self, receiver: Any) -> dict[str, Any]:
        """执行完整验收并返回只含脱敏事实的状态对象。

        任一断言失败都会冒泡给调用方，``execute_runtime`` 的 finally 再执行凭据清理；
        本函数绝不把局部成功写成 passed，也不输出 HTTP 正文或异常中的敏感字段。
        """

        database_ready = self._verify_database_ready()
        self.base._verify_service_health()
        method_not_allowed = self._verify_feedback_method_not_allowed()
        self.base.admin_token = self.base._login(
            self.args.admin_email,
            self.args.admin_password,
            role="admin",
        )
        self.base._current_identity()
        environment = self.base._default_environment()
        self.base.role_tokens = self.base._create_role_users()
        route = self.base._create_route(environment, receiver)
        key_one = self._create_virtual_key(environment["environment_id"], "one")
        key_two = self._create_virtual_key(environment["environment_id"], "two")
        self._create_webhook(receiver)

        session_source = self._gateway_call(route, key_one, "session_source")
        key_source = self._gateway_call(route, key_one, "key_source")
        before_quality = self._model_quality(route["model_a_id"], "quality_before")

        role_checks = self._verify_roles(session_source)
        session_result = self._verify_session_feedback(session_source)
        virtual_result = self._verify_virtual_key_feedback(key_source, key_one, key_two)
        events = self._wait_feedback_events(receiver, session_source, key_source)
        summary = self._verify_summary(route["model_a_id"])
        surfaces = self._verify_read_surfaces(
            session_source,
            key_source,
            route["model_a_id"],
            before_quality,
        )
        database = self._verify_database(
            route["model_a_id"],
            session_result["request_feedback_id"],
            virtual_result["request_feedback_id"],
        )
        privacy = self._verify_privacy()

        return {
            "status": "passed",
            "prd_id": "PRD-049",
            "suffix": self.suffix,
            "database_file": self.args.database_path.name,
            "resource_ids": {
                key: value
                for key, value in self.resource_ids.items()
                if key
                in {
                    "main_tenant_id",
                    "admin_user_id",
                    "developer_user_id",
                    "viewer_user_id",
                    "provider_a_id",
                    "provider_b_id",
                    "model_a_id",
                    "model_b_id",
                    "alias_id",
                    "routing_policy_id",
                    "virtual_key_one_id",
                    "virtual_key_two_id",
                    "webhook_endpoint_id",
                }
            },
            "source_request_ids": [session_source, key_source],
            "checks": {
                **database_ready,
                **method_not_allowed,
                **role_checks,
                **events,
                **summary,
                **surfaces,
                **database,
                **privacy,
                "session_idempotent_replay": True,
                "session_request_conflict": True,
                "virtual_key_same_key_create": True,
                "virtual_key_cross_key_hidden": True,
                "unknown_request_hidden": True,
            },
        }

    def _verify_database_ready(self) -> dict[str, int | bool]:
        """确认独立库至少为 0039，且反馈核心表和六项冲突保护 trigger 均真实存在。"""

        with closing(self._connect()) as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            tables = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name IN (
                      'request_feedbacks', 'model_feedback_statistics',
                      'request_feedback_events', 'webhook_deliveries'
                    )
                    """
                )
            }
            triggers = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='trigger' AND name IN (
                      'trg_request_feedback_conflict_insert_prd049',
                      'trg_request_feedback_event_conflict_insert_prd049',
                      'trg_request_log_feedback_parent_conflict_insert_prd049',
                      'trg_request_log_feedback_parent_conflict_update_prd049',
                      'trg_routing_attempt_feedback_parent_conflict_insert_prd049',
                      'trg_routing_attempt_feedback_parent_conflict_update_prd049'
                    )
                    """
                )
            }
        assert int(version or 0) >= 39, "database migration 0039 has not been applied"
        assert tables == {
            "request_feedbacks",
            "model_feedback_statistics",
            "request_feedback_events",
            "webhook_deliveries",
        }
        assert len(triggers) == 6, "database migration 0039 hardening trigger count is invalid"
        assert triggers == PRD049_HARDENING_TRIGGERS
        return {
            "migration_0039_applied": True,
            "migration_0039_trigger_count": len(triggers),
        }

    def _verify_feedback_method_not_allowed(self) -> dict[str, bool]:
        """真实探测 Control/Gateway 的 Feedback 方法拒绝 wire，并确认两服务完全同构。"""

        evidence: list[tuple[int, str, str, str, str, str]] = []
        for service, client in (("control", self.control), ("gateway", self.gateway)):
            request_id = f"req_prd049_runtime_{service}_method_not_allowed_{self.suffix}"
            trace_id = request_id.replace("req_", "trc_", 1)
            response = client.get(
                "/feedback",
                headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
            )
            body = _BASE.expect_json(response, 405, request_id, trace_id)
            assert response.headers.get("X-Request-ID") == request_id
            assert response.headers.get("X-Trace-ID") == trace_id
            assert response.headers.get("Allow") == "POST"
            assert response.headers.get("Cache-Control") == "no-store, private"
            evidence.append(
                (
                    response.status_code,
                    str(body.get("code")),
                    str(body.get("message")),
                    str(body.get("suggestion")),
                    str(response.headers.get("Allow")),
                    str(response.headers.get("Cache-Control")),
                )
            )
        # 链路 ID 按请求不同，不进入同构比较；其余 wire 字段必须逐项一致且使用统一错误码。
        assert evidence[0] == evidence[1]
        assert evidence[0][1:4] == (
            "infra.method_not_allowed",
            "Request method is not allowed.",
            "Use a method listed in the Allow response header.",
        )
        return {
            "control_feedback_method_not_allowed": True,
            "gateway_feedback_method_not_allowed": True,
            "feedback_method_not_allowed_parity": True,
        }

    def _create_virtual_key(self, environment_id: str, label: str) -> str:
        """通过 Control API 创建一把临时 Key，明文仅保存在当前进程内存。"""

        operation_id = f"req_prd049_runtime_key_{label}_{self.suffix}"
        body = _BASE.expect_json(
            self.control.post(
                "/virtual-keys",
                headers=self.base._headers(self.base.admin_token, operation_id),
                json={
                    "name": f"PRD049 runtime {label} {self.suffix}",
                    "environment_id": environment_id,
                    "description": "PRD-049 isolated runtime acceptance",
                },
            ),
            201,
            operation_id,
        )
        key_info = body.get("virtual_key")
        assert isinstance(key_info, dict)
        key_id = str(key_info.get("virtual_key_id", ""))
        plain_value = str(body.get("plain_value", ""))
        assert key_id and plain_value.startswith("mhv_")
        self.resource_ids[f"virtual_key_{label}_id"] = key_id
        self.virtual_keys.append(plain_value)
        return plain_value

    # Webhook 必须在反馈创建前注册，才能同时观察 Control 同步投递和 Gateway 队列投递。
    def _create_webhook(self, receiver: Any) -> None:
        """创建只订阅 ``feedback.created`` 的本地签名事件接收端并登记清理标识。"""

        host, port = receiver.server_address
        operation_id = f"req_prd049_runtime_webhook_{self.suffix}"
        body = _BASE.expect_json(
            self.control.post(
                "/webhooks",
                headers=self.base._headers(self.base.admin_token, operation_id),
                json={
                    "name": f"PRD049 runtime {self.suffix}",
                    "endpoint_url": f"http://{host}:{port}/hook",
                    "event_types": ["feedback.created"],
                    "timeout_ms": 2000,
                    "status": "active",
                },
            ),
            200,
            operation_id,
        )
        webhook = body.get("webhook")
        assert isinstance(webhook, dict)
        self.resource_ids["webhook_endpoint_id"] = str(webhook["webhook_endpoint_id"])
        secret = str(body.get("signing_secret", ""))
        assert secret
        self.base.webhook_secret = secret
        receiver.signing_secret = secret

    def _gateway_call(self, route: dict[str, Any], key: str, label: str) -> str:
        """使用指定 Key 发起真实非流式模型调用，并只返回源 request_id。"""

        source_request_id = f"req_prd049_runtime_{label}_{self.suffix}"
        self.base.virtual_key = key
        response = self.base._gateway_chat(route, source_request_id)
        body = _BASE.expect_json(
            response,
            200,
            source_request_id,
            source_request_id.replace("req_", "trc_", 1),
        )
        # 正文只在当前栈帧确认上游真实执行，绝不进入状态或数据库验收输出。
        assert body.get("choices", [{}])[0].get("message", {}).get("content") == "selected-a"
        return source_request_id

    def _feedback_post(
        self,
        client: httpx.Client,
        authorization: str,
        operation_id: str,
        source_request_id: str,
        *,
        rating: int = 5,
        accepted: bool = True,
        tags: list[str] | None = None,
    ) -> httpx.Response:
        """发送严格反馈 JSON；调用方负责断言角色、状态和统一错误码。"""

        return client.post(
            "/feedback",
            headers={
                "Authorization": f"Bearer {authorization}",
                "X-Request-ID": operation_id,
                "X-Trace-ID": operation_id.replace("req_", "trc_", 1),
            },
            json={
                "request_id": source_request_id,
                "rating": rating,
                "accepted": accepted,
                "tags": tags or ["accurate", "helpful"],
            },
        )

    def _verify_roles(self, source_request_id: str) -> dict[str, bool]:
        """证明 admin/developer 可读汇总，而 viewer 在解析反馈正文前即被后端拒绝。"""

        for role, token in (
            ("admin", self.base.admin_token),
            ("developer", self.base.role_tokens["developer"]),
        ):
            operation_id = f"req_prd049_runtime_summary_{role}_{self.suffix}"
            response = self.control.get(
                "/feedback/summary",
                headers=self.base._headers(token, operation_id),
            )
            _BASE.expect_json(response, 200, operation_id)
        viewer_operation = f"req_prd049_runtime_viewer_denied_{self.suffix}"
        response = self.control.post(
            "/feedback",
            headers=self.base._headers(
                self.base.role_tokens["viewer"], viewer_operation
            ),
            # 故意发送非法正文；403 证明 RBAC 先于 body 解析，而不是前端隐藏按钮。
            content=b"not-json",
        )
        denied = _BASE.expect_json(response, 403, viewer_operation)
        assert str(denied.get("code", "")).startswith("auth.")
        viewer_summary_operation = f"req_prd049_runtime_viewer_summary_{self.suffix}"
        viewer_summary = _BASE.expect_json(
            self.control.get(
                "/feedback/summary",
                headers=self.base._headers(
                    self.base.role_tokens["viewer"], viewer_summary_operation
                ),
            ),
            403,
            viewer_summary_operation,
        )
        assert str(viewer_summary.get("code", "")).startswith("auth.")
        gateway_summary_operation = f"req_prd049_runtime_gateway_summary_{self.suffix}"
        gateway_summary = self.gateway.get(
            "/feedback/summary",
            headers={
                "Authorization": f"Bearer {self.virtual_keys[0]}",
                "X-Request-ID": gateway_summary_operation,
            },
        )
        # 数据面故意不注册汇总路由，固定 404 证明聚合扫描不会进入模型热路径。
        _BASE.expect_json(gateway_summary, 404, gateway_summary_operation)
        return {
            "admin_summary_read": True,
            "developer_summary_read": True,
            "viewer_feedback_denied_before_body": True,
            "viewer_summary_denied": True,
            "gateway_summary_absent": True,
        }

    def _verify_session_feedback(self, source_request_id: str) -> dict[str, str]:
        """验证 Session 首建、同意图重放、冲突和未知 ID 防枚举。"""

        operation_id = f"req_prd049_runtime_session_feedback_{self.suffix}"
        first = _BASE.expect_json(
            self._feedback_post(
                self.control,
                self.base.role_tokens["developer"],
                operation_id,
                source_request_id,
            ),
            201,
            operation_id,
            operation_id.replace("req_", "trc_", 1),
        )
        replay = _BASE.expect_json(
            self._feedback_post(
                self.control,
                self.base.role_tokens["developer"],
                operation_id,
                source_request_id,
            ),
            200,
            operation_id,
        )
        assert first["idempotent_replay"] is False
        assert replay["idempotent_replay"] is True
        assert first["feedback"] == replay["feedback"]
        conflict = _BASE.expect_json(
            self._feedback_post(
                self.control,
                self.base.role_tokens["developer"],
                operation_id,
                source_request_id,
                rating=4,
            ),
            409,
            operation_id,
        )
        assert conflict.get("code") == "feedback.request_conflict"
        unknown_operation = f"req_prd049_runtime_unknown_{self.suffix}"
        unknown = _BASE.expect_json(
            self._feedback_post(
                self.control,
                self.base.role_tokens["developer"],
                unknown_operation,
                f"req_prd049_unknown_{self.suffix}",
            ),
            404,
            unknown_operation,
        )
        assert unknown.get("code") == "feedback.request_not_found"
        feedback = first.get("feedback")
        assert isinstance(feedback, dict) and feedback.get("actor_type") == "session_user"
        return {"request_feedback_id": str(feedback["request_feedback_id"])}

    def _verify_virtual_key_feedback(
        self,
        source_request_id: str,
        key_one: str,
        key_two: str,
    ) -> dict[str, str]:
        """证明仅产生调用的同一 Key 可反馈，另一把同租户 Key 得到统一 404。"""

        cross_operation = f"req_prd049_runtime_cross_key_{self.suffix}"
        hidden = _BASE.expect_json(
            self._feedback_post(
                self.gateway,
                key_two,
                cross_operation,
                source_request_id,
                rating=3,
                accepted=False,
                tags=["incomplete"],
            ),
            404,
            cross_operation,
        )
        assert hidden.get("code") == "feedback.request_not_found"
        create_operation = f"req_prd049_runtime_key_feedback_{self.suffix}"
        created = _BASE.expect_json(
            self._feedback_post(
                self.gateway,
                key_one,
                create_operation,
                source_request_id,
                rating=4,
                accepted=False,
                tags=["relevant", "clear"],
            ),
            201,
            create_operation,
            create_operation.replace("req_", "trc_", 1),
        )
        feedback = created.get("feedback")
        assert isinstance(feedback, dict) and feedback.get("actor_type") == "virtual_key"
        assert feedback.get("tags") == ["relevant", "clear"]
        return {"request_feedback_id": str(feedback["request_feedback_id"])}

    def _wait_feedback_events(
        self,
        receiver: Any,
        session_source: str,
        key_source: str,
    ) -> dict[str, Any]:
        """有界等待两个 ``feedback.created`` HMAC 投递并核对精确安全字段。"""

        expected = {session_source, key_source}
        deadline = time.monotonic() + EVENT_WAIT_SECONDS
        selected: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            selected = [
                event
                for event in receiver.webhook_events()
                if event.get("event_type") == "feedback.created"
                and event.get("request_id") in expected
            ]
            # 两个源调用的事件都到齐才退出；只收到一个不能证明双入口统一事件合同。
            if {event.get("request_id") for event in selected} == expected:
                break
            time.sleep(0.1)
        assert {event.get("request_id") for event in selected} == expected
        assert all(event.get("signature_valid") is True for event in selected)
        assert all(event.get("privacy_ok") is True for event in selected)
        assert all(str(event.get("event_id", "")).startswith("fbe_") for event in selected)
        assert all(str(event.get("object_id", "")).startswith("rfb_") for event in selected)
        expected_keys = {
            "accepted",
            "actor_type",
            "model_id",
            "rating",
            "request_feedback_id",
            "request_id",
            "tags",
        }
        assert all(set(event.get("payload_keys", [])) == expected_keys for event in selected)
        return {
            "webhook_feedback_created_count": len(selected),
            "webhook_hmac_valid": True,
            "webhook_payload_privacy": True,
        }

    def _verify_summary(self, model_id: str) -> dict[str, Any]:
        """核对确定窗口内的总量、评分、采纳率、标签与模型分页统计。"""

        start, end = _utc_window()
        operation_id = f"req_prd049_runtime_summary_final_{self.suffix}"
        response = self.control.get(
            "/feedback/summary",
            params={
                "model_id": model_id,
                "from_time": start,
                "to_time": end,
                "limit": "20",
                "offset": "0",
            },
            headers=self.base._headers(self.base.role_tokens["developer"], operation_id),
        )
        body = _BASE.expect_json(response, 200, operation_id)
        summary = body.get("summary")
        models = body.get("models")
        assert isinstance(summary, dict) and isinstance(models, list) and len(models) == 1
        assert summary["feedback_count"] == 2
        assert summary["rating_sum"] == 9
        assert summary["average_rating_milli"] == 4500
        assert summary["accepted_count"] == 1
        assert summary["adoption_rate_bps"] == 5000
        assert summary["rating_distribution"] == {
            "1": 0,
            "2": 0,
            "3": 0,
            "4": 1,
            "5": 1,
        }
        tags = {item["tag"]: item["count"] for item in summary["tag_counts"]}
        assert tags["accurate"] == tags["helpful"] == tags["relevant"] == tags["clear"] == 1
        assert models[0]["model_id"] == model_id and models[0]["feedback_count"] == 2
        return {
            "summary_feedback_count": 2,
            "summary_average_rating_milli": 4500,
            "summary_adoption_rate_bps": 5000,
        }

    def _model_quality(self, model_id: str, label: str) -> dict[str, Any]:
        """读取目标模型质量行并拒绝任何隐式跨租户或正文数据。"""

        operation_id = f"req_prd049_runtime_{label}_{self.suffix}"
        body = _BASE.expect_json(
            self.control.get(
                "/model-quality-scores",
                params={"model_id": model_id},
                headers=self.base._headers(
                    self.base.role_tokens["developer"], operation_id
                ),
            ),
            200,
            operation_id,
        )
        rows = body.get("model_quality_scores")
        assert isinstance(rows, list) and len(rows) == 1
        return rows[0]

    def _verify_read_surfaces(
        self,
        session_source: str,
        key_source: str,
        model_id: str,
        before_quality: dict[str, Any],
    ) -> dict[str, Any]:
        """核对 RequestLog 资格/详情、ModelQuality 聚合及 PRD-048 字段隔离。"""

        session_detail = self.base._request_log_detail(session_source, label="session_feedback")
        key_detail = self.base._request_log_detail(key_source, label="key_feedback")
        for detail, expected_actor in (
            (session_detail, "session_user"),
            (key_detail, "virtual_key"),
        ):
            assert detail["feedback_eligible"] is False
            assert detail["feedback_ineligible_reason"] == "already_recorded"
            feedback = detail.get("feedback")
            assert isinstance(feedback, dict) and feedback["actor_type"] == expected_actor
            assert detail["log"]["has_feedback"] is True
        after_quality = self._model_quality(model_id, "quality_after")
        assert all(
            after_quality[field] == before_quality[field]
            for field in PRD048_ISOLATED_FIELDS
        )
        assert after_quality["request_feedback_count"] == 2
        assert after_quality["request_feedback_rating_sum"] == 9
        assert after_quality["request_feedback_average_milli"] == 4500
        assert after_quality["request_feedback_accepted_count"] == 1
        assert after_quality["request_feedback_adoption_rate_bps"] == 5000
        assert after_quality["request_feedback_statistics_version"] == 2
        return {
            "request_log_feedback_join": True,
            "request_log_eligibility_locked": True,
            "model_quality_feedback_count": 2,
            "prd048_statistics_isolated": True,
        }

    # API 断言之后再从独立库核对持久图，避免只凭响应误判事务、聚合或投递已经落盘。
    def _verify_database(
        self,
        model_id: str,
        session_feedback_id: str,
        key_feedback_id: str,
    ) -> dict[str, Any]:
        """核对反馈事实、派生聚合、持久事件、投递图、外键和数据库整体完整性。"""

        tenant_id = self.resource_ids["main_tenant_id"]
        with closing(self._connect()) as connection:
            feedbacks = connection.execute(
                """
                SELECT request_feedback_id, actor_type, rating, accepted, tags_json,
                       operation_request_id, feedback_event_id
                FROM request_feedbacks
                WHERE tenant_id=? AND request_feedback_id IN (?, ?)
                ORDER BY request_feedback_id
                """,
                (tenant_id, session_feedback_id, key_feedback_id),
            ).fetchall()
            assert len(feedbacks) == 2
            assert {row["actor_type"] for row in feedbacks} == {"session_user", "virtual_key"}
            events = connection.execute(
                """
                SELECT event_type, request_feedback_id, source_request_id, payload_json
                FROM request_feedback_events
                WHERE tenant_id=? AND request_feedback_id IN (?, ?)
                """,
                (tenant_id, session_feedback_id, key_feedback_id),
            ).fetchall()
            assert len(events) == 2 and all(
                row["event_type"] == "feedback.created" for row in events
            )
            for event in events:
                payload = json.loads(event["payload_json"])
                assert set(payload) == {
                    "request_feedback_id",
                    "request_id",
                    "model_id",
                    "actor_type",
                    "rating",
                    "accepted",
                    "tags",
                }
            aggregate = connection.execute(
                """
                SELECT feedback_count, rating_sum, accepted_count,
                       feedback_statistics_version
                FROM model_feedback_statistics
                WHERE tenant_id=? AND model_id=?
                """,
                (tenant_id, model_id),
            ).fetchone()
            assert aggregate is not None and tuple(aggregate) == (2, 9, 1, 2)
            delivery_count = connection.execute(
                """
                SELECT COUNT(*) FROM webhook_deliveries
                WHERE tenant_id=? AND event_type='feedback.created'
                  AND event_id IN (
                    SELECT feedback_event_id FROM request_feedback_events
                    WHERE tenant_id=? AND request_feedback_id IN (?, ?)
                  )
                """,
                (tenant_id, tenant_id, session_feedback_id, key_feedback_id),
            ).fetchone()[0]
            foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
        assert delivery_count >= 2
        assert foreign_key_issues == []
        # row_factory 会返回 sqlite3.Row，显式转 tuple 后再核对唯一 ``ok`` 结果。
        assert [tuple(row) for row in integrity] == [("ok",)]
        return {
            "database_feedback_count": 2,
            "database_event_count": 2,
            "database_delivery_count": int(delivery_count),
            "database_aggregate_exact": True,
            "foreign_key_check_clean": True,
            "integrity_check_ok": True,
        }

    def _verify_privacy(self) -> dict[str, int]:
        """扫描独立 SQLite 全表和显式服务日志，证明本轮敏感明文未落盘。"""

        sensitive = self.sensitive_values()
        database_hits = 0
        with closing(self._connect()) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                columns = [str(row[1]) for row in connection.execute(
                    f"PRAGMA table_info({_BASE.quote_identifier(table)})"
                )]
                if not columns:
                    continue
                predicate = " OR ".join(
                    f"instr(CAST({_BASE.quote_identifier(column)} AS TEXT), ?) > 0"
                    for column in columns
                )
                query = f"SELECT COUNT(*) FROM {_BASE.quote_identifier(table)} WHERE {predicate}"
                for value in sensitive:
                    database_hits += int(connection.execute(
                        query, tuple(value for _ in columns)
                    ).fetchone()[0])
        log_hits = 0
        for path in self.log_paths:
            raw = path.read_bytes()
            for value in sensitive:
                log_hits += raw.count(value.encode("utf-8"))
        assert database_hits == 0
        assert log_hits == 0
        return {
            "database_sensitive_hits": database_hits,
            "log_sensitive_hits": log_hits,
            "log_files_scanned": len(self.log_paths),
        }

    def cleanup_credentials(self) -> dict[str, Any]:
        """按 tenant 和稳定 ID 撤销本批次凭据，允许失败路径只有部分资源账本。

        Feedback/Event 保留在独立验收库作为验收证据；清理只撤销 Session、临时用户、
        Virtual Key 与 Webhook，且重复执行得到零额外变化。
        """

        tenant_id = self.resource_ids.get("main_tenant_id")
        # 资源尚未创建或数据库已不存在时没有可撤销对象，失败清理应保持幂等成功。
        if not tenant_id or not self.args.database_path.is_file():
            return {"verified": True, "changed": 0}
        changed = 0
        now = _BASE.utc_now()
        with closing(self._connect()) as connection, connection:
            for role in ("admin", "developer", "viewer"):
                session_id = self.resource_ids.get(f"{role}_session_id")
                # 只撤销本次登录返回并已登记的精确 Session，不影响同账号其他会话。
                if session_id:
                    changed += connection.execute(
                        """
                        UPDATE sessions SET status='revoked', revoked_at=?, updated_at=?
                        WHERE tenant_id=? AND session_id=? AND status='active'
                        """,
                        (now, now, tenant_id, session_id),
                    ).rowcount
            for role in ("developer", "viewer"):
                user_id = self.resource_ids.get(f"{role}_user_id")
                if user_id:
                    changed += connection.execute(
                        """
                        UPDATE users SET status='disabled', updated_at=?
                        WHERE tenant_id=? AND user_id=? AND status='active'
                        """,
                        (now, tenant_id, user_id),
                    ).rowcount
            for label in ("one", "two"):
                key_id = self.resource_ids.get(f"virtual_key_{label}_id")
                if key_id:
                    changed += connection.execute(
                        """
                        UPDATE virtual_keys SET status='disabled', disabled_at=?, updated_at=?
                        WHERE tenant_id=? AND virtual_key_id=? AND status='active'
                        """,
                        (now, now, tenant_id, key_id),
                    ).rowcount
            webhook_id = self.resource_ids.get("webhook_endpoint_id")
            if webhook_id:
                changed += connection.execute(
                    """
                    UPDATE webhook_endpoints SET status='disabled', updated_at=?
                    WHERE tenant_id=? AND webhook_endpoint_id=? AND status='active'
                    """,
                    (now, tenant_id, webhook_id),
                ).rowcount
            active = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sessions WHERE tenant_id=? AND session_id IN (?, ?, ?)
                    AND status='active')
                  + (SELECT COUNT(*) FROM virtual_keys
                    WHERE tenant_id=? AND virtual_key_id IN (?, ?)
                    AND status='active')
                  + (SELECT COUNT(*) FROM webhook_endpoints WHERE tenant_id=?
                    AND webhook_endpoint_id=? AND status='active')
                """,
                (
                    tenant_id,
                    self.resource_ids.get("admin_session_id", ""),
                    self.resource_ids.get("developer_session_id", ""),
                    self.resource_ids.get("viewer_session_id", ""),
                    tenant_id,
                    self.resource_ids.get("virtual_key_one_id", ""),
                    self.resource_ids.get("virtual_key_two_id", ""),
                    tenant_id,
                    self.resource_ids.get("webhook_endpoint_id", ""),
                ),
            ).fetchone()[0]
        assert active == 0
        return {"verified": True, "changed": changed}

    def _connect(self) -> sqlite3.Connection:
        """连接显式独立验收库并始终启用 SQLite 外键约束。"""

        connection = sqlite3.connect(self.args.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def execute_runtime(
    args: argparse.Namespace,
    *,
    acceptance_factory: Any = RuntimeAcceptance,
    server_factory: Any = _BASE.RunningAcceptanceServer,
) -> dict[str, Any]:
    """运行验收、原子写入脱敏状态，并在所有路径撤销本批次凭据。"""

    args.state_output.unlink(missing_ok=True)
    acceptance = acceptance_factory(args)
    cleanup: dict[str, Any] = {"verified": False, "changed": 0}
    try:
        with server_factory(acceptance.base.prompt_sentinel) as receiver:
            state = acceptance.run(receiver)
        assert state.get("status") == "passed"
        _BASE.assert_redacted_state(state, acceptance.sensitive_values())
        return state
    finally:
        try:
            cleanup = acceptance.cleanup_credentials()
        finally:
            acceptance.close()
        # 只有完整通过后才写状态；失败路径不留下可被误认作验收证据的局部 JSON。
        if "state" in locals() and state.get("status") == "passed":
            state["lifecycle"] = {
                "credentials_cleanup_completed": True,
                "credentials_cleanup_verified": cleanup["verified"],
                "credentials_changed": cleanup["changed"],
            }
            _BASE.assert_redacted_state(state, [])
            _BASE.write_state_atomically(args.state_output, state)


def _artifact_root() -> Path:
    """返回仓库内 PRD-049 独立验收制品根目录。"""

    return (Path.cwd().resolve() / "artifacts" / "prd049").resolve()


def _path_inside(path: Path, root: Path, *, label: str) -> Path:
    """规范化并限制文件位于 PRD-049 制品根目录下。"""

    resolved = path.resolve()
    assert root in resolved.parents, f"{label} must be inside artifacts/prd049"
    return resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析回环服务、独立数据库、状态、日志和进程内凭据参数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--control-url", default="http://127.0.0.1:18001")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:18000")
    parser.add_argument("--console-url", default="http://127.0.0.1:15173")
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--state-output", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, action="append", required=True)
    parser.add_argument(
        "--suffix",
        default=datetime.now(UTC).strftime("%Y%m%dt%H%M%S").lower()
        + secrets.token_hex(3),
    )
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    parser.add_argument(
        "--admin-password",
        default=os.getenv("METAHUB_PRD049_ADMIN_PASSWORD", ""),
    )
    parser.add_argument(
        "--developer-password",
        default=os.getenv("METAHUB_PRD049_DEVELOPER_PASSWORD", "")
        or f"Prd049Developer!{secrets.token_urlsafe(18)}",
    )
    parser.add_argument(
        "--viewer-password",
        default=os.getenv("METAHUB_PRD049_VIEWER_PASSWORD", "")
        or f"Prd049Viewer!{secrets.token_urlsafe(18)}",
    )
    args = parser.parse_args(argv)
    root = _artifact_root()
    for label in ("control_url", "gateway_url", "console_url"):
        _BASE.assert_loopback_url(getattr(args, label), label)
    args.database_path = _path_inside(args.database_path, root, label="database")
    args.state_output = _path_inside(args.state_output, root, label="state output")
    args.log_path = [
        _path_inside(path, root, label="log path") for path in args.log_path
    ]
    assert args.database_path.is_file() and args.database_path.suffix == ".db"
    assert args.state_output.suffix == ".json"
    assert args.database_path.parent == args.state_output.parent
    assert all(path.is_file() for path in args.log_path)
    assert all(path.parent == args.database_path.parent for path in args.log_path)
    assert _BASE.SAFE_SUFFIX_PATTERN.fullmatch(args.suffix)
    assert args.admin_password, "METAHUB_PRD049_ADMIN_PASSWORD or --admin-password is required"
    return args


def main(argv: list[str] | None = None) -> int:
    """执行真实验收并仅向终端输出脱敏 JSON 状态。"""

    state = execute_runtime(parse_args(argv))
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
