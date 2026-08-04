"""PRD-064 Control API、权限、租户、幂等与失败事件 Source Outbox 专项测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.adapter_contracts import outbox as contract_outbox
from metahub_control.adapter_contracts import service as adapter_contract_service
from metahub_control.adapter_contracts.routes import router as adapter_contracts_router
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.webhooks import service as webhook_service
from metahub_control.webhooks.routes import router as webhooks_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.provider_adapter_spec import catalog_identity, provider_adapter_specs
from metahub_shared.runtime.webhooks import canonical_json_bytes


# 固定凭据只服务当前专项角色矩阵；内部 Token 不写入报告、数据库断言或日志证据。
ADMIN_PASSWORD = "Prd064Admin!2026"
DEVELOPER_PASSWORD = "Prd064Developer!2026"
VIEWER_PASSWORD = "Prd064Viewer!2026"
SECOND_PASSWORD = "Prd064Second!2026"
INTERNAL_TOKEN = "prd064-internal-token-value-2026-abcdef"
_FIXED_CASES = (
    ("non_stream", "success"),
    ("stream", "success"),
    ("error", "auth_401"),
    ("error", "rate_limit_429"),
    ("error", "server_5xx"),
    ("error", "timeout"),
    ("usage", "standard"),
    ("usage", "missing"),
    ("usage", "invalid"),
)
_FAILURE_CODES = {
    "request": "adapter.contract_request_failed",
    "non_stream": "adapter.contract_non_stream_failed",
    "stream": "adapter.contract_stream_failed",
    "error": "adapter.contract_error_mapping_failed",
    "usage": "adapter.contract_usage_failed",
}


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str]:
    """应用 0056、seed 默认租户，并创建 Developer/Viewer/第二租户权限样本。"""

    database_url = _sqlite_url(tmp_path / "prd064-adapter-contracts.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd064-auth-secret-not-for-production")
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    second_tenant = "tenant_prd064_second"
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,'PRD064 Second','internal_only','active',datetime('now'),datetime('now'))
        """,
        (second_tenant, "prd064-second"),
    )
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',datetime('now'),datetime('now'))
        """,
        [
            (
                "usr_prd064_developer",
                seed.tenant_id,
                "prd064-developer@example.com",
                "PRD064 Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "usr_prd064_viewer",
                seed.tenant_id,
                "prd064-viewer@example.com",
                "PRD064 Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
            (
                "usr_prd064_second",
                second_tenant,
                "prd064-second@example.com",
                "PRD064 Second Developer",
                hash_password(SECOND_PASSWORD),
                "developer",
            ),
        ],
    )
    connection.commit()
    connection.close()
    # 返回三个隔离身份供后续主流程、权限与跨租户断言共享同一数据库事实。
    return database_url, seed.tenant_id, second_tenant


def _build_app():
    """构造只含本专项身份和合同路由的 Control API，避免无关 scheduler 干扰证据。"""

    app = create_service_app("control-plane-api", "PRD-064 Adapter Contract Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(adapter_contracts_router)
    # 失败合同的 pending/dead-letter 恢复必须经过真实 Webhook HTTP 路由完成重放。
    app.include_router(webhooks_router)
    return app


async def _login(
    client: AsyncClient,
    tenant_slug: str,
    email: str,
    password: str,
    request_id: str,
) -> str:
    """登录一个测试角色并返回仅在当前协程内使用的 Bearer Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200, response.text
    # Token 只留在当前测试协程内构造 Authorization，不写入测试证据或日志。
    return response.json()["access_token"]


def _report(
    tenant_id: str,
    *,
    run_hex: str,
    request_id: str,
    trace_id: str,
    failed: bool = False,
) -> dict[str, object]:
    """从 Shared spec 动态构造 exact-set 安全报告，不写死 92 条。"""

    run_id = "acr_" + run_hex * 32
    cases: list[dict[str, object]] = []
    failed_assigned = False
    for spec in provider_adapter_specs():
        definitions = [
            ("request", "auth", auth_type) for auth_type in spec.allowed_auth_types
        ] + [(kind, variant, None) for kind, variant in _FIXED_CASES]
        for kind, variant, auth_type in definitions:
            suffix = f":{auth_type}" if auth_type else ""
            case_key = f"{spec.contract_case_id}:{kind}:{variant}{suffix}"
            should_fail = failed and not failed_assigned and kind == "usage" and variant == "standard"
            if should_fail:
                failed_assigned = True
            cases.append(
                {
                    "case_key": case_key,
                    "adapter_contract_case_id": "acc_"
                    + hashlib.sha256(f"{run_id}:{case_key}".encode()).hexdigest()[:32],
                    "contract_case_id": spec.contract_case_id,
                    "tenant_id": tenant_id,
                    "spec_id": spec.spec_id,
                    "provider_type": spec.provider_type,
                    "adapter_name": spec.adapter_name,
                    "adapter_version": spec.adapter_version,
                    "case_kind": kind,
                    "variant": variant,
                    "auth_type": auth_type,
                    "status": "failed" if should_fail else "passed",
                    "failure_code": _FAILURE_CODES[kind] if should_fail else None,
                    "duration_ms": 1,
                    "evidence_hash": hashlib.sha256(case_key.encode()).hexdigest(),
                }
            )
    completed = (datetime.now(UTC) - timedelta(seconds=1)).replace(microsecond=0)
    started = completed - timedelta(seconds=1)
    failed_count = 1 if failed else 0
    identity = catalog_identity()
    return {
        "schema_version": "adapter-contract-report-v1",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "catalog_version": identity.version,
        "catalog_hash": identity.catalog_hash,
        "runner_version": "1.0.0",
        "suite_kind": "merge_gate",
        "mode": "mock",
        "source_revision_hash": "b" * 64,
        "status": "failed" if failed else "passed",
        "coverage_complete": True,
        "total_cases": len(cases),
        "passed_cases": len(cases) - failed_count,
        "failed_cases": failed_count,
        "failure_codes": ["adapter.contract_usage_failed"] if failed else [],
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "completed_at": completed.isoformat().replace("+00:00", "Z"),
        "cases": cases,
    }


