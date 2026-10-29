"""PRD-070 高危会话 Control API 的租户、权限、处置和 Store 对账测试。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.session_risks import service as session_risk_service
from metahub_control.session_risks.routes import router as session_risk_router
from metahub_gateway.session_risk.models import (
    SessionRiskSnapshot,
    SessionRiskStoreUnavailable,
)
from metahub_gateway.session_risk.store import InMemorySessionRiskStore
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.session_risk import (
    SessionRiskHmacKey,
    SessionRiskScope,
    derive_owner_slot,
)

# 所有凭据仅用于 pytest 隔离库；HMAC 根密钥与 Auth 根密钥物理分离，复现生产启动门禁。
ADMIN_PASSWORD = "PRD070-Admin-Local!2026"
SECURITY_PASSWORD = "PRD070-Security-Local!2026"
AUTH_SECRET = "prd070-auth-token-root-secret-00000001"
SESSION_RISK_SECRET = "prd070-session-risk-root-secret-000001"
SENSITIVE_MARKER = "PRD070_PROMPT_HEADER_TOKEN_MUST_NOT_PERSIST"


@dataclass(frozen=True, slots=True)
class _RiskFixture:
    """保存测试可公开使用的 tenant/key/score/scope，不包含登录 Token 或原始 Header。"""

    tenant_id: str
    virtual_key_id: str
    score_id: str
    scope: SessionRiskScope
    snapshot: SessionRiskSnapshot


class _FailNextRestoreStore(InMemorySessionRiskStore):
    """仅在明确开启后让一次 restore 失败，验证已提交动作可用原幂等键恢复。"""

    def __init__(self) -> None:
        """初始化可用内存 Store；种子恢复阶段不会注入故障。"""

        super().__init__()
        self.fail_next_restore = False

    async def restore(
        self,
        scope: SessionRiskScope,
        snapshot: SessionRiskSnapshot,
    ) -> bool:
        """按开关抛固定 Store 异常一次，其余调用保持生产合同。"""

        if self.fail_next_restore:
            self.fail_next_restore = False
            # 注入一次固定 Store 故障，验证数据库已提交时接口仍返回可恢复的统一错误。
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        return await super().restore(scope, snapshot)


def _sqlite_url(path: Path) -> str:
    """把 pytest 沙箱内路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _db_time(value: datetime) -> str:
    """生成数据库迁移约束接受的规范 UTC 秒级时间文本。

    参数为带时区测试时间，返回值用于构造确定性风险事实，避免本机时区影响断言。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _configure_runtime(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """配置隔离 Control 运行时；Redis URL 不会联网，因为 app.state 注入内存 Store。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_SESSION_RISK_ENABLED", "true")
    monkeypatch.setenv("METAHUB_SESSION_RISK_HMAC_VERSION", "1")
    monkeypatch.setenv("METAHUB_SESSION_RISK_HMAC_SECRET", SESSION_RISK_SECRET)
    monkeypatch.setenv("METAHUB_REDIS_URL", "redis://127.0.0.1:6379/15")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")


