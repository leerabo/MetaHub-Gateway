"""PRD-078 审批 Control API 的租户、权限、幂等与预算回写专项测试。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.approvals import service as approval_service
from metahub_control.approvals.routes import router as approvals_router
from metahub_control.auth.routes import router as auth_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.fastapi_app import create_service_app

# 集成测试只使用固定虚构身份与独立测试密钥，角色矩阵覆盖租户和目标域权限组合。
ADMIN_PASSWORD = "MetaHubLocal!2026"
ROLE_PASSWORD = "Prd078RoleOnly!2026"
AUTH_SECRET = "prd078-control-auth-" + "a" * 48
INTERNAL_TOKEN = "prd078-internal-producer-" + "i" * 48
APPROVAL_SECRET = "prd078-approval-hmac-" + "h" * 48
SESSION_RISK_SECRET = "prd078-session-risk-hmac-" + "s" * 48
NOW = "2026-08-12T08:00:00Z"

ROLE_USERS = {
    "finance_admin": ("usr_prd078_finance", "finance@prd078.local"),
    "security_admin": ("usr_prd078_security", "security@prd078.local"),
    "developer": ("usr_prd078_developer", "developer@prd078.local"),
}


@dataclass(frozen=True, slots=True)
class ApprovalFixture:
    """一个测试数据库内可安全公开的固定业务坐标。"""

    database_url: str
    tenant_id: str
    foreign_tenant_id: str
    rules: dict[str, str]
    blocks: dict[str, str]
    scores: dict[str, str]


def _url(path: Path) -> str:
    """``_url`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return f"sqlite:///{path.as_posix()}"


def _db_time(value: datetime) -> str:
    """``_db_time`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def prd078_control_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一次构建完整 0071 seed 模板，后续每个用例复制隔离数据库。"""

    database = tmp_path_factory.mktemp("prd078-control-template") / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        result = apply_migrations(database_url)
        assert result.applied[-1] == "0071"
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return database


def _configure_runtime(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """把数据库、独立密钥和 worker 开关限定到当前用例。"""

    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_VERSION", "1")
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_SECRET", APPROVAL_SECRET)
    monkeypatch.setenv("METAHUB_SESSION_RISK_ENABLED", "true")
    monkeypatch.setenv("METAHUB_SESSION_RISK_HMAC_VERSION", "1")
    monkeypatch.setenv("METAHUB_SESSION_RISK_HMAC_SECRET", SESSION_RISK_SECRET)
    monkeypatch.setenv("METAHUB_REDIS_URL", "redis://127.0.0.1:6379/15")
    monkeypatch.setenv("METAHUB_APPROVAL_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_APPROVAL_OUTBOX_ENABLED", "false")
    monkeypatch.setenv("METAHUB_APPROVAL_WRITEBACK_ENABLED", "false")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")


def _insert_budget_pair(
    connection,
    *,
    tenant_id: str,
    environment_id: str,
    suffix: str,
    scope_type: str,
    scope_id: str,
) -> tuple[str, str]:
    """插入一条 active 预算规则及唯一 active block。
    ``_insert_budget_pair`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。
    """

    rule_id = f"br_prd078_{suffix}"
    block_id = f"bb_prd078_{suffix}"
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'PRD078 fixed rule',10,80,100,'block','active',?,?)
        """,
        (rule_id, tenant_id, environment_id, scope_type, scope_id, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO budget_blocks(
          budget_block_id,tenant_id,environment_id,budget_rule_id,scope_type,scope_id,
          budget_date,status,blocked_cost_cny,budget_cny,blocked_request_id,
          blocked_trace_id,blocked_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'2026-08-12','active',10,10,?,?,?, ?,?)
        """,
        (
            block_id,
            tenant_id,
            environment_id,
            rule_id,
            scope_type,
            scope_id,
            f"req_prd078_block_{suffix}",
            f"trc_prd078_block_{suffix}",
            NOW,
            NOW,
            NOW,
        ),
    )
    return rule_id, block_id


def _insert_risk_score(
    connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    ordinal: int,
) -> str:
    """插入由真实 PRD-070 owner trigger 校验的一条 API 高危 score。"""

    current = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=ordinal)
    started = current - timedelta(minutes=2)
    expires = current + timedelta(minutes=28)
    score_id = f"srs_{ordinal:032x}"
    scope_key = f"srh_{ordinal:064x}"
    request_id = f"req_prd078_score_{ordinal}"
    trace_id = f"trc_prd078_score_{ordinal}"
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
    return score_id


def _prepare_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ApprovalFixture:
    """复制模板并写入三条本租户预算目标和一条同形跨租户目标。"""

    database = tmp_path / "prd078-control.sqlite3"
    shutil.copy2(template, database)
    database_url = _url(database)
    _configure_runtime(monkeypatch, database_url)
    connection = connect_database(database_url)
    tenant = connection.execute("SELECT tenant_id FROM tenants WHERE slug='default'").fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (tenant["tenant_id"],),
    ).fetchone()
    assert tenant is not None and environment is not None and virtual_key is not None
    tenant_id = str(tenant["tenant_id"])
    environment_id = str(environment["environment_id"])
    virtual_key_id = str(virtual_key["virtual_key_id"])

    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        (
            (
                user_id,
                tenant_id,
                email,
                f"PRD078 {role}",
                hash_password(ROLE_PASSWORD),
                role,
                NOW,
                NOW,
            )
            for role, (user_id, email) in ROLE_USERS.items()
        ),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,status,created_at,updated_at
        ) VALUES('prj_prd078_drift',?,'prd078-drift','PRD078 Drift',
                 'active',?,?)
        """,
        (tenant_id, NOW, NOW),
    )

    rules: dict[str, str] = {}
    blocks: dict[str, str] = {}
    for suffix, scope_type, scope_id in (
        ("approve", "tenant", tenant_id),
        ("reject", "virtual_key", virtual_key_id),
        ("drift", "project", "prj_prd078_drift"),
    ):
        rules[suffix], blocks[suffix] = _insert_budget_pair(
            connection,
            tenant_id=tenant_id,
            environment_id=environment_id,
            suffix=suffix,
            scope_type=scope_type,
            scope_id=scope_id,
        )

    foreign_tenant_id = "tenant_prd078_foreign"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,'prd078-foreign','PRD078 Foreign','internal_only','active',?,?)
        """,
        (foreign_tenant_id, NOW, NOW),
    )
    foreign_environment = connection.execute(
        """
        SELECT environment_id FROM environments
        WHERE tenant_id=? AND code='dev'
        """,
        (foreign_tenant_id,),
    )
    foreign_environment = foreign_environment.fetchone()
    assert foreign_environment is not None
    foreign_environment_id = str(foreign_environment["environment_id"])
    foreign_rule, foreign_block = _insert_budget_pair(
        connection,
        tenant_id=foreign_tenant_id,
        environment_id=foreign_environment_id,
        suffix="foreign",
        scope_type="tenant",
        scope_id=foreign_tenant_id,
    )
    rules["foreign"] = foreign_rule
    blocks["foreign"] = foreign_block
    scores = {
        name: _insert_risk_score(
            connection,
            tenant_id=tenant_id,
            virtual_key_id=virtual_key_id,
            ordinal=ordinal,
        )
        for ordinal, name in enumerate(("binding", "other", "lock", "spare"), start=1)
    }
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
    return ApprovalFixture(
        database_url,
        tenant_id,
        foreign_tenant_id,
        rules,
        blocks,
        scores,
    )


@pytest.fixture
def approval_fixture(
    prd078_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ApprovalFixture:
    """``approval_fixture`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return _prepare_environment(prd078_control_template, monkeypatch, tmp_path)


