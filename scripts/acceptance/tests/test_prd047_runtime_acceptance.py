"""PRD-047 Runtime 验收预算证据与失败清理的无服务单元测试。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# 验收脚本文件名包含连字符，测试通过显式模块规格加载且不会执行 main。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd047-runtime-acceptance.py"
# 固定内部模块名保证 traceback 可读，并在执行前登记以支持运行时类型解析。
MODULE_SPEC = importlib.util.spec_from_file_location("prd047_runtime_acceptance", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块对象仅暴露纯函数和编排类给本测试，模块名不触发脚本 main 分支。
RUNTIME_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME_MODULE
MODULE_SPEC.loader.exec_module(RUNTIME_MODULE)


def _settlement_connection() -> sqlite3.Connection:
    """建立只含 Budget/Usage 三键结算合同所需列的内存数据库。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE budget_rules (
          tenant_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE budget_reservations (
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          reserved_cost_cny NUMERIC NOT NULL,
          outstanding_cost_cny NUMERIC
        );
        CREATE TABLE usage_ledger (
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL
        );
        """
    )
    return connection


def test_budget_settlement_summary_is_tenant_scoped_and_redacted() -> None:
    """同 request/rule 的其他租户不得干扰结算，结果不得携带金额原值。"""

    connection = _settlement_connection()
    connection.executemany(
        "INSERT INTO budget_rules(tenant_id, budget_rule_id, status) VALUES(?, ?, 'active')",
        [("tenant_a", "br_shared"), ("tenant_b", "br_shared")],
    )
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          tenant_id, request_id, budget_rule_id, reserved_cost_cny, outstanding_cost_cny
        ) VALUES(?, 'req_shared', 'br_shared', ?, ?)
        """,
        [("tenant_a", "12.75", "0"), ("tenant_b", "99.25", "7")],
    )
    connection.executemany(
        "INSERT INTO usage_ledger(tenant_id, request_id) VALUES(?, 'req_shared')",
        [("tenant_a",), ("tenant_b",)],
    )

    summary = RUNTIME_MODULE.budget_settlement_summary(
        connection,
        tenant_id="tenant_a",
        request_id="req_shared",
        budget_rule_id="br_shared",
    )

    assert summary == {
        "reservation_count": 1,
        "usage_count": 1,
        "reserved_positive": True,
        "outstanding_zero": True,
        "tenant_request_rule_scoped": True,
    }
    encoded = json.dumps(summary, sort_keys=True)
    assert "12.75" not in encoded and "99.25" not in encoded


def test_budget_settlement_summary_rejects_nonzero_outstanding() -> None:
    """Usage 已存在但 outstanding 非零时必须拒绝通过，不能把部分结算误报为完成。"""

    connection = _settlement_connection()
    connection.execute(
        "INSERT INTO budget_rules VALUES('tenant_a', 'br_a', 'active')"
    )
    connection.execute(
        "INSERT INTO budget_reservations VALUES('tenant_a', 'req_a', 'br_a', 1, 0.5)"
    )
    connection.execute("INSERT INTO usage_ledger VALUES('tenant_a', 'req_a')")

    with pytest.raises(AssertionError, match="outstanding"):
        RUNTIME_MODULE.budget_settlement_summary(
            connection,
            tenant_id="tenant_a",
            request_id="req_a",
            budget_rule_id="br_a",
        )