def _insert_risk_score(
    database_url: str,
    *,
    tenant_id: str,
    virtual_key_id: str,
    ordinal: int,
    current: datetime,
) -> _RiskFixture:
    """插入一条通过 owner trigger 的 API 高危 score，并返回同 owner Redis 快照。"""

    score_id = f"srs_{ordinal:032x}"
    scope_key = f"srh_{ordinal:064x}"
    request_id = f"req-prd070-seed-{ordinal}"
    trace_id = f"trace-prd070-seed-{ordinal}"
    started = current - timedelta(minutes=2)
    expires = current + timedelta(minutes=28)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
          policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, virtual_key_id, _db_time(current)),
    )
    connection.execute(
        """
        INSERT INTO session_risk_scores(
          session_risk_score_id,tenant_id,scope_type,scope_key,hmac_key_version,
          virtual_key_id,chat_session_id,status,score,score_scale,risky_turn_count,
          jailbreak_count,sensitive_count,tool_inducement_count,state_revision,
          threshold_version,detector_version,ruleset_version,last_source_request_id,
          last_source_trace_id,window_started_at,last_risk_at,window_expires_at,
          first_high_risk_at,frozen_at,resolved_at,created_at,updated_at
        ) VALUES(?,?,'api_session',?,1,?,NULL,'high_risk',7500,10000,2,
                 1,1,0,1,'session-risk-threshold-v1','intent-risk-rules-v1',
                 'session-risk-ruleset-v1',?,?,?,?,?,?,NULL,NULL,?,?)
        """,
        (
            score_id,
            tenant_id,
            scope_key,
            virtual_key_id,
            request_id,
            trace_id,
            _db_time(started),
            _db_time(current),
            _db_time(expires),
            _db_time(current),
            _db_time(started),
            _db_time(current),
        ),
    )
    connection.commit()
    connection.close()

    key = SessionRiskHmacKey(1, SESSION_RISK_SECRET)
    owner_slot = derive_owner_slot(
        key,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type="api_session",
        scope_key=scope_key,
    )
    scope = SessionRiskScope(
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type="api_session",
        scope_key=scope_key,
        owner_slot=owner_slot.digest,
        hmac_key_version=1,
    )
    snapshot = SessionRiskSnapshot(
        score_id=score_id,
        score=7500,
        risky_turn_count=2,
        jailbreak_count=1,
        sensitive_count=1,
        tool_inducement_count=0,
        state="high_risk",
        revision=1,
        window_started_at_epoch=int(started.timestamp()),
        last_risk_at_epoch=int(current.timestamp()),
        expires_at_epoch=int(expires.timestamp()),
        hmac_key_version=1,
        threshold_version="session-risk-threshold-v1",
    )
    # 固件只返回安全技术标识与内存快照，不把测试 Secret 或请求正文带入断言对象。
    return _RiskFixture(tenant_id, virtual_key_id, score_id, scope, snapshot)


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    score_count: int,
) -> tuple[str, list[_RiskFixture], str]:
    """迁移并 seed 独立 0062 数据库，同时创建安全管理员和跨租户风险事实。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd070-control.db")
    _configure_runtime(monkeypatch, database_url)
    assert apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    virtual_key = connection.execute(
        """
        SELECT virtual_key_id FROM virtual_keys
        WHERE tenant_id=? AND status='active' ORDER BY virtual_key_id LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert virtual_key is not None
    now = _db_time(datetime.now(UTC))
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('usr_prd070_security',?,'security-prd070@example.com','PRD070 Security',
                 ?,'security_admin','active',?,?)
        """,
        (seed.tenant_id, hash_password(SECURITY_PASSWORD), now, now),
    )

    # 第二租户只用于证明默认租户查询/动作无法枚举其合法 score ID。
    other_tenant_id = "tenant_prd070_other"
    other_key_id = "22222222-2222-4222-8222-222222222222"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (other_tenant_id, "prd070-other", "PRD070 Other", now, now),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at
        ) VALUES(?,?,?,'mh_prd070_other','prd070-other-key-hash','active',?,?)
        """,
        (other_key_id, other_tenant_id, "PRD070 Other Key", now, now),
    )
    connection.commit()
    connection.close()

    current = datetime.now(UTC).replace(microsecond=0)
    fixtures = [
        _insert_risk_score(
            database_url,
            tenant_id=seed.tenant_id,
            virtual_key_id=str(virtual_key["virtual_key_id"]),
            ordinal=index,
            current=current + timedelta(seconds=index),
        )
        for index in range(1, score_count + 1)
    ]
    other = _insert_risk_score(
        database_url,
        tenant_id=other_tenant_id,
        virtual_key_id=other_key_id,
        ordinal=999,
        current=current,
    )
    return database_url, fixtures, other.score_id


def _build_app(store: InMemorySessionRiskStore):
    """构造仅含认证与高危会话路由的隔离 Control 应用，并注入同合同内存 Store。"""

    app = create_service_app("control-plane-api", "PRD070 Session Risk Test API")
    app.include_router(auth_router)
    app.include_router(session_risk_router)
    app.state.session_risk_store = store
    return app


