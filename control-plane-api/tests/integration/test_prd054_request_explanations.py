"""PRD-054 Request Explainability Center 的 Control API 与 source outbox 专项测试。

测试使用真实迁移、Session 鉴权、租户触发器和 SQLite lease/CAS，不通过 mock 绕过权限或
访问审计。Webhook 网络边界只替换公共 dispatcher，既验证稳定信封，又避免专项访问外网。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.auth.routes import router as auth_router
from metahub_control.request_explanations import outbox
from metahub_control.request_explanations.routes import router as explanation_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试身份覆盖 admin/developer/viewer；所有密码只存在测试进程，不进入响应或业务日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
SOURCE_REQUEST_ID = "req_prd054_explain"
MASK_SOURCE_REQUEST_ID = "req_prd068_mask_explain"
OTHER_REQUEST_ID = "req_prd054_other_tenant"
OTHER_TENANT_ID = "tenant_prd054_other"
FIXTURE_TIME = "2026-07-28T01:00:00Z"
# 0047 会由失败原始 Delivery 的 32 位十六进制主体派生唯一 DeadLetter ID；
# 隐私夹具必须采用同一生产格式，避免测试专用短 ID 在 0048 约束前提前失败。
SENSITIVE_WEBHOOK_DELIVERY_ID = f"whd_{'5' * 32}"
STAGE_ORDER = [
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider",
    "usage_and_finops",
    "logs_and_webhooks",
]
SECTION_ORDER = [
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider_response",
    "usage_and_finops",
    "logs",
    "webhooks",
]
SENSITIVE_VALUES = {
    "sk-provider-prd054-plain",
    "Bearer prd054-sensitive-token",
    "https://secret.example.invalid/hook",
    "prd054 raw provider response",
}
FORBIDDEN_KEYS = {
    "prompt",
    "response",
    "raw_usage",
    "payload",
    "payload_json",
    "endpoint_url",
    "encrypted_secret",
    "secret",
    "token",
    "credential_id",
    "error_summary",
    "match_text",
}


@dataclass(frozen=True, slots=True)
class ExplanationFixture:
    """保存专项测试共享的数据库、租户和源请求稳定标识。"""

    database_url: str
    tenant_id: str
    admin_user_id: str


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换成项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含统一运行时、认证和 Explain 路由的最小真实 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-054 Explainability Test")
    app.include_router(auth_router)
    app.include_router(explanation_router)
    return app


def _prepare_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_created_at: str = FIXTURE_TIME,
    source_updated_at: str = "2026-07-28T01:00:01Z",
) -> ExplanationFixture:
    """应用全部迁移并写入两个租户的最小可解释请求及隐私陷阱数据。"""

    # 可注入 source 时间只用于验证合法 SQLite 时间文本的快照复核兼容性。
    database_url = _sqlite_url(tmp_path / "prd054-control.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd054-test-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        metadata = connection.execute(
            """
            SELECT vk.virtual_key_id, environment.environment_id
            FROM virtual_keys vk
            JOIN environments environment
              ON environment.tenant_id=vk.tenant_id
             AND environment.environment_id=vk.environment_id
            WHERE vk.tenant_id=?
            ORDER BY vk.created_at, vk.virtual_key_id
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert metadata is not None
        admin_user = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND email=? LIMIT 1",
            (seed.tenant_id, seed.admin_email),
        ).fetchone()
        assert admin_user is not None
        connection.executemany(
            """
            INSERT INTO users(
              user_id, tenant_id, email, display_name, password_hash,
              role, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            [
                (
                    "user_prd054_developer",
                    seed.tenant_id,
                    "developer@example.com",
                    "PRD054 Developer",
                    hash_password(DEVELOPER_PASSWORD),
                    "developer",
                    FIXTURE_TIME,
                    FIXTURE_TIME,
                ),
                (
                    "user_prd054_viewer",
                    seed.tenant_id,
                    "viewer@example.com",
                    "PRD054 Viewer",
                    hash_password(VIEWER_PASSWORD),
                    "viewer",
                    FIXTURE_TIME,
                    FIXTURE_TIME,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id, slug, name, service_mode, status, created_at, updated_at
            ) VALUES(?, 'prd054-other', 'PRD054 Other Tenant',
                     'internal_only', 'active', ?, ?)
            """,
            (OTHER_TENANT_ID, FIXTURE_TIME, FIXTURE_TIME),
        )
        # request_logs 的 tenant/trace/snapshot 必须先由不可变 claim 冻结；其他租户请求
        # 故意不带 Key，证明 404 只依赖认证 tenant，而不是可猜测的全局请求 ID。
        connection.executemany(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
            """,
            [
                (
                    seed.tenant_id,
                    SOURCE_REQUEST_ID,
                    "trc_prd054_explain",
                    metadata["virtual_key_id"],
                    source_created_at,
                ),
                (
                    OTHER_TENANT_ID,
                    OTHER_REQUEST_ID,
                    "trc_prd054_other",
                    None,
                    FIXTURE_TIME,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id,
              environment_id, environment_code, endpoint,
              request_model, resolved_model, provider_code,
              status, latency_ms, input_tokens, output_tokens, total_tokens,
              estimated_cost_amount, currency, policy_snapshot_status,
              policy_decision, dlp_status, fallback_triggered,
              routing_attempt_count, created_at, updated_at
            ) VALUES(
              ?, ?, 'trc_prd054_explain', ?, ?, 'dev', '/v1/chat/completions',
              'company-default', 'default-chat', 'local_openai',
              'success', 41, 11, 7, 18, 0.123456, 'CNY', 'disabled',
              NULL, 'clean', 0, 0, ?, ?
            )
            """,
            (
                SOURCE_REQUEST_ID,
                seed.tenant_id,
                metadata["virtual_key_id"],
                metadata["environment_id"],
                source_created_at,
                source_updated_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, status, policy_snapshot_status,
              created_at, updated_at
            ) VALUES(?, ?, 'trc_prd054_other', 'success', 'disabled', ?, ?)
            """,
            (OTHER_REQUEST_ID, OTHER_TENANT_ID, FIXTURE_TIME, FIXTURE_TIME),
        )
        _insert_sensitive_webhook_history(connection, seed.tenant_id)
        connection.commit()
    finally:
        connection.close()
    return ExplanationFixture(
        database_url=database_url,
        tenant_id=seed.tenant_id,
        admin_user_id=str(admin_user["user_id"]),
    )


def _insert_mask_explanation_request(fixture: ExplanationFixture) -> None:
    """插入只有非终止 mask、最终默认 allow 的冻结请求事实。

    参数：已完成通用迁移和租户初始化的 Explain 夹具。返回值：无。关键约束：先创建真实
    published Snapshot，再写不可变 claim 与 RequestLog，避免用不可能状态验证安全投影。
    """

    connection = connect_database(fixture.database_url)
    try:
        virtual_key = connection.execute(
            "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
            (fixture.tenant_id,),
        ).fetchone()
        assert virtual_key is not None
        connection.execute(
            """
            INSERT INTO policy_snapshots(
              policy_snapshot_id, tenant_id, snapshot_version, schema_version, state,
              operation, request_id, trace_id, request_fingerprint, change_reason,
              approval_status, created_by, created_at
            ) VALUES(
              'psnap_prd068_explain_v1', ?, 1, 'policy-snapshot-v1', 'building',
              'publish', 'req_prd068_snapshot_publish', 'trc_prd068_snapshot_publish', ?,
              'PRD-068 Explain mask 投影夹具', 'not_required', ?, '2026-08-03T01:00:00Z'
            )
            """,
            (fixture.tenant_id, "8" * 64, fixture.admin_user_id),
        )
        connection.execute(
            """
            UPDATE policy_snapshots
            SET state='published', content_hash=?, artifact_hash=?, policy_count=0,
                published_at='2026-08-03T01:00:01Z'
            WHERE tenant_id=? AND policy_snapshot_id='psnap_prd068_explain_v1'
            """,
            ("9" * 64, "a" * 64, fixture.tenant_id),
        )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id, request_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status, claimed_at
            ) VALUES(?, ?, 'trc_prd068_mask_explain', ?, 1, 'current',
                     '2026-08-03T01:00:02Z')
            """,
            (fixture.tenant_id, MASK_SOURCE_REQUEST_ID, virtual_key["virtual_key_id"]),
        )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id, tenant_id, trace_id, virtual_key_id,
              snapshot_version, policy_snapshot_status,
              policy_decision, policy_reason_code,
              policy_mask_applied, policy_mask_policy_id,
              status, created_at, updated_at
            ) VALUES(
              ?, ?, 'trc_prd068_mask_explain', ?, 1, 'current',
              'allow', 'policy.default_allow', 1, 'pol_prd068_mask_explain',
              'success', '2026-08-03T01:00:02Z', '2026-08-03T01:00:03Z'
            )
            """,
            (MASK_SOURCE_REQUEST_ID, fixture.tenant_id, virtual_key["virtual_key_id"]),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_sensitive_webhook_history(connection: Any, tenant_id: str) -> None:
    """写入可关联但绝不能出现在 Explain 响应中的 URL、payload 与错误正文。"""

    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status,
          timeout_ms, max_attempts, created_at, updated_at
        ) VALUES(
          'whe_prd054_sensitive', ?, 'PRD054 Receiver', ?, '["request.failed"]',
          'ciphertext-prd054', 'whsec_pr', 'fingerprint-prd054', 'active',
          1000, 3, ?, ?
        )
        """,
        (tenant_id, "https://secret.example.invalid/hook", FIXTURE_TIME, FIXTURE_TIME),
    )
    envelope = {
        "event_id": "evt_prd054_sensitive",
        "event_type": "request.failed",
        "tenant_id": tenant_id,
        "timestamp": FIXTURE_TIME,
        "actor_id": None,
        "object_id": SOURCE_REQUEST_ID,
        "trace_id": "trc_prd054_explain",
        "payload": {
            "request_id": SOURCE_REQUEST_ID,
            "token": "Bearer prd054-sensitive-token",
            "provider_response": "prd054 raw provider response",
        },
    }
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id,
          event_id, event_type, event_timestamp, payload_json,
          status, attempt_count, max_attempts, http_status, duration_ms,
          error_code, error_summary, secret_version, created_at, updated_at
        ) VALUES(
          ?, ?, 'whe_prd054_sensitive',
          'evt_prd054_sensitive', 'request.failed', ?, ?,
          'failed', 1, 3, 500, 25, 'webhook.remote_error',
          'sk-provider-prd054-plain', 1, ?, ?
        )
        """,
        (
            SENSITIVE_WEBHOOK_DELIVERY_ID,
            tenant_id,
            FIXTURE_TIME,
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            FIXTURE_TIME,
            FIXTURE_TIME,
        ),
    )
    connection.execute(
        """
        INSERT INTO webhook_delivery_attempts(
          webhook_delivery_attempt_id, tenant_id, webhook_delivery_id,
          webhook_endpoint_id, attempt_no, status, http_status, duration_ms,
          error_code, error_summary, created_at
        ) VALUES(
          'wha_prd054_sensitive', ?, ?,
          'whe_prd054_sensitive', 1, 'failed', 500, 25,
          'webhook.remote_error', 'sk-provider-prd054-plain', ?
        )
        """,
        (tenant_id, SENSITIVE_WEBHOOK_DELIVERY_ID, FIXTURE_TIME),
    )


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
) -> str:
    """通过真实登录接口建立 Session，并只返回当前调用栈需要的 Bearer Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 仅把令牌交给后续内存请求头，测试输出和持久证据都不得记录其明文。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造认证与 operation request_id 头，Token 不进入测试快照。"""

    # 权限测试同时固定 operation request_id，用于核对响应头、错误体和访问事实的一致性。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _assert_error(response: Response, status: int, code: str) -> None:
    """断言统一错误码以及正文/响应头的 operation request_id 一致。"""

    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["request_id"] == response.headers["X-Request-ID"]


def _explanation_rows(database_url: str) -> list[Any]:
    """按创建顺序读取访问事实安全字段，不读取事件 payload。"""

    connection = connect_database(database_url)
    try:
        return connection.execute(
            """
            SELECT request_explanation_id, event_id, actor_user_id,
                   operation_request_id, operation_trace_id,
                   event_dispatch_status, event_dispatch_attempts,
                   event_next_attempt_at, event_lease_expires_at,
                   event_last_error_code, viewed_at
            FROM request_explanations
            ORDER BY created_at, request_explanation_id
            """
        ).fetchall()
    finally:
        connection.close()


def _walk_json(value: object):
    """递归枚举响应的键和值，供敏感字段门禁断言复用。"""

    if isinstance(value, dict):
        for key, child in value.items():
            yield ("key", str(key))
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)
    elif isinstance(value, str):
        yield ("value", value)


def _parse_time(value: object) -> datetime:
    """解析数据库 canonical UTC 时间，测试计算不依赖本地时区。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@pytest.mark.anyio