@pytest.mark.parametrize("registered_rule_id", ["br_target", None, "br_wrong_response"])
def test_failure_cleanup_disables_exact_budget_rule_and_is_idempotent(
    tmp_path: Path,
    registered_rule_id: str | None,
) -> None:
    """正确、缺失或错误响应 ID 都只禁用目标 owner 一行，重复执行精确返回零。"""

    database_path = tmp_path / "prd047-runtime-budget-cleanup.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE budget_rules (
          tenant_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          environment_id TEXT NOT NULL,
          scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          name TEXT NOT NULL,
          status TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO budget_rules VALUES(
          'tenant_a', 'br_target', 'env_a', 'virtual_key', 'vk_shared',
          'PRD047 runtime budget suffix123', 'active', 'before'
        );
        INSERT INTO budget_rules VALUES(
          'tenant_b', 'br_other', 'env_b', 'virtual_key', 'vk_shared',
          'PRD047 runtime budget suffix123', 'active', 'before'
        );
        """
    )
    connection.commit()
    connection.close()
    acceptance = object.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.args = SimpleNamespace(database_path=database_path)
    acceptance.resource_ids = (
        {"budget_rule_id": registered_rule_id} if registered_rule_id else {}
    )
    acceptance.budget_intent = {
        "tenant_id": "tenant_a",
        "environment_id": "env_a",
        "scope_id": "vk_shared",
        "rule_name": "PRD047 runtime budget suffix123",
    }

    first = acceptance.cleanup_after_failure()
    second = acceptance.cleanup_after_failure()

    assert first["budget_rules"] == 1
    assert second["budget_rules"] == 0
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        "SELECT tenant_id, status FROM budget_rules ORDER BY tenant_id"
    ).fetchall()
    connection.close()
    assert rows == [("tenant_a", "disabled"), ("tenant_b", "active")]


def test_budget_rbac_attacks_use_unique_traceable_requests_and_leave_no_rules(
    tmp_path: Path,
) -> None:
    """Budget developer/viewer/跨租户攻击必须命中统一错误且不能改变数据库规则图。"""

    database_path = tmp_path / "prd047-runtime-budget-rbac.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE budget_rules (
          tenant_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          environment_id TEXT NOT NULL,
          scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          name TEXT NOT NULL,
          status TEXT NOT NULL
        );
        INSERT INTO budget_rules VALUES(
          'tenant_main', 'br_target', 'env_main', 'virtual_key', 'vk_main',
          'PRD047 runtime budget testrun01', 'active'
        );
        INSERT INTO budget_rules VALUES(
          'tenant_main', 'br_baseline', 'env_main', 'tenant', 'tenant_main',
          'Baseline budget', 'active'
        );
        """
    )
    connection.commit()
    connection.close()

    class FakeControl:
        """返回固定统一响应，并记录 Budget 攻击头与请求体供安全断言。"""

        def __init__(self) -> None:
            """初始化只含非敏感请求 ID、trace ID 和 JSON 配置的调用账本。"""

            self.budget_calls: list[dict[str, object]] = []

        @staticmethod
        def _response(
            status: int,
            headers: dict[str, str],
            *,
            code: str | None = None,
            model_aliases: list[dict[str, str]] | None = None,
        ) -> object:
            """按请求头原样回显链路 ID，模拟 Control API 的统一 JSON wire。"""

            request_id = headers["X-Request-ID"]
            trace_id = headers.get("X-Trace-ID", f"trc_{request_id}")
            body: dict[str, object] = {
                "request_id": request_id,
                "trace_id": trace_id,
            }
            if code is not None:
                body["code"] = code
            # 只有 developer 列表成功响应需要模型数组，错误响应保持统一最小结构。
            if model_aliases is not None:
                body["model_aliases"] = model_aliases
            return RUNTIME_MODULE.httpx.Response(status, json=body)

        def patch(
            self,
            path: str,
            *,
            headers: dict[str, str],
            json: dict[str, object] | None = None,
            content: bytes | None = None,
        ) -> object:
            """模拟路由与预算 PATCH；权限结果只由 Bearer 角色和目标路径决定。"""

            del content
            token = headers["Authorization"]
            if path == "/budgets":
                assert json is not None
                self.budget_calls.append(
                    {"request_id": headers["X-Request-ID"], "trace_id": headers["X-Trace-ID"], "json": dict(json)}
                )
                # 非管理员即使直接攻击写入口也只能得到后端权限拒绝。
                if token in {"Bearer developer-token", "Bearer viewer-token"}:
                    # 固定错误响应不读取或回显预算值，模拟 permission-first 安全边界。
                    return self._response(403, headers, code="auth.permission_denied")
                assert json["environment_id"] == "env_other"
                return self._response(404, headers, code="environment.not_found")
            # Alias 写攻击同样按角色返回 403，管理员跨租户 ID 则统一表现为不存在。
            if token in {"Bearer developer-token", "Bearer viewer-token"}:
                # 角色拒绝响应只携安全链路字段和统一错误码。
                return self._response(403, headers, code="auth.permission_denied")
            return self._response(404, headers, code="model_alias.not_found")

        def get(self, path: str, *, headers: dict[str, str]) -> object:
            """模拟 developer 可读和 viewer 拒绝的 Alias 列表响应。"""

            assert path.startswith("/model-aliases")
            # developer 仅可读取默认租户 Alias；响应不包含第二租户对象。
            if headers["Authorization"] == "Bearer developer-token":
                return self._response(
                    200,
                    headers,
                    model_aliases=[{"alias": "prd047-default-testrun01"}],
                )
            # viewer 的列表读取也必须由后端拒绝，不能依赖 Console 隐藏入口。
            return self._response(403, headers, code="auth.permission_denied")

    acceptance = object.__new__(RUNTIME_MODULE.RuntimeAcceptance)
    acceptance.args = SimpleNamespace(database_path=database_path)
    acceptance.suffix = "testrun01"
    acceptance.admin_token = "admin-token"
    acceptance.role_tokens = {
        "developer": "developer-token",
        "viewer": "viewer-token",
    }
    acceptance.resource_ids = {"other_environment_id": "env_other"}
    acceptance.budget_intent = {
        "tenant_id": "tenant_main",
        "environment_id": "env_main",
        "scope_id": "vk_main",
        "rule_name": "PRD047 runtime budget testrun01",
        "budget_rule_id": "br_target",
    }
    acceptance.control = FakeControl()

    summary = acceptance._verify_rbac_and_tenant(
        {"alias_id": "alias_main", "alias": "prd047-default-testrun01", "version": 1},
        "alias_other",
    )

    assert summary["budget_developer_write_denied"] is True
    assert summary["budget_viewer_write_denied"] is True
    assert summary["budget_cross_tenant_environment_not_found"] is True
    assert summary["budget_attack_no_extra_rules"] is True
    assert summary["budget_target_rule_unique"] is True
    budget_calls = acceptance.control.budget_calls
    assert len({str(call["request_id"]) for call in budget_calls}) == 3
    assert len({str(call["trace_id"]) for call in budget_calls}) == 3
    assert all(call["json"]["scope_id"] == "vk_main" for call in budget_calls)
    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        "SELECT budget_rule_id, status FROM budget_rules ORDER BY budget_rule_id"
    ).fetchall()
    connection.close()
    assert rows == [("br_baseline", "active"), ("br_target", "active")]