async def _login(client: AsyncClient, *, email: str, password: str) -> str:
    """登录隔离用户并只在当前测试调用栈返回短期 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 测试令牌仅驻留当前用例内存并用于认证请求，不写入日志、URL 或持久测试证据。
    return response.json()["access_token"]


def _set_security_permissions(database_url: str, permissions: set[str]) -> None:
    """合法重建 security_admin 当前权限快照，验证 read/manage 不互相蕴含。"""

    connection = connect_database(database_url)
    role = connection.execute(
        """
        SELECT role_id,permission_revision FROM roles
        WHERE tenant_id=(SELECT tenant_id FROM tenants WHERE slug='default')
          AND code='security_admin'
        """
    ).fetchone()
    assert role is not None
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?",
        (tenant_id, role["role_id"]),
    )
    now = _db_time(datetime.now(UTC) + timedelta(minutes=1))
    for code in sorted(permissions):
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?",
            (tenant_id, code),
        ).fetchone()
        assert permission is not None
        connection.execute(
            """
            INSERT INTO role_permissions(
              role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                f"rperm_prd070_{code.replace('.', '_')}",
                tenant_id,
                role["role_id"],
                permission["permission_id"],
                now,
                now,
            ),
        )
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            int(role["permission_revision"]) + 1,
            permission_matrix_sha256(permissions),
            now,
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


async def _post_action(
    client: AsyncClient,
    token: str,
    score_id: str,
    *,
    action: str,
    reason_code: str,
    expected_revision: int,
    idempotency_key: str,
):
    """按 strict wire 合同提交一次动作，测试调用方负责断言结果。"""

    return await client.post(
        f"/security/session-risks/{score_id}/action",
        json={
            "action": action,
            "reason_code": reason_code,
            "expected_revision": expected_revision,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
        },
    )


