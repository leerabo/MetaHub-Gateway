"""PRD-053 Runtime 验收器的纯沙箱聚焦合同测试。

测试只使用 ``httpx.MockTransport`` 和 pytest 独占的 ``D:\\MetaHub`` 临时目录；不会
连接已运行服务、外部网络或开发数据库。真实迁移夹具用于证明受控 Usage 必须先经过
0041 正式账本投影，再由 PRD-053 内部检测器生成异常，禁止验收器直接插入 anomaly。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.webhooks import build_webhook_event, webhook_signature_header


# 文件名含连字符，显式加载可避免导入阶段误入命令行入口。
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "prd053-runtime-acceptance.py"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "prd053_runtime_acceptance",
    SCRIPT_PATH,
)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
# 动态模块对象仅服务本文件合同测试，禁止触发脚本命令行入口或真实网络运行。
RUNTIME = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = RUNTIME
MODULE_SPEC.loader.exec_module(RUNTIME)


def _runtime_config(batch: Path, database: Path) -> object:
    """构造仅含单元测试凭据的验收配置，所有路径都位于 pytest 独占目录。"""

    return RUNTIME.RuntimeConfig(
        control_origin="http://localhost:8001",
        gateway_origin="http://localhost:8000",
        artifact_directory=batch,
        database_path=database,
        tenant_slug="default",
        admin_email="admin@metahub.local",
        admin_password="UnitOnly!2026",
        timeout_seconds=2.0,
    )


def _unused_transport(_request: httpx.Request) -> httpx.Response:
    """任何 HTTP 调用都立即失败，证明合同测试没有偷偷访问端口。"""

    # 一旦触发即证明测试越过 MockTransport 边界，必须立即失败而不能访问本机服务。
    raise AssertionError("contract test must not issue HTTP requests")


def _response(
    status: int,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    *,
    cache_control: str = "no-store, private",
) -> httpx.Response:
    """构造带真实 request 对象的统一 HTTP 响应。"""

    return httpx.Response(
        status,
        headers={
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Cache-Control": cache_control,
        },
        json={"request_id": request_id, "trace_id": trace_id, **payload},
        request=httpx.Request("GET", "http://localhost:8001/cost-anomalies"),
    )


def _prepare_owner_chain(database_url: str, tenant_id: str) -> dict[str, str]:
    """在迁移夹具内补齐 Project/Application 归属，供正式 Usage owner trigger 校验。"""

    connection = connect_database(database_url)
    try:
        key = connection.execute(
            """
            SELECT virtual_key_id, environment_id
            FROM virtual_keys
            WHERE tenant_id=? AND status='active'
            ORDER BY virtual_key_id LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        assert key is not None
        timestamp = "2026-07-27T00:00:00Z"
        project_id = "prj_prd053_runtime_contract"
        application_id = "app_prd053_runtime_contract"
        connection.execute(
            """
            INSERT INTO projects(
              project_id, tenant_id, project_code, name, status, created_at, updated_at
            ) VALUES(?, ?, 'prd053-runtime-contract', 'PRD053 runtime contract',
                     'active', ?, ?)
            """,
            (project_id, tenant_id, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO applications(
              application_id, tenant_id, project_id, application_code, name,
              status, created_at, updated_at
            ) VALUES(?, ?, ?, 'prd053-runtime-contract', 'PRD053 runtime contract',
                     'active', ?, ?)
            """,
            (application_id, tenant_id, project_id, timestamp, timestamp),
        )
        connection.execute(
            """
            UPDATE virtual_keys SET project_id=?, application_id=?
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (project_id, application_id, tenant_id, key["virtual_key_id"]),
        )
        connection.commit()
        return {
            "tenant_id": tenant_id,
            "environment_id": str(key["environment_id"]),
            "project_id": project_id,
            "application_id": application_id,
            "virtual_key_id": str(key["virtual_key_id"]),
        }
    finally:
        connection.close()


def _valid_cost_event() -> tuple[dict[str, object], bytes]:
    """生成共享合同认可的 20 字段成本异常事件与稳定序列化正文。"""

    anomaly_id = "can_" + "1" * 32
    event = build_webhook_event(
        tenant_id="tenant_unit",
        event_type="cost.anomaly_detected",
        event_id="caevt_" + "2" * 32,
        timestamp=datetime(2026, 7, 27, 10, 7, tzinfo=UTC),
        actor_id=None,
        object_id=anomaly_id,
        trace_id="trc_prd053_contract",
        payload={
            "anomaly_id": anomaly_id,
            "rule_type": "virtual_key_spend_spike",
            "severity": "critical",
            "status": "open",
            "environment_id": "env_unit",
            "scope_type": "virtual_key",
            "scope_id": "vk_unit",
            "window_start": "2026-07-27T10:00:00Z",
            "window_end": "2026-07-27T10:05:00Z",
            "current_cost_micros": "1000000",
            "baseline_total_micros": "1000",
            "observed_ratio_millis": "288000000",
            "baseline_bucket_count": 288,
            "threshold_multiplier_millis": 5000,
            "minimum_cost_micros": 10000,
            "source_entry_count": 1,
            "estimated_entry_count": 0,
            "affected_virtual_key_count": 1,
            "recommended_action": "apply_temporary_rate_limit",
            "detection_version": "cost-anomaly-rules-v1",
        },
    )
    envelope = event.to_payload()
    body = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return envelope, body


def test_loopback_and_batch_database_boundaries(tmp_path: Path) -> None:
    """两个服务只能是显式 HTTP 回环，数据库只能是本批次 runtime.db。"""

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
    batch = root / "artifacts" / "prd053" / "unit-batch01"
    batch.mkdir(parents=True)
    resolved = RUNTIME.resolve_artifact_directory(batch, workspace_root=root)
    database = batch / "runtime.db"
    assert resolved == batch.resolve()
    assert RUNTIME.resolve_runtime_database(
        RUNTIME.sqlite_url_for_path(database), artifact_directory=resolved
    ) == database.resolve()
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.resolve_runtime_database(
            RUNTIME.sqlite_url_for_path(root / "runtime.db"),
            artifact_directory=resolved,
        )


def test_response_contract_requires_chain_error_and_no_store() -> None:
    """成功与错误响应都必须携带一致链路，成本接口还必须显式 no-store。"""

    request_id = "req_prd053_contract"
    trace_id = "trc_prd053_contract"
    payload = RUNTIME.verify_response_contract(
        _response(200, request_id, trace_id, {"cost_anomalies": []}),
        stage="unit.response",
        expected_status=200,
        request_id=request_id,
        trace_id=trace_id,
        require_no_store=True,
    )
    assert payload["cost_anomalies"] == []

    # OpenAI-compatible 成功体保持上游字段；链路仍必须由响应 Header 提供并进入安全证据。
    provider_response = httpx.Response(
        200,
        headers={"X-Request-ID": request_id, "X-Trace-ID": trace_id},
        json={"id": "chatcmpl_prd053_contract", "object": "chat.completion"},
        request=httpx.Request("POST", "http://localhost:8000/v1/chat/completions"),
    )
    provider_payload = RUNTIME.verify_response_contract(
        provider_response,
        stage="unit.provider_success",
        expected_status=200,
        request_id=request_id,
        trace_id=trace_id,
        require_body_chain=False,
    )
    assert RUNTIME._public_http_evidence(provider_response, provider_payload) == {
        "status": 200,
        "request_id": request_id,
        "trace_id": trace_id,
    }
    # 默认仍要求正文链路，避免普通 Control 成功响应被错误放宽。
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.verify_response_contract(
            provider_response,
            stage="unit.provider_strict_default",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
        )

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
                {"cost_anomalies": []},
                cache_control="private",
            ),
            stage="unit.response",
            expected_status=200,
            request_id=request_id,
            trace_id=trace_id,
            require_no_store=True,
        )