async def test_prd054_api_rbac_tenant_shape_audit_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 API 主流程、权限优先、同形 404、固定结构、隐私和访问事实原子性。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

        # 权限依赖必须先于畸形 path/query/body；拒绝请求不能留下访问审计事实。
        anonymous = await client.request(
            "GET",
            "/request-explain/%20?tenant_id=forged",
            content=b"sensitive-body",
            headers={"Content-Encoding": "gzip"},
        )
        viewer_denied = await client.request(
            "GET",
            "/request-explain/%20?tenant_id=forged",
            content=b"sensitive-body",
            headers={
                **_headers(viewer, "req_prd054_viewer_denied"),
                "Content-Encoding": "gzip",
            },
        )
        assert len(_explanation_rows(fixture.database_url)) == 0

        admin_ok = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_admin_view"),
        )
        developer_ok = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(developer, "req_prd054_developer_view"),
        )
        cross_tenant = await client.get(
            f"/request-explain/{OTHER_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_cross_tenant"),
        )
        missing = await client.get(
            "/request-explain/req_prd054_missing",
            headers=_headers(admin, "req_prd054_missing"),
        )
        invalid = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}?include_raw=true",
            headers=_headers(admin, "req_prd054_invalid_query"),
        )

    _assert_error(anonymous, 401, "auth.token_missing")
    _assert_error(viewer_denied, 403, "auth.permission_denied")
    _assert_error(cross_tenant, 404, "request_explanation.not_found")
    _assert_error(missing, 404, "request_explanation.not_found")
    _assert_error(invalid, 422, "request_explanation.request_invalid")
    assert admin_ok.status_code == developer_ok.status_code == 200
    for response in (admin_ok, developer_ok):
        body = response.json()
        explanation = body["request_explanation"]
        assert body["request_id"] == response.headers["X-Request-ID"]
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.headers["Pragma"] == "no-cache"
        assert response.headers["Vary"] == "Authorization"
        assert [stage["stage"] for stage in explanation["decision_chain"]] == STAGE_ORDER
        assert len(explanation["decision_chain"]) == 10
        assert all(
            set(explanation[name]) == {"evidence_state", "reason_code", "facts", "truncated"}
            for name in SECTION_ORDER
        )
        assert len(SECTION_ORDER) == 11
        walked = list(_walk_json(body))
        assert not ({item.lower() for kind, item in walked if kind == "key"} & FORBIDDEN_KEYS)
        returned_values = {item for kind, item in walked if kind == "value"}
        assert not (returned_values & SENSITIVE_VALUES)

    rows = _explanation_rows(fixture.database_url)
    assert len(rows) == 2
    assert len({row["request_explanation_id"] for row in rows}) == 2
    assert len({row["event_id"] for row in rows}) == 2
    assert {row["actor_user_id"] for row in rows} == {
        fixture.admin_user_id,
        "user_prd054_developer",
    }
    assert {row["operation_request_id"] for row in rows} == {
        "req_prd054_admin_view",
        "req_prd054_developer_view",
    }
    assert all(row["event_dispatch_status"] == "pending" for row in rows)
    # 五个失败请求均不得形成访问事实；通用 audit_logs 也不能复制同义业务事件。
    connection = connect_database(fixture.database_url)
    try:
        duplicated = connection.execute(
            "SELECT COUNT(*) AS total FROM audit_logs WHERE action='request.explanation_viewed'"
        ).fetchone()
        assert duplicated is not None and int(duplicated["total"]) == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd068_request_explain_projects_mask_modifier_without_body_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """mask Explain 只返回 applied 与稳定 Policy ID，最终 allow 决定保持独立。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    _insert_mask_explanation_request(fixture)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/request-explain/{MASK_SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd068_view_mask_explain"),
        )

    assert response.status_code == 200, response.text
    explanation = response.json()["request_explanation"]
    assert explanation["policy"]["evidence_state"] == "complete"
    facts = explanation["policy"]["facts"]
    assert len(facts) == 1
    assert facts[0]["fact_id"] == "pol_prd068_mask_explain"
    values = facts[0]["values"]
    assert values["policy_decision"] == "allow"
    assert values["policy_id"] is None
    assert values["policy_mask_applied"] is True
    assert values["policy_mask_policy_id"] == "pol_prd068_mask_explain"
    assert set(values) == {
        "snapshot_version",
        "snapshot_status",
        "policy_id",
        "policy_action_type",
        "policy_target_model_id",
        "policy_decision",
        "policy_reason_code",
        "policy_mask_applied",
        "policy_mask_policy_id",
        "routing_policy_id",
        "routing_policy_version",
        "routing_mode",
    }
    for forbidden in ("condition_expr", "canonical_expression", "match_text", "impact_hash"):
        assert forbidden not in response.text


@pytest.mark.anyio
async def test_prd054_microsecond_source_time_preserves_owner_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证合法微秒时间不会因响应规范化而破坏不可变审计的原始快照复核。"""

    source_created_at = "2026-07-28T01:00:00.000000Z"
    source_updated_at = "2026-07-28T01:00:01.000000Z"
    fixture = _prepare_fixture(
        monkeypatch,
        tmp_path,
        source_created_at=source_created_at,
        source_updated_at=source_updated_at,
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_microsecond_time"),
        )

    assert response.status_code == 200, response.text
    connection = connect_database(fixture.database_url)
    try:
        row = connection.execute(
            """
            SELECT source_created_at, source_updated_at
            FROM request_explanations
            WHERE operation_request_id='req_prd054_microsecond_time'
            """
        ).fetchone()
        assert row is not None
        assert row["source_created_at"] == source_created_at
        assert row["source_updated_at"] == source_updated_at
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd054_outbox_stable_event_stale_cas_backoff_and_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证稳定事件、lease 恢复、stale CAS、分级退避和第十次失败终结。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        first = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_outbox_stale"),
        )
        second = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_outbox_retry"),
        )
    assert first.status_code == second.status_code == 200
    rows = _explanation_rows(fixture.database_url)
    first_id = first.json()["request_explanation"]["request_explanation_id"]
    first_row = next(row for row in rows if row["request_explanation_id"] == first_id)
    config = load_app_config("control-plane-api")
    captured_events: list[Any] = []

    def successful_dispatch(_config, tenant_id, event, **_kwargs):
        """捕获安全事件信封；不访问网络，返回值与无订阅者投递一致。"""

        assert tenant_id == fixture.tenant_id
        captured_events.append(event)
        return []

    monkeypatch.setattr(outbox, "dispatch_webhook_event", successful_dispatch)
    initial_due = _parse_time(first_row["event_next_attempt_at"])
    claim_one = outbox.claim_next_request_explanation_event(
        config,
        worker_id="worker-prd054-stale-one",
        now=initial_due,
    )
    assert claim_one is not None
    assert claim_one.event_dispatch_attempts == 1
    assert claim_one.event_lease_expires_at - timedelta(seconds=30) >= initial_due
    claim_two = outbox.claim_next_request_explanation_event(
        config,
        worker_id="worker-prd054-too-early",
        now=claim_one.event_lease_expires_at - timedelta(seconds=1),
    )
    assert claim_two is not None  # 第二条 pending 访问事实仍可独立领取。

    # 先把第二条 claim 保留给后续失败循环；第一条 lease 到期后由新 owner 恢复。
    recovered = outbox.claim_next_request_explanation_event(
        config,
        worker_id="worker-prd054-stale-two",
        now=claim_one.event_lease_expires_at,
    )
    assert recovered is not None
    assert recovered.request_explanation_id == claim_one.request_explanation_id
    assert recovered.event_id == claim_one.event_id
    assert recovered.event_dispatch_attempts == 2
    stale_finished = outbox.dispatch_claimed_request_explanation_event(
        config,
        claim_one,
        now=claim_one.event_lease_expires_at + timedelta(seconds=1),
    )
    assert stale_finished is False
    assert outbox.dispatch_claimed_request_explanation_event(
        config,
        recovered,
        now=recovered.event_lease_expires_at - timedelta(seconds=29),
    ) is True
    assert len(captured_events) == 2
    assert {event.event_id for event in captured_events} == {claim_one.event_id}
    payload = captured_events[-1].payload
    assert payload["request_explanation_id"] == claim_one.request_explanation_id
    assert payload["source_request_id"] == SOURCE_REQUEST_ID
    assert set(payload) == {
        "schema_version",
        "request_explanation_id",
        "source_request_id",
        "source_trace_id",
        "source_status",
        "explanation_status",
        "missing_sections",
        "truncated_sections",
        "evidence_version",
        "viewed_at",
    }

    def failed_dispatch(*_args, **_kwargs):
        """模拟公共 dispatcher 的稳定领域失败，不携带 URL 或远端正文。"""

        # 模拟器只抛出固定领域错误，避免用任意异常正文污染 outbox 的安全错误码断言。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(outbox, "dispatch_webhook_event", failed_dispatch)
    # 找到尚处 processing 的第二条事实；其 lease 到期后作为第一次失败尝试恢复。
    connection = connect_database(fixture.database_url)
    try:
        retry_row = connection.execute(
            """
            SELECT * FROM request_explanations
            WHERE request_explanation_id=?
            """,
            (claim_two.request_explanation_id,),
        ).fetchone()
        assert retry_row is not None
        due = _parse_time(retry_row["event_lease_expires_at"])
    finally:
        connection.close()

    stable_event_id: str | None = None
    # 第二条行已经被上面的独立领取推进到 attempt=1，因此恢复后从 attempt=2 跑到 10。
    for expected_attempt in range(2, 11):
        claim = outbox.claim_next_request_explanation_event(
            config,
            worker_id=f"worker-prd054-retry-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.event_dispatch_attempts == expected_attempt
        stable_event_id = stable_event_id or claim.event_id
        assert claim.event_id == stable_event_id
        completed_at = claim.event_lease_expires_at - timedelta(seconds=29)
        assert outbox.dispatch_claimed_request_explanation_event(
            config,
            claim,
            now=completed_at,
        ) is True
        connection = connect_database(fixture.database_url)
        try:
            state = connection.execute(
                "SELECT * FROM request_explanations WHERE request_explanation_id=?",
                (claim.request_explanation_id,),
            ).fetchone()
            assert state is not None
        finally:
            connection.close()
        if expected_attempt < 10:
            expected_delay = (5, 30, 300)[min(expected_attempt - 1, 2)]
            assert state["event_dispatch_status"] == "pending"
            due = _parse_time(state["event_next_attempt_at"])
            assert due - completed_at == timedelta(seconds=expected_delay)
        else:
            assert state["event_dispatch_status"] == "failed"
            assert state["event_dispatch_attempts"] == 10
            assert state["event_next_attempt_at"] is None
            assert state["event_last_error_code"] == "webhook.delivery_unavailable"
            assert outbox.claim_next_request_explanation_event(
                config,
                worker_id="worker-prd054-after-terminal",
                now=completed_at + timedelta(days=1),
            ) is None


@pytest.mark.anyio
async def test_prd054_expired_tenth_attempt_is_terminally_recovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """第十次调用后进程崩溃时，过期 lease 必须终结，不能永久 processing。"""

    fixture = _prepare_fixture(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/request-explain/{SOURCE_REQUEST_ID}",
            headers=_headers(admin, "req_prd054_orphan_tenth"),
        )
    assert response.status_code == 200
    explanation_id = response.json()["request_explanation"]["request_explanation_id"]
    row = _explanation_rows(fixture.database_url)[0]
    due = _parse_time(row["event_next_attempt_at"])
    config = load_app_config("control-plane-api")

    # 前九次 lease 都模拟 worker 在 dispatcher 返回前退出；第十次也不执行 CAS 回写。
    for expected_attempt in range(1, 11):
        claim = outbox.claim_next_request_explanation_event(
            config,
            worker_id=f"worker-prd054-orphan-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.request_explanation_id == explanation_id
        assert claim.event_dispatch_attempts == expected_attempt
        due = claim.event_lease_expires_at

    # 下一次 drain 只终结已过期的最大次数 lease，不产生第十一次网络调用。
    assert outbox.claim_next_request_explanation_event(
        config,
        worker_id="worker-prd054-orphan-reaper",
        now=due,
    ) is None
    connection = connect_database(fixture.database_url)
    try:
        terminal = connection.execute(
            "SELECT * FROM request_explanations WHERE request_explanation_id=?",
            (explanation_id,),
        ).fetchone()
        assert terminal is not None
        assert terminal["event_dispatch_status"] == "failed"
        assert terminal["event_dispatch_attempts"] == 10
        assert terminal["event_claim_token"] is None
        assert terminal["event_lease_owner_id"] is None
        assert terminal["event_lease_expires_at"] is None
        assert terminal["event_last_error_code"] == "request_explanation.event_unavailable"
    finally:
        connection.close()
