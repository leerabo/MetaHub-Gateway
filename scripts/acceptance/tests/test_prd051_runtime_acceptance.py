"""PRD-051 聚焦运行验收脚本的纯合同测试。

这些测试不启动服务、不访问端口，也不创建真实财务分录；它们只锁定回环地址、证据路径、
统一响应字段、敏感信息拒绝和 write-once 发布语义，避免实际验收器自身形成安全缺口。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest


# 文件名包含连字符，测试必须显式加载模块且不能触发命令行 main 分支。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd051-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd051_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块常量仅供纯合同测试调用；导入时不会执行网络、服务或命令行入口。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _response(
    *,
    status: int,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    cache_control: str = "no-store, private",
) -> httpx.Response:
    """构造带真实 httpx Request 的最小响应，供公共证据投影读取固定 API 路径。"""

    return httpx.Response(
        status,
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Cache-Control": cache_control,
        },
        json={"request_id": request_id, "trace_id": trace_id, **payload},
        request=httpx.Request("GET", "http://localhost:8001/finops/ledger"),
    )


def test_loopback_origin_rejects_external_or_credentialed_urls() -> None:
    """Control origin 只接受显式端口的 HTTP 回环地址，外部和 URL 凭据都失败关闭。"""

    assert RUNTIME.normalize_loopback_origin("http://localhost:8001") == "http://localhost:8001"
    assert RUNTIME.normalize_loopback_origin("http://127.0.0.1:18001/") == "http://127.0.0.1:18001"
    for value in (
        "https://localhost:8001",
        "http://example.com:8001",
        "http://admin:plain@localhost:8001",
        "http://localhost:8001/finops",
        "http://localhost:8001?next=external",
        "http://localhost",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.normalize_loopback_origin(value)


def test_artifact_directory_is_single_batch_inside_workspace(tmp_path: Path) -> None:
    """证据目录必须是 PRD-051 根下的单个批次，不能使用根本身、嵌套目录或越界路径。"""

    workspace = tmp_path / "MetaHub"
    expected = workspace / "artifacts" / "prd051" / "unit-batch01"
    assert RUNTIME.resolve_artifact_directory(
        expected,
        workspace_root=workspace,
    ) == expected.resolve()
    for invalid in (
        workspace / "artifacts" / "prd051",
        expected / "nested",
        workspace / "artifacts" / "prd050" / "unit-batch01",
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.resolve_artifact_directory(invalid, workspace_root=workspace)


def test_parse_config_reads_identity_only_from_environment() -> None:
    """解析器没有口令命令行参数，并在返回前删除两种口令环境变量。"""

    environment = {
        "METAHUB_PRD051_ADMIN_PASSWORD": "UnitOnly!2026",
        "METAHUB_ADMIN_PASSWORD": "FallbackMustAlsoDisappear!2026",
        "METAHUB_PRD051_ADMIN_EMAIL": "ADMIN@METAHUB.LOCAL",
        "METAHUB_PRD051_TENANT_SLUG": "default",
    }
    config = RUNTIME.parse_config(
        [
            "--control-url",
            "http://localhost:8001",
            "--artifact-dir",
            "artifacts/prd051/unit-batch01",
        ],
        environ=environment,
    )
    assert config.admin_password == "UnitOnly!2026"
    assert config.admin_email == "admin@metahub.local"
    assert config.tenant_slug == "default"
    assert "METAHUB_PRD051_ADMIN_PASSWORD" not in environment
    assert "METAHUB_ADMIN_PASSWORD" not in environment
    with pytest.raises(SystemExit):
        RUNTIME.parse_config(
            ["--admin-password", "forbidden"],
            environ={"METAHUB_PRD051_ADMIN_PASSWORD": "UnitOnly!2026"},
        )


def test_success_and_error_responses_require_request_trace_and_no_store() -> None:
    """成功与异常响应使用同一链路和缓存门禁，错误分支还必须匹配统一 code。"""

    success = _response(
        status=200,
        request_id="req_prd051_unit_list",
        trace_id="trc_prd051_unit_list",
        payload={"ledger_entries": [], "total": 0},
    )
    assert RUNTIME.verify_response_contract(
        success,
        stage="unit.success",
        expected_status=200,
        request_id="req_prd051_unit_list",
        trace_id="trc_prd051_unit_list",
        require_no_store=True,
    )["total"] == 0

    conflict = _response(
        status=409,
        request_id="req_prd051_unit_conflict",
        trace_id="trc_prd051_unit_conflict",
        payload={"code": "finops.idempotency_conflict"},
    )
    assert RUNTIME.verify_response_contract(
        conflict,
        stage="unit.conflict",
        expected_status=409,
        request_id="req_prd051_unit_conflict",
        trace_id="trc_prd051_unit_conflict",
        require_no_store=True,
        expected_code="finops.idempotency_conflict",
    )["code"] == "finops.idempotency_conflict"


def test_response_contract_rejects_missing_cache_or_mismatched_request_id() -> None:
    """异常即使 HTTP 状态正确，只要 no-store 或 request_id 不一致也不能形成通过证据。"""

    missing_cache = _response(
        status=409,
        request_id="req_prd051_unit_missing_cache",
        trace_id="trc_prd051_unit_missing_cache",
        payload={"code": "finops.idempotency_conflict"},
        cache_control="private",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            missing_cache,
            stage="unit.missing_cache",
            expected_status=409,
            request_id="req_prd051_unit_missing_cache",
            trace_id="trc_prd051_unit_missing_cache",
            require_no_store=True,
            expected_code="finops.idempotency_conflict",
        )

    mismatched = _response(
        status=200,
        request_id="req_prd051_unit_actual",
        trace_id="trc_prd051_unit_actual",
        payload={},
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            mismatched,
            stage="unit.request_mismatch",
            expected_status=200,
            request_id="req_prd051_unit_expected",
            trace_id="trc_prd051_unit_actual",
            require_no_store=True,
        )


def test_safe_evidence_rejects_sensitive_keys_and_values() -> None:
    """安全投影同时拒绝敏感字段槽位、已知明文和 Bearer 片段。"""

    RUNTIME.assert_safe_evidence(
        {"status": "passed", "privacy": {"safe_projection_verified": True}},
        sensitive_values=("not-present",),
    )
    for unsafe, known_values in (
        ({"admin_password": "plain"}, ()),
        ({"status": "known-sensitive"}, ("known-sensitive",)),
        ({"status": "Bearer opaque-session-value"}, ()),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_evidence(unsafe, sensitive_values=known_values)


def test_evidence_publish_is_write_once(tmp_path: Path) -> None:
    """最终 JSON 只允许独占创建，同一批次重跑不得覆盖既有通过文件。"""

    evidence = {"schema_version": 1, "status": "passed", "prd_id": "PRD-051"}
    target = RUNTIME.publish_evidence(tmp_path / "batch", evidence)
    assert target.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.publish_evidence(tmp_path / "batch", evidence)