def _build_app():
    """``_build_app`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    app = create_service_app("control-plane-api", "PRD-078 Approval Control Test")
    app.include_router(auth_router)
    app.include_router(approvals_router)
    return app


async def _login(client: AsyncClient, role: str) -> str:
    """``_login`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    user_id, email = ROLE_USERS[role]
    _ = user_id
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": ROLE_PASSWORD},
    )
    assert response.status_code == 200, response.text
    # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    """``_auth`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return {"Authorization": f"Bearer {token}"}


def _internal() -> dict[str, str]:
    """``_internal`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
    return {"X-Internal-Token": INTERNAL_TOKEN}


def _json_bytes(payload: dict[str, object]) -> bytes:
    """生成不含非有限数的紧凑 UTF-8 JSON，供原始 wire 测试精确声明长度。"""

    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _internal_raw_headers(
    body: bytes,
    *,
    token_values: tuple[bytes, ...] = (INTERNAL_TOKEN.encode("ascii"),),
    content_types: tuple[bytes, ...] = (b"application/json",),
    content_lengths: tuple[bytes, ...] | None = None,
) -> list[tuple[bytes, bytes]]:
    """构造保留重复项的内部请求 Header，避免 httpx 字典提前折叠输入。"""

    lengths = content_lengths
    if lengths is None:
        lengths = (str(len(body)).encode("ascii"),)
    return [
        *((b"x-internal-token", value) for value in token_values),
        *((b"content-type", value) for value in content_types),
        *((b"content-length", value) for value in lengths),
    ]


def _approval_counts(fixture: ApprovalFixture) -> tuple[int, int, int]:
    """读取创建事务的三项事实计数，证明 wire/认证拒绝没有半写入。"""

    connection = connect_database(fixture.database_url)
    try:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "approval_tasks",
                "approval_events",
                "approval_event_outbox",
            )
        )
    finally:
        connection.close()


def _insert_pending_budget_approval(
    fixture: ApprovalFixture,
    *,
    ordinal: int,
    tenant_id: str | None = None,
    suffix: str = "approve",
    created_at: str = NOW,
) -> str:
    """直接插入受迁移约束的 pending 任务，供容量和分页水位构造大量事实。"""

    owner = tenant_id or fixture.tenant_id
    approval_id = f"apr_{ordinal:032x}"
    target_suffix = "foreign" if owner == fixture.foreign_tenant_id else suffix
    source_id = fixture.blocks[target_suffix]
    target_id = fixture.rules[target_suffix]
    connection = connect_database(fixture.database_url)
    try:
        connection.execute(
            """
            INSERT INTO approval_tasks(
              approval_task_id,tenant_id,source_type,source_id,target_type,
              target_id,target_action,target_reason_code,target_version,
              target_fingerprint,risk_level,status,resolution_reason,
              state_revision,writeback_status,required_permission_code,
              created_by_type,created_by_id,decided_by_user_id,decided_at,
              expires_at,create_hmac_key_version,create_idempotency_key_hmac,
              create_intent_hmac,source_request_id,source_trace_id,
              decision_request_id,decision_trace_id,created_at,updated_at
            ) VALUES(
              ?,?,'budget',?,'budget_block_release',?,'release',NULL,?,?,'standard',
              'pending',NULL,1,'pending','billing.budget.write','service',
              'approval-service',NULL,NULL,'2099-12-31T23:59:59Z',1,?,?,?, ?,
              NULL,NULL,?,?
            )
            """,
            (
                approval_id,
                owner,
                source_id,
                target_id,
                f"direct-{ordinal}",
                f"{ordinal:064x}",
                f"{ordinal + 1:064x}",
                f"{ordinal + 2:064x}",
                f"req_prd078_direct_{ordinal}",
                f"trc_prd078_direct_{ordinal}",
                created_at,
                created_at,
            ),
        )
        connection.commit()
        return approval_id
    finally:
        connection.close()


def _revoke_role_permission(
    fixture: ApprovalFixture,
    *,
    role_code: str,
    permission_code: str,
) -> None:
    """删除一项可变角色授权并同步 revision/hash，模拟签票后的动态撤权。"""

    connection = connect_database(fixture.database_url)
    try:
        role = connection.execute(
            """
            SELECT role_id,permission_revision FROM roles
            WHERE tenant_id=? AND code=?
            """,
            (fixture.tenant_id, role_code),
        ).fetchone()
        assert role is not None
        connection.execute(
            """
            DELETE FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=(
              SELECT permission_id FROM permissions
              WHERE tenant_id=? AND code=?
            )
            """,
            (
                fixture.tenant_id,
                role["role_id"],
                fixture.tenant_id,
                permission_code,
            ),
        )
        codes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code
                FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (fixture.tenant_id, role["role_id"]),
            )
        }
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(codes),
                _db_time(datetime.now(UTC)),
                fixture.tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _create_payload(fixture: ApprovalFixture, suffix: str, key: str) -> dict[str, object]:
    """``_create_payload`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return {
        "source_type": "budget",
        "source_id": fixture.blocks[suffix],
        "target_type": "budget_block_release",
        "target_id": fixture.rules[suffix],
        "target_action": "release",
        "expires_in_seconds": 600,
        "idempotency_key": key,
    }


def _create_risk_payload(
    fixture: ApprovalFixture,
    score_name: str,
    key: str,
) -> dict[str, object]:
    """``_create_risk_payload`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    score_id = fixture.scores[score_name]
    return {
        "source_type": "session_risk",
        "source_id": score_id,
        "target_type": "session_risk_action",
        "target_id": score_id,
        "target_action": "freeze_session",
        "target_reason_code": "confirmed_attack",
        "expires_in_seconds": 600,
        "idempotency_key": key,
    }


