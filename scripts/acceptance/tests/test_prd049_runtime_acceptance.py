"""PRD-049 真实运行验收脚本的路径、生命周期、脱敏与 HMAC 轻量测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from contextlib import AbstractContextManager, closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from metahub_shared.runtime.webhooks import webhook_signature_header

# 被测脚本文件名含连字符，需要显式模块规格加载且不能触发 main 命令行分支。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd049-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd049_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块必须先登记，确保脚本内部复用模块和 traceback 都能解析稳定模块名。
RUNTIME_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME_MODULE
MODULE_SPEC.loader.exec_module(RUNTIME_MODULE)

# 测试端独立冻结六项名称，避免生产常量与测试夹具同时漏项时形成自证循环。
EXPECTED_HARDENING_TRIGGERS = (
    "trg_request_feedback_conflict_insert_prd049",
    "trg_request_feedback_event_conflict_insert_prd049",
    "trg_request_log_feedback_parent_conflict_insert_prd049",
    "trg_request_log_feedback_parent_conflict_update_prd049",
    "trg_routing_attempt_feedback_parent_conflict_insert_prd049",
    "trg_routing_attempt_feedback_parent_conflict_update_prd049",
)


class FakeReceiverContext(AbstractContextManager[object]):
    """提供不监听端口的接收器上下文，隔离生命周期单测。"""

    def __init__(self, _prompt_sentinel: str) -> None:
        """接受与真实接收器相同的 Prompt 参数，但不保存或读取该敏感值。"""

        self.receiver = object()

    def __enter__(self) -> object:
        """返回无状态接收器占位对象。"""

        return self.receiver

    def __exit__(self, *_exc_info: object) -> None:
        """无端口、线程或文件资源，因此退出无需操作。"""


class FakeAcceptance:
    """模拟成功运行，用调用账本验证状态落盘前后的清理顺序。"""

    instances: list[FakeAcceptance] = []

    def __init__(self, args: argparse.Namespace) -> None:
        """保存参数并构造满足工厂协议的最小基础对象。"""

        self.args = args
        self.base = SimpleNamespace(prompt_sentinel="unit-sensitive-prompt")
        self.cleanup_calls = 0
        self.closed = False
        self.__class__.instances.append(self)

    def run(self, _receiver: object) -> dict[str, Any]:
        """返回不含凭据的最小 PRD-049 成功状态。"""

        return {
            "status": "passed",
            "prd_id": "PRD-049",
            "suffix": "testrun01",
            "database_file": self.args.database_path.name,
            "checks": {"feedback_count": 2},
        }

    def cleanup_credentials(self) -> dict[str, Any]:
        """登记凭据撤销并返回已验证计数。"""

        self.cleanup_calls += 1
        return {"verified": True, "changed": 7}

    def sensitive_values(self) -> list[str]:
        """提供应被状态门禁拒绝的内存哨兵。"""

        return ["unit-sensitive-prompt"]

    def close(self) -> None:
        """登记敏感引用和客户端已经释放。"""

        self.closed = True


class FailingAcceptance(FakeAcceptance):
    """模拟 API 中途失败，证明失败也清理且不产生状态文件。"""

    def run(self, _receiver: object) -> dict[str, Any]:
        """抛出固定、不携带凭据或 HTTP 正文的异常。"""

        # 固定失败文本不包含凭据、路径或响应正文，便于单独验证 finally 行为。
        raise RuntimeError("fixed acceptance failure")


def _args(tmp_path: Path) -> argparse.Namespace:
    """创建 execute_runtime 生命周期单测所需同批次路径。"""

    batch = tmp_path / "artifacts" / "prd049" / "testrun01"
    batch.mkdir(parents=True)
    database = batch / "metahub-prd049.db"
    database.touch()
    return SimpleNamespace(
        database_path=database,
        state_output=batch / "runtime-state.json",
    )


def _create_hardening_probe_database(database_path: Path) -> None:
    """创建仅含 Runtime 启动门禁所需对象的 0039 探针库。"""

    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations(version) VALUES (39);
            CREATE TABLE request_feedbacks(probe_id INTEGER PRIMARY KEY);
            CREATE TABLE model_feedback_statistics(probe_id INTEGER PRIMARY KEY);
            CREATE TABLE request_feedback_events(probe_id INTEGER PRIMARY KEY);
            CREATE TABLE webhook_deliveries(probe_id INTEGER PRIMARY KEY);
            """
        )
        for trigger_name in EXPECTED_HARDENING_TRIGGERS:
            # 名称来自测试端固定白名单；逐项创建可验证两个 UPDATE 冲突守卫也不可缺失。
            connection.execute(
                f'CREATE TRIGGER "{trigger_name}" BEFORE INSERT ON request_feedbacks '
                "BEGIN SELECT 1; END"
            )
        connection.commit()