def _internal_headers(request_id: str, trace_id: str) -> dict[str, str]:
    """构造内部上报的唯一 Token 与链路 Header，不把 Token放入报告。"""

    return {
        "X-Internal-Token": INTERNAL_TOKEN,
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
    }


def _db_datetime(value: str) -> datetime:
    """把数据库规范 UTC 秒转换为带时区时间，供 lease 与固定退避断言使用。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _assert_error(response: Response, code: str, status: int) -> None:
    """断言统一错误信封、链路 ID 与合同路径 no-store。"""

    assert response.status_code == status, response.text
    payload = response.json()
    assert payload["code"] == code
    assert payload["request_id"]
    assert payload["trace_id"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.anyio
async def test_internal_ingest_auth_strict_json_idempotency_and_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主/异常：身份先于正文、严格 JSON、成功写入、精确重放、冲突和目录漂移。"""

    database_url, tenant_id, second_tenant = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    request_id = "req_prd064_upload_passed"
    trace_id = "trc_prd064_upload_passed"
    report = _report(
        tenant_id,
        run_hex="1",
        request_id=request_id,
        trace_id=trace_id,
    )
    path = f"/internal/tenants/{tenant_id}/adapter-contract-runs"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        missing = await client.post(
            path,
            content=b"{",
            headers={"Content-Type": "application/json", "X-Request-ID": "req_missing"},
        )
        wrong = await client.post(
            path,
            json=report,
            headers={"X-Internal-Token": "invalid-internal-token"},
        )
        collision = await client.post(
            path,
            content=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": INTERNAL_TOKEN,
                "X-Trace-ID": INTERNAL_TOKEN,
            },
        )
        mixed = await client.post(
            path,
            json=report,
            headers={
                **_internal_headers(request_id, trace_id),
                "Authorization": "Bearer user-session-not-allowed",
            },
        )
        duplicate = await client.post(
            path,
            content=(
                b'{"schema_version":"adapter-contract-report-v1",'
                b'"schema_version":"adapter-contract-report-v1"}'
            ),
            headers={**_internal_headers("req_prd064_duplicate", "trc_prd064_duplicate"),
                     "Content-Type": "application/json"},
        )
        oversized = await client.post(
            path,
            content=b'{"payload":"' + (b"x" * (512 * 1024)) + b'"}',
            headers={**_internal_headers("req_prd064_oversized", "trc_prd064_oversized"),
                     "Content-Type": "application/json"},
        )
        first = await client.post(path, json=report, headers=_internal_headers(request_id, trace_id))
        replay = await client.post(path, json=report, headers=_internal_headers(request_id, trace_id))
        cross_request_id = "req_prd064_cross_tenant_run_collision"
        cross_trace_id = "trc_prd064_cross_tenant_run_collision"
        cross_report = _report(
            second_tenant,
            run_hex="1",
            request_id=cross_request_id,
            trace_id=cross_trace_id,
        )
        cross_tenant_conflict = await client.post(
            f"/internal/tenants/{second_tenant}/adapter-contract-runs",
            json=cross_report,
            headers=_internal_headers(cross_request_id, cross_trace_id),
        )
        changed = dict(report)
        changed["source_revision_hash"] = "c" * 64
        conflict = await client.post(
            path,
            json=changed,
            headers=_internal_headers(request_id, trace_id),
        )
        mismatch_report = _report(
            tenant_id,
            run_hex="2",
            request_id="req_prd064_catalog_mismatch",
            trace_id="trc_prd064_catalog_mismatch",
        )
        mismatch_report["catalog_hash"] = "0" * 64
        mismatch = await client.post(
            path,
            json=mismatch_report,
            headers=_internal_headers(
                "req_prd064_catalog_mismatch", "trc_prd064_catalog_mismatch"
            ),
        )
        probe_request_id = "req_prd064_probe_mock_kind"
        probe_trace_id = "trc_prd064_probe_mock_kind"
        probe_report = _report(
            tenant_id,
            run_hex="5",
            request_id=probe_request_id,
            trace_id=probe_trace_id,
        )
        probe_case = next(
            case for case in probe_report["cases"] if case["case_kind"] == "request"
        )
        probe_report.update(
            {
                "suite_kind": "probe",
                "mode": "gateway_probe",
                "coverage_complete": False,
                "total_cases": 1,
                "passed_cases": 1,
                "failed_cases": 0,
                "cases": [probe_case],
            }
        )
        disguised_probe = await client.post(
            path,
            json=probe_report,
            headers=_internal_headers(probe_request_id, probe_trace_id),
        )
        valid_probe_request_id = "req_prd064_probe_valid"
        valid_probe_trace_id = "trc_prd064_probe_valid"
        valid_probe_report = _report(
            tenant_id,
            run_hex="9",
            request_id=valid_probe_request_id,
            trace_id=valid_probe_trace_id,
        )
        valid_probe_case = next(
            case
            for case in valid_probe_report["cases"]
            if case["case_kind"] == "non_stream"
        )
        valid_probe_report.update(
            {
                "suite_kind": "probe",
                "mode": "gateway_probe",
                "coverage_complete": False,
                "total_cases": 1,
                "passed_cases": 1,
                "failed_cases": 0,
                "cases": [valid_probe_case],
            }
        )
        valid_probe = await client.post(
            path,
            json=valid_probe_report,
            headers=_internal_headers(valid_probe_request_id, valid_probe_trace_id),
        )

    _assert_error(missing, "auth.internal_token_required", 401)
    _assert_error(wrong, "auth.internal_token_invalid", 403)
    _assert_error(collision, "adapter.contract_report_invalid", 422)
    # 内部 Token 即使被恶意复用为链路 Header，也必须在响应头和统一错误正文中彻底替换。
    assert collision.headers["X-Request-ID"] != INTERNAL_TOKEN
    assert collision.headers["X-Trace-ID"] != INTERNAL_TOKEN
    assert INTERNAL_TOKEN not in collision.text
    _assert_error(mixed, "auth.internal_token_invalid", 403)
    _assert_error(duplicate, "adapter.contract_report_invalid", 422)
    _assert_error(oversized, "adapter.contract_report_invalid", 422)
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["replayed"] is False
    assert first.json()["adapter_contract_run"]["total_cases"] == len(report["cases"])
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["adapter_contract_run"] == first.json()["adapter_contract_run"]
    _assert_error(conflict, "adapter.contract_run_conflict", 409)
    _assert_error(cross_tenant_conflict, "adapter.contract_run_conflict", 409)
    _assert_error(mismatch, "adapter.contract_catalog_mismatch", 409)
    _assert_error(disguised_probe, "adapter.contract_report_invalid", 422)
    assert valid_probe.status_code == 200, valid_probe.text
    assert valid_probe.json()["adapter_contract_run"]["suite_kind"] == "probe"
    assert valid_probe.json()["adapter_contract_run"]["total_cases"] == 1

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_runs WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_cases WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0] == len(report["cases"]) + 1
    # 全局 run_id 冲突不能在第二租户留下父行、case 或失败事件，也不能复用首租户事实。
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_runs WHERE tenant_id=?",
        (second_tenant,),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_cases WHERE tenant_id=?",
        (second_tenant,),
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_event_outbox WHERE tenant_id=?",
        (second_tenant,),
    ).fetchone()[0] == 0
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
    # 访问日志和错误日志也必须使用替换后的链路身份，不能只保证 HTTP 响应不回显 Token。
    assert all(
        INTERNAL_TOKEN not in record.getMessage()
        and getattr(record, "request_id", None) != INTERNAL_TOKEN
        and getattr(record, "trace_id", None) != INTERNAL_TOKEN
        for record in caplog.records
    )


