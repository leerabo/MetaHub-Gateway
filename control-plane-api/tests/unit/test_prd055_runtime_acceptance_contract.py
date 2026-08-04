r"""PRD-055 Runtime 验收器的纯沙箱合同测试。

测试只使用 httpx.MockTransport 与 pytest 的 D:\MetaHub\.tmp 独占目录，不连接本机
服务、外网或开发数据库；最小 SQLite 夹具仅验证 0045 就绪门禁。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest


# 脚本名含连字符，显式加载可避免导入阶段执行命令行入口。
SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "acceptance" / (
    "prd055-runtime-acceptance.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd055_runtime_acceptance_contract",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块固定为本工作区脚本，防止测试误用环境中同名安装包。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _runtime_config(batch: Path, database: Path) -> object:
    """构造只含单元测试凭据的运行配置，数据库位于当前 pytest 独占目录。"""

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
    """任何 HTTP 调用都立即失败，证明纯合同测试没有访问真实端口。"""

    # MockTransport 一旦被调用即说明测试越界，必须立即失败而不能伪造响应。
    raise AssertionError("contract test must not issue HTTP requests")


def _response(
    status: int,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    *,
    cache_control: str = "no-store, private",
) -> httpx.Response:
    """构造携带统一链路和 RequestLog 禁缓存头的内存响应。"""

    return httpx.Response(
        status,
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Cache-Control": cache_control,
            "Pragma": "no-cache",
            "Vary": "Accept-Encoding, Authorization",
        },
        json={"request_id": request_id, "trace_id": trace_id, **payload},
        request=httpx.Request("GET", "http://localhost:8001/request-logs"),
    )


def _create_ready_database(path: Path) -> None:
    """建立 0045 验收器只读检查需要的最小表、索引和触发器。"""

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
              version TEXT PRIMARY KEY, status TEXT NOT NULL, checksum TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES('0045', 'applied', ?)",
            (RUNTIME._migration_checksum(),),
        )
        connection.execute("CREATE TABLE fixture_base(value INTEGER)")
        connection.execute("CREATE TABLE saved_filters(value INTEGER)")
        connection.execute("CREATE TABLE request_log_query_events(value INTEGER)")
        for name in RUNTIME.REQUIRED_INDEXES:
            connection.execute(f'CREATE INDEX "{name}" ON fixture_base(value)')
        for name in RUNTIME.REQUIRED_TRIGGERS:
            connection.execute(
                f'CREATE TRIGGER "{name}" AFTER INSERT ON fixture_base '
                "BEGIN SELECT 1; END"
            )


def test_loopback_batch_and_runtime_database_boundaries(tmp_path: Path) -> None:
    """三服务必须是显式 HTTP 回环，数据库必须是同批次 runtime.db。"""

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
    batch = root / "artifacts" / "prd055" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    database.touch()
    assert RUNTIME.resolve_artifact_directory(
        batch, workspace_root=root
    ) == batch.resolve()
    database_url = "sqlite:///" + database.as_posix()
    assert RUNTIME.resolve_runtime_database(
        database_url, artifact_directory=batch
    ) == database.resolve()
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.resolve_runtime_database(
            "sqlite:///" + (root / "runtime.db").as_posix(),
            artifact_directory=batch,
        )


def test_response_contract_requires_chain_error_code_and_private_no_store() -> None:
    """成功和错误均须携带同一链路，身份相关成功响应还必须禁止缓存。"""

    request_id = "req_prd055_contract"
    trace_id = "trc_prd055_contract"
    payload = RUNTIME.verify_response_contract(
        _response(200, request_id, trace_id, {"request_logs": []}),
        stage="unit.success",
        expected_status=200,
        request_id=request_id,
        trace_id=trace_id,
        require_private_cache=True,
    )
    assert payload["request_logs"] == []
    RUNTIME.verify_response_contract(
        _response(
            422,
            request_id,
            trace_id,
            {"code": "request_log.query_invalid"},
        ),
        stage="unit.error",
        expected_status=422,
        request_id=request_id,
        trace_id=trace_id,
        expected_code="request_log.query_invalid",
    )
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            _response(
                200,
                request_id,
                trace_id,
                {"request_logs": []},
                cache_control="private",
            ),
            stage="unit.cache",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            require_private_cache=True,
        )


def test_wire_filter_is_unpadded_base64url_v1_and_p95_uses_nearest_rank() -> None:
    """高级筛选保持 v1 Base64URL，P95 不能用平均数掩盖尾延迟。"""

    wire = RUNTIME._wire_filters(
        {
            "v": 1,
            "status": "failed",
            "provider_code": "deepseek",
            "latency_ms_min": 0,
        }
    )
    assert "=" not in wire
    padding = "=" * (-len(wire) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(wire + padding))
    assert decoded == {
        "latency_ms_min": 0,
        "provider_code": "deepseek",
        "status": "failed",
        "v": 1,
    }
    assert RUNTIME._nearest_rank_p95(list(range(1, 21))) == 19


def test_readiness_requires_complete_0045_objects_without_http(tmp_path: Path) -> None:
    """0045 校验和、关键对象和完整性齐全时通过，过程不得发出 HTTP。"""

    batch = tmp_path / "artifacts" / "prd055" / "unit-ready01"
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
            _runtime_config(batch, database),
            control,
            gateway,
            console,
        )
        evidence = acceptance.verify_runtime_database_ready()
    assert evidence["migration_0045_applied"] is True
    assert evidence["migration_checksum_verified"] is True
    assert evidence["required_indexes_present"] is True
    assert evidence["required_triggers_present"] is True


def test_readiness_rejects_missing_0045_without_mutation(tmp_path: Path) -> None:
    """缺少 0045 时立即失败，验收器不得自行迁移或补表。"""

    batch = tmp_path / "artifacts" / "prd055" / "unit-missing01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations("
            "version TEXT PRIMARY KEY, status TEXT NOT NULL, checksum TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES('0044', 'applied', ?)",
            ("2" * 64,),
        )
    transport = httpx.MockTransport(_unused_transport)
    with (
        httpx.Client(transport=transport) as control,
        httpx.Client(transport=transport) as gateway,
        httpx.Client(transport=transport) as console,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(
            _runtime_config(batch, database),
            control,
            gateway,
            console,
        )
        with pytest.raises(RUNTIME.AcceptanceFailure):
            acceptance.verify_runtime_database_ready()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version, status FROM schema_migrations"
        ).fetchall() == [("0044", "applied")]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 1


def test_safe_payload_and_runtime_evidence_are_write_once(tmp_path: Path) -> None:
    """最终证据拒绝敏感槽位和凭据形态，并以独占创建防止覆盖。"""

    evidence = {
        "schema_version": 1,
        "prd_id": "PRD-055",
        "status": "passed",
        "generated_resources": {
            "shared_request_id": "req_prd055_shared_contract",
            "performance_rows": 100000,
        },
        "checks": {
            "migration_0045_applied": True,
            "session_revoked": True,
            "estimated_cost_amount": 0.012345,
        },
    }
    RUNTIME.assert_safe_payload(evidence, sensitive_values=("not-present",))
    target = RUNTIME.publish_evidence(tmp_path, evidence)
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "passed"
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.publish_evidence(tmp_path, evidence)
    for unsafe, sensitive in (
        ({"password": "plain"}, ()),
        ({"status": "prefix-known-secret-suffix"}, ("known-secret",)),
        ({"status": "Bearer opaque-session-value"}, ()),
        ({"estimated_cost_amount": float("nan")}, ()),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_payload(unsafe, sensitive_values=sensitive)


def test_runtime_source_freezes_real_dataset_samples_and_no_external_temp() -> None:
    """验收器必须保留十万行、20 次 HTTP 样本和批次库边界。"""

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "PERFORMANCE_FIXTURE_ROWS = 100_000" in source
    assert "PERFORMANCE_SAMPLE_COUNT = 20" in source
    assert "PERFORMANCE_P95_LIMIT_MS = 500" in source
    assert "PERFORMANCE_SINGLE_LIMIT_MS = 1_500" in source
    assert 'expected = Path(os.path.abspath(artifact_directory / "runtime.db"))' in source
    assert "tempfile" not in source
    assert "C:\\tmp" not in source