def _acceptance_for_database(database_path: Path) -> Any:
    """构造只执行数据库启动门禁的最小 RuntimeAcceptance。"""

    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(
        RUNTIME_MODULE.RuntimeAcceptance
    )
    acceptance.args = SimpleNamespace(database_path=database_path)
    return acceptance


def test_database_ready_requires_0039_and_all_six_hardening_triggers(
    tmp_path: Path,
) -> None:
    """数据库门禁必须拒绝 0038 以及任一父记录 UPDATE 冲突守卫缺失。"""

    database_path = tmp_path / "prd049-runtime-hardening.db"
    _create_hardening_probe_database(database_path)
    acceptance = _acceptance_for_database(database_path)

    assert RUNTIME_MODULE.PRD049_HARDENING_TRIGGERS == frozenset(
        EXPECTED_HARDENING_TRIGGERS
    )
    assert acceptance._verify_database_ready() == {
        "migration_0039_applied": True,
        "migration_0039_trigger_count": 6,
    }

    for trigger_name in (
        "trg_request_log_feedback_parent_conflict_update_prd049",
        "trg_routing_attempt_feedback_parent_conflict_update_prd049",
    ):
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.commit()
        with pytest.raises(AssertionError):
            acceptance._verify_database_ready()
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                f'CREATE TRIGGER "{trigger_name}" BEFORE INSERT ON request_feedbacks '
                "BEGIN SELECT 1; END"
            )
            connection.commit()

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("UPDATE schema_migrations SET version=38 WHERE version=39")
        connection.commit()
    with pytest.raises(AssertionError, match="0039"):
        acceptance._verify_database_ready()