@pytest.mark.anyio
async def test_read_permissions_tenant_isolation_and_failed_event_outbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/事件：Developer 可读、Viewer 403、匿名 401、第二租户 404，失败 run 可恢复精确事件。"""

    database_url, tenant_id, _second_tenant = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    request_id = "req_prd064_upload_failed"
    trace_id = "trc_prd064_upload_failed"
    report = _report(
        tenant_id,
        run_hex="3",
        request_id=request_id,
        trace_id=trace_id,
        failed=True,
    )
    path = f"/internal/tenants/{tenant_id}/adapter-contract-runs"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        uploaded = await client.post(path, json=report, headers=_internal_headers(request_id, trace_id))
        developer = await _login(
            client,
            "default",
            "prd064-developer@example.com",
            DEVELOPER_PASSWORD,
            "req_prd064_login_developer",
        )
        viewer = await _login(
            client,
            "default",
            "prd064-viewer@example.com",
            VIEWER_PASSWORD,
            "req_prd064_login_viewer",
        )
        second = await _login(
            client,
            "prd064-second",
            "prd064-second@example.com",
            SECOND_PASSWORD,
            "req_prd064_login_second",
        )
        developer_headers = {"Authorization": f"Bearer {developer}"}
        listed = await client.get(
            "/adapter-contract-runs?status=failed&suite_kind=merge_gate&mode=mock&limit=1",
            headers=developer_headers,
        )
        detail = await client.get(
            f"/adapter-contract-runs/{report['run_id']}",
            headers=developer_headers,
        )
        anonymous_denied = await client.get(
            "/adapter-contract-runs",
            headers={
                "X-Request-ID": "req_prd064_anonymous_read",
                "X-Trace-ID": "trc_prd064_anonymous_read",
            },
        )
        viewer_denied = await client.get(
            "/adapter-contract-runs",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        developer_navigation = await client.get(
            "/console/navigation?active_item_id=adapter-contract-runs",
            headers=developer_headers,
        )
        viewer_navigation_denied = await client.get(
            "/console/navigation?active_item_id=adapter-contract-runs",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        tenant_query = await client.get(
            "/adapter-contract-runs?tenant_id=other",
            headers=developer_headers,
        )
        duplicate_query = await client.get(
            "/adapter-contract-runs?status=failed&status=passed",
            headers=developer_headers,
        )
        second_hidden = await client.get(
            f"/adapter-contract-runs/{report['run_id']}",
            headers={"Authorization": f"Bearer {second}"},
        )

    assert uploaded.status_code == 200, uploaded.text
    assert listed.status_code == 200, listed.text
    assert listed.headers["Cache-Control"] == "no-store"
    assert [item["tenant_id"] for item in listed.json()["adapter_contract_runs"]] == [tenant_id]
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["cases"]) == report["total_cases"]
    assert {case["tenant_id"] for case in detail.json()["cases"]} == {tenant_id}
    _assert_error(anonymous_denied, "auth.token_missing", 401)
    _assert_error(viewer_denied, "auth.permission_denied", 403)
    assert developer_navigation.status_code == 200, developer_navigation.text
    contract_navigation = next(
        item
        for item in developer_navigation.json()["navigation_items"]
        if item["id"] == "adapter-contract-runs"
    )
    assert contract_navigation["target_section"] == "adapter-contract-runs"
    assert contract_navigation["required_permissions"] == ["provider.health.read"]
    assert contract_navigation["order"] == 44
    assert viewer_navigation_denied.status_code == 422
    assert viewer_navigation_denied.json()["code"] == "console.navigation_invalid"
    assert viewer_navigation_denied.json()["request_id"]
    _assert_error(tenant_query, "adapter.contract_report_invalid", 422)
    _assert_error(duplicate_query, "adapter.contract_report_invalid", 422)
    _assert_error(second_hidden, "adapter.contract_run_not_found", 404)

    captured: list[object] = []

    def _capture_dispatch(*args: object, **kwargs: object) -> list[object]:
        """捕获已通过 Shared exact validator 的事件，避免专项测试执行任何网络 I/O。"""

        event = kwargs.get("event") if "event" in kwargs else args[2]
        captured.append(event)
        return []

    monkeypatch.setattr(contract_outbox, "dispatch_webhook_event", _capture_dispatch)
    assert contract_outbox.drain_adapter_contract_event_once(
        app.state.config,
        worker_id="prd064-test-worker",
    ) is True
    assert len(captured) == 1
    event = captured[0]
    assert event.event_type == "adapter.contract_failed"
    assert event.tenant_id == tenant_id
    assert event.object_id == report["run_id"]
    assert event.actor_id == "adapter-contract-runner"
    assert event.trace_id == trace_id
    assert event.payload == {
        "run_id": report["run_id"],
        "catalog_version": report["catalog_version"],
        "catalog_hash": report["catalog_hash"],
        "suite_kind": "merge_gate",
        "mode": "mock",
        "total_cases": report["total_cases"],
        "failed_cases": 1,
        "failure_codes": ["adapter.contract_usage_failed"],
        "completed_at": report["completed_at"],
    }
    connection = connect_database(database_url)
    source = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code
        FROM adapter_contract_event_outbox WHERE tenant_id=? AND adapter_contract_run_id=?
        """,
        (tenant_id, report["run_id"]),
    ).fetchone()
    stored_request_id = connection.execute(
        """
        SELECT source_request_id FROM adapter_contract_runs
        WHERE tenant_id=? AND adapter_contract_run_id=?
        """,
        (tenant_id, report["run_id"]),
    ).fetchone()[0]
    # 模拟进程重启后只剩公开 pending 信封；恢复校验必须从不可变 run 找回首次上报身份。
    pending_delivery = connection.execute(
        """
        SELECT ? AS payload_json,? AS tenant_id,? AS event_id,
               ? AS event_type,? AS event_timestamp
        """,
        (
            json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True),
            tenant_id,
            event.event_id,
            event.event_type,
            event.timestamp,
        ),
    ).fetchone()
    resumed_body = webhook_service._validated_pending_delivery_body(
        connection,
        pending_delivery,
    )
    connection.close()
    assert tuple(source) == ("dispatched", 1, None)
    assert stored_request_id == request_id
    assert resumed_body == canonical_json_bytes(event.to_payload())
    assert b"source_request_id" not in resumed_body

    retry_request_id = "req_prd064_upload_retry"
    retry_trace_id = "trc_prd064_upload_retry"
    retry_report = _report(
        tenant_id,
        run_hex="4",
        request_id=retry_request_id,
        trace_id=retry_trace_id,
        failed=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        retry_uploaded = await client.post(
            path,
            json=retry_report,
            headers=_internal_headers(retry_request_id, retry_trace_id),
        )
    assert retry_uploaded.status_code == 200, retry_uploaded.text

    connection = connect_database(database_url)
    retry_source = connection.execute(
        """
        SELECT * FROM adapter_contract_event_outbox
        WHERE tenant_id=? AND adapter_contract_run_id=?
        """,
        (tenant_id, retry_report["run_id"]),
    ).fetchone()
    connection.close()
    due = _db_datetime(retry_source["next_attempt_at"])

    def _failed_dispatch(*_args: object, **_kwargs: object) -> list[object]:
        """模拟公共 Webhook dispatcher 固定失败，不携带 Endpoint 或响应正文。"""

        # 只抛共享固定错误码，验证 source 不会持久化任意异常文本。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(contract_outbox, "dispatch_webhook_event", _failed_dispatch)
    first_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-retry-worker-1",
        now=due,
    )
    assert first_claim is not None and first_claim.dispatch_attempts == 1
    first_completed = first_claim.lease_expires_at - timedelta(seconds=29)
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        first_claim,
        now=first_completed,
    ) is True
    connection = connect_database(database_url)
    retry_pending = connection.execute(
        "SELECT * FROM adapter_contract_event_outbox WHERE event_id=?",
        (first_claim.event_id,),
    ).fetchone()
    connection.close()
    assert retry_pending["dispatch_status"] == "pending"
    assert retry_pending["dispatch_attempts"] == 1
    assert retry_pending["last_error_code"] == "webhook.delivery_unavailable"
    second_due = _db_datetime(retry_pending["next_attempt_at"])
    assert second_due - first_completed == timedelta(seconds=5)

    retried_events: list[object] = []

    def _successful_retry(*args: object, **kwargs: object) -> list[object]:
        """捕获退避后的恢复投递，并确认仍使用首次事件身份和恢复模式。"""

        retried_events.append(kwargs.get("event") if "event" in kwargs else args[2])
        assert kwargs["resume_existing_pending"] is True
        return []

    monkeypatch.setattr(contract_outbox, "dispatch_webhook_event", _successful_retry)
    second_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-retry-worker-2",
        now=second_due,
    )
    assert second_claim is not None and second_claim.dispatch_attempts == 2
    assert second_claim.event_id == first_claim.event_id
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        second_claim,
        now=second_claim.lease_expires_at - timedelta(seconds=29),
    ) is True
    assert len(retried_events) == 1
    assert retried_events[0].event_id == first_claim.event_id
    connection = connect_database(database_url)
    retry_finished = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code
        FROM adapter_contract_event_outbox WHERE event_id=?
        """,
        (first_claim.event_id,),
    ).fetchone()
    connection.close()
    assert tuple(retry_finished) == ("dispatched", 2, None)

    # 用无业务表的隔离内存库制造真实 SQLite 查询失败；认证仍从原数据库恢复，证明只有
    # Adapter Contract 读路径被故障注入，列表和详情都必须统一转换为 503。
    monkeypatch.setattr(
        adapter_contract_service,
        "_connect",
        lambda _config: sqlite3.connect(":memory:"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        list_storage_failure = await client.get(
            "/adapter-contract-runs",
            headers={
                **developer_headers,
                "X-Request-ID": "req_prd064_list_storage_failure",
                "X-Trace-ID": "trc_prd064_list_storage_failure",
            },
        )
        detail_storage_failure = await client.get(
            f"/adapter-contract-runs/{report['run_id']}",
            headers={
                **developer_headers,
                "X-Request-ID": "req_prd064_detail_storage_failure",
                "X-Trace-ID": "trc_prd064_detail_storage_failure",
            },
        )
    _assert_error(
        list_storage_failure,
        "adapter.contract_storage_unavailable",
        503,
    )
    _assert_error(
        detail_storage_failure,
        "adapter.contract_storage_unavailable",
        503,
    )
    assert list_storage_failure.json()["request_id"] == "req_prd064_list_storage_failure"
    assert detail_storage_failure.json()["request_id"] == "req_prd064_detail_storage_failure"


@pytest.mark.anyio
async def test_contract_failed_webhook_pending_dead_letter_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """恢复闭环：合同失败事件从 source pending 恢复，并可由最终失败进入死信后重放。"""

    database_url, tenant_id, _second_tenant = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    path = f"/internal/tenants/{tenant_id}/adapter-contract-runs"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(
            client,
            "default",
            "prd064-developer@example.com",
            DEVELOPER_PASSWORD,
            "req_prd064_webhook_login",
        )
        developer_headers = {"Authorization": f"Bearer {developer}"}
        endpoint = await client.post(
            "/webhooks",
            headers=developer_headers,
            json={
                "name": "PRD064 contract recovery",
                "endpoint_url": "https://webhook.invalid/prd064-contract",
                "event_types": ["adapter.contract_failed"],
                "timeout_ms": 1000,
            },
        )
    assert endpoint.status_code == 200, endpoint.text
    endpoint_id = endpoint.json()["webhook"]["webhook_endpoint_id"]
    assert endpoint.json()["webhook"]["event_types"] == ["adapter.contract_failed"]

    pending_request_id = "req_prd064_webhook_pending"
    pending_trace_id = "trc_prd064_webhook_pending"
    pending_report = _report(
        tenant_id,
        run_hex="a",
        request_id=pending_request_id,
        trace_id=pending_trace_id,
        failed=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        pending_uploaded = await client.post(
            path,
            json=pending_report,
            headers=_internal_headers(pending_request_id, pending_trace_id),
        )
    assert pending_uploaded.status_code == 200, pending_uploaded.text

    pending_bodies: list[bytes] = []

    def _interrupt_after_intent(
        _config: object,
        _endpoint: object,
        body: bytes,
        _delivery_id: str,
    ) -> dict[str, object]:
        """模拟 Endpoint intent 已提交、首次联网尚未形成 attempt 时进程中断。"""

        pending_bodies.append(body)
        # 固定领域错误模拟可恢复中断，避免把 Endpoint、响应正文或自由异常写入 source outbox。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "_send_once", _interrupt_after_intent)
    connection = connect_database(database_url)
    pending_due = _db_datetime(
        connection.execute(
            "SELECT next_attempt_at FROM adapter_contract_event_outbox WHERE event_id=?",
            (f"evt_{pending_report['run_id']}",),
        ).fetchone()[0]
    )
    connection.close()
    first_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-public-pending-worker-1",
        now=pending_due,
    )
    assert first_claim is not None
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        first_claim,
        now=first_claim.lease_expires_at - timedelta(seconds=29),
    ) is True

    connection = connect_database(database_url)
    pending_source = connection.execute(
        "SELECT * FROM adapter_contract_event_outbox WHERE event_id=?",
        (first_claim.event_id,),
    ).fetchone()
    pending_delivery = connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND webhook_endpoint_id=? AND event_id=? AND delivery_kind='original'
        """,
        (tenant_id, endpoint_id, first_claim.event_id),
    ).fetchone()
    connection.close()
    assert pending_source["dispatch_status"] == "pending"
    assert pending_source["last_error_code"] == "webhook.delivery_unavailable"
    assert pending_delivery["status"] == "pending"
    assert pending_delivery["attempt_count"] == 0
    assert len(pending_bodies) == 1
    pending_envelope = json.loads(pending_bodies[0].decode("utf-8"))
    assert pending_envelope["event_type"] == "adapter.contract_failed"
    assert pending_envelope["event_id"] == first_claim.event_id
    assert pending_envelope["payload"]["run_id"] == pending_report["run_id"]
    assert "source_request_id" not in pending_envelope

    recovered_bodies: list[bytes] = []

    def _recover_success(
        _config: object,
        _endpoint: object,
        body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """恢复同一 pending delivery，并只返回不含响应正文的固定成功摘要。"""

        assert delivery_id == pending_delivery["webhook_delivery_id"]
        recovered_bodies.append(body)
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    monkeypatch.setattr(webhook_service, "_send_once", _recover_success)
    second_due = _db_datetime(pending_source["next_attempt_at"])
    second_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-public-pending-worker-2",
        now=second_due,
    )
    assert second_claim is not None
    assert second_claim.event_id == first_claim.event_id
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        second_claim,
        now=second_claim.lease_expires_at - timedelta(seconds=29),
    ) is True
    connection = connect_database(database_url)
    recovered_source = connection.execute(
        "SELECT dispatch_status,dispatch_attempts,last_error_code "
        "FROM adapter_contract_event_outbox WHERE event_id=?",
        (first_claim.event_id,),
    ).fetchone()
    recovered_delivery = connection.execute(
        "SELECT status,attempt_count,event_id,payload_json FROM webhook_deliveries "
        "WHERE webhook_delivery_id=?",
        (pending_delivery["webhook_delivery_id"],),
    ).fetchone()
    connection.close()
    assert tuple(recovered_source) == ("dispatched", 2, None)
    assert tuple(recovered_delivery[:3]) == ("success", 1, first_claim.event_id)
    assert recovered_bodies == pending_bodies
    assert recovered_delivery["payload_json"].encode("utf-8") == pending_bodies[0]

    dead_request_id = "req_prd064_webhook_dead_letter"
    dead_trace_id = "trc_prd064_webhook_dead_letter"
    dead_report = _report(
        tenant_id,
        run_hex="b",
        request_id=dead_request_id,
        trace_id=dead_trace_id,
        failed=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        dead_uploaded = await client.post(
            path,
            json=dead_report,
            headers=_internal_headers(dead_request_id, dead_trace_id),
        )
    assert dead_uploaded.status_code == 200, dead_uploaded.text

    dead_bodies: list[bytes] = []

    def _terminal_failure(
        _config: object,
        _endpoint: object,
        body: bytes,
        _delivery_id: str,
    ) -> dict[str, object]:
        """让公共 dispatcher 用持久尝试上限终结，并由迁移 trigger 原子生成死信。"""

        dead_bodies.append(body)
        return {
            "status": "failed",
            "http_status": 503,
            "duration_ms": 1,
            "error_code": "webhook.http_error",
            "error_summary": "Receiver is unavailable.",
        }

    monkeypatch.setattr(webhook_service, "_send_once", _terminal_failure)
    connection = connect_database(database_url)
    dead_due = _db_datetime(
        connection.execute(
            "SELECT next_attempt_at FROM adapter_contract_event_outbox WHERE event_id=?",
            (f"evt_{dead_report['run_id']}",),
        ).fetchone()[0]
    )
    connection.close()
    dead_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-public-dead-letter-worker",
        now=dead_due,
    )
    assert dead_claim is not None
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        dead_claim,
        now=dead_claim.lease_expires_at - timedelta(seconds=29),
    ) is True
    connection = connect_database(database_url)
    failed_delivery = connection.execute(
        "SELECT * FROM webhook_deliveries WHERE tenant_id=? AND event_id=?",
        (tenant_id, dead_claim.event_id),
    ).fetchone()
    dead_letter = connection.execute(
        "SELECT * FROM webhook_dead_letter_events WHERE tenant_id=? AND event_id=?",
        (tenant_id, dead_claim.event_id),
    ).fetchone()
    dead_source = connection.execute(
        "SELECT dispatch_status,dispatch_attempts,last_error_code "
        "FROM adapter_contract_event_outbox WHERE event_id=?",
        (dead_claim.event_id,),
    ).fetchone()
    connection.close()
    assert failed_delivery["status"] == "failed"
    assert failed_delivery["attempt_count"] == failed_delivery["max_attempts"] == 3
    assert len(dead_bodies) == 3
    assert len(set(dead_bodies)) == 1
    assert dead_letter["status"] == "dead_letter"
    assert dead_letter["event_type"] == "adapter.contract_failed"
    assert dead_letter["webhook_delivery_id"] == failed_delivery["webhook_delivery_id"]
    assert tuple(dead_source) == ("dispatched", 1, None)

    replay_bodies: list[bytes] = []

    def _replay_success(
        _config: object,
        _endpoint: object,
        body: bytes,
        _delivery_id: str,
    ) -> dict[str, object]:
        """死信重放只使用持久信封，并返回固定成功摘要完成根状态收敛。"""

        replay_bodies.append(body)
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    monkeypatch.setattr(webhook_service, "_send_once", _replay_success)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        replayed = await client.post(
            f"/webhook-deliveries/{failed_delivery['webhook_delivery_id']}/replay",
            headers=developer_headers,
            json={"idempotency_key": "prd064_contract_replay_001"},
        )
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers["Cache-Control"] == "no-store"
    assert replayed.json()["delivery"]["status"] == "success"
    assert replayed.json()["dead_letter"]["status"] == "replayed"
    assert replayed.json()["delivery"]["event_id"] == dead_claim.event_id
    assert replay_bodies == [dead_bodies[0]]


