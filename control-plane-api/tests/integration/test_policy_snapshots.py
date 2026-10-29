"""PRD-045 策略快照 Control API 的发布、回滚、权限、幂等与内部拉取测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.policies.routes import router as policy_router
from metahub_control.policy_snapshots.routes import router as snapshot_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试口令与内部 Token 只存在于临时进程内存，不进入快照、审计、Webhook 或日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"
INTERNAL_TOKEN = "prd045-internal-token-for-tests-only"


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时文件转换为项目数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只含认证、Policy 候选和快照路由的隔离 Control 应用。"""

    app = create_service_app(
        "control-plane-api",
        "Policy Snapshot Test API",
        extra_cors_allow_headers=("If-Match", "If-Policy-Candidate-Match"),
        extra_cors_expose_headers=("ETag", "X-Policy-Candidate-ETag"),
    )
    app.include_router(auth_router)
    app.include_router(policy_router)
    app.include_router(snapshot_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """准备默认租户、三角色、同租户第二管理员和一个无 head 的其他租户。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd045.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-policy-snapshot-auth-secret")
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", INTERNAL_TOKEN)
    seed = apply_seed_data(database_url) if apply_migrations(database_url) else None
    assert seed is not None
    connection = connect_database(database_url)
    now = "2026-07-17T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd045_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                now,
                now,
            ),
            (
                "user_prd045_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                now,
                now,
            ),
            (
                "user_prd045_admin_two",
                seed.tenant_id,
                "admin-two@example.com",
                "Second Admin",
                hash_password(OTHER_ADMIN_PASSWORD),
                "admin",
                now,
                now,
            ),
        ],
    )
    other_tenant_id = "tenant_prd045_other"
    other_user_id = "user_prd045_other_admin"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(?, 'prd045-other', 'PRD045 Other', 'internal_only', 'active', ?, ?)
        """,
        (other_tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        ) VALUES(?, ?, 'admin@other.example.com', 'Other Admin', ?, 'admin', 'active', ?, ?)
        """,
        (other_user_id, other_tenant_id, hash_password(OTHER_ADMIN_PASSWORD), now, now),
    )
    connection.commit()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "other_tenant_id": other_tenant_id,
    }


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录测试用户并只把短期 Bearer Token 保存在当前测试内存。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只在当前测试请求内存中复用，禁止写入断言消息、快照或任何持久事件。
    return response.json()["access_token"]


async def _create_policy(client: AsyncClient, token: str, expression: str) -> dict[str, object]:
    """通过真实 Policy API 创建 active deny 候选，避免测试绕过编译和审计边界。"""

    response = await client.post(
        "/policies",
        json={
            "name": "生产环境阻断策略",
            "description": "用于快照主流程测试",
            "status": "active",
            "priority": 100,
            "rule": {"condition_expr": expression},
            "action": {"type": "deny"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["policy"]


async def _current(client: AsyncClient, token: str):
    """读取当前快照与 candidate ETag，供后续双 CAS 请求复用。"""

    response = await client.get(
        "/policy-snapshots/current",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response


async def _publish(
    client: AsyncClient,
    token: str,
    current,
    *,
    reason: str,
    request_id: str,
):
    """使用 current 返回的两个强 ETag 发起一次发布。"""

    return await client.post(
        "/policy-snapshots/publish",
        json={"reason": reason},
        headers={
            "Authorization": f"Bearer {token}",
            "If-Match": current.headers["ETag"],
            "If-Policy-Candidate-Match": current.headers["X-Policy-Candidate-ETag"],
            "X-Request-ID": request_id,
        },
    )


@pytest.mark.anyio
async def test_publish_history_rollback_and_candidate_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：A 发布 v1、B 发布 v2、回滚 A 生成 v3，历史/hash/head 均可解释。"""

    state = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched: list[object] = []

    def _capture_dispatch(_config, _tenant_id, event, **_kwargs):
        """捕获提交后事件，验证 shared 严格 payload，而不执行外部网络副作用。"""

        dispatched.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_dispatch)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        policy = await _create_policy(client, admin, 'env=="prod"')
        current_v0 = await _current(client, admin)
        assert current_v0.headers["etag"] == '"0"'
        assert current_v0.json()["candidate_status"] == "valid"
        published_v1 = await _publish(
            client,
            admin,
            current_v0,
            reason="发布生产阻断基线",
            request_id="req_prd045_publish_v1",
        )
        assert published_v1.status_code == 201, published_v1.text
        v1 = published_v1.json()["policy_snapshot"]
        assert v1["snapshot_version"] == 1
        assert v1["policy_count"] == 1

        updated = await client.patch(
            f"/policies/{policy['policy_id']}",
            json={"rule": {"condition_expr": 'env == "dev"'}},
            headers={"Authorization": f"Bearer {admin}", "If-Match": '"1"'},
        )
        assert updated.status_code == 200, updated.text
        current_v1 = await _current(client, admin)
        assert current_v1.json()["candidate_matches_current"] is False
        published_v2 = await _publish(
            client,
            admin,
            current_v1,
            reason="切换为开发环境阻断",
            request_id="req_prd045_publish_v2",
        )
        assert published_v2.status_code == 201, published_v2.text
        v2 = published_v2.json()["policy_snapshot"]
        assert v2["snapshot_version"] == 2
        assert v2["content_hash"] != v1["content_hash"]

        rolled_back = await client.post(
            f"/policy-snapshots/{v1['policy_snapshot_id']}/rollback",
            json={"reason": "回滚上一版已验证内容"},
            headers={
                "Authorization": f"Bearer {admin}",
                "If-Match": '"2"',
                "X-Request-ID": "req_prd045_rollback_v1",
            },
        )
        assert rolled_back.status_code == 201, rolled_back.text
        v3 = rolled_back.json()["policy_snapshot"]
        assert v3["snapshot_version"] == 3
        assert v3["operation"] == "rollback"
        assert v3["rollback_source_snapshot_version"] == 1
        assert v3["content_hash"] == v1["content_hash"]
        assert v3["artifact_hash"] not in {v1["artifact_hash"], v2["artifact_hash"]}

        current_v3 = await _current(client, admin)
        history = await client.get(
            "/policy-snapshots?limit=10&offset=0",
            headers={"Authorization": f"Bearer {admin}"},
        )

    assert current_v3.headers["etag"] == '"3"'
    assert current_v3.json()["candidate_matches_current"] is False
    assert [item["snapshot_version"] for item in history.json()["policy_snapshots"]] == [3, 2, 1]
    connection = connect_database(state["database_url"])
    assert connection.execute("SELECT COUNT(*) c FROM policy_snapshot_events").fetchone()["c"] == 3
    event_payloads = [
        json.loads(row["payload_json"])
        for row in connection.execute(
            "SELECT payload_json FROM policy_snapshot_events ORDER BY snapshot_version"
        ).fetchall()
    ]
    assert set(event_payloads[0]) == {
        "version",
        "content_hash",
        "policy_count",
        "operation",
        "previous_version",
        "rollback_source_version",
    }
    assert event_payloads[2]["version"] == 3
    assert event_payloads[2]["previous_version"] == 2
    assert event_payloads[2]["rollback_source_version"] == 1
    assert connection.execute(
        "SELECT COUNT(*) c FROM audit_logs WHERE tenant_id=? AND object_type='policy_snapshot'",
        (state["tenant_id"],),
    ).fetchone()["c"] == 3
    assert connection.execute(
        "SELECT current_snapshot_version v FROM policy_snapshot_heads WHERE tenant_id=?",
        (state["tenant_id"],),
    ).fetchone()["v"] == 3
    snapshot_webhooks = [
        event for event in dispatched if event.event_type == "policy_snapshot.published"
    ]
    assert len(snapshot_webhooks) == 3
    assert snapshot_webhooks[0].event_id.startswith("evt_psevt_")
    assert snapshot_webhooks[2].payload["version"] == 3
    assert snapshot_webhooks[2].payload["rollback_source_version"] == 1


