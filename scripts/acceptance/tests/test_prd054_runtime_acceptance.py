"""PRD-054 Runtime 验收器的纯沙箱聚焦合同测试。

测试只使用 ``httpx.MockTransport`` 和 pytest 独占临时目录，不连接本机服务、外网或开发
数据库。SQLite 夹具只建立验收器读取所需的最小对象，用于证明 0044 与 outbox 门禁。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest


# 脚本名含连字符，显式加载可避免导入阶段执行命令行入口。
# 动态模块对象固定为测试常量，避免测试误用已安装的同名包。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd054-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location("prd054_runtime_acceptance", SCRIPT_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# RUNTIME 绑定当前工作区脚本，避免合同测试误用环境中的同名安装包。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _runtime_config(batch: Path, database: Path) -> object:
    """构造只含单元测试凭据的运行配置，路径由当前 pytest 临时目录独占。"""

    return RUNTIME.RuntimeConfig(
        control_origin="http://localhost:8001",
        gateway_origin="http://localhost:8000",
        console_origin="http://localhost:5173",
        artifact_directory=batch,
        database_path=database,
        tenant_slug="default",
        admin_email="admin@metahub.local",
        admin_password="UnitOnly!2026",
        timeout_seconds=2.0,
    )


def _unused_transport(_request: httpx.Request) -> httpx.Response:
    """任何 HTTP 调用都立即失败，证明纯合同测试没有偷偷访问端口。"""

    # 合同测试出现网络调用即说明边界失守，不能返回伪造响应继续执行。
    raise AssertionError("contract test must not issue HTTP requests")


def _response(
    status: int,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    *,
    cache_control: str = "no-store, private",
) -> httpx.Response:
    """构造带真实 request 对象和 Explain 防缓存头的统一响应。"""

    return httpx.Response(
        status,
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Cache-Control": cache_control,
            "Pragma": "no-cache",
            "Vary": "Authorization",
        },
        json={"request_id": request_id, "trace_id": trace_id, **payload},
        request=httpx.Request("GET", "http://localhost:8001/request-explain/req_unit"),
    )


def _explanation_document(status: str = "complete") -> dict[str, object]:
    """构造独立于服务端实现的最小 exact-key RequestExplanation 文档。"""

    missing = [] if status == "complete" else ["routing"]
    sections = {
        name: {
            "evidence_state": (
                "not_recorded" if status == "partial" and name == "routing" else "not_applicable"
            ),
            "reason_code": f"request_explanation.{name}_unit",
            "facts": [],
            "truncated": False,
        }
        for name in RUNTIME.SECTION_ORDER
    }
    sections["authentication"] = {
        "evidence_state": "complete",
        "reason_code": "request_explanation.authentication_verified",
        "facts": [
            {
                "kind": "authentication",
                "fact_id": "req_prd054_unit",
                "occurred_at": "2026-07-28T00:00:00Z",
                "values": {
                    key: (
                        1
                        if key == "snapshot_version"
                        else "disabled" if key == "policy_snapshot_status" else None
                    )
                    for key in RUNTIME.FACT_VALUE_KEYS["authentication"]
                },
            }
        ],
        "truncated": False,
    }
    return {
        "request_id": "req_prd054_operation_unit",
        "trace_id": "trc_prd054_operation_unit",
        "request_explanation": {
            "request_explanation_id": "rxe_" + "1" * 32,
            "source_request_id": "req_prd054_unit",
            "source_trace_id": "trc_prd054_unit",
            "status": status,
            "evidence_version": "request_explanation_v1",
            "captured_at": "2026-07-28T00:00:01Z",
            "summary": {
                "source_status": "failed" if status == "complete" else "success",
                "error_code": "gateway.rate_limit_exceeded" if status == "complete" else None,
                "request_model": "company-default",
                "resolved_model": None,
                "provider_code": None,
                "environment_id": "env_unit",
                "environment_name": "Unit",
                "project_id": None,
                "project_name": None,
                "application_id": None,
                "application_name": None,
                "snapshot_version": None,
                "latency_ms": 3,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_amount": "12345678901234567890.123456",
                "currency": "CNY",
                "fallback_triggered": False,
                "created_at": "2026-07-28T00:00:00Z",
                "updated_at": "2026-07-28T00:00:00Z",
            },
            "decision_chain": [
                {
                    "stage": stage,
                    "status": "not_evaluated",
                    "evidence_state": "not_applicable",
                    "reason_code": f"request_explanation.{stage}_unit",
                    "occurred_at": None,
                    "evidence_refs": [],
                }
                for stage in RUNTIME.STAGE_ORDER
            ],
            **sections,
            "missing_sections": missing,
            "truncated_sections": [],
            "view_event_status": "pending",
        },
    }


def _create_ready_database(path: Path) -> None:
    """建立包含 0044 关键表、索引和触发器名称的最小就绪数据库。"""

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES('0044', 'applied')")
        connection.execute("CREATE TABLE fixture_base(value INTEGER)")
        connection.execute("CREATE TABLE request_explanations(value INTEGER)")
        connection.execute("CREATE TABLE request_webhook_delivery_links(value INTEGER)")
        for name in RUNTIME.REQUIRED_INDEXES:
            connection.execute(f'CREATE INDEX "{name}" ON fixture_base(value)')
        for name in RUNTIME.REQUIRED_TRIGGERS:
            connection.execute(
                f'CREATE TRIGGER "{name}" AFTER INSERT ON fixture_base BEGIN SELECT 1; END'
            )


def test_loopback_artifact_and_database_boundaries(tmp_path: Path) -> None:
    """三服务必须是显式 HTTP 回环，数据库必须是当前批次 runtime.db。"""

    assert RUNTIME.normalize_loopback_origin(
        "http://127.0.0.1:18001/", stage="unit.origin"
    ) == "http://127.0.0.1:18001"
    for unsafe in (
        "https://localhost:8001",
        "http://example.com:8001",
        "http://user:pass@localhost:8001",
        "http://localhost:8001/path",
        "http://localhost",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.normalize_loopback_origin(unsafe, stage="unit.origin")

    root = tmp_path / "MetaHub"
    batch = root / "artifacts" / "prd054" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    assert RUNTIME.resolve_artifact_directory(batch, workspace_root=root) == batch.resolve()
    assert RUNTIME.resolve_runtime_database(
        RUNTIME.sqlite_url_for_path(database), artifact_directory=batch
    ) == database.resolve()
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.resolve_runtime_database(
            RUNTIME.sqlite_url_for_path(root / "runtime.db"), artifact_directory=batch
        )


def test_response_contract_requires_chain_and_explain_cache_headers() -> None:
    """成功与错误都必须携带同一 operation 链路，Explain 成功还必须禁止缓存。"""

    request_id = "req_prd054_contract"
    trace_id = "trc_prd054_contract"
    payload = RUNTIME.verify_response_contract(
        _response(200, request_id, trace_id, {"request_explanation": {}}),
        stage="unit.success",
        expected_status=200,
        request_id=request_id,
        trace_id=trace_id,
        require_explanation_cache_headers=True,
    )
    assert payload["request_id"] == request_id
    RUNTIME.verify_response_contract(
        _response(403, request_id, trace_id, {"code": "auth.permission_denied"}),
        stage="unit.error",
        expected_status=403,
        request_id=request_id,
        trace_id=trace_id,
        expected_code="auth.permission_denied",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            _response(
                200,
                request_id,
                trace_id,
                {"request_explanation": {}},
                cache_control="private",
            ),
            stage="unit.no_store",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            require_explanation_cache_headers=True,
        )


def test_complete_partial_dto_exact_keys_precision_and_privacy() -> None:
    """DTO 必须保持十阶段、十一区块、扁平白名单和金额字符串精度。"""

    complete = _explanation_document("complete")
    partial = _explanation_document("partial")
    assert RUNTIME.validate_explanation_document(
        complete,
        expected_source_request_id="req_prd054_unit",
        expected_status="complete",
    ) == {
        "status": "complete",
        "decision_stage_count": 10,
        "section_count": 11,
        "privacy_verified": True,
        "view_event_pending": True,
    }
    assert RUNTIME.validate_explanation_document(
        partial,
        expected_source_request_id="req_prd054_unit",
        expected_status="partial",
    )["status"] == "partial"

    unsafe_key = copy.deepcopy(complete)
    unsafe_key["request_explanation"]["logs"]["facts"].append(
        {"kind": "request_log", "fact_id": None, "occurred_at": None, "values": {"prompt": "x"}}
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.validate_explanation_document(
            unsafe_key,
            expected_source_request_id="req_prd054_unit",
            expected_status="complete",
        )
    unsafe_value = copy.deepcopy(complete)
    unsafe_value["request_explanation"]["summary"]["environment_name"] = (
        "Bearer prd054-private-token-value"
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.validate_explanation_document(
            unsafe_value,
            expected_source_request_id="req_prd054_unit",
            expected_status="complete",
        )


def test_readiness_accepts_complete_0044_objects_without_http(tmp_path: Path) -> None:
    """0044 版本、关键对象和完整性齐全时通过，过程不得发出 HTTP。"""

    batch = tmp_path / "artifacts" / "prd054" / "unit-ready01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    _create_ready_database(database)
    transport = httpx.MockTransport(_unused_transport)
    with (
        httpx.Client(transport=transport) as control,
        httpx.Client(transport=transport) as gateway,
        httpx.Client(transport=transport) as console,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(
            _runtime_config(batch, database), control, gateway, console
        )
        evidence = acceptance.verify_runtime_database_ready()
    assert evidence["migration_0044_applied"] is True
    assert evidence["required_triggers_present"] is True


def test_readiness_rejects_missing_0044_without_mutation(tmp_path: Path) -> None:
    """缺少 0044 时立即失败，验收器不得自行迁移或补表。"""

    batch = tmp_path / "artifacts" / "prd054" / "unit-missing01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES('0043', 'applied')")
    transport = httpx.MockTransport(_unused_transport)
    with (
        httpx.Client(transport=transport) as control,
        httpx.Client(transport=transport) as gateway,
        httpx.Client(transport=transport) as console,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(
            _runtime_config(batch, database), control, gateway, console
        )
        with pytest.raises(RUNTIME.AcceptanceFailure):
            acceptance.verify_runtime_database_ready()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version, status FROM schema_migrations").fetchall() == [
            ("0043", "applied")
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 1


def test_outbox_evidence_requires_three_unique_success_facts(tmp_path: Path) -> None:
    """只有三次成功读取形成独立访问事实，失败、跨租户和通用 Audit 均为零。"""

    batch = tmp_path / "artifacts" / "prd054" / "unit-outbox01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    suffix = "abc123def456"
    tenant_id = "tenant_unit"
    complete_id = f"req_prd054_complete_{suffix}"
    partial_id = f"req_prd054_partial_{suffix}"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE request_explanations(
              request_explanation_id TEXT, event_id TEXT, tenant_id TEXT,
              source_request_id TEXT, actor_user_id TEXT, actor_session_id TEXT,
              operation_request_id TEXT, operation_trace_id TEXT,
              event_dispatch_status TEXT, event_dispatch_attempts INTEGER,
              created_at TEXT
            )
            """
        )
        connection.execute("CREATE TABLE audit_logs(tenant_id TEXT, action TEXT)")
        for index, action in enumerate(
            ("admin_complete", "developer_complete", "admin_partial"), start=1
        ):
            connection.execute(
                "INSERT INTO request_explanations VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                (
                    "rxe_" + f"{index:032x}",
                    "rxevt_" + f"{index:032x}",
                    tenant_id,
                    partial_id if action == "admin_partial" else complete_id,
                    f"user_{index}",
                    f"session_{index}",
                    RUNTIME._request_identity(action, suffix)[0],
                    RUNTIME._request_identity(action, suffix)[1],
                    f"2026-07-28T00:00:0{index}Z",
                ),
            )
    transport = httpx.MockTransport(_unused_transport)
    with (
        httpx.Client(transport=transport) as control,
        httpx.Client(transport=transport) as gateway,
        httpx.Client(transport=transport) as console,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(
            _runtime_config(batch, database), control, gateway, console
        )
        acceptance.suffix = suffix
        acceptance.resource_ids.update(
            {
                "tenant_id": tenant_id,
                "complete_request_id": complete_id,
                "partial_request_id": partial_id,
            }
        )
        result = acceptance.verify_access_outbox()
    assert result["successful_view_fact_count"] == 3
    assert result["stable_event_ids_unique"] is True
    assert result["generic_audit_not_duplicated"] is True


def test_safe_evidence_and_write_once(tmp_path: Path) -> None:
    """证据拒绝敏感槽位/明文，并以独占创建防止同批次覆盖。"""

    evidence = {
        "schema_version": 1,
        "prd_id": "PRD-054",
        "status": "passed",
        "generated_resources": {
            "complete_request_id": "req_prd054_complete_unit",
            "partial_request_id": "req_prd054_partial_unit",
        },
        "checks": {"migration_0044_applied": True, "session_revoked": True},
    }
    RUNTIME.assert_safe_evidence(evidence, sensitive_values=("not-present",))
    target = RUNTIME.publish_evidence(tmp_path, evidence)
    assert json.loads(target.read_text(encoding="utf-8"))["prd_id"] == "PRD-054"
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.publish_evidence(tmp_path, evidence)
    for unsafe, known in (
        ({"password": "plain"}, ()),
        ({"status": "prefix-known-value-suffix"}, ("known-value",)),
        ({"status": "Bearer opaque-session-value"}, ()),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_evidence(unsafe, sensitive_values=known)


def test_runtime_fixture_uses_real_environment_code_column() -> None:
    """真实夹具必须匹配 Environment 字段与正式 Gateway 失败事件合同。"""

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "environment.code AS environment_code" in source
    assert "environment.environment_code" not in source
    assert "gateway.request_failed" in source
    assert "request.failed" not in source