def test_feedback_method_not_allowed_probe_requires_control_gateway_wire_parity() -> None:
    """405 探测：两个真实客户端请求相同路径，并严格核对统一错误 wire 与安全响应头。"""

    observed_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """模拟两服务的固定 405；响应只回显调用方提供的安全链路 ID。"""

        assert request.method == "GET"
        assert request.url.path == "/feedback"
        observed_hosts.append(str(request.url.host))
        request_id = str(request.headers["X-Request-ID"])
        trace_id = str(request.headers["X-Trace-ID"])
        return httpx.Response(
            405,
            headers={
                "X-Request-ID": request_id,
                "X-Trace-ID": trace_id,
                "Allow": "POST",
                "Cache-Control": "no-store, private",
            },
            json={
                "code": "infra.method_not_allowed",
                "message": "Request method is not allowed.",
                "suggestion": "Use a method listed in the Allow response header.",
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )

    transport = httpx.MockTransport(handler)
    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(
        RUNTIME_MODULE.RuntimeAcceptance
    )
    acceptance.suffix = "unitrun01"
    acceptance.control = httpx.Client(
        base_url="http://control.local",
        transport=transport,
    )
    acceptance.gateway = httpx.Client(
        base_url="http://gateway.local",
        transport=transport,
    )
    try:
        evidence = acceptance._verify_feedback_method_not_allowed()
    finally:
        # 即使断言失败也关闭两个测试连接池，保持验收器自身的资源生命周期约束。
        acceptance.control.close()
        acceptance.gateway.close()

    assert observed_hosts == ["control.local", "gateway.local"]
    assert evidence == {
        "control_feedback_method_not_allowed": True,
        "gateway_feedback_method_not_allowed": True,
        "feedback_method_not_allowed_parity": True,
    }


@pytest.mark.parametrize(
    ("drift_field", "drift_value"),
    [
        ("message", "Method contract drifted."),
        ("suggestion", "Retry with another method."),
        ("allow", "GET"),
    ],
)
def test_feedback_method_not_allowed_probe_rejects_frozen_wire_drift(
    drift_field: str,
    drift_value: str,
) -> None:
    """405 门禁必须拒绝 message、suggestion 或 Allow 任一字段漂移。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """生成单字段漂移的 405，证明验收器不会只检查状态码。"""

        request_id = str(request.headers["X-Request-ID"])
        trace_id = str(request.headers["X-Trace-ID"])
        body = {
            "code": "infra.method_not_allowed",
            "message": "Request method is not allowed.",
            "suggestion": "Use a method listed in the Allow response header.",
            "request_id": request_id,
            "trace_id": trace_id,
        }
        headers = {
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Allow": "POST",
            "Cache-Control": "no-store, private",
        }
        if drift_field == "allow":
            headers["Allow"] = drift_value
        else:
            body[drift_field] = drift_value
        return httpx.Response(405, headers=headers, json=body)

    transport = httpx.MockTransport(handler)
    acceptance = RUNTIME_MODULE.RuntimeAcceptance.__new__(
        RUNTIME_MODULE.RuntimeAcceptance
    )
    acceptance.suffix = "unitrun01"
    acceptance.control = httpx.Client(
        base_url="http://control.local",
        transport=transport,
    )
    acceptance.gateway = httpx.Client(
        base_url="http://gateway.local",
        transport=transport,
    )
    try:
        with pytest.raises(AssertionError):
            acceptance._verify_feedback_method_not_allowed()
    finally:
        acceptance.control.close()
        acceptance.gateway.close()


def test_execute_runtime_cleans_credentials_before_persisting_passed_state(
    tmp_path: Path,
) -> None:
    """成功状态必须包含已验证凭据清理事实，且敏感哨兵不得写入文件。"""

    FakeAcceptance.instances.clear()
    args = _args(tmp_path)
    state = RUNTIME_MODULE.execute_runtime(
        args,
        acceptance_factory=FakeAcceptance,
        server_factory=FakeReceiverContext,
    )

    acceptance = FakeAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1
    assert acceptance.closed is True
    assert state["lifecycle"] == {
        "credentials_cleanup_completed": True,
        "credentials_cleanup_verified": True,
        "credentials_changed": 7,
    }
    assert json.loads(args.state_output.read_text(encoding="utf-8")) == state
    assert "unit-sensitive-prompt" not in args.state_output.read_text(encoding="utf-8")


def test_execute_runtime_failure_cleans_credentials_without_state(tmp_path: Path) -> None:
    """中途失败仍撤销凭据，但绝不能留下可误认成通过证据的局部状态。"""

    FailingAcceptance.instances.clear()
    args = _args(tmp_path)
    with pytest.raises(RuntimeError, match="fixed acceptance failure"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FailingAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = FailingAcceptance.instances[-1]
    assert acceptance.cleanup_calls == 1
    assert acceptance.closed is True
    assert args.state_output.exists() is False


def test_parse_args_requires_isolated_database_state_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """数据库、状态和日志必须位于同一个 PRD-049 独立批次目录。"""

    monkeypatch.chdir(tmp_path)
    batch = tmp_path / "artifacts" / "prd049" / "testrun01"
    batch.mkdir(parents=True)
    database = batch / "metahub-prd049.db"
    log = batch / "services.log"
    database.touch()
    log.touch()
    args = RUNTIME_MODULE.parse_args(
        [
            "--database-path",
            str(database),
            "--state-output",
            str(batch / "runtime-state.json"),
            "--log-path",
            str(log),
            "--suffix",
            "testrun01",
            "--admin-password",
            "unit-admin-password",
        ]
    )
    assert args.database_path == database.resolve()
    assert args.log_path == [log.resolve()]
    assert args.control_url.endswith(":18001")
    assert args.gateway_url.endswith(":18000")
    assert args.console_url.endswith(":15173")

    outside_log = tmp_path / "outside.log"
    outside_log.touch()
    with pytest.raises(AssertionError, match="artifacts/prd049"):
        RUNTIME_MODULE.parse_args(
            [
                "--database-path",
                str(database),
                "--state-output",
                str(batch / "runtime-state.json"),
                "--log-path",
                str(outside_log),
                "--suffix",
                "testrun01",
                "--admin-password",
                "unit-admin-password",
            ]
        )


def test_feedback_created_receiver_verifies_hmac_and_keeps_only_safe_summary() -> None:
    """复用接收器能验证 feedback.created HMAC 且不保留原始信封或评分正文。"""

    base = RUNTIME_MODULE._load_base()
    secret = "whsec_prd049_unit_value"
    source_request_id = "req_prd049_unit_source"
    envelope = {
        "event_id": "fbe_0123456789abcdef0123456789abcdef",
        "event_type": "feedback.created",
        "object_id": "rfb_0123456789abcdef0123456789abcdef",
        "payload": {
            "request_feedback_id": "rfb_0123456789abcdef0123456789abcdef",
            "request_id": source_request_id,
            "model_id": "model_unit",
            "actor_type": "session_user",
            "rating": 5,
            "accepted": True,
            "tags": ["accurate", "helpful"],
        },
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    signature = webhook_signature_header(secret, int(time.time()), body)
    with base.RunningAcceptanceServer("unit-prompt-sentinel") as receiver:
        receiver.signing_secret = secret
        host, port = receiver.server_address
        response = httpx.post(
            f"http://{host}:{port}/hook",
            headers={
                "Content-Type": "application/json",
                "X-MetaHub-Signature": signature,
            },
            content=body,
            timeout=5,
        )
        captured = receiver.webhook_events()

    assert response.status_code == 204
    assert len(captured) == 1
    assert captured[0]["event_type"] == "feedback.created"
    assert captured[0]["request_id"] == source_request_id
    assert captured[0]["signature_valid"] is True
    assert captured[0]["privacy_ok"] is True
    assert set(captured[0]["payload_keys"]) == set(envelope["payload"])
    assert "rating" not in captured[0]
    assert "accepted" not in captured[0]
    assert "tags" not in captured[0]
    assert body.decode("utf-8") not in json.dumps(captured, sort_keys=True)