@pytest.mark.anyio
async def test_outbox_lease_takeover_and_tenth_orphan_reaper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常恢复：过期 lease 可接管，旧 owner CAS 失败，第十次孤儿只终结不再出网。"""

    database_url, tenant_id, _second_tenant = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    takeover_report = _report(
        tenant_id,
        run_hex="6",
        request_id="req_prd064_takeover",
        trace_id="trc_prd064_takeover",
        failed=True,
    )
    orphan_report = _report(
        tenant_id,
        run_hex="7",
        request_id="req_prd064_orphan",
        trace_id="trc_prd064_orphan",
        failed=True,
    )
    path = f"/internal/tenants/{tenant_id}/adapter-contract-runs"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        takeover_uploaded = await client.post(
            path,
            json=takeover_report,
            headers=_internal_headers("req_prd064_takeover", "trc_prd064_takeover"),
        )
        orphan_uploaded = await client.post(
            path,
            json=orphan_report,
            headers=_internal_headers("req_prd064_orphan", "trc_prd064_orphan"),
        )
    assert takeover_uploaded.status_code == 200, takeover_uploaded.text
    assert orphan_uploaded.status_code == 200, orphan_uploaded.text

    connection = connect_database(database_url)
    takeover_due = connection.execute(
        "SELECT next_attempt_at FROM adapter_contract_event_outbox WHERE event_id=?",
        (f"evt_{takeover_report['run_id']}",),
    ).fetchone()[0]
    connection.close()
    stale_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-stale-owner",
        now=_db_datetime(takeover_due),
    )
    assert stale_claim is not None and stale_claim.dispatch_attempts == 1
    recovered_claim = contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-recovered-owner",
        now=stale_claim.lease_expires_at,
    )
    assert recovered_claim is not None and recovered_claim.dispatch_attempts == 2
    assert recovered_claim.event_id == stale_claim.event_id

    delivered: list[object] = []

    def _capture_takeover(*args: object, **kwargs: object) -> list[object]:
        """捕获相同稳定事件的 at-least-once 投递，不访问任何真实 Endpoint。"""

        delivered.append(kwargs.get("event") if "event" in kwargs else args[2])
        return []

    monkeypatch.setattr(contract_outbox, "dispatch_webhook_event", _capture_takeover)
    # 旧 owner 即使网络调用返回也不能覆盖第二个 claim；稳定 event_id 允许接收端幂等去重。
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        stale_claim,
        now=stale_claim.lease_expires_at + timedelta(seconds=1),
    ) is False
    assert contract_outbox.dispatch_claimed_adapter_contract_event(
        app.state.config,
        recovered_claim,
        now=recovered_claim.lease_expires_at - timedelta(seconds=29),
    ) is True
    assert len(delivered) == 2
    assert {event.event_id for event in delivered} == {stale_claim.event_id}

    connection = connect_database(database_url)
    takeover_finished = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code
        FROM adapter_contract_event_outbox WHERE event_id=?
        """,
        (stale_claim.event_id,),
    ).fetchone()
    orphan_due = connection.execute(
        "SELECT next_attempt_at FROM adapter_contract_event_outbox WHERE event_id=?",
        (f"evt_{orphan_report['run_id']}",),
    ).fetchone()[0]
    connection.close()
    assert tuple(takeover_finished) == ("dispatched", 2, None)

    due = _db_datetime(orphan_due)
    for expected_attempt in range(1, 11):
        orphan_claim = contract_outbox.claim_next_adapter_contract_event(
            app.state.config,
            worker_id=f"prd064-orphan-owner-{expected_attempt}",
            now=due,
        )
        assert orphan_claim is not None
        assert orphan_claim.adapter_contract_run_id == orphan_report["run_id"]
        assert orphan_claim.dispatch_attempts == expected_attempt
        due = orphan_claim.lease_expires_at
    # 第十次 lease 到期只执行本地终结，不能产生第十一次 Webhook 调用。
    assert contract_outbox.claim_next_adapter_contract_event(
        app.state.config,
        worker_id="prd064-orphan-reaper",
        now=due,
    ) is None
    connection = connect_database(database_url)
    orphan_finished = connection.execute(
        """
        SELECT dispatch_status,dispatch_attempts,last_error_code,
               claim_token,lease_owner_id,lease_expires_at
        FROM adapter_contract_event_outbox WHERE event_id=?
        """,
        (f"evt_{orphan_report['run_id']}",),
    ).fetchone()
    connection.close()
    assert tuple(orphan_finished) == (
        "failed",
        10,
        "adapter.contract_storage_unavailable",
        None,
        None,
        None,
    )


@pytest.mark.anyio
async def test_outbox_insert_failure_rolls_back_run_cases_and_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务异常：source 触发器失败时 run/cases 全部回滚，并返回统一存储错误。"""

    database_url, tenant_id, _second_tenant = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        CREATE TRIGGER trg_prd064_test_source_failure
        BEFORE INSERT ON adapter_contract_event_outbox FOR EACH ROW
        BEGIN SELECT RAISE(ABORT,'prd064_test_source_failure'); END
        """
    )
    connection.commit()
    connection.close()
    app = _build_app()
    request_id = "req_prd064_source_failure"
    trace_id = "trc_prd064_source_failure"
    report = _report(
        tenant_id,
        run_hex="8",
        request_id=request_id,
        trace_id=trace_id,
        failed=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        response = await client.post(
            f"/internal/tenants/{tenant_id}/adapter-contract-runs",
            json=report,
            headers=_internal_headers(request_id, trace_id),
        )
    _assert_error(response, "adapter.contract_storage_unavailable", 503)
    connection = connect_database(database_url)
    counts = tuple(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        for table_name in (
            "adapter_contract_runs",
            "adapter_contract_cases",
            "adapter_contract_event_outbox",
        )
    )
    connection.close()
    assert counts == (0, 0, 0)
