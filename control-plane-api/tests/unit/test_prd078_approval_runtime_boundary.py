"""PRD-078 共享 request-context 与审批私有缓存边界合同。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from metahub_control.approvals import scheduler
from metahub_control.approvals.service import SchedulerTenantLease
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """构造最小 Control 壳，直接观察最外层路径策略。"""

    config = AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        service_mode="internal_only",
        log_level="INFO",
        allowed_origins=[],
        auth_token_secret="test-auth-secret-that-is-long-enough",
        cache_affinity_hmac_secret="test-cache-affinity-secret-is-long-enough",
    )
    monkeypatch.setattr(
        "metahub_shared.runtime.fastapi_app.load_app_config",
        lambda _service_name: config,
    )
    service = create_service_app("control-plane-api", "test")

    @service.get("/approvals")
    async def approval_list() -> dict[str, bool]:
        """``approval_list`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        return {"ok": True}

    @service.post("/approvals/{approval_id}/step-up")
    async def approval_step_up(approval_id: str) -> dict[str, str]:
        """``approval_step_up`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        return {"approval_id": approval_id}

    @service.post("/internal/tenants/{tenant_id}/approvals")
    async def internal_create(tenant_id: str) -> dict[str, str]:
        """``internal_create`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        # tenant_id 必须来自可信所有者关系并参与每次读写条件；缺失或不一致时立即失败关闭。
        return {"tenant_id": tenant_id}

    return service


@pytest.mark.parametrize(
    ("method", "path", "expected_vary"),
    [
        ("GET", "/approvals", "Authorization"),
        ("POST", "/approvals/apr_" + "0" * 32 + "/step-up", "Authorization"),
        ("POST", "/internal/tenants/tenant_demo/approvals", "X-Internal-Token"),
    ],
)
def test_all_approval_responses_are_private(
    app: FastAPI,
    method: str,
    path: str,
    expected_vary: str,
) -> None:
    """成功和框架错误都不能绕过审批私有缓存边界。"""

    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == expected_vary


def test_sensitive_approval_posts_ignore_client_request_ids(app: FastAPI) -> None:
    """密码、票据或内部 Token 可能碰撞链路 Header，敏感 POST 必须统一换号。"""

    supplied = "client-controlled-sensitive-id"
    paths = (
        "/approvals/apr_" + "0" * 32 + "/step-up",
        "/approvals/apr_" + "0" * 32 + "/approve",
        "/internal/tenants/tenant_demo/approvals",
    )
    with TestClient(app) as client:
        for path in paths:
            response = client.post(
                path,
                headers={"X-Request-ID": supplied, "X-Trace-ID": supplied},
            )
            assert response.headers["x-request-id"] != supplied
            assert response.headers["x-trace-id"] != supplied


@pytest.mark.parametrize("wire_kind", ("folded", "duplicate"))
def test_internal_token_candidates_never_reach_chain_ids_or_logs(
    wire_kind: str,
    app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """逗号分段与重复 Header 第二值都属于凭据，不得经链路 Header 回显。"""

    credential = "prd078-folded-internal-credential-" + "x" * 32
    token_headers = (
        [("X-Internal-Token", f"{credential},invalid")]
        if wire_kind == "folded"
        else [
            ("X-Internal-Token", "harmless-first-value"),
            ("X-Internal-Token", credential),
        ]
    )
    caplog.clear()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    with TestClient(app) as client:
        response = client.get(
            "/internal/tenants/tenant_demo/approvals",
            headers=[
                *token_headers,
                ("X-Request-ID", credential),
                ("X-Trace-ID", credential),
            ],
        )

    assert response.headers["x-request-id"] != credential
    assert response.headers["x-trace-id"] != credential
    assert credential not in response.text
    completed = [
        record
        for record in caplog.records
        if record.name == "metahub.runtime"
        and record.getMessage() == "request.completed"
        and getattr(record, "path", None) == "/internal/tenants/{tenant_id}/approvals"
    ]
    assert len(completed) == 1
    rendered = f"{completed[0].getMessage()} {completed[0].__dict__!r}"
    assert credential not in rendered
    assert completed[0].request_id != credential
    assert completed[0].trace_id != credential


def test_timeout_worker_reports_only_durable_business_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空租户扫描不能冒充业务积压，否则 timeout loop 会持续争抢 SQLite 写锁。"""

    lease = SchedulerTenantLease(
        tenant_id="tenant_prd078_idle",
        lease_generation=1,
        lease_owner_id="prd078-timeout-test",
        lease_token="a" * 64,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
    )
    config = SimpleNamespace(approval_scheduler_poll_interval_seconds=1.0)
    monkeypatch.setattr(scheduler, "claim_next_scheduler_tenant", lambda *_args, **_kwargs: lease)

    # 领取 tenant lease 后没有找到到期任务时，worker 必须回到正常轮询间隔。
    monkeypatch.setattr(scheduler, "timeout_one_due_approval", lambda *_args, **_kwargs: None)
    assert scheduler.run_approval_timeout_once(config, worker_id="prd078-idle") is False

    # 只有提交了真实 timeout 终态才允许报告进展并继续排空当前积压。
    monkeypatch.setattr(
        scheduler,
        "timeout_one_due_approval",
        lambda *_args, **_kwargs: SimpleNamespace(approval_id="apr_" + "0" * 32),
    )
    assert scheduler.run_approval_timeout_once(config, worker_id="prd078-due") is True