@pytest.mark.anyio
async def test_double_cas_idempotency_and_no_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """并发边界：原 actor 可回放，其他 fingerprint/actor、旧 head 与旧 candidate 均被拒绝。"""

    _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        admin_two = await _login(client, "admin-two@example.com", OTHER_ADMIN_PASSWORD)
        await _create_policy(client, admin, 'env == "prod"')
        current = await _current(client, admin)
        first = await _publish(
            client,
            admin,
            current,
            reason="首次发布并验证幂等",
            request_id="req_prd045_idempotent",
        )
        replay = await _publish(
            client,
            admin,
            current,
            reason="首次发布并验证幂等",
            request_id="req_prd045_idempotent",
        )
        changed_reason = await _publish(
            client,
            admin,
            current,
            reason="相同请求号但不同原因",
            request_id="req_prd045_idempotent",
        )
        other_actor = await _publish(
            client,
            admin_two,
            current,
            reason="首次发布并验证幂等",
            request_id="req_prd045_idempotent",
        )
        stale_head = await _publish(
            client,
            admin,
            current,
            reason="旧 head 新请求",
            request_id="req_prd045_stale_head",
        )
        latest = await _current(client, admin)
        wrong_candidate = await client.post(
            "/policy-snapshots/publish",
            json={"reason": "错误候选 hash"},
            headers={
                "Authorization": f"Bearer {admin}",
                "If-Match": latest.headers["ETag"],
                "If-Policy-Candidate-Match": f'"{"0" * 64}"',
                "X-Request-ID": "req_prd045_candidate_conflict",
            },
        )
        no_changes = await _publish(
            client,
            admin,
            latest,
            reason="候选内容未变化",
            request_id="req_prd045_no_changes",
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["policy_snapshot"] == first.json()["policy_snapshot"]
    assert changed_reason.status_code == 409
    assert changed_reason.json()["code"] == "policy_snapshot.idempotency_conflict"
    assert other_actor.status_code == 409
    assert other_actor.json()["code"] == "policy_snapshot.idempotency_conflict"
    assert stale_head.status_code == 409
    assert stale_head.json()["code"] == "policy_snapshot.version_conflict"
    assert wrong_candidate.status_code == 409
    assert wrong_candidate.json()["code"] == "policy_snapshot.candidate_conflict"
    assert no_changes.status_code == 409
    assert no_changes.json()["code"] == "policy_snapshot.no_changes"


@pytest.mark.anyio
async def test_idempotent_replay_recovers_post_commit_webhook_gap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """崩溃恢复：publish/rollback 提交后漏投时，同 request_id 回放重派两个稳定事件。"""

    state = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    failed_event_ids: list[str] = []

    def _fail_before_delivery(_config, _tenant_id, event, **_kwargs):
        """模拟进程在领域 COMMIT 后、Webhook delivery intent 建立前退出。"""

        failed_event_ids.append(event.event_id)
        # 固定故障文本只存在于测试进程内，不进入生产日志、Webhook 或持久业务字段。
        raise RuntimeError("synthetic post-commit crash before delivery intent")

    def _ignore_dispatch(_config, _tenant_id, _event, **_kwargs):
        """准备下一版本时隔离无关 Webhook 出网，仅保留领域事务。"""

        return []

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        policy = await _create_policy(client, admin, 'env == "prod"')
        current_v0 = await _current(client, admin)

        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _fail_before_delivery)
        published_v1 = await _publish(
            client,
            admin,
            current_v0,
            reason="验证发布提交后崩溃恢复",
            request_id="req_prd045_publish_crash_recovery",
        )
        assert published_v1.status_code == 201, published_v1.text
        assert len(failed_event_ids) == 2

        connection = connect_database(state["database_url"])
        v1 = published_v1.json()["policy_snapshot"]
        publish_audit_id = connection.execute(
            "SELECT audit_log_id FROM audit_logs WHERE tenant_id=? AND object_id=?",
            (state["tenant_id"], v1["policy_snapshot_id"]),
        ).fetchone()["audit_log_id"]
        publish_snapshot_event_id = connection.execute(
            "SELECT policy_snapshot_event_id FROM policy_snapshot_events "
            "WHERE tenant_id=? AND policy_snapshot_id=?",
            (state["tenant_id"], v1["policy_snapshot_id"]),
        ).fetchone()["policy_snapshot_event_id"]
        publish_expected_ids = {
            f"evt_{publish_audit_id}",
            f"evt_{publish_snapshot_event_id}",
        }
        assert set(failed_event_ids) == publish_expected_ids

        recovered_publish_ids: list[str] = []

        def _capture_publish_recovery(_config, _tenant_id, event, **_kwargs):
            """记录幂等回放重派的稳定 ID，不执行真实网络请求。"""

            recovered_publish_ids.append(event.event_id)
            return []

        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_publish_recovery)
        replayed_v1 = await _publish(
            client,
            admin,
            current_v0,
            reason="验证发布提交后崩溃恢复",
            request_id="req_prd045_publish_crash_recovery",
        )
        assert replayed_v1.status_code == 201, replayed_v1.text
        assert replayed_v1.json()["idempotent_replay"] is True
        assert set(recovered_publish_ids) == publish_expected_ids
        assert len(recovered_publish_ids) == 2

        # 先形成 v2，随后才能按 M1 合同回滚当前 head 的上一版 v1。
        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _ignore_dispatch)
        updated = await client.patch(
            f"/policies/{policy['policy_id']}",
            json={"rule": {"condition_expr": 'env == "dev"'}},
            headers={"Authorization": f"Bearer {admin}", "If-Match": '"1"'},
        )
        assert updated.status_code == 200, updated.text
        current_v1 = await _current(client, admin)
        published_v2 = await _publish(
            client,
            admin,
            current_v1,
            reason="形成回滚恢复测试的第二版",
            request_id="req_prd045_publish_for_rollback_recovery",
        )
        assert published_v2.status_code == 201, published_v2.text

        failed_event_ids.clear()
        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _fail_before_delivery)
        rollback_path = f"/policy-snapshots/{v1['policy_snapshot_id']}/rollback"
        rollback_headers = {
            "Authorization": f"Bearer {admin}",
            "If-Match": '"2"',
            "X-Request-ID": "req_prd045_rollback_crash_recovery",
        }
        rollback_body = {"reason": "验证回滚提交后崩溃恢复"}
        rolled_back = await client.post(rollback_path, json=rollback_body, headers=rollback_headers)
        assert rolled_back.status_code == 201, rolled_back.text
        assert len(failed_event_ids) == 2

        v3 = rolled_back.json()["policy_snapshot"]
        rollback_audit_id = connection.execute(
            "SELECT audit_log_id FROM audit_logs WHERE tenant_id=? AND object_id=?",
            (state["tenant_id"], v3["policy_snapshot_id"]),
        ).fetchone()["audit_log_id"]
        rollback_snapshot_event_id = connection.execute(
            "SELECT policy_snapshot_event_id FROM policy_snapshot_events "
            "WHERE tenant_id=? AND policy_snapshot_id=?",
            (state["tenant_id"], v3["policy_snapshot_id"]),
        ).fetchone()["policy_snapshot_event_id"]
        rollback_expected_ids = {
            f"evt_{rollback_audit_id}",
            f"evt_{rollback_snapshot_event_id}",
        }
        assert set(failed_event_ids) == rollback_expected_ids

        recovered_rollback_ids: list[str] = []

        def _capture_rollback_recovery(_config, _tenant_id, event, **_kwargs):
            """记录 rollback 幂等回放重派的审计与快照稳定事件。"""

            recovered_rollback_ids.append(event.event_id)
            return []

        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_rollback_recovery)
        replayed_rollback = await client.post(
            rollback_path,
            json=rollback_body,
            headers=rollback_headers,
        )

    assert replayed_rollback.status_code == 201, replayed_rollback.text
    assert replayed_rollback.json()["idempotent_replay"] is True
    assert set(recovered_rollback_ids) == rollback_expected_ids
    assert len(recovered_rollback_ids) == 2
    assert connection.execute("SELECT COUNT(*) c FROM policy_snapshots").fetchone()["c"] == 3