def test_readiness_requires_0043_without_mutating_database(tmp_path: Path) -> None:
    """缺少 0043 时立即失败，验收器不得自行迁移或补表。"""

    batch = tmp_path / "artifacts" / "prd053" / "unit-batch01"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations VALUES('0042', 'applied')")
    config = _runtime_config(batch, database)
    with (
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as control,
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        with pytest.raises(RUNTIME.AcceptanceFailure):
            acceptance.verify_runtime_database_ready()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version, status FROM schema_migrations"
        ).fetchall() == [("0042", "applied")]
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0] == 1


def test_formal_usage_projection_precedes_internal_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受控 Usage 先投影正式账本，只有内部 detector 运行后才能出现异常。"""

    batch = tmp_path / "artifacts" / "prd053" / "unit-formal"
    batch.mkdir(parents=True)
    database = batch / "runtime.db"
    database_url = RUNTIME.sqlite_url_for_path(database)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "UnitOnly!2026")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_COST_ANOMALY_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    resources = _prepare_owner_chain(database_url, seed.tenant_id)

    config = _runtime_config(batch, database)
    with (
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as control,
        httpx.Client(transport=httpx.MockTransport(_unused_transport)) as gateway,
    ):
        acceptance = RUNTIME.RuntimeAcceptance(config, control, gateway)
        acceptance.suffix = "unitformal053"
        acceptance.resource_ids.update(resources)
        assert acceptance.verify_runtime_database_ready()["migration_0043_applied"] is True

        now = datetime.now(UTC).replace(second=0, microsecond=0)
        current_window = RUNTIME.align_detection_window(now - timedelta(minutes=10))
        baseline_at = current_window - timedelta(hours=24) + timedelta(seconds=10)
        current_at = current_window + timedelta(seconds=10)
        acceptance.insert_formal_usage(
            label="baseline",
            amount_cny="0.001000",
            created_at=baseline_at,
        )
        acceptance.insert_formal_usage(
            label="current",
            amount_cny="1.000000",
            created_at=current_at,
        )

        connection = connect_database(database_url)
        try:
            assert connection.execute("SELECT COUNT(*) FROM cost_anomalies").fetchone()[0] == 0
            entries = connection.execute(
                """
                SELECT entry_type, direction, status, normalized_cny_micros,
                       cny_normalization_status, price_evidence_status
                FROM finops_ledger_entries
                WHERE tenant_id=? AND environment_id=? ORDER BY ledger_sequence DESC LIMIT 2
                """,
                (resources["tenant_id"], resources["environment_id"]),
            ).fetchall()
            assert [row["normalized_cny_micros"] for row in reversed(entries)] == [1000, 1_000_000]
            assert all(
                tuple(row[key] for key in (
                    "entry_type",
                    "direction",
                    "status",
                    "cny_normalization_status",
                    "price_evidence_status",
                )) == ("usage_charge", "debit", "posted", "normalized", "legacy_amount_only")
                for row in entries
            )
        finally:
            connection.close()

        result = acceptance.run_detection_until_idle(now=now, maximum_iterations=8)
        assert result["detector_generated_anomaly"] is True

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT rule_type, scope_type, scope_id, status
            FROM cost_anomalies WHERE tenant_id=? AND environment_id=?
            """,
            (resources["tenant_id"], resources["environment_id"]),
        ).fetchall()
        assert any(
            row["rule_type"] == "virtual_key_spend_spike"
            and row["scope_type"] == "virtual_key"
            and row["scope_id"] == resources["virtual_key_id"]
            and row["status"] == "open"
            for row in rows
        )
    finally:
        connection.close()