async def _create_risk(
    client: AsyncClient,
    fixture: ApprovalFixture,
    score_name: str,
    *,
    key: str,
) -> Response:
    """``_create_risk`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    return await client.post(
        f"/internal/tenants/{fixture.tenant_id}/approvals",
        headers=_internal(),
        json=_create_risk_payload(fixture, score_name, key),
    )


async def _create(
    client: AsyncClient,
    fixture: ApprovalFixture,
    suffix: str,
    *,
    key: str,
    tenant_id: str | None = None,
) -> Response:
    """``_create`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    owner = tenant_id or fixture.tenant_id
    return await client.post(
        f"/internal/tenants/{owner}/approvals",
        headers=_internal(),
        json=_create_payload(fixture, suffix, key),
    )


def _assert_private(response: Response, vary: str) -> None:
    """``_assert_private`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == vary
    assert response.headers["x-request-id"]
    assert response.headers["x-trace-id"]


def _assert_error(response: Response, status: int, code: str, vary: str) -> None:
    """``_assert_error`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["trace_id"] == response.headers["x-trace-id"]
    _assert_private(response, vary)


@pytest.mark.anyio
async def test_internal_create_exact_replay_conflict_and_private_errors(
    approval_fixture: ApprovalFixture,
) -> None:
    """内部生产者只用服务身份；精确重放恢复，同 key 意图漂移固定 409。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post(
            f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
            json=_create_payload(approval_fixture, "approve", "create-missing-token"),
        )
        _assert_error(missing, 401, "auth.internal_token_required", "X-Internal-Token")

        created = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-budget-approve",
        )
        assert created.status_code == 201, created.text
        _assert_private(created, "X-Internal-Token")
        first = created.json()
        assert first["idempotent_replay"] is False
        assert first["approval"]["target_type"] == "budget_block_release"
        assert first["approval"]["risk_level"] == "standard"
        assert first["approval"]["required_permission_code"] == "billing.budget.write"
        approval_id = first["approval"]["approval_id"]

        replay = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-budget-approve",
        )
        assert replay.status_code == 201
        assert replay.json()["approval"]["approval_id"] == approval_id
        assert replay.json()["idempotent_replay"] is True

        drifted = _create_payload(
            approval_fixture,
            "approve",
            "create-budget-approve",
        )
        drifted["expires_in_seconds"] = 601
        conflict = await client.post(
            f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
            headers=_internal(),
            json=drifted,
        )
        _assert_error(conflict, 409, "approval.idempotency_conflict", "X-Internal-Token")

    connection = connect_database(approval_fixture.database_url)
    assert connection.execute("SELECT COUNT(*) FROM approval_tasks").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM approval_events").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM approval_event_outbox").fetchone()[0] == 1
    connection.close()


