"""PRD-048 真实运行验收脚本的生命周期、清理和脱敏单元测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from metahub_shared.runtime.webhooks import webhook_signature_header

# 脚本文件名包含连字符，必须用显式模块规格加载，且导入时不会执行 ``main`` 分支。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd048-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd048_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块对象先登记到 sys.modules，保证运行时类型和 traceback 能解析稳定模块名。
RUNTIME_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME_MODULE
MODULE_SPEC.loader.exec_module(RUNTIME_MODULE)


def _resource_ids() -> dict[str, str]:
    """返回覆盖完整终清理合同的稳定假 ID，不包含凭据或业务正文。"""

    return {
        "main_tenant_id": "tenant_main",
        "admin_user_id": "user_admin",
        "admin_session_id": "session_admin",
        "developer_user_id": "user_developer",
        "developer_session_id": "session_developer",
        "viewer_user_id": "user_viewer",
        "viewer_session_id": "session_viewer",
        "provider_a_id": "provider_a",
        "provider_b_id": "provider_b",
        "model_a_id": "model_a",
        "model_b_id": "model_b",
        "cross_tenant_id": "tenant_cross",
        "cross_tenant_environment_id": "env_cross",
        "cross_tenant_provider_id": "provider_cross",
        "cross_tenant_model_id": "model_cross",
        "cross_tenant_score_id": "mqs_cross",
        "alias_id": "alias_main",
        "alias_name": "prd048-runtime-testrun01",
        "routing_policy_id": "rpol_main",
        "virtual_key_id": "virtual_key_main",
        "webhook_endpoint_id": "webhook_main",
    }


def _safe_runtime_state(database_file: str = "metahub-prd048.db") -> dict[str, Any]:
    """构造 cleanup-state 可接受的最小脱敏运行状态。"""

    return {
        "status": "passed",
        "prd_id": "PRD-048",
        "suffix": "testrun01",
        "database_file": database_file,
        "resource_ids": _resource_ids(),
        "request_ids": {
            "quality_wins": "req_quality_testrun01",
            "all_low": "req_low_testrun01",
        },
        "checks": {"webhook_hmac_valid": True},
    }


class FakeReceiverContext(AbstractContextManager[object]):
    """为生命周期单测提供不启动端口的接收器上下文。"""

    def __init__(self, _prompt_sentinel: str) -> None:
        """接收与真实工厂相同的哨兵参数，但测试不读取其明文。"""

        self.receiver = object()

    def __enter__(self) -> object:
        """返回不携带状态的假接收器。"""

        return self.receiver

    def __exit__(self, *_exc_info: object) -> None:
        """假接收器没有线程或端口，无需额外释放。"""


class FakeAcceptance:
    """模拟成功 runtime，用调用账本证明默认清理和显式保留分支。"""

    instances: list[FakeAcceptance] = []

    def __init__(self, args: argparse.Namespace) -> None:
        """保存隔离参数并初始化不含真实凭据的调用账本。"""

        self.args = args
        self.prompt_sentinel = "unit-prompt-secret"
        self.cleanup_calls: list[bool] = []
        self.closed = False
        self.__class__.instances.append(self)

    def run(self, _receiver: object) -> dict[str, Any]:
        """返回与真实脚本相同的安全状态骨架。"""

        return _safe_runtime_state(self.args.database_path.name)

    def cleanup(self, *, require_complete: bool) -> dict[str, Any]:
        """登记清理模式并返回全零、已验证摘要。"""

        self.cleanup_calls.append(require_complete)
        return {
            "sessions": 0,
            "users": 0,
            "tenants": 0,
            "environments": 0,
            "aliases": 0,
            "models": 0,
            "providers": 0,
            "virtual_keys": 0,
            "webhooks": 0,
            "verified": True,
        }

    def sensitive_values(self) -> list[str]:
        """返回写盘前必须排除的假敏感值。"""

        # 假值只验证脱敏门禁，不模拟或暴露任何本地真实密码和 Token。
        return [self.prompt_sentinel, "unit-token-secret"]

    def close(self) -> None:
        """登记连接与凭据引用已经释放。"""

        self.closed = True


class FailingAcceptance(FakeAcceptance):
    """模拟真实 API 中途失败，验证 finally 使用部分资源清理。"""

    def run(self, _receiver: object) -> dict[str, Any]:
        """在状态落盘前抛出固定异常。"""

        # 固定异常不携带接收器、凭据或路径，便于断言 finally 行为。
        raise RuntimeError("fixed runtime failure")


# 生命周期测试统一使用同批次数据库/状态路径，匹配生产脚本的 sibling 约束。
def _runtime_args(tmp_path: Path, *, retain: bool) -> argparse.Namespace:
    """创建 ``execute_runtime`` 所需的隔离路径参数。

    参数：pytest 临时根目录与是否保留给浏览器。返回值：只含生命周期所需字段的
    Namespace。关键约束：数据库和状态必须处于同一 PRD-048 批次目录。
    """

    batch = tmp_path / "artifacts" / "prd048" / "testrun01"
    batch.mkdir(parents=True)
    database_path = batch / "metahub-prd048.db"
    database_path.touch()
    return SimpleNamespace(
        database_path=database_path,
        state_output=batch / "runtime-state.json",
        retain_for_browser=retain,
    )


def test_execute_runtime_defaults_to_verified_cleanup(tmp_path: Path) -> None:
    """默认成功路径必须在写状态前完整清理，状态明确记录已验证终态。"""

    FakeAcceptance.instances.clear()
    args = _runtime_args(tmp_path, retain=False)
    state = RUNTIME_MODULE.execute_runtime(
        args,
        acceptance_factory=FakeAcceptance,
        server_factory=FakeReceiverContext,
    )

    acceptance = FakeAcceptance.instances[-1]
    assert acceptance.cleanup_calls == [True]
    assert acceptance.closed is True
    assert state["lifecycle"]["retained_for_browser"] is False
    assert state["lifecycle"]["cleanup_completed"] is True
    assert state["lifecycle"]["cleanup_verified"] is True
    assert json.loads(args.state_output.read_text(encoding="utf-8")) == state


def test_execute_runtime_retains_only_after_atomic_state_success(tmp_path: Path) -> None:
    """显式浏览器保留成功时不清理资源，并写出可供后续终清理的生命周期标记。"""

    FakeAcceptance.instances.clear()
    args = _runtime_args(tmp_path, retain=True)
    state = RUNTIME_MODULE.execute_runtime(
        args,
        acceptance_factory=FakeAcceptance,
        server_factory=FakeReceiverContext,
    )

    acceptance = FakeAcceptance.instances[-1]
    assert acceptance.cleanup_calls == []
    assert acceptance.closed is True
    assert state["lifecycle"] == {
        "retained_for_browser": True,
        "cleanup_completed": False,
        "cleanup_verified": False,
    }
    assert args.state_output.is_file()


def test_execute_runtime_failure_always_uses_partial_cleanup(tmp_path: Path) -> None:
    """即使显式请求保留，状态落盘前失败也必须执行部分账本清理。"""

    FailingAcceptance.instances.clear()
    args = _runtime_args(tmp_path, retain=True)
    with pytest.raises(RuntimeError, match="fixed runtime failure"):
        RUNTIME_MODULE.execute_runtime(
            args,
            acceptance_factory=FailingAcceptance,
            server_factory=FakeReceiverContext,
        )

    acceptance = FailingAcceptance.instances[-1]
    assert acceptance.cleanup_calls == [False]
    assert acceptance.closed is True
    assert args.state_output.exists() is False


def _prepare_cross_tenant_fixture_database(path: Path) -> None:
    """建立真实第二租户夹具所需最小表和 Model->Score 原子 trigger。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tenants(
          tenant_id TEXT PRIMARY KEY, slug TEXT, name TEXT, service_mode TEXT,
          status TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE providers(
          provider_id TEXT PRIMARY KEY, tenant_id TEXT, provider_code TEXT, name TEXT,
          base_url TEXT, status TEXT, provider_type TEXT, auth_type TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE environments(
          environment_id TEXT PRIMARY KEY, tenant_id TEXT, code TEXT, status TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE models(
          model_id TEXT PRIMARY KEY, tenant_id TEXT, provider_id TEXT, model_alias TEXT,
          upstream_model_name TEXT, capabilities TEXT, status TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE model_quality_scores(
          model_quality_score_id TEXT PRIMARY KEY, tenant_id TEXT, model_id TEXT,
          status TEXT, sample_count INTEGER, success_count INTEGER, error_count INTEGER,
          latency_sample_count INTEGER, total_latency_ms INTEGER,
          manual_quality_bps INTEGER, statistics_version INTEGER, feedback_version INTEGER,
          created_at TEXT, updated_at TEXT
        );
        CREATE TRIGGER trg_models_prd048_quality_score_insert
        AFTER INSERT ON models
        FOR EACH ROW
        BEGIN
          INSERT INTO model_quality_scores(
            model_quality_score_id, tenant_id, model_id, status,
            sample_count, success_count, error_count, latency_sample_count,
            total_latency_ms, statistics_version, feedback_version,
            created_at, updated_at
          ) VALUES(
            'mqs_' || lower(hex(randomblob(16))), NEW.tenant_id, NEW.model_id, 'active',
            0, 0, 0, 0, 0, 0, 1, NEW.created_at, NEW.created_at
          );
        END;
        CREATE TRIGGER trg_tenants_default_environment
        AFTER INSERT ON tenants
        FOR EACH ROW
        BEGIN
          INSERT INTO environments VALUES(
            'env_' || lower(hex(randomblob(16))), NEW.tenant_id, 'dev', 'active',
            NEW.created_at, NEW.updated_at
          );
        END;
        INSERT INTO tenants VALUES(
          'tenant_main', 'default', 'Main', 'internal_only', 'active',
          '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        );
        """
    )
    connection.commit()
    connection.close()


def test_cross_tenant_score_fixture_and_api_requests_never_send_tenant_id(
    tmp_path: Path,
) -> None:
    """真实第二租户 Score 存在时，GET/PATCH 404 且请求不携带 tenant_id。"""

    database_path = tmp_path / "cross-tenant.db"
    _prepare_cross_tenant_fixture_database(database_path)
    args = SimpleNamespace(
        suffix="testrun01",
        database_path=database_path,
        control_url="http://control.test",
        gateway_url="http://gateway.test",
        admin_password="",
        developer_password="",
        viewer_password="",
    )
    acceptance = RUNTIME_MODULE.RuntimeAcceptance(args)
    acceptance.resource_ids["main_tenant_id"] = "tenant_main"
    fixture = acceptance._create_cross_tenant_score_fixture()
    captured: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记录安全请求形状，并用统一 404 模拟后端跨租户防枚举。"""

        captured.append((request.method, str(request.url), request.content))
        request_id = request.headers["X-Request-ID"]
        return httpx.Response(
            404,
            request=request,
            json={
                "code": "model_quality_score.not_found",
                "request_id": request_id,
                "trace_id": "trc_prd048_unit_cross_tenant",
            },
        )

    acceptance.control.close()
    acceptance.control = httpx.Client(
        base_url="http://control.test",
        transport=httpx.MockTransport(handler),
    )
    acceptance.admin_token = "unit-admin-token"
    try:
        evidence = acceptance._verify_cross_tenant_score_isolation()
    finally:
        acceptance.close()

    assert evidence == {
        "get_hidden": True,
        "patch_hidden": True,
        "score_unchanged": True,
        "tenant_input_absent": True,
    }
    assert fixture["cross_tenant_id"] != "tenant_main"
    assert [method for method, _url, _body in captured] == ["GET", "PATCH"]
    assert all("tenant_id" not in url for _method, url, _body in captured)
    patch_body = json.loads(captured[1][2])
    assert set(patch_body) == {
        "model_id",
        "manual_quality_bps",
        "expected_feedback_version",
    }
    assert patch_body["model_id"] == fixture["cross_tenant_model_id"]


def _prepare_cleanup_database(path: Path) -> None:
    """建立终清理所需最小表，并加入同 ID 跨租户行验证 owner 限定。"""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions(
          session_id TEXT, tenant_id TEXT, user_id TEXT, status TEXT,
          revoked_at TEXT, updated_at TEXT
        );
        CREATE TABLE users(
          user_id TEXT, tenant_id TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE tenants(
          tenant_id TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE providers(
          provider_id TEXT, tenant_id TEXT, status TEXT, disabled_at TEXT, updated_at TEXT
        );
        CREATE TABLE environments(
          environment_id TEXT, tenant_id TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE models(
          model_id TEXT, tenant_id TEXT, status TEXT, disabled_at TEXT, updated_at TEXT
        );
        CREATE TABLE model_aliases(
          model_alias_id TEXT, tenant_id TEXT, status TEXT, disabled_at TEXT, updated_at TEXT
        );
        CREATE TABLE virtual_keys(
          virtual_key_id TEXT, tenant_id TEXT, status TEXT, disabled_at TEXT, updated_at TEXT
        );
        CREATE TABLE webhook_endpoints(
          webhook_endpoint_id TEXT, tenant_id TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE model_quality_scores(
          model_quality_score_id TEXT, tenant_id TEXT, model_id TEXT,
          status TEXT, sample_count INTEGER, statistics_version INTEGER,
          feedback_version INTEGER, manual_quality_bps INTEGER
        );
        """
    )
    connection.executemany(
        "INSERT INTO tenants VALUES(?, 'active', 'before')",
        [("tenant_main",), ("tenant_other",), ("tenant_cross",)],
    )
    connection.executemany(
        "INSERT INTO sessions VALUES(?, 'tenant_main', ?, 'active', NULL, 'before')",
        [
            ("session_admin", "user_admin"),
            ("session_admin_other", "user_admin"),
            ("session_developer", "user_developer"),
            ("session_developer_browser", "user_developer"),
            ("session_viewer", "user_viewer"),
            ("session_viewer_browser", "user_viewer"),
        ],
    )
    connection.execute(
        "INSERT INTO sessions VALUES(?, 'tenant_other', ?, 'active', NULL, 'before')",
        ("session_other_developer", "user_developer"),
    )
    connection.executemany(
        "INSERT INTO users VALUES(?, 'tenant_main', 'active', 'before')",
        [("user_developer",), ("user_viewer",)],
    )
    connection.executemany(
        "INSERT INTO users VALUES(?, 'tenant_other', 'active', 'before')",
        [("user_developer",), ("user_viewer",)],
    )
    for table, column, values in (
        ("providers", "provider_id", ["provider_a", "provider_b"]),
        ("models", "model_id", ["model_a", "model_b"]),
        ("model_aliases", "model_alias_id", ["alias_main"]),
        ("virtual_keys", "virtual_key_id", ["virtual_key_main"]),
        ("webhook_endpoints", "webhook_endpoint_id", ["webhook_main"]),
    ):
        extra = ", NULL" if table != "webhook_endpoints" else ""
        for value in values:
            connection.execute(
                f"INSERT INTO {table}({column}, tenant_id, status, updated_at"
                + (", disabled_at" if table != "webhook_endpoints" else "")
                + ") VALUES(?, 'tenant_main', 'active', 'before'"
                + extra
                + ")",
                (value,),
            )
            # 同标识符的第二租户行证明清理必须同时绑定 tenant_id，不能只凭全局 ID。
            connection.execute(
                f"INSERT INTO {table}({column}, tenant_id, status, updated_at"
                + (", disabled_at" if table != "webhook_endpoints" else "")
                + ") VALUES(?, 'tenant_other', 'active', 'before'"
                + extra
                + ")",
                (value,),
            )
    connection.execute(
        "INSERT INTO providers VALUES('provider_cross', 'tenant_cross', 'active', NULL, 'before')"
    )
    connection.execute(
        "INSERT INTO environments VALUES('env_cross', 'tenant_cross', 'active', 'before')"
    )
    connection.execute(
        "INSERT INTO models VALUES('model_cross', 'tenant_cross', 'active', NULL, 'before')"
    )
    connection.execute(
        """
        INSERT INTO model_quality_scores VALUES(
          'mqs_cross', 'tenant_cross', 'model_cross', 'active', 0, 0, 1, NULL
        )
        """
    )
    connection.commit()
    connection.close()


def test_cleanup_state_is_tenant_scoped_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """终清理撤销临时账号全部 Session、只禁用目标租户行，重复执行得到零变更。"""

    monkeypatch.chdir(tmp_path)
    batch = tmp_path / "artifacts" / "prd048" / "testrun01"
    batch.mkdir(parents=True)
    database_path = batch / "metahub-prd048.db"
    _prepare_cleanup_database(database_path)
    state_path = batch / "runtime-state.json"
    state_path.write_text(
        json.dumps(_safe_runtime_state(), ensure_ascii=False),
        encoding="utf-8",
    )

    first = RUNTIME_MODULE.cleanup_from_state(state_path)
    second = RUNTIME_MODULE.cleanup_from_state(state_path)

    assert first["lifecycle"]["cleanup_counts"] == {
        "sessions": 5,
        "users": 2,
        "tenants": 1,
        "environments": 1,
        "aliases": 1,
        "models": 3,
        "providers": 3,
        "virtual_keys": 1,
        "webhooks": 1,
    }
    assert second["lifecycle"]["cleanup_counts"] == {
        "sessions": 0,
        "users": 0,
        "tenants": 0,
        "environments": 0,
        "aliases": 0,
        "models": 0,
        "providers": 0,
        "virtual_keys": 0,
        "webhooks": 0,
    }
    assert second["lifecycle"]["cleanup_verified"] is True
    connection = sqlite3.connect(database_path)
    for table in (
        "users",
        "providers",
        "environments",
        "models",
        "model_aliases",
        "virtual_keys",
        "webhook_endpoints",
    ):
        rows = connection.execute(
            f"SELECT tenant_id, status FROM {table} ORDER BY tenant_id, rowid"
        ).fetchall()
        assert all(status == "disabled" for tenant, status in rows if tenant == "tenant_main")
        assert all(status == "active" for tenant, status in rows if tenant == "tenant_other")
    tenant_statuses = dict(connection.execute("SELECT tenant_id, status FROM tenants").fetchall())
    cross_provider_status = connection.execute(
        "SELECT status FROM providers "
        "WHERE tenant_id='tenant_cross' AND provider_id='provider_cross'"
    ).fetchone()[0]
    cross_environment_status = connection.execute(
        "SELECT status FROM environments "
        "WHERE tenant_id='tenant_cross' AND environment_id='env_cross'"
    ).fetchone()[0]
    cross_model_status = connection.execute(
        "SELECT status FROM models WHERE tenant_id='tenant_cross' AND model_id='model_cross'"
    ).fetchone()[0]
    sessions = dict(
        connection.execute(
            "SELECT session_id, status FROM sessions ORDER BY session_id"
        ).fetchall()
    )
    cross_score = connection.execute(
        """
        SELECT tenant_id, model_id, status, feedback_version, manual_quality_bps
        FROM model_quality_scores WHERE model_quality_score_id='mqs_cross'
        """
    ).fetchone()
    connection.close()
    assert sessions["session_admin"] == "revoked"
    # 复用管理员的其他 Session 不属于资源账本，必须保留；browser 成功依赖严格 logout。
    assert sessions["session_admin_other"] == "active"
    assert sessions["session_developer"] == "revoked"
    assert sessions["session_developer_browser"] == "revoked"
    assert sessions["session_viewer"] == "revoked"
    assert sessions["session_viewer_browser"] == "revoked"
    assert sessions["session_other_developer"] == "active"
    assert tenant_statuses == {
        "tenant_main": "active",
        "tenant_other": "active",
        "tenant_cross": "disabled",
    }
    assert cross_provider_status == "disabled"
    assert cross_environment_status == "disabled"
    assert cross_model_status == "disabled"
    assert cross_score == ("tenant_cross", "model_cross", "active", 1, None)


def test_state_redaction_rejects_forbidden_keys_and_known_secrets() -> None:
    """状态写盘门禁同时拒绝敏感字段名和嵌套敏感明文。"""

    with pytest.raises(AssertionError, match="forbidden state key"):
        RUNTIME_MODULE.assert_redacted_state(
            {"status": "passed", "nested": {"access_token": "not-persisted"}},
            [],
        )
    with pytest.raises(AssertionError, match="sensitive value"):
        RUNTIME_MODULE.assert_redacted_state(
            {"status": "passed", "checks": ["prefix-secret-value-suffix"]},
            ["secret-value"],
        )


def test_expect_json_does_not_echo_untrusted_error_body() -> None:
    """状态不符时只输出白名单诊断，服务端伪造的敏感 code/request_id 必须被替换。"""

    response = httpx.Response(
        503,
        headers={"Content-Type": "application/json"},
        json={
            "code": "private response token value",
            "request_id": "mhv_private_key_value",
            "trace_id": "trc_safe",
        },
    )
    with pytest.raises(AssertionError) as captured:
        RUNTIME_MODULE.expect_json(response, 200)

    message = str(captured.value)
    assert "private response token value" not in message
    assert "mhv_private_key_value" not in message
    assert message.count("<redacted>") == 2


def test_webhook_receiver_verifies_hmac_without_retaining_body() -> None:
    """回环接收器验证真实 HMAC，只暴露评分事件白名单摘要。"""

    secret = "whsec_unit_test_value"
    request_id = "req_prd048_unit_webhook"
    envelope = {
        "event_id": "mqe_unit",
        "event_type": "model.feedback_recorded",
        "object_id": "mqs_unit",
        "payload": {
            "trigger": "manual_score_updated",
            "request_id": request_id,
            "model_id": "model_unit",
            "feedback_version": 2,
            "statistics_version": 0,
        },
    }
    body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = webhook_signature_header(secret, int(time.time()), body)
    with RUNTIME_MODULE.RunningAcceptanceServer("unit-prompt-sentinel") as receiver:
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
    assert captured == [
        {
            "event_id": "mqe_unit",
            "event_type": "model.feedback_recorded",
            "object_id": "mqs_unit",
            "request_id": request_id,
            "model_id": "model_unit",
            "feedback_version": 2,
            "statistics_version": 0,
            "trigger": "manual_score_updated",
            "payload_keys": [
                "feedback_version",
                "model_id",
                "request_id",
                "statistics_version",
                "trigger",
            ],
            "signature_valid": True,
            "privacy_ok": True,
        }
    ]
    assert body.decode("utf-8") not in json.dumps(captured, sort_keys=True)


def test_parse_args_requires_explicit_browser_passwords_and_accepts_cleanup_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """保留模式缺少临时密码时失败，cleanup-state 模式不依赖任何凭据或服务。"""

    monkeypatch.chdir(tmp_path)
    batch = tmp_path / "artifacts" / "prd048" / "testrun01"
    batch.mkdir(parents=True)
    database_path = batch / "metahub-prd048.db"
    database_path.touch()
    state_path = batch / "runtime-state.json"
    state_path.write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError, match="developer/viewer passwords"):
        RUNTIME_MODULE.parse_args(
            [
                "--retain-for-browser",
                "--database-path",
                str(database_path),
                "--state-output",
                str(state_path),
                "--suffix",
                "testrun01",
                "--admin-password",
                "admin-unit-secret",
            ]
        )

    cleanup = RUNTIME_MODULE.parse_args(["--cleanup-state", str(state_path)])
    assert cleanup.cleanup_state == state_path.resolve()