def test_cost_webhook_requires_signature_exact_twenty_fields_and_stable_id() -> None:
    """接收端只接受验签后的精确 20 字段事件，并保留稳定 event/anomaly ID。"""

    envelope, body = _valid_cost_event()
    secret = "whsec_unit_only_contract"
    now_seconds = int(time.time())
    signature = webhook_signature_header(secret, now_seconds, body)
    summary = RUNTIME.validate_cost_anomaly_webhook(
        body,
        signature=signature,
        signing_secret=secret,
        now_seconds=now_seconds,
    )
    assert summary == {
        "event_id": envelope["event_id"],
        "anomaly_id": envelope["object_id"],
        "payload_field_count": 20,
        "signature_valid": True,
        "privacy_verified": True,
    }

    unsafe = dict(envelope)
    unsafe_payload = dict(envelope["payload"])
    unsafe_payload["prompt"] = "must never be accepted"
    unsafe["payload"] = unsafe_payload
    unsafe_body = json.dumps(unsafe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    unsafe_signature = webhook_signature_header(secret, now_seconds, unsafe_body)
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.validate_cost_anomaly_webhook(
            unsafe_body,
            signature=unsafe_signature,
            signing_secret=secret,
            now_seconds=now_seconds,
        )


def test_safe_evidence_and_write_once(tmp_path: Path) -> None:
    """证据只保留安全 ID/状态，既拒绝明文也不能覆盖既有通过文件。"""

    evidence = {
        "schema_version": 1,
        "prd_id": "PRD-053",
        "status": "passed",
        "generated_resources": {
            "tenant_id": "tenant_unit",
            "environment_id": "env_unit",
            "project_id": "prj_unit",
            "application_id": "app_unit",
            "virtual_key_id": "vk_unit",
            # Provider Key 的不透明资源 ID 可用于追踪；只有 Key 明文和密文槽位必须拒绝。
            "provider_id": "provider_unit",
            "provider_key_id": "provider_key_unit",
            "cost_anomaly_id": "can_" + "1" * 32,
        },
        "checks": {
            "detector_generated_anomaly": True,
            "provider_attempt_count_zero": True,
            "release_restored_gateway": True,
            "all_sessions_revoked": True,
        },
    }
    RUNTIME.assert_safe_evidence(evidence, sensitive_values=("not-present",))
    target = RUNTIME.publish_evidence(tmp_path, evidence)
    assert target.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(RUNTIME.AcceptanceFailure):
        RUNTIME.publish_evidence(tmp_path, evidence)
    for unsafe, known in (
        ({"password": "plain"}, ()),
        ({"status": "prefix-known-secret-suffix"}, ("known-secret",)),
        ({"status": "Bearer opaque-session"}, ()),
        ({"status": "mhv_complete_virtual_key_value"}, ()),
    ):
        with pytest.raises(RUNTIME.AcceptanceFailure):
            RUNTIME.assert_safe_evidence(unsafe, sensitive_values=known)
