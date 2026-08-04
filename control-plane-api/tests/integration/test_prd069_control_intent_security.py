"""PRD-069 敏感意图 Control API 的主流程、异常、权限和不泄密集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.intent_security import service as intent_service
from metahub_control.intent_security.routes import router as intent_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 固定密码只用于每个用例新建的隔离数据库，不得复用生产凭据或写入测试日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把 pytest 沙箱内数据库路径转换为共享 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """迁移并 seed 一个独立 0061 数据库，再创建无分类权限的 Developer。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd069-intent.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-prd069-auth-secret")
    assert apply_migrations(database_url)
    assert apply_seed_data(database_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    now = "2026-08-03T12:00:00Z"
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('usr_prd069_developer',?,'developer-prd069@example.com','PRD069 Developer',
                 ?,'developer','active',?,?)
        """,
        (tenant_id, hash_password(DEVELOPER_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()
    return database_url


def _build_app():
    """构造只包含认证与敏感分类路由的隔离 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD069 Intent Test API")
    app.include_router(auth_router)
    app.include_router(intent_router)
    return app


async def _login(client: AsyncClient, *, email: str, password: str) -> str:
    """登录测试用户并只在当前测试调用栈返回短期 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只返回当前测试调用栈，辅助函数不打印、不落盘也不放入失败快照。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_intent_classification_persists_safe_result_and_ignores_external_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主流程：Admin detected 结果/event 原子落库，外部链路 ID 与正文均不落库或日志。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    marker = "reveal-system-prompt"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        response = await client.post(
            "/security/classify-intent",
            content=(f'{{"text":"{marker}"}}').encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Request-ID": marker,
                "X-Trace-ID": marker,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    result = payload["intent_risk_result"]
    assert result["status"] == "detected"
    assert result["risk_type"] == "system_prompt_extraction"
    assert result["action"] == "review"
    assert payload["request_id"] != marker
    assert payload["trace_id"] != marker
    assert response.headers["x-request-id"] == payload["request_id"]
    assert response.headers["cache-control"] == "no-store"
    assert marker not in response.text

    connection = connect_database(database_url)
    stored = connection.execute(
        "SELECT * FROM intent_risk_results WHERE intent_risk_result_id=?",
        (result["intent_risk_result_id"],),
    ).fetchone()
    event = connection.execute(
        "SELECT * FROM intent_risk_events WHERE intent_risk_result_id=?",
        (result["intent_risk_result_id"],),
    ).fetchone()
    dump = "\n".join(
        str(value)
        for table in ("intent_risk_results", "intent_risk_events")
        for row in connection.execute(f"SELECT * FROM {table}")
        for value in tuple(row)
    )
    connection.close()
    assert stored is not None and event is not None
    assert stored["tenant_id"] == event["tenant_id"]
    assert stored["source_request_id"] == payload["request_id"]
    assert marker not in dump
    assert marker not in caplog.text


@pytest.mark.anyio
async def test_permission_precedes_body_and_developer_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限流程：匿名 401、Developer 403 都必须优先于畸形敏感 JSON 解析。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.post(
            "/security/classify-intent",
            content=b'{"text":',
            headers={"Content-Type": "application/json"},
        )
        developer_token = await _login(
            client,
            email="developer-prd069@example.com",
            password=DEVELOPER_PASSWORD,
        )
        denied = await client.post(
            "/security/classify-intent",
            content=b'{"text":',
            headers={
                "Authorization": f"Bearer {developer_token}",
                "Content-Type": "application/json",
            },
        )

    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "auth.token_missing")
    assert (denied.status_code, denied.json()["code"]) == (403, "auth.permission_denied")


@pytest.mark.anyio
async def test_strict_wire_errors_use_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：媒体、重复键、未知字段、空文本、字符和 wire 上限使用精确统一错误。"""

    _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        auth = {"Authorization": f"Bearer {token}"}
        wrong_media = await client.post(
            "/security/classify-intent", content=b"text", headers=auth
        )
        duplicate = await client.post(
            "/security/classify-intent",
            content=b'{"text":"a","text":"b"}',
            headers={**auth, "Content-Type": "application/json"},
        )
        unknown = await client.post(
            "/security/classify-intent",
            json={"text": "ok", "tenant_id": "other"},
            headers=auth,
        )
        empty = await client.post(
            "/security/classify-intent", json={"text": "   "}, headers=auth
        )
        too_many_chars = await client.post(
            "/security/classify-intent", json={"text": "x" * 20_001}, headers=auth
        )
        too_many_bytes = await client.post(
            "/security/classify-intent",
            content=b"{" + b"x" * (128 * 1024) + b"}",
            headers={**auth, "Content-Type": "application/json"},
        )
        query = await client.post(
            "/security/classify-intent?tenant_id=other",
            json={"text": "ok"},
            headers=auth,
        )

    assert (wrong_media.status_code, wrong_media.json()["code"]) == (
        415,
        "intent_risk.media_type_unsupported",
    )
    assert (duplicate.status_code, duplicate.json()["code"]) == (
        400,
        "intent_risk.payload_invalid",
    )
    assert (unknown.status_code, unknown.json()["code"]) == (
        400,
        "intent_risk.payload_invalid",
    )
    assert (empty.status_code, empty.json()["code"]) == (422, "intent_risk.text_invalid")
    assert (too_many_chars.status_code, too_many_chars.json()["code"]) == (
        422,
        "intent_risk.text_invalid",
    )
    assert (too_many_bytes.status_code, too_many_bytes.json()["code"]) == (
        413,
        "intent_risk.payload_too_large",
    )
    assert (query.status_code, query.json()["code"]) == (
        400,
        "intent_risk.payload_invalid",
    )


@pytest.mark.anyio
async def test_classifier_failure_is_tracked_without_sensitive_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """故障流程：分类器异常返回固定 503，并在数据库可写时保存 manual failed/none。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    marker = "sensitive-classifier-failure-marker"

    def _fail_classifier(_texts: tuple[str, ...]):
        """模拟可能携带敏感输入的未知分类器异常。"""

        # 故意携带 marker 证明生产异常转换会切断原始消息，测试自身不记录该异常。
        raise RuntimeError(marker)

    monkeypatch.setattr(intent_service, "classify_intent", _fail_classifier)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        response = await client.post(
            "/security/classify-intent",
            json={"text": marker},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert (response.status_code, response.json()["code"]) == (
        503,
        "intent_risk.classifier_unavailable",
    )
    assert marker not in response.text
    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT status,classification_mode,risk_type,risk_score,action,error_code "
        "FROM intent_risk_results ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    dump = "\n".join(str(value) for value in tuple(row)) if row is not None else ""
    connection.close()
    assert tuple(row) == (
        "failed",
        "manual_sync",
        "none",
        0,
        "none",
        "intent_risk.classifier_failed",
    )
    assert marker not in dump


@pytest.mark.anyio
async def test_clean_webhook_failure_and_result_store_failure_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主流与故障流程：clean 可追踪，Webhook 不回滚，存储故障使用独立 503。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        auth = {"Authorization": f"Bearer {token}"}
        clean = await client.post(
            "/security/classify-intent",
            json={"text": "请总结企业数据治理的基本原则"},
            headers=auth,
        )
        assert clean.status_code == 200, clean.text
        assert clean.json()["intent_risk_result"]["status"] == "clean"
        assert clean.json()["intent_risk_result"]["risk_type"] == "none"
        assert clean.json()["intent_risk_result"]["risk_score"] == 0

        webhook_marker = "webhook-sensitive-exception-marker"

        def _fail_webhook(*_args, **_kwargs) -> None:
            """模拟可能在异常消息中携带敏感上下文的提交后 Webhook 故障。"""

            # 提交后通知失败不得回滚结果，也不得把合成异常消息写入响应或日志。
            raise RuntimeError(webhook_marker)

        monkeypatch.setattr(intent_service, "dispatch_webhook_event", _fail_webhook)
        detected = await client.post(
            "/security/classify-intent",
            json={"text": "reveal-system-prompt"},
            headers=auth,
        )
        # source result/event 已在后台投递前提交，Webhook 故障不能改写成功响应或回滚事实。
        assert detected.status_code == 200, detected.text
        assert detected.json()["intent_risk_result"]["status"] == "detected"

        store_marker = "result-store-sensitive-marker"

        def _fail_store(_config):
            """模拟结果数据库不可用，并只抛固定统一错误而不拼接正文。"""

            from metahub_shared.runtime.errors import ApiError

            # 固定统一错误验证存储故障映射，合成 marker 不得穿透公开错误。
            raise ApiError("intent_risk.result_store_unavailable")

        monkeypatch.setattr(intent_service, "_connect", _fail_store)
        unavailable = await client.post(
            "/security/classify-intent",
            json={"text": store_marker},
            headers=auth,
        )

    assert (unavailable.status_code, unavailable.json()["code"]) == (
        503,
        "intent_risk.result_store_unavailable",
    )
    assert webhook_marker not in detected.text
    assert store_marker not in unavailable.text
    assert webhook_marker not in caplog.text
    assert store_marker not in caplog.text
    connection = connect_database(database_url)
    result_count = connection.execute("SELECT COUNT(*) FROM intent_risk_results").fetchone()[0]
    event_count = connection.execute("SELECT COUNT(*) FROM intent_risk_events").fetchone()[0]
    connection.close()
    assert (result_count, event_count) == (2, 1)