@pytest.mark.anyio
async def test_session_risk_list_pagination_tenant_and_permission_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """查询流程覆盖 keyset、筛选、跨租户隐藏和 read/manage 后端权限优先级。"""

    database_url, fixtures, other_score_id = _prepare_database(
        monkeypatch,
        tmp_path,
        score_count=3,
    )
    store = InMemorySessionRiskStore()
    app = _build_app(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        anonymous = await client.get(
            "/security/session-risks?tenant_id=other&tenant_id=again"
        )
        admin_token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        first = await client.get(
            "/security/session-risks?status=high_risk&limit=1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert first.status_code == 200, first.text
        cursor = first.json()["next_cursor"]
        second = await client.get(
            f"/security/session-risks?status=high_risk&limit=1&cursor={cursor}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        by_key = await client.get(
            f"/security/session-risks?virtual_key_id={fixtures[0].virtual_key_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        invalid_query = await client.get(
            "/security/session-risks?status=high_risk&status=frozen",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        security_token = await _login(
            client,
            email="security-prd070@example.com",
            password=SECURITY_PASSWORD,
        )
        _set_security_permissions(database_url, {"session_risk.read"})
        read_only = await client.get(
            "/security/session-risks",
            headers={"Authorization": f"Bearer {security_token}"},
        )
        denied_write = await client.post(
            f"/security/session-risks/{fixtures[0].score_id}/action",
            content=(f'{{"marker":"{SENSITIVE_MARKER}"').encode(),
            headers={
                "Authorization": f"Bearer {security_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": SENSITIVE_MARKER,
            },
        )
        _set_security_permissions(database_url, {"session_risk.manage"})
        denied_read = await client.get(
            "/security/session-risks?status=not-a-real-status",
            headers={"Authorization": f"Bearer {security_token}"},
        )
        manage_only = await client.post(
            f"/security/session-risks/{fixtures[0].score_id}/action",
            content=(f'{{"marker":"{SENSITIVE_MARKER}"').encode(),
            headers={
                "Authorization": f"Bearer {security_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": SENSITIVE_MARKER,
            },
        )
        cross_tenant = await _post_action(
            client,
            admin_token,
            other_score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-cross-tenant",
        )

    assert (anonymous.status_code, anonymous.json()["code"]) == (
        401,
        "auth.token_missing",
    )
    for response in (
        anonymous,
        first,
        second,
        by_key,
        invalid_query,
        read_only,
        denied_write,
        denied_read,
        manage_only,
        cross_tenant,
    ):
        # 权限依赖和严格解析会在路由正文前失败，所有分支仍须由路径策略禁止缓存。
        assert response.headers["cache-control"] == "no-store"
    assert len(first.json()["session_risks"]) == 1 and cursor
    for response in (first, second, by_key):
        for risk in response.json()["session_risks"]:
            # API 会话只公开 api_ 命名空间的固定短引用，不能泄漏内部完整 srh_ HMAC。
            assert re.fullmatch(r"api_[0-9a-f]{16}", risk["scope_reference"])
            assert "srh_" not in risk["scope_reference"]
    assert len(second.json()["session_risks"]) == 1
    assert (
        first.json()["session_risks"][0]["session_risk_score_id"]
        != second.json()["session_risks"][0]["session_risk_score_id"]
    )
    assert first.json()["summary"]["high_risk_count"] == 3
    assert all(
        item["session_risk_score_id"] != other_score_id
        for item in by_key.json()["session_risks"]
    )
    assert (invalid_query.status_code, invalid_query.json()["code"]) == (
        400,
        "session_risk.query_invalid",
    )
    assert read_only.status_code == 200
    assert (denied_write.status_code, denied_write.json()["code"]) == (
        403,
        "auth.permission_denied",
    )
    assert (denied_read.status_code, denied_read.json()["code"]) == (
        403,
        "auth.permission_denied",
    )
    assert (manage_only.status_code, manage_only.json()["code"]) == (
        403,
        "auth.permission_denied",
    )
    assert (cross_tenant.status_code, cross_tenant.json()["code"]) == (
        404,
        "session_risk.not_found",
    )


@pytest.mark.anyio
async def test_session_risk_actions_idempotency_store_recovery_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """五种处置、CAS/幂等异常、Store 恢复及敏感值不落库形成完整闭环。"""

    database_url, fixtures, _other_score_id = _prepare_database(
        monkeypatch,
        tmp_path,
        score_count=4,
    )
    store = _FailNextRestoreStore()
    for fixture in fixtures:
        assert await store.restore(fixture.scope, fixture.snapshot)
    app = _build_app(store)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )

        freeze_session = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-freeze-session",
        )
        replay = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-freeze-session",
        )
        conflict = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=2,
            idempotency_key="prd070-freeze-session",
        )
        stale = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="release_session",
            reason_code="incident_contained",
            expected_revision=1,
            idempotency_key="prd070-stale-revision",
        )
        release_session = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="release_session",
            reason_code="incident_contained",
            expected_revision=2,
            idempotency_key="prd070-release-session",
        )

        freeze_key = await _post_action(
            client,
            token,
            fixtures[1].score_id,
            action="freeze_virtual_key",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-freeze-key",
        )
        # 两类 overlay 必须可按任意顺序独立叠加；先冻结 Key 后仍可冻结来源 Session。
        freeze_session_after_key = await _post_action(
            client,
            token,
            fixtures[1].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=2,
            idempotency_key="prd070-freeze-session-after-key",
        )
        release_key = await _post_action(
            client,
            token,
            fixtures[1].score_id,
            action="release_virtual_key",
            reason_code="false_positive",
            expected_revision=3,
            idempotency_key="prd070-release-key",
        )
        release_session_after_key = await _post_action(
            client,
            token,
            fixtures[1].score_id,
            action="release_session",
            reason_code="incident_contained",
            expected_revision=4,
            idempotency_key="prd070-release-session-after-key",
        )
        dismissed = await _post_action(
            client,
            token,
            fixtures[2].score_id,
            action="dismiss",
            reason_code="verified_safe",
            expected_revision=1,
            idempotency_key="prd070-dismiss-safe",
        )

        # 第四条首次提交时 Redis restore 失败：HTTP 503，但数据库动作只写一次；同一原键
        # 重试必须恢复该动作并完成 Redis 对账，不能要求管理员换键制造第二条 action。
        store.fail_next_restore = True
        uncertain = await _post_action(
            client,
            token,
            fixtures[3].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-recover-store",
        )
        recovered = await _post_action(
            client,
            token,
            fixtures[3].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-recover-store",
        )
        strict_payload = await client.post(
            f"/security/session-risks/{fixtures[2].score_id}/action",
            json={
                "action": "dismiss",
                "reason_code": "verified_safe",
                "expected_revision": 2,
                "tenant_id": SENSITIVE_MARKER,
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": SENSITIVE_MARKER,
            },
        )

    assert freeze_session.status_code == 200, freeze_session.text
    assert freeze_session.json()["session_risk"]["status"] == "frozen"
    assert freeze_session.json()["session_risk"]["state_revision"] == 2
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    assert replay.json()["action"]["session_risk_action_id"] == (
        freeze_session.json()["action"]["session_risk_action_id"]
    )
    assert (conflict.status_code, conflict.json()["code"]) == (
        409,
        "session_risk.idempotency_conflict",
    )
    assert (stale.status_code, stale.json()["code"]) == (
        409,
        "session_risk.state_conflict",
    )
    assert release_session.status_code == 200
    assert release_session.json()["session_risk"]["status"] == "resolved"
    assert await store.load(fixtures[0].scope) is None
    assert freeze_key.status_code == 200 and freeze_session_after_key.status_code == 200
    assert freeze_key.json()["session_risk"]["freezes"][0]["target_type"] == "virtual_key"
    assert release_key.status_code == 200
    assert release_key.json()["session_risk"]["status"] == "frozen"
    assert release_session_after_key.status_code == 200
    assert release_session_after_key.json()["session_risk"]["status"] == "resolved"
    assert dismissed.status_code == 200
    assert dismissed.json()["session_risk"]["status"] == "resolved"
    assert (uncertain.status_code, uncertain.json()["code"]) == (
        503,
        "session_risk.store_unavailable",
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["idempotent_replay"] is True
    recovered_hot = await store.load(fixtures[3].scope)
    assert recovered_hot is not None and recovered_hot.state == "frozen"
    assert recovered_hot.revision == 2
    assert (strict_payload.status_code, strict_payload.json()["code"]) == (
        400,
        "session_risk.payload_invalid",
    )

    connection = connect_database(database_url)
    counts = {
        "actions": connection.execute(
            "SELECT COUNT(*) AS value FROM session_risk_actions WHERE actor_type='user'"
        ).fetchone()["value"],
        "events": connection.execute(
            "SELECT COUNT(*) AS value FROM session_risk_events WHERE actor_type='user'"
        ).fetchone()["value"],
        "audits": connection.execute(
            "SELECT COUNT(*) AS value FROM audit_logs WHERE action LIKE 'session_risk.%'"
        ).fetchone()["value"],
    }
    recovered_action_count = connection.execute(
        """
        SELECT COUNT(*) AS value FROM session_risk_actions
        WHERE tenant_id=? AND session_risk_score_id=? AND actor_type='user'
        """,
        (fixtures[3].tenant_id, fixtures[3].score_id),
    ).fetchone()["value"]
    dump = "\n".join(
        str(value)
        for table in (
            "session_risk_scores",
            "session_risk_freezes",
            "session_risk_actions",
            "session_risk_events",
            "audit_logs",
        )
        for row in connection.execute(f"SELECT * FROM {table}")
        for value in tuple(row)
    )
    connection.close()
    assert counts == {"actions": 8, "events": 8, "audits": 8}
    assert recovered_action_count == 1
    assert SENSITIVE_MARKER not in dump
    assert SENSITIVE_MARKER not in caplog.text


@pytest.mark.anyio
async def test_session_risk_idempotency_key_can_be_reused_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """24 小时后同一 actor 可复用原键执行新意图，同时保留两条不可变历史证据。"""

    database_url, fixtures, _other_score_id = _prepare_database(
        monkeypatch,
        tmp_path,
        score_count=2,
    )
    store = InMemorySessionRiskStore()
    for fixture in fixtures:
        assert await store.restore(fixture.scope, fixture.snapshot)
    current = datetime.now(UTC).replace(microsecond=0)
    clock = [current]
    # 服务时钟只控制幂等有效期和持久时间；Redis 测试 Store 仍使用自己的安全 TTL 时钟。
    monkeypatch.setattr(
        session_risk_service,
        "_service_now",
        lambda _value=None: clock[0],
    )
    app = _build_app(store)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        first = await _post_action(
            client,
            token,
            fixtures[0].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-expiring-key",
        )
        clock[0] = current + timedelta(hours=25)
        second = await _post_action(
            client,
            token,
            fixtures[1].score_id,
            action="freeze_session",
            reason_code="confirmed_attack",
            expected_revision=1,
            idempotency_key="prd070-expiring-key",
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is False
    assert (
        second.json()["action"]["session_risk_action_id"]
        != first.json()["action"]["session_risk_action_id"]
    )
    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT idempotency_key_digest,intent_digest FROM session_risk_actions
        WHERE actor_type='user' ORDER BY created_at,session_risk_action_id
        """
    ).fetchall()
    audit_count = connection.execute(
        "SELECT COUNT(*) AS value FROM audit_logs WHERE action='session_risk.freeze_session'"
    ).fetchone()["value"]
    connection.close()
    assert len(rows) == 2
    assert rows[0]["idempotency_key_digest"] == rows[1]["idempotency_key_digest"]
    assert rows[0]["intent_digest"] != rows[1]["intent_digest"]
    assert audit_count == 2