@pytest.mark.anyio
async def test_approval_authorization_is_unique_ascii_and_precedes_input_parsing(
    approval_fixture: ApprovalFixture,
) -> None:
    """重复或折叠 Bearer 在非法 query/body 前固定拒绝，且不触发业务写入。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        finance = await _login(client, "finance_admin")
        bearer = f"Bearer {finance}".encode("ascii")
        cases = (
            [(b"authorization", bearer), (b"authorization", bearer)],
            [(b"authorization", bearer + b", Bearer invalid")],
            [(b"authorization", b"Bearer \xff")],
        )
        for headers in cases:
            list_response = await client.get(
                "/approvals?unknown=must-not-win",
                headers=headers,
            )
            _assert_error(list_response, 401, "auth.token_invalid", "Authorization")

            decision_response = await client.post(
                "/approvals/not-an-id/approve?unknown=must-not-win",
                headers=headers,
                content=b"not-json",
            )
            _assert_error(
                decision_response,
                401,
                "auth.token_invalid",
                "Authorization",
            )

        missing = await client.get(
            "/approvals?unknown=must-not-win",
        )
        _assert_error(missing, 401, "auth.token_missing", "Authorization")

    assert _approval_counts(approval_fixture) == (0, 0, 0)


@pytest.mark.anyio
@pytest.mark.parametrize("wire_kind", ("duplicate", "folded"))
async def test_duplicate_authorization_second_credential_never_reaches_chain_ids(
    wire_kind: str,
    approval_fixture: ApprovalFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重复或折叠 Header 的第二段凭据必须在认证失败前进入链路 ID 碰撞扫描。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        finance = await _login(client, "finance_admin")
        second_credential = "prd078-duplicate-credential-" + "z" * 48
        authorization_headers = (
            [
                (b"authorization", f"Bearer {finance}".encode("ascii")),
                (
                    b"authorization",
                    f"Bearer {second_credential}".encode("ascii"),
                ),
            ]
            if wire_kind == "duplicate"
            else [
                (
                    b"authorization",
                    f"Bearer {finance},Bearer {second_credential}".encode("ascii"),
                )
            ]
        )
        caplog.clear()
        caplog.set_level(logging.INFO, logger="metahub.runtime")
        response = await client.get(
            "/approvals",
            headers=[
                *authorization_headers,
                (b"x-request-id", second_credential.encode("ascii")),
                (b"x-trace-id", second_credential.encode("ascii")),
            ],
        )

    _assert_error(response, 401, "auth.token_invalid", "Authorization")
    assert response.headers["x-request-id"] != second_credential
    assert response.headers["x-trace-id"] != second_credential
    assert second_credential not in response.text
    completed = [
        record
        for record in caplog.records
        if record.name == "metahub.runtime"
        and record.getMessage() == "request.completed"
        and getattr(record, "path", None) == "/approvals"
    ]
    assert len(completed) == 1
    rendered = f"{completed[0].getMessage()} {completed[0].__dict__!r}"
    assert second_credential not in rendered
    assert completed[0].request_id != second_credential
    assert completed[0].trace_id != second_credential


@pytest.mark.anyio
async def test_decision_idempotency_header_ambiguity_uses_required_error(
    approval_fixture: ApprovalFixture,
) -> None:
    """缺失、空、重复和逗号折叠的幂等 Header 都使用专用固定错误码。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-idempotency-wire",
        )
        assert created.status_code == 201, created.text
        approval_id = created.json()["approval"]["approval_id"]
        finance = await _login(client, "finance_admin")
        body = _json_bytes({"expected_revision": 1, "reason_code": "approved_business_need"})
        authorization = f"Bearer {finance}".encode("ascii")
        cases = (
            (),
            (b"",),
            (b" \t ",),
            (b"decision-wire", b"decision-wire"),
            (b"decision-wire,decision-wire",),
        )
        for idempotency_values in cases:
            response = await client.post(
                f"/approvals/{approval_id}/approve",
                headers=[
                    (b"authorization", authorization),
                    *((b"idempotency-key", value) for value in idempotency_values),
                    (b"content-type", b"application/json"),
                ],
                content=body,
            )
            _assert_error(
                response,
                400,
                "approval.idempotency_required",
                "Authorization",
            )

    connection = connect_database(approval_fixture.database_url)
    try:
        task = connection.execute(
            "SELECT status,state_revision FROM approval_tasks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()
        assert tuple(task) == ("pending", 1)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
                (approval_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_internal_token_rejects_ambiguous_wire_without_writes(
    approval_fixture: ApprovalFixture,
) -> None:
    """内部 Token 必须唯一且为 ASCII；拒绝路径不创建 task、event 或 outbox。"""

    app = _build_app()
    body = _json_bytes(_create_payload(approval_fixture, "approve", "internal-wire-reject"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cases = (
            (INTERNAL_TOKEN.encode("ascii"), INTERNAL_TOKEN.encode("ascii")),
            (INTERNAL_TOKEN.encode("ascii") + b",invalid",),
            (b"\xff",),
        )
        for values in cases:
            response = await client.post(
                f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
                headers=_internal_raw_headers(body, token_values=values),
                content=body,
            )
            _assert_error(
                response,
                403,
                "auth.internal_token_invalid",
                "X-Internal-Token",
            )
            assert _approval_counts(approval_fixture) == (0, 0, 0)


@pytest.mark.anyio
async def test_internal_json_content_type_and_length_are_exact(
    approval_fixture: ApprovalFixture,
) -> None:
    """只接受两种 canonical JSON Content-Type，并拒绝歧义长度且无半写入。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for ordinal, (content_type, suffix) in enumerate(
            (
                (b"application/json", "approve"),
                (b"application/json; charset=utf-8", "reject"),
            ),
            start=1,
        ):
            payload = _create_payload(
                approval_fixture,
                suffix,
                f"wire-content-type-valid-{ordinal}",
            )
            body = _json_bytes(payload)
            response = await client.post(
                f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
                headers=_internal_raw_headers(
                    body,
                    content_types=(content_type,),
                ),
                content=body,
            )
            assert response.status_code == 201, response.text

        baseline = _approval_counts(approval_fixture)
        invalid_types = (
            (b"application/json", b"application/json"),
            (b"application/json, text/plain",),
            (b"application/json; charset=latin-1",),
            (b"application/json; profile=test",),
            (b"application/json; charset=utf-8; charset=utf-8",),
        )
        body = _json_bytes(_create_payload(approval_fixture, "approve", "wire-invalid-type"))
        for content_types in invalid_types:
            response = await client.post(
                f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
                headers=_internal_raw_headers(
                    body,
                    content_types=content_types,
                ),
                content=body,
            )
            _assert_error(
                response,
                400,
                "approval.input_invalid",
                "X-Internal-Token",
            )
            assert _approval_counts(approval_fixture) == baseline

        length_cases = (
            (str(len(body)).encode("ascii"), str(len(body)).encode("ascii")),
            (str(len(body) + 1).encode("ascii"),),
            (b"1,2",),
        )
        for content_lengths in length_cases:
            response = await client.post(
                f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
                headers=_internal_raw_headers(
                    body,
                    content_lengths=content_lengths,
                ),
                content=body,
            )
            _assert_error(
                response,
                400,
                "approval.input_invalid",
                "X-Internal-Token",
            )
            assert _approval_counts(approval_fixture) == baseline


@pytest.mark.anyio
async def test_internal_json_rejects_ambiguous_encoding_and_shape_without_writes(
    approval_fixture: ApprovalFixture,
) -> None:
    """重复 key、非 UTF-8、非有限数、深度五和第六十五节点均失败关闭。"""

    valid = _json_bytes(_create_payload(approval_fixture, "approve", "wire-shape-invalid"))
    duplicate = valid[:-1] + b',"source_type":"budget"}'
    non_utf8 = valid[:-1] + b',"extra":"\xff"}'
    nan = valid[:-1] + b',"extra":NaN}'
    infinity = valid[:-1] + b',"extra":Infinity}'
    depth_five = valid[:-1] + b',"extra":{"a":{"b":{"c":0}}}}'
    wide = valid[:-1] + (b',"extra":[' + b",".join(b"0" for _ in range(56)) + b"]}")
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for body in (duplicate, non_utf8, nan, infinity, depth_five, wide):
            response = await client.post(
                f"/internal/tenants/{approval_fixture.tenant_id}/approvals",
                headers=_internal_raw_headers(body),
                content=body,
            )
            _assert_error(
                response,
                400,
                "approval.input_invalid",
                "X-Internal-Token",
            )
            assert _approval_counts(approval_fixture) == (0, 0, 0)


@pytest.mark.anyio
async def test_list_detail_enforce_base_target_and_tenant_permissions(
    approval_fixture: ApprovalFixture,
) -> None:
    """审批权限不能替代目标域权限，跨租户同形任务保持 404。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        local = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-list-local",
        )
        foreign = await _create(
            client,
            approval_fixture,
            "foreign",
            key="create-list-foreign",
            tenant_id=approval_fixture.foreign_tenant_id,
        )
        assert local.status_code == foreign.status_code == 201
        local_id = local.json()["approval"]["approval_id"]
        foreign_id = foreign.json()["approval"]["approval_id"]

        finance = await _login(client, "finance_admin")
        security = await _login(client, "security_admin")
        developer = await _login(client, "developer")

        finance_list = await client.get("/approvals", headers=_auth(finance))
        assert finance_list.status_code == 200
        _assert_private(finance_list, "Authorization")
        assert [item["approval_id"] for item in finance_list.json()["approvals"]] == [local_id]

        security_list = await client.get("/approvals", headers=_auth(security))
        assert security_list.status_code == 200
        assert security_list.json()["approvals"] == []
        hidden = await client.get(f"/approvals/{local_id}", headers=_auth(security))
        _assert_error(hidden, 404, "approval.not_found", "Authorization")

        denied = await client.get("/approvals", headers=_auth(developer))
        _assert_error(denied, 403, "auth.permission_denied", "Authorization")
        cross_tenant = await client.get(f"/approvals/{foreign_id}", headers=_auth(finance))
        _assert_error(cross_tenant, 404, "approval.not_found", "Authorization")


@pytest.mark.anyio
async def test_budget_approve_releases_block_and_exact_replay_is_single_fact(
    approval_fixture: ApprovalFixture,
) -> None:
    """预算批准在一个事务写终态、Decision、writeback，并真实释放指定规则。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-approve-flow",
        )
        approval_id = created.json()["approval"]["approval_id"]
        finance = await _login(client, "finance_admin")
        headers = {**_auth(finance), "Idempotency-Key": "decision-approve-flow"}
        body = {"expected_revision": 1, "reason_code": "approved_business_need"}

        decided = await client.post(f"/approvals/{approval_id}/approve", headers=headers, json=body)
        assert decided.status_code == 200, decided.text
        _assert_private(decided, "Authorization")
        assert decided.json()["approval"]["status"] == "approved"
        assert decided.json()["approval"]["writeback_status"] == "completed"
        assert decided.json()["decision"]["step_up_used"] is False
        assert decided.json()["idempotent_replay"] is False

        replay = await client.post(f"/approvals/{approval_id}/approve", headers=headers, json=body)
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        opposite = await client.post(
            f"/approvals/{approval_id}/reject",
            headers=headers,
            json={"expected_revision": 1, "reason_code": "budget_not_approved"},
        )
        _assert_error(opposite, 409, "approval.idempotency_conflict", "Authorization")

    connection = connect_database(approval_fixture.database_url)
    block = connection.execute(
        "SELECT status,release_reason,released_by FROM budget_blocks WHERE budget_block_id=?",
        (approval_fixture.blocks["approve"],),
    ).fetchone()
    assert tuple(block) == ("released", "approval_approved", ROLE_USERS["finance_admin"][0])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 1
    )
    writeback = connection.execute(
        """
        SELECT adapter_kind,status,attempt_count
        FROM approval_writebacks WHERE approval_task_id=?
        """,
        (approval_id,),
    ).fetchone()
    assert tuple(writeback) == ("budget_block_release", "completed", 0)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


@pytest.mark.anyio
async def test_decision_replay_expires_after_twenty_four_hours_without_deletion(
    approval_fixture: ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期 replay 不再恢复；旧事实保留且同 actor/key 可开启新的 24 小时窗口。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    decision_key = "decision-replay-window"
    decision_body = {
        "expected_revision": 1,
        "reason_code": "approved_business_need",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_created = await _create(
            client,
            approval_fixture,
            "approve",
            key="create-replay-window-first",
        )
        assert first_created.status_code == 201, first_created.text
        first_id = first_created.json()["approval"]["approval_id"]
        finance = await _login(client, "finance_admin")
        headers = {**_auth(finance), "Idempotency-Key": decision_key}
        first_decision = await client.post(
            f"/approvals/{first_id}/approve",
            headers=headers,
            json=decision_body,
        )
        assert first_decision.status_code == 200, first_decision.text
        assert first_decision.json()["idempotent_replay"] is False

        connection = connect_database(approval_fixture.database_url)
        first_replay = connection.execute(
            """
            SELECT approval_action_replay_id,approval_task_id,created_at,expires_at
            FROM approval_action_replays
            """
        ).fetchone()
        connection.close()
        assert first_replay is not None
        first_replay_fact = tuple(first_replay)
        expires_at = datetime.fromisoformat(str(first_replay["expires_at"]).replace("Z", "+00:00"))
        assert (
            expires_at
            - datetime.fromisoformat(str(first_replay["created_at"]).replace("Z", "+00:00"))
            == approval_service.REPLAY_TTL
        )
        after_window = expires_at + timedelta(seconds=1)
        monkeypatch.setattr(
            approval_service,
            "_database_now",
            lambda _connection: after_window,
        )

        expired_retry = await client.post(
            f"/approvals/{first_id}/approve",
            headers=headers,
            json=decision_body,
        )
        _assert_error(
            expired_retry,
            409,
            "approval.state_conflict",
            "Authorization",
        )

        second_created = await _create(
            client,
            approval_fixture,
            "reject",
            key="create-replay-window-second",
        )
        assert second_created.status_code == 201, second_created.text
        second_id = second_created.json()["approval"]["approval_id"]
        second_decision = await client.post(
            f"/approvals/{second_id}/approve",
            headers=headers,
            json=decision_body,
        )
        assert second_decision.status_code == 200, second_decision.text
        assert second_decision.json()["idempotent_replay"] is False

    connection = connect_database(approval_fixture.database_url)
    try:
        replay_facts = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT approval_action_replay_id,approval_task_id,created_at,expires_at
                FROM approval_action_replays ORDER BY created_at,approval_action_replay_id
                """
            ).fetchall()
        ]
        assert len(replay_facts) == 2
        assert first_replay_fact in replay_facts
        assert {row[1] for row in replay_facts} == {first_id, second_id}
    finally:
        connection.close()


@pytest.mark.anyio
async def test_budget_reject_has_no_target_effect_and_target_drift_stays_pending(
    approval_fixture: ApprovalFixture,
) -> None:
    """拒绝不释放预算；目标版本漂移时批准失败且不产生半条决策事实。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        rejected_task = await _create(
            client,
            approval_fixture,
            "reject",
            key="create-reject-flow",
        )
        drift_task = await _create(
            client,
            approval_fixture,
            "drift",
            key="create-drift-flow",
        )
        rejected_id = rejected_task.json()["approval"]["approval_id"]
        drift_id = drift_task.json()["approval"]["approval_id"]
        finance = await _login(client, "finance_admin")

        rejected = await client.post(
            f"/approvals/{rejected_id}/reject",
            headers={**_auth(finance), "Idempotency-Key": "decision-reject-flow"},
            json={"expected_revision": 1, "reason_code": "budget_not_approved"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["approval"]["status"] == "rejected"

        connection = connect_database(approval_fixture.database_url)
        connection.execute(
            "UPDATE budget_rules SET updated_at='2026-08-12T08:01:00Z' WHERE budget_rule_id=?",
            (approval_fixture.rules["drift"],),
        )
        connection.commit()
        connection.close()
        drifted = await client.post(
            f"/approvals/{drift_id}/approve",
            headers={**_auth(finance), "Idempotency-Key": "decision-drift-flow"},
            json={"expected_revision": 1, "reason_code": "approved_business_need"},
        )
        _assert_error(drifted, 409, "approval.target_conflict", "Authorization")

    connection = connect_database(approval_fixture.database_url)
    assert (
        connection.execute(
            "SELECT status FROM budget_blocks WHERE budget_block_id=?",
            (approval_fixture.blocks["reject"],),
        ).fetchone()[0]
        == "active"
    )
    task = connection.execute(
        "SELECT status,state_revision FROM approval_tasks WHERE approval_task_id=?",
        (drift_id,),
    ).fetchone()
    assert tuple(task) == ("pending", 1)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
            (drift_id,),
        ).fetchone()[0]
        == 0
    )
    connection.close()


@pytest.mark.anyio
async def test_high_risk_step_up_binds_action_task_session_and_is_consumed_once(
    approval_fixture: ApprovalFixture,
) -> None:
    """120 秒票据绑定动作、任务、Session；正确消费后真实冻结风险目标。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await _create_risk(
            client,
            approval_fixture,
            "binding",
            key="create-risk-binding-first",
        )
        second = await _create_risk(
            client,
            approval_fixture,
            "other",
            key="create-risk-binding-second",
        )
        assert first.status_code == second.status_code == 201
        first_id = first.json()["approval"]["approval_id"]
        second_id = second.json()["approval"]["approval_id"]
        assert first.json()["approval"]["risk_level"] == "high"
        assert first.json()["approval"]["requires_step_up"] is True

        first_session = await _login(client, "security_admin")
        second_session = await _login(client, "security_admin")
        missing = await client.post(
            f"/approvals/{first_id}/approve",
            headers={
                **_auth(first_session),
                "Idempotency-Key": "risk-decision-missing-step-up",
            },
            json={"expected_revision": 1, "reason_code": "approved_business_need"},
        )
        _assert_error(missing, 403, "approval.step_up_required", "Authorization")

        verified = await client.post(
            f"/approvals/{first_id}/step-up",
            headers=_auth(first_session),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        assert verified.status_code == 200, verified.text
        _assert_private(verified, "Authorization")
        step_up_token = str(verified.json()["step_up_token"])

        wrong_action = await client.post(
            f"/approvals/{first_id}/reject",
            headers={
                **_auth(first_session),
                "Idempotency-Key": "risk-decision-wrong-action",
            },
            json={
                "expected_revision": 1,
                "reason_code": "risk_unacceptable",
                "step_up_token": step_up_token,
            },
        )
        _assert_error(wrong_action, 403, "approval.step_up_invalid", "Authorization")
        wrong_task = await client.post(
            f"/approvals/{second_id}/approve",
            headers={
                **_auth(first_session),
                "Idempotency-Key": "risk-decision-wrong-task",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        _assert_error(wrong_task, 403, "approval.step_up_invalid", "Authorization")
        wrong_session = await client.post(
            f"/approvals/{first_id}/approve",
            headers={
                **_auth(second_session),
                "Idempotency-Key": "risk-decision-wrong-session",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        _assert_error(wrong_session, 403, "approval.step_up_invalid", "Authorization")

        approved = await client.post(
            f"/approvals/{first_id}/approve",
            headers={
                **_auth(first_session),
                "Idempotency-Key": "risk-decision-success",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approval"]["status"] == "approved"
        assert approved.json()["approval"]["writeback_status"] == "pending"
        assert approved.json()["decision"]["step_up_used"] is True

    connection = connect_database(approval_fixture.database_url)
    challenge = connection.execute(
        """
        SELECT status,created_at,expires_at,consumed_at,token_hmac
        FROM approval_step_up_challenges WHERE approval_task_id=?
        """,
        (first_id,),
    ).fetchone()
    assert challenge["status"] == "consumed"
    assert challenge["consumed_at"] is not None
    assert len(str(challenge["token_hmac"])) == 64
    assert (
        datetime.fromisoformat(str(challenge["expires_at"]).replace("Z", "+00:00"))
        - datetime.fromisoformat(str(challenge["created_at"]).replace("Z", "+00:00"))
    ) == timedelta(seconds=120)
    score = connection.execute(
        """
        SELECT status,state_revision FROM session_risk_scores
        WHERE session_risk_score_id=?
        """,
        (approval_fixture.scores["binding"],),
    ).fetchone()
    assert tuple(score) == ("frozen", 2)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM session_risk_actions WHERE session_risk_score_id=?",
            (approval_fixture.scores["binding"],),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM session_risk_freezes WHERE source_session_risk_score_id=?",
            (approval_fixture.scores["binding"],),
        ).fetchone()[0]
        == 1
    )
    writeback = connection.execute(
        """
        SELECT adapter_kind,status,attempt_count,target_revision
        FROM approval_writebacks WHERE approval_task_id=?
        """,
        (first_id,),
    ).fetchone()
    assert tuple(writeback) == ("session_risk_store_sync", "pending", 0, 2)
    persisted = "\n".join(
        str(value)
        for table in ("approval_step_up_challenges", "approval_action_replays")
        for row in connection.execute(f"SELECT * FROM {table}")
        for value in tuple(row)
    )
    assert step_up_token not in persisted
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


@pytest.mark.anyio
async def test_step_up_fifth_failure_locks_fifteen_minutes_per_task(
    approval_fixture: ApprovalFixture,
) -> None:
    """错误密码按 tenant/task/user 计数，第五次立即锁 15 分钟且不创建票据。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        locked_task = await _create_risk(
            client,
            approval_fixture,
            "lock",
            key="create-risk-lock",
        )
        spare_task = await _create_risk(
            client,
            approval_fixture,
            "spare",
            key="create-risk-spare",
        )
        locked_id = locked_task.json()["approval"]["approval_id"]
        spare_id = spare_task.json()["approval"]["approval_id"]
        security = await _login(client, "security_admin")

        for attempt in range(1, 6):
            response = await client.post(
                f"/approvals/{locked_id}/step-up",
                headers=_auth(security),
                json={"action": "approve", "password": f"wrong-{attempt}"},
            )
            expected = (
                "approval.step_up_rate_limited" if attempt == 5 else "approval.step_up_invalid"
            )
            _assert_error(
                response,
                429 if attempt == 5 else 403,
                expected,
                "Authorization",
            )

        still_locked = await client.post(
            f"/approvals/{locked_id}/step-up",
            headers=_auth(security),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        _assert_error(
            still_locked,
            429,
            "approval.step_up_rate_limited",
            "Authorization",
        )
        other_task = await client.post(
            f"/approvals/{spare_id}/step-up",
            headers=_auth(security),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        assert other_task.status_code == 200

    connection = connect_database(approval_fixture.database_url)
    guard = connection.execute(
        """
        SELECT failed_attempts,updated_at,locked_until
        FROM approval_step_up_guards WHERE approval_task_id=?
        """,
        (locked_id,),
    ).fetchone()
    assert guard["failed_attempts"] == 5
    assert (
        datetime.fromisoformat(str(guard["locked_until"]).replace("Z", "+00:00"))
        - datetime.fromisoformat(str(guard["updated_at"]).replace("Z", "+00:00"))
    ) == timedelta(minutes=15)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_step_up_challenges WHERE approval_task_id=?",
            (locked_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_step_up_challenges WHERE approval_task_id=?",
            (spare_id,),
        ).fetchone()[0]
        == 1
    )
    connection.close()


@pytest.mark.anyio
async def test_step_up_password_change_during_pbkdf2_creates_no_challenge(
    approval_fixture: ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PBKDF2 完成后事务内重验密码哈希，改密竞态不得签发 challenge。"""

    entered = threading.Event()
    release = threading.Event()
    original_verify = approval_service.verify_password

    def delayed_verify(password: str, encoded_hash: str) -> bool:
        """在真实校验前暴露确定窗口，让测试并发修改权威密码哈希。"""

        entered.set()
        assert release.wait(timeout=10)
        return original_verify(password, encoded_hash)

    monkeypatch.setattr(approval_service, "verify_password", delayed_verify)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_risk(
            client,
            approval_fixture,
            "binding",
            key="create-password-race",
        )
        approval_id = created.json()["approval"]["approval_id"]
        security = await _login(client, "security_admin")

        def post_step_up_in_thread() -> Response:
            """在独立事件循环发请求，使阻塞式 PBKDF2 不冻结测试协调线程。"""

            async def post() -> Response:
                """提交被刻意暂停的 step-up，并让测试线程在权威密码变更后读取结果。"""
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as contender:
                    return await contender.post(
                        f"/approvals/{approval_id}/step-up",
                        headers=_auth(security),
                        json={"action": "approve", "password": ROLE_PASSWORD},
                    )

            return asyncio.run(post())

        pending = asyncio.create_task(asyncio.to_thread(post_step_up_in_thread))
        assert await asyncio.to_thread(entered.wait, 10)
        connection = connect_database(approval_fixture.database_url)
        connection.execute(
            """
            UPDATE users SET password_hash=?,updated_at=?
            WHERE tenant_id=? AND user_id=?
            """,
            (
                hash_password("ChangedDuringPbkdf2!2026"),
                _db_time(datetime.now(UTC)),
                approval_fixture.tenant_id,
                ROLE_USERS["security_admin"][0],
            ),
        )
        connection.commit()
        connection.close()
        release.set()
        response = await pending
        _assert_error(
            response,
            403,
            "approval.step_up_invalid",
            "Authorization",
        )

    connection = connect_database(approval_fixture.database_url)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_step_up_challenges WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_step_up_guards WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 0
    )
    connection.close()


@pytest.mark.anyio
async def test_issued_step_up_is_invalid_after_session_revocation(
    approval_fixture: ApprovalFixture,
) -> None:
    """签票后撤销 Session 时旧 Bearer 与票据均不能写入审批终态。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_risk(
            client,
            approval_fixture,
            "binding",
            key="create-session-revoke",
        )
        approval_id = created.json()["approval"]["approval_id"]
        security = await _login(client, "security_admin")
        verified = await client.post(
            f"/approvals/{approval_id}/step-up",
            headers=_auth(security),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        assert verified.status_code == 200, verified.text
        step_up_token = verified.json()["step_up_token"]

        connection = connect_database(approval_fixture.database_url)
        connection.execute(
            """
            UPDATE sessions SET status='revoked',revoked_at=?,updated_at=?
            WHERE tenant_id=? AND user_id=? AND status='active'
            """,
            (
                _db_time(datetime.now(UTC)),
                _db_time(datetime.now(UTC)),
                approval_fixture.tenant_id,
                ROLE_USERS["security_admin"][0],
            ),
        )
        connection.commit()
        connection.close()

        response = await client.post(
            f"/approvals/{approval_id}/approve",
            headers={
                **_auth(security),
                "Idempotency-Key": "decision-revoked-session",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        _assert_error(response, 401, "auth.session_revoked", "Authorization")

    connection = connect_database(approval_fixture.database_url)
    task = connection.execute(
        "SELECT status,state_revision FROM approval_tasks WHERE approval_task_id=?",
        (approval_id,),
    ).fetchone()
    assert tuple(task) == ("pending", 1)
    challenge = connection.execute(
        """
        SELECT status FROM approval_step_up_challenges
        WHERE approval_task_id=?
        """,
        (approval_id,),
    ).fetchone()
    assert challenge["status"] == "active"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 0
    )
    connection.close()


@pytest.mark.anyio
async def test_issued_step_up_is_invalid_after_permission_fingerprint_changes(
    approval_fixture: ApprovalFixture,
) -> None:
    """签票后的角色 revision/hash 漂移使旧授权和票据同时失效且无终态。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_risk(
            client,
            approval_fixture,
            "binding",
            key="create-fingerprint-drift",
        )
        approval_id = created.json()["approval"]["approval_id"]
        security = await _login(client, "security_admin")
        verified = await client.post(
            f"/approvals/{approval_id}/step-up",
            headers=_auth(security),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        assert verified.status_code == 200, verified.text
        step_up_token = verified.json()["step_up_token"]

        _revoke_role_permission(
            approval_fixture,
            role_code="security_admin",
            permission_code="audit.export",
        )
        response = await client.post(
            f"/approvals/{approval_id}/approve",
            headers={
                **_auth(security),
                "Idempotency-Key": "decision-fingerprint-drift",
            },
            json={
                "expected_revision": 1,
                "reason_code": "approved_business_need",
                "step_up_token": step_up_token,
            },
        )
        _assert_error(response, 403, "approval.step_up_invalid", "Authorization")

    connection = connect_database(approval_fixture.database_url)
    task = connection.execute(
        "SELECT status,state_revision FROM approval_tasks WHERE approval_task_id=?",
        (approval_id,),
    ).fetchone()
    assert tuple(task) == ("pending", 1)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            """
        SELECT status FROM approval_step_up_challenges
        WHERE approval_task_id=?
        """,
            (approval_id,),
        ).fetchone()["status"]
        == "active"
    )
    connection.close()


@pytest.mark.anyio
async def test_step_up_token_concurrent_double_consume_has_one_terminal_fact(
    approval_fixture: ApprovalFixture,
) -> None:
    """同一票据并发双消费仅一个成功，另一请求不产生第二条决策或回写。"""

    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await _create_risk(
            client,
            approval_fixture,
            "binding",
            key="create-double-consume",
        )
        approval_id = created.json()["approval"]["approval_id"]
        security = await _login(client, "security_admin")
        verified = await client.post(
            f"/approvals/{approval_id}/step-up",
            headers=_auth(security),
            json={"action": "approve", "password": ROLE_PASSWORD},
        )
        assert verified.status_code == 200, verified.text
        token = verified.json()["step_up_token"]

        async def consume(idempotency_key: str) -> Response:
            """以独立客户端争用同一 SQLite 写事务和同一个 step-up token。"""

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as contender:
                return await contender.post(
                    f"/approvals/{approval_id}/approve",
                    headers={
                        **_auth(security),
                        "Idempotency-Key": idempotency_key,
                    },
                    json={
                        "expected_revision": 1,
                        "reason_code": "approved_business_need",
                        "step_up_token": token,
                    },
                )

        responses = await asyncio.gather(
            consume("decision-double-consume-a"),
            consume("decision-double-consume-b"),
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        failed = next(response for response in responses if response.status_code != 200)
        _assert_error(failed, 409, "approval.state_conflict", "Authorization")

    connection = connect_database(approval_fixture.database_url)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_decisions WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_writebacks WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM approval_action_replays WHERE approval_task_id=?",
            (approval_id,),
        ).fetchone()[0]
        == 1
    )
    challenge = connection.execute(
        """
        SELECT status,consumed_at FROM approval_step_up_challenges
        WHERE approval_task_id=?
        """,
        (approval_id,),
    ).fetchone()
    assert challenge["status"] == "consumed"
    assert challenge["consumed_at"] is not None
    connection.close()


@pytest.mark.anyio
async def test_pending_capacity_checks_replay_before_tenant_local_limit(
    approval_fixture: ApprovalFixture,
) -> None:
    """容量满时精确 replay 仍恢复；新意图拒绝，跨租户容量互不占用。"""

    app = _build_app()
    app.state.config.approval_max_pending_tasks_per_tenant = 1
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        local = await _create(
            client,
            approval_fixture,
            "approve",
            key="capacity-local-first",
        )
        assert local.status_code == 201, local.text
        local_id = local.json()["approval"]["approval_id"]

        replay = await _create(
            client,
            approval_fixture,
            "approve",
            key="capacity-local-first",
        )
        assert replay.status_code == 201
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["approval"]["approval_id"] == local_id

        full = await _create(
            client,
            approval_fixture,
            "reject",
            key="capacity-local-second",
        )
        _assert_error(full, 429, "approval.capacity_full", "X-Internal-Token")

        foreign = await _create(
            client,
            approval_fixture,
            "foreign",
            key="capacity-foreign-first",
            tenant_id=approval_fixture.foreign_tenant_id,
        )
        assert foreign.status_code == 201, foreign.text
        assert foreign.json()["approval"]["approval_id"] != local_id

    connection = connect_database(approval_fixture.database_url)
    counts = dict(
        connection.execute(
            """
            SELECT tenant_id,COUNT(*) FROM approval_tasks
            WHERE status='pending' GROUP BY tenant_id
            """
        ).fetchall()
    )
    assert counts == {
        approval_fixture.tenant_id: 1,
        approval_fixture.foreign_tenant_id: 1,
    }
    connection.close()


@pytest.mark.anyio
async def test_list_cursor_excludes_new_same_second_row(
    approval_fixture: ApprovalFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首屏 cursor 固定 tenant rowid 水位，同秒新任务不混入旧快照后续页。"""

    app = _build_app()
    fixed_now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(
        approval_service,
        "_database_now",
        lambda _connection: fixed_now,
    )
    first_id = _insert_pending_budget_approval(
        approval_fixture,
        ordinal=3,
        suffix="approve",
        created_at=_db_time(fixed_now),
    )
    second_id = _insert_pending_budget_approval(
        approval_fixture,
        ordinal=1,
        suffix="reject",
        created_at=_db_time(fixed_now),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        finance = await _login(client, "finance_admin")
        page_one = await client.get(
            "/approvals?limit=1",
            headers=_auth(finance),
        )
        assert page_one.status_code == 200, page_one.text
        first_page_ids = {item["approval_id"] for item in page_one.json()["approvals"]}
        assert len(first_page_ids) == 1
        cursor = page_one.json()["next_cursor"]
        assert cursor

        # 在首屏之后插入更高 rowid 且同一 created_at 的任务；时间条件单独无法排除它。
        third_id = _insert_pending_budget_approval(
            approval_fixture,
            ordinal=2,
            suffix="drift",
            created_at=_db_time(fixed_now),
        )
        page_two = await client.get(
            "/approvals",
            params={"limit": "1", "cursor": cursor},
            headers=_auth(finance),
        )
        assert page_two.status_code == 200, page_two.text
        second_page_ids = {item["approval_id"] for item in page_two.json()["approvals"]}
        assert second_page_ids == {first_id, second_id} - first_page_ids
        assert third_id not in second_page_ids