@pytest.mark.anyio
async def test_permissions_payload_and_cross_tenant_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/隔离：developer/viewer、未知字段/query 与跨租户 snapshot ID 均不能越权。"""

    _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer@example.com", VIEWER_PASSWORD)
        other_admin = await _login(
            client,
            "admin@other.example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug="prd045-other",
        )
        other_current = await _current(client, other_admin)
        other_published = await _publish(
            client,
            other_admin,
            other_current,
            reason="其他租户空基线",
            request_id="req_prd045_other_publish",
        )
        other_id = other_published.json()["policy_snapshot"]["policy_snapshot_id"]

        developer_get = await client.get(
            "/policy-snapshots/current",
            headers={"Authorization": f"Bearer {developer}"},
        )
        viewer_get = await client.get(
            "/policy-snapshots",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        developer_post = await client.post(
            "/policy-snapshots/publish",
            json={"reason": "越权发布"},
            headers={
                "Authorization": f"Bearer {developer}",
                "If-Match": '"0"',
                "If-Policy-Candidate-Match": f'"{"0" * 64}"',
            },
        )
        unknown_query = await client.get(
            "/policy-snapshots/current?tenant_id=tenant_prd045_other",
            headers={"Authorization": f"Bearer {admin}"},
        )
        unknown_body = await client.post(
            "/policy-snapshots/publish",
            json={"reason": "非法夹带", "tenant_id": "tenant_prd045_other"},
            headers={
                "Authorization": f"Bearer {admin}",
                "If-Match": '"0"',
                "If-Policy-Candidate-Match": f'"{"0" * 64}"',
            },
        )
        cross_tenant = await client.post(
            f"/policy-snapshots/{other_id}/rollback",
            json={"reason": "尝试跨租户回滚"},
            headers={"Authorization": f"Bearer {admin}", "If-Match": '"0"'},
        )
        missing_match = await client.post(
            "/policy-snapshots/publish",
            json={"reason": "缺少条件头"},
            headers={"Authorization": f"Bearer {admin}"},
        )

    for response in (developer_get, viewer_get, developer_post):
        assert response.status_code == 403
        assert response.json()["code"] == "auth.permission_denied"
        assert response.json()["request_id"]
    assert unknown_query.status_code == 422
    assert unknown_query.json()["code"] == "policy_snapshot.payload_invalid"
    assert unknown_body.status_code == 422
    assert unknown_body.json()["code"] == "policy_snapshot.payload_invalid"
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "policy_snapshot.not_found"
    assert missing_match.status_code == 428
    assert missing_match.json()["code"] == "policy_snapshot.if_match_required"


@pytest.mark.anyio
async def test_internal_token_wire_etag_and_tenant_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """内部拉取：所有环境强制 Token，200/304/null/head 与 active tenant 语义可区分。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        await _create_policy(client, admin, 'env == "prod"')
        current = await _current(client, admin)
        published = await _publish(
            client,
            admin,
            current,
            reason="内部 wire 测试发布",
            request_id="req_prd045_internal_publish",
        )
        assert published.status_code == 201

        path = f"/internal/policy-snapshots/{state['tenant_id']}/current"
        missing = await client.get(path)
        wrong = await client.get(path, headers={"X-Internal-Token": "wrong-token"})
        fetched = await client.get(path, headers={"X-Internal-Token": INTERNAL_TOKEN})
        not_modified = await client.get(
            path,
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "If-None-Match": fetched.headers["ETag"],
            },
        )
        mixed_auth = await client.get(
            path,
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "Authorization": f"Bearer {admin}",
            },
        )
        no_head = await client.get(
            f"/internal/policy-snapshots/{state['other_tenant_id']}/current",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        unknown = await client.get(
            "/internal/policy-snapshots/tenant_unknown/current",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )
        unknown_query = await client.get(
            f"{path}?tenant_id=other",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )

    assert missing.status_code == 401
    assert missing.json()["code"] == "auth.internal_token_required"
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "auth.internal_token_invalid"
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "no-store"
    wire = fetched.json()
    assert wire["tenant_id"] == state["tenant_id"]
    assert wire["snapshot"]["snapshot_version"] == 1
    assert wire["snapshot"]["policies"][0]["canonical_expression"] == 'env == "prod"'
    serialized = json.dumps(wire, ensure_ascii=False)
    assert "condition_expr" not in serialized
    assert "生产环境阻断策略" not in serialized
    assert "内部 wire 测试发布" not in serialized
    assert INTERNAL_TOKEN not in serialized
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert mixed_auth.status_code == 422
    assert no_head.status_code == 200
    assert no_head.json()["snapshot"] is None
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "policy_snapshot.tenant_unavailable"
    assert unknown_query.status_code == 422


@pytest.mark.anyio
async def test_candidate_safety_and_transaction_webhook_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：不可信变量发布失败，事件/审计故障回滚，commit 后 Webhook 故障不回滚。"""

    state = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            json={
                "name": "尚无可信来源的数据分类策略",
                "status": "active",
                "priority": 100,
                "rule": {"condition_expr": 'data_class == "restricted"'},
                "action": {"type": "deny"},
            },
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert created.status_code == 201
        invalid_current = await _current(client, admin)
        assert invalid_current.json()["candidate_status"] == "invalid"
        assert "x-policy-candidate-etag" not in invalid_current.headers
        variable_failed = await client.post(
            "/policy-snapshots/publish",
            json={"reason": "不能信任调用方声明的数据分类"},
            headers={
                "Authorization": f"Bearer {admin}",
                "If-Match": '"0"',
                "If-Policy-Candidate-Match": f'"{"0" * 64}"',
                "X-Request-ID": "req_prd045_variable_failed",
            },
        )
        assert variable_failed.status_code == 422
        assert variable_failed.json()["code"] == "policy_snapshot.variable_unsupported"

        policy_id = created.json()["policy"]["policy_id"]
        fixed = await client.patch(
            f"/policies/{policy_id}",
            # PRD-068 已让 mask 成为正式非终止动作；这里改用仍被明确禁止的 data_class
            # 验证候选失败关闭，修复时只替换规则，避免把新增运行时能力误判为兼容回归。
            json={"rule": {"condition_expr": 'env == "prod"'}},
            headers={"Authorization": f"Bearer {admin}", "If-Match": '"1"'},
        )
        assert fixed.status_code == 200
        valid_current = await _current(client, admin)

        connection = connect_database(state["database_url"])
        connection.execute(
            """
            CREATE TRIGGER prd045_test_reject_event
            BEFORE INSERT ON policy_snapshot_events
            BEGIN SELECT RAISE(ABORT, 'synthetic snapshot event failure'); END
            """
        )
        connection.commit()
        event_failed = await _publish(
            client,
            admin,
            valid_current,
            reason="事件故障必须整体回滚",
            request_id="req_prd045_event_failed",
        )
        assert event_failed.status_code == 503
        connection.execute("DROP TRIGGER prd045_test_reject_event")
        connection.commit()

        from metahub_control.webhooks import service as webhook_service

        def _fail_dispatch(*_args, **_kwargs):
            """模拟提交后 Endpoint 不可用，异常不得改变已发布事实。"""

            # 故障文本只用于进程内注入，生产服务会捕获且仅记录固定脱敏事件码。
            raise RuntimeError("synthetic post-commit webhook failure")

        monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _fail_dispatch)
        succeeded = await _publish(
            client,
            admin,
            valid_current,
            reason="提交后投递失败仍保持发布",
            request_id="req_prd045_webhook_failed",
        )

    assert succeeded.status_code == 201, succeeded.text
    connection = connect_database(state["database_url"])
    assert connection.execute("SELECT COUNT(*) c FROM policy_snapshots").fetchone()["c"] == 1
    assert connection.execute("SELECT COUNT(*) c FROM policy_snapshot_events").fetchone()["c"] == 1
    assert connection.execute(
        "SELECT current_snapshot_version v FROM policy_snapshot_heads WHERE tenant_id=?",
        (state["tenant_id"],),
    ).fetchone()["v"] == 1
